"""
Router - 调度大脑

负责三个核心决策：
1. 上下文依赖分析 → 自己做还是外包
2. 任务分类 → 路由到哪个 Worker
3. 模型选择 → 用哪个模型 + fallback 链
"""

import re

from src.models.base import (
    Task, TaskType, ContextDependency, FallbackChain, DEFAULT_CHAINS, SpawnResult, RuntimeAdapter, ErrorClass, classify_error,
)


class Router:
    """智能任务路由器"""

    def __init__(self, adapter: RuntimeAdapter, chains: dict = None):
        self.adapter = adapter
        self.chains = chains or DEFAULT_CHAINS

    # ─── 决策 1: 上下文依赖分析 ──────────────────────

    def analyze_context(self, task: Task) -> ContextDependency:
        """
        判断任务是否依赖当前对话的上下文。
        
        强依赖特征：代词引用（这个、那个、它）、"继续"、"刚才"、"改成"
        弱依赖特征：独立需求（帮我查、写一个、翻译）
        """
        desc = task.description.lower()
        
        strong_patterns = [
            r"把这个", r"把这个改成", r"继续", r"刚才", r"刚才那个",
            r"那个", r"它", r"上一", r"之前的", r"之前说的",
            r"change this", r"continue", r"the previous", r"that one",
        ]
        for p in strong_patterns:
            if re.search(p, desc):
                return ContextDependency.STRONG
        
        return ContextDependency.WEAK

    # ─── 决策 2: 任务分类 ────────────────────────────

    def classify_task(self, task: Task) -> TaskType:
        """
        基于关键词和规则将任务分类到 6 档之一。
        
        优先级：AUDIT > CODING > RESEARCH > DOC > LIGHT_CODING > STANDARD > TRIVIAL
        """
        desc = task.description.lower()

        # 审计
        audit_kw = ["审计", "audit", "安全检查", "code review", "渗透", "漏洞"]
        if any(k in desc for k in audit_kw):
            return TaskType.AUDIT

        # 复杂编码
        coding_kw = ["开发", "重构", "全栈", "新功能", "refactor", "implement",
                     "build a", "新项目", "完整实现", "后端", "前端", "系统",
                     "合约", "contract", "api", "服务端", "微服务"]
        if any(k in desc for k in coding_kw):
            return TaskType.CODING

        # 深度调研
        research_kw = ["调研", "竞品", "市场分析", "趋势", "报告", "research",
                       "market analysis", "comparison", "对比"]
        if any(k in desc for k in research_kw):
            return TaskType.RESEARCH

        # 文档生成
        doc_kw = ["白皮书", "文档", "ppt", "幻灯片", "whitepaper", "slides",
                  "presentation", "排版"]
        if any(k in desc for k in doc_kw):
            return TaskType.DOC

        # 轻量编码
        light_kw = ["脚本", "简单", "配置", "快速", "随便", "小工具",
                    "script", "config", "simple"]
        if any(k in desc for k in light_kw):
            return TaskType.LIGHT_CODING

        # 标准任务（搜索/解释等）
        standard_kw = ["搜索", "查询", "解释", "分析", "search", "explain",
                       "analyze", "总结", "帮我查"]
        if any(k in desc for k in standard_kw):
            return TaskType.STANDARD

        # 兜底：极简杂活
        return TaskType.TRIVIAL

    # ─── 决策 3: 模型选择 + fallback ─────────────────

    def select_model(self, task_type: TaskType) -> FallbackChain:
        """返回该任务类型的候选链"""
        return self.chains.get(task_type, self.chains[TaskType.STANDARD])

    # ─── 上下文打包 ─────────────────────────────────

    @staticmethod
    def pack_context(context: str, task_desc: str, constraints: list[str] = None) -> str:
        """
        上下文打包协议：用 XML 标签隔离数据与指令。
        防止数据内容被误读为指令（Prompt Injection 防护）。
        """
        parts = [f"<context>\n{context}\n</context>\n"]
        parts.append(f"<task>\n{task_desc}\n</task>\n")
        if constraints:
            parts.append("<constraints>\n")
            for c in constraints:
                parts.append(f"- {c}\n")
            parts.append("</constraints>\n")
        return "".join(parts)

    # ─── 主调度入口 ─────────────────────────────────

    def dispatch(self, description: str, context: str = None) -> SpawnResult | str:
        """
        主调度入口。
        
        Returns:
            SpawnResult: 如果外包给子 Agent
            str: 如果主 Agent 自己处理（返回处理建议）
        """
        task = Task(description=description)
        
        # 1. 上下文依赖分析
        task.context_dependency = self.analyze_context(task)
        if task.context_dependency == ContextDependency.STRONG:
            return "⚠️ 此任务依赖当前对话上下文，建议由主 Agent 直接处理。"

        # 2. 任务分类
        task.task_type = self.classify_task(task)

        # 3. 模型选择
        chain = self.select_model(task.task_type)
        primary = chain.candidates[0] if chain.candidates else None
        if not primary:
            return "❌ 无可用模型"

        # 4. 上下文打包
        packed = self.pack_context(
            context=context or "（无额外上下文）",
            task_desc=description,
            constraints=[
                "所有产出文件必须保存在指定目录",
                "完成后用最多 3 句话概述做了什么、产出路径、有无遗留问题",
            ]
        )

        # 5. 派发
        return self.adapter.spawn(
            task=packed,
            model=primary.model_id,
            timeout_seconds=task.timeout_seconds,
        )

    # ─── 带降级的派发 ──────────────────────────────

    def dispatch_with_fallback(self, task: Task, context: str = None) -> SpawnResult:
        """
        带自动降级的派发，按错误类型选择降级策略：

        - RATE_LIMIT(429)/AUTH：拉黑整个 provider，跳到下一家 provider 的候选；
        - SERVER_ERROR(5xx)：同模型重试一次，仍失败再降级；
        - TIMEOUT：立即降级到更快（speed_rank 更低）的候选；
        - UNKNOWN：顺序降级到下一候选。

        provider 级隔离确保 Gemini 配额耗尽不会拖累 GPT，反之亦然。
        """
        task.context_dependency = self.analyze_context(task)
        task.task_type = self.classify_task(task)
        chain = self.select_model(task.task_type)

        packed = self.pack_context(
            context=context or "（无额外上下文）",
            task_desc=task.description,
        )

        attempts: list[str] = []
        dead_providers: set[str] = set()
        retried_server: set[str] = set()

        # 候选队列（保留原始优先级），按错误分级动态重排/跳过。
        queue = list(chain.candidates)
        while queue:
            candidate = queue.pop(0)
            if candidate.provider in dead_providers:
                attempts.append(f"skip {candidate.model_id} (provider {candidate.provider} 已拉黑)")
                continue

            result = self.adapter.spawn(
                task=packed,
                model=candidate.model_id,
                timeout_seconds=task.timeout_seconds,
            )

            if result.status != "error":
                result.model = candidate.model_id
                attempts.append(f"ok {candidate.model_id}")
                result.attempts = attempts
                return result

            err_class = classify_error(result)
            attempts.append(f"fail {candidate.model_id} [{err_class.value}] {result.error or ''}".strip())

            if err_class in (ErrorClass.RATE_LIMIT, ErrorClass.AUTH):
                # 整个 provider 不可用：拉黑，余下同 provider 候选会被跳过。
                dead_providers.add(candidate.provider)
            elif err_class == ErrorClass.SERVER_ERROR and candidate.model_id not in retried_server:
                # 瞬时 5xx：同模型重试一次（插回队首）。
                retried_server.add(candidate.model_id)
                queue.insert(0, candidate)
            elif err_class == ErrorClass.TIMEOUT:
                # 超时：优先降级到更快的候选。
                queue.sort(key=lambda c: c.speed_rank)
            # UNKNOWN / 已重试过的 SERVER_ERROR：自然顺序降级。

        return SpawnResult(
            run_id="", status="error",
            error="所有候选模型均失败", attempts=attempts,
        )
