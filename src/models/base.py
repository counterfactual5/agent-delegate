"""
Agent Delegate - Production-grade multi-agent orchestration

核心抽象层：RuntimeAdapter
所有 Worker 和 Router 通过这个接口与底层 runtime 交互，
不直接依赖 OpenClaw / LangChain / OpenAI 等任何具体实现。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ─── 数据模型 ─────────────────────────────────────────────

class TaskType(Enum):
    TRIVIAL = "trivial"           # 极简杂活（翻译、闲聊、归纳）
    STANDARD = "standard"         # 标准任务（搜索、代码解释）
    CODING = "coding"             # 复杂编码（功能开发、重构）
    RESEARCH = "research"         # 深度调研（市场调研、报告撰写）
    LIGHT_CODING = "light_coding" # 轻量编码（简单脚本、配置）
    AUDIT = "audit"               # 专项审计（安全审计、Code Review）
    DOC = "doc"                   # 文档生成（白皮书、PPT）


class ContextDependency(Enum):
    STRONG = "strong"    # 强依赖主会话上下文 → 主 Agent 处理
    WEAK = "weak"        # 弱/无依赖 → 可外包给子 Agent


class DependencyType(Enum):
    INDEPENDENT = "independent"      # 无依赖 → 并行 spawn
    SEQUENTIAL = "sequential"        # 串行依赖 → 合并打包
    PARTIAL = "partial"              # 部分依赖 → 主 Agent 整合


@dataclass
class ModelCandidate:
    """模型候选条目"""
    model_id: str
    provider: str           # e.g. "gemini", "openai", "anthropic"
    speed_rank: int = 5     # 1=最快, 10=最慢
    cost_rank: int = 5      # 1=最便宜, 10=最贵


@dataclass
class Task:
    """待调度的任务"""
    description: str
    task_type: Optional[TaskType] = None
    context_dependency: Optional[ContextDependency] = None
    dependency_type: Optional[DependencyType] = None
    model_override: Optional[str] = None
    timeout_seconds: int = 300
    cleanup_on_complete: bool = False


class ErrorClass(Enum):
    """错误分级，用于决定降级策略（灵感来自 AI-DLC 的错误严重度分级）。"""
    NONE = "none"               # 无错误
    RATE_LIMIT = "rate_limit"   # 429 / 配额耗尽 → 切 provider
    AUTH = "auth"               # 401/403 / 密钥失效 → 拉黑该 provider
    SERVER_ERROR = "server"     # 5xx → 同模型重试一次后降级
    TIMEOUT = "timeout"         # 超时 → 立即降级到更快的候选
    UNKNOWN = "unknown"         # 其它 → 顺序降级


# 关键字 → 错误分级映射（按优先级匹配 error 文本）。
_ERROR_SIGNATURES: list[tuple[ErrorClass, tuple[str, ...]]] = [
    (ErrorClass.RATE_LIMIT, ("429", "rate limit", "ratelimit", "too many requests",
                             "quota", "配额", "限流")),
    (ErrorClass.AUTH, ("401", "403", "unauthorized", "forbidden", "invalid api key",
                       "api key", "认证", "鉴权", "密钥")),
    (ErrorClass.TIMEOUT, ("timeout", "timed out", "deadline", "超时")),
    (ErrorClass.SERVER_ERROR, ("500", "502", "503", "504", "internal server",
                               "bad gateway", "unavailable", "服务不可用")),
]


def classify_error(result: "SpawnResult") -> ErrorClass:
    """根据 SpawnResult 的状态/错误文本推断错误分级。"""
    if result.status != "error":
        return ErrorClass.NONE
    text = (result.error or "").lower()
    if not text:
        return ErrorClass.UNKNOWN
    for err_class, needles in _ERROR_SIGNATURES:
        if any(n in text for n in needles):
            return err_class
    return ErrorClass.UNKNOWN


@dataclass
class SpawnResult:
    """spawn 返回值"""
    run_id: str
    status: str = "pending"  # pending | running | completed | error
    error: Optional[str] = None
    model: Optional[str] = None        # 实际命中的模型
    attempts: list = field(default_factory=list)  # 降级审计轨迹


@dataclass
class WorkerOutput:
    """Worker 产出"""
    success: bool
    summary: str
    output_path: Optional[str] = None
    artifacts: list = field(default_factory=list)
    issues: list = field(default_factory=list)


# ─── RuntimeAdapter 抽象接口 ──────────────────────────────

class RuntimeAdapter(ABC):
    """
    Runtime 适配层抽象接口。
    
    所有具体的 runtime（OpenClaw / LangChain / OpenAI / 自定义）
    都需要实现这 4 个方法。
    """

    @abstractmethod
    def spawn(self, task: str, model: str, **kwargs) -> SpawnResult:
        """
        创建子 agent 执行任务。
        
        Args:
            task: 完整的任务描述（已打包上下文）
            model: 模型 ID
            **kwargs: 扩展参数
                - thinking: str, 思考级别 ("off"|"standard"|"high")
                - timeout_seconds: int, 超时
                - cleanup: bool, 完成后清理
                - label: str, Agent 标签
                - context: str, 上下文模式 ("isolated"|"fork")
        
        Returns:
            SpawnResult with run_id
        """
        ...

    @abstractmethod
    def listen(self, run_id: str, timeout_ms: int = 30000) -> WorkerOutput:
        """
        等待子 agent 完成（阻塞）。
        
        注意：生产环境中推荐用事件驱动（yield + callback），
        此方法主要用于同步测试场景。
        """
        ...

    @abstractmethod
    def send(self, message: str, **kwargs) -> None:
        """
        发送消息到用户/通道。
        
        Args:
            message: 消息内容
            **kwargs: 
                - channel: str, 通道 (如 "telegram", "slack")
                - to: str, 目标 ID
        """
        ...

    @abstractmethod
    def list_runs(self, **kwargs) -> list:
        """列出当前活跃的子 agent 运行。"""
        ...


# ─── Fallback Chain ──────────────────────────────────────

@dataclass
class FallbackChain:
    """模型候选链：按优先级排列，失败自动降级"""
    candidates: list[ModelCandidate] = field(default_factory=list)
    
    def next(self, failed_model: Optional[str] = None) -> Optional[ModelCandidate]:
        """返回下一个候选模型。如果 failed_model 不为空，跳过它。"""
        for c in self.candidates:
            if c.model_id != failed_model:
                return c
        return None
    
    def next_by_provider(self, failed_provider: Optional[str] = None) -> Optional[ModelCandidate]:
        """返回下一个不同 provider 的候选模型。"""
        for c in self.candidates:
            if c.provider != failed_provider:
                return c
        return self.candidates[0] if self.candidates else None


# 预定义的 6 档候选链
DEFAULT_CHAINS: dict[TaskType, FallbackChain] = {
    TaskType.TRIVIAL: FallbackChain(candidates=[
        ModelCandidate("gemini-flash", "gemini", speed_rank=1, cost_rank=1),
        ModelCandidate("gpt-flash", "openai", speed_rank=2, cost_rank=2),
    ]),
    TaskType.STANDARD: FallbackChain(candidates=[
        ModelCandidate("gemini-pro", "gemini", speed_rank=3, cost_rank=3),
        ModelCandidate("gpt-standard", "openai", speed_rank=4, cost_rank=4),
    ]),
    TaskType.CODING: FallbackChain(candidates=[
        ModelCandidate("gemini-pro-high", "gemini", speed_rank=6, cost_rank=6),
        ModelCandidate("gpt-codex", "openai", speed_rank=5, cost_rank=5),
        ModelCandidate("gpt-codex-mini", "openai", speed_rank=3, cost_rank=3),
    ]),
    TaskType.RESEARCH: FallbackChain(candidates=[
        ModelCandidate("gemini-pro-high", "gemini", speed_rank=6, cost_rank=6),
        ModelCandidate("gpt-standard", "openai", speed_rank=4, cost_rank=4),
    ]),
    TaskType.LIGHT_CODING: FallbackChain(candidates=[
        ModelCandidate("gemini-pro-low", "gemini", speed_rank=4, cost_rank=3),
        ModelCandidate("gpt-codex-mini", "openai", speed_rank=3, cost_rank=2),
    ]),
    TaskType.AUDIT: FallbackChain(candidates=[
        ModelCandidate("gemini-pro-high", "gemini", speed_rank=6, cost_rank=6),
        ModelCandidate("gpt-codex-max", "openai", speed_rank=7, cost_rank=7),
    ]),
}
