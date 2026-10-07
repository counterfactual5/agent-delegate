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
    context_window: int = 128000  # 上下文窗口大小（tokens），默认 128k


@dataclass
class Task:
    """待调度的任务"""
    description: str
    task_type: Optional[TaskType] = None
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
    CONTEXT_LENGTH = "context_length"  # 上下文长度超限 → 降级到更大上下文窗口的模型
    UNKNOWN = "unknown"         # 其它 → 顺序降级


# 关键字 → 错误分级映射（按优先级匹配 error 文本）。
_ERROR_SIGNATURES: list[tuple[ErrorClass, tuple[str, ...]]] = [
    (ErrorClass.RATE_LIMIT, ("429", "rate limit", "ratelimit", "too many requests",
                             "quota", "配额", "限流")),
    (ErrorClass.AUTH, ("401", "403", "unauthorized", "forbidden", "invalid api key",
                       "api key", "认证", "鉴权", "密钥")),
    (ErrorClass.CONTEXT_LENGTH, ("context length", "context_length", "token limit", 
                                 "maximum context", "too long", "上下文长度", 
                                 "令牌数超限", "exceeds context", "context window")),
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
class AttemptRecord:
    """单个候选模型的调用/跳过审计记录"""
    model: str
    provider: str
    outcome: str  # ok | fail | skip
    status: Optional[str] = None
    error_class: Optional[str] = None
    error: Optional[str] = None
    reason: Optional[str] = None

    def __str__(self) -> str:
        if self.outcome == "ok":
            return f"ok {self.model}"
        if self.outcome == "skip":
            reason_str = f" ({self.reason})" if self.reason else ""
            return f"skip {self.model}{reason_str}"
        # outcome == "fail" or others
        err_cls = f" [{self.error_class}]" if self.error_class else ""
        err_msg = f" {self.error}" if self.error else ""
        return f"{self.outcome} {self.model}{err_cls}{err_msg}".strip()


@dataclass
class SpawnResult:
    """spawn 返回值"""
    run_id: str
    status: str = "pending"  # pending | running | completed | error
    error: Optional[str] = None
    model: Optional[str] = None        # 实际命中的模型
    attempts: list[AttemptRecord] = field(default_factory=list)  # 降级审计轨迹
    summary: Optional[str] = None      # 子 agent 的产出摘要
    artifacts: list = field(default_factory=list)  # 产物路径
    output_path: Optional[str] = None  # 主产出路径


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
            task: Packed task description (context + instructions)
            model: Model ID
            **kwargs: Runtime-specific options (timeout_seconds, wait, etc.)
        
        Returns:
            SpawnResult with terminal status (completed or error).
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

    def send(self, message: str, **kwargs) -> None:
        """
        Optional: send a notification message. No-op by default.
        
        Args:
            message: 消息内容
            **kwargs: 
                - channel: str, 通道 (如 "telegram", "slack")
                - to: str, 目标 ID
        """
        ...

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


# EXAMPLE ONLY — these model names WILL go stale. For production use, load
# chains from config via load_chains() or pass Router(adapter, chains={...}).
# Source: artificialanalysis.ai leaderboard, late 2025.
DEFAULT_CHAINS: dict[TaskType, FallbackChain] = {
    TaskType.TRIVIAL: FallbackChain(candidates=[
        ModelCandidate("gemini-3.5-flash-lite", "google", speed_rank=1, cost_rank=1, context_window=1000000),
        ModelCandidate("gpt-5.6-sol-low", "openai", speed_rank=2, cost_rank=2, context_window=1000000),
    ]),
    TaskType.STANDARD: FallbackChain(candidates=[
        ModelCandidate("gemini-2.5-flash", "google", speed_rank=3, cost_rank=3, context_window=1000000),
        ModelCandidate("gpt-5.6-sol-medium", "openai", speed_rank=4, cost_rank=4, context_window=1000000),
    ]),
    TaskType.CODING: FallbackChain(candidates=[
        ModelCandidate("claude-sonnet-5.5", "anthropic", speed_rank=6, cost_rank=6, context_window=1000000),
        ModelCandidate("gpt-5.6-sol-high", "openai", speed_rank=5, cost_rank=5, context_window=1000000),
        ModelCandidate("gemini-2.5-flash", "google", speed_rank=3, cost_rank=3, context_window=1000000),
    ]),
    TaskType.RESEARCH: FallbackChain(candidates=[
        ModelCandidate("gemini-4-argon", "google", speed_rank=5, cost_rank=5, context_window=1000000),
        ModelCandidate("claude-sonnet-5.5", "anthropic", speed_rank=6, cost_rank=6, context_window=1000000),
    ]),
    TaskType.LIGHT_CODING: FallbackChain(candidates=[
        ModelCandidate("gemini-2.5-flash", "google", speed_rank=3, cost_rank=3, context_window=1000000),
        ModelCandidate("gpt-5.6-sol-low", "openai", speed_rank=2, cost_rank=2, context_window=1000000),
    ]),
    TaskType.AUDIT: FallbackChain(candidates=[
        ModelCandidate("claude-opus-5", "anthropic", speed_rank=7, cost_rank=7, context_window=1000000),
        ModelCandidate("gpt-6.1-sol", "openai", speed_rank=6, cost_rank=6, context_window=1000000),
    ]),
}


def load_chains(path: str) -> dict:
    """
    Load task-type chains from a YAML or JSON config file.

    Recommended over DEFAULT_CHAINS for production — hardcoded model names
    go stale; config files stay current.

    YAML format:
        trivial:
          - model_id: gemini-3.5-flash-lite
            provider: google
            speed_rank: 1
            cost_rank: 1
            context_window: 1000000
          - model_id: gpt-5.6-sol-low
            provider: openai
            speed_rank: 2
            cost_rank: 2
        coding:
          - model_id: claude-sonnet-5.5
            provider: anthropic
            speed_rank: 6
            cost_rank: 6

    Usage:
        chains = load_chains("chains.yaml")
        router = Router(adapter, chains=chains)
    """
    import json
    import yaml  # optional dependency; pip install pyyaml

    with open(path) as f:
        if path.endswith((".yaml", ".yml")):
            raw = yaml.safe_load(f)
        else:
            raw = json.load(f)

    chains = {}
    for task_type_str, candidates in raw.items():
        task_type = TaskType(task_type_str)
        chain = FallbackChain(candidates=[
            ModelCandidate(**c) for c in candidates
        ])
        chains[task_type] = chain
    return chains
