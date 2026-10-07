"""
Router - 调度大脑

负责三个核心决策：
1. 上下文依赖分析 → 自己做还是外包
2. 任务分类 → 路由到哪个 Worker
3. 模型选择 → 用哪个模型 + fallback 链

调用方优先：调用方给了候选模型或任务类型时直接采用，关键词分类只是兜底。
"""

import logging
import re
import time

from agent_delegate.models.base import (
    Task, TaskType, ContextDependency, FallbackChain, DEFAULT_CHAINS, ChainNotConfigured, SpawnResult, RuntimeAdapter, ErrorClass, classify_error, AttemptRecord,
)

logger = logging.getLogger(__name__)


class Router:
    """智能任务路由器"""

    def __init__(self, adapter: RuntimeAdapter, chains: dict = None):
        self.adapter = adapter
        self.chains = chains if chains is not None else dict(DEFAULT_CHAINS)

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
        """返回该任务类型的候选链；未配置时抛 ChainNotConfigured，不悄悄换用其它类型的链。"""
        chain = self.chains.get(task_type)
        if chain is None or not chain.candidates:
            name = getattr(task_type, "value", task_type)
            raise ChainNotConfigured(
                f"no model chain configured for task type {name!r}; pass Task.candidates / "
                f"Task.model_override, or configure Router(chains=...)"
            )
        return chain

    def resolve_chain(self, task: Task) -> FallbackChain:
        """
        按调用方优先的顺序确定候选链：
        task.candidates > task.model_override > 调用方设置的 task.task_type > classify_task 关键词分类。

        候选模型应来自当前运行环境实际可用的模型列表，不要凭记忆填写。
        """
        if task.candidates:
            return FallbackChain.from_ids(task.candidates)
        if task.model_override:
            return FallbackChain.from_ids([task.model_override])
        if task.task_type is None:
            task.task_type = self.classify_task(task)
        return self.select_model(task.task_type)

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
        主调度入口：基于关键词的粗略路由，保留作兼容，不推荐。任务类型靠关键词猜，
        只用候选链首选、不降级；调用方能判断任务时改用 dispatch_with_fallback，
        并给出 Task.candidates 或 task_type。
        
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

    def dispatch_with_fallback(
        self, task: Task, context: str = None, chain: FallbackChain = None,
    ) -> SpawnResult:
        """
        带自动降级的派发，按错误类型选择降级策略：

        - RATE_LIMIT(429)/AUTH：拉黑整个 provider，跳到下一家 provider 的候选；
        - SERVER_ERROR(5xx)：同模型重试一次，仍失败再降级；
        - TIMEOUT：立即降级到更快（speed_rank 更低）的候选；
        - CONTEXT_LENGTH：降级到 context_window 更大的候选，无更大者即耗尽；
        - UNKNOWN：顺序降级到下一候选。

        provider 级隔离确保 Gemini 配额耗尽不会拖累 GPT，反之亦然。
        每次尝试记为 AttemptRecord（含耗时），耗尽时 error 带聚合摘要。

        传入 chain 时直接使用该候选链（PipelineRunner 按阶段档位选链时使用）；
        否则按 resolve_chain 的顺序确定。没有可用候选链时抛 ChainNotConfigured。
        """
        task.context_dependency = self.analyze_context(task)
        if chain is None:
            chain = self.resolve_chain(task)

        packed = self.pack_context(
            context=context or "（无额外上下文）",
            task_desc=task.description,
        )

        attempts: list[AttemptRecord] = []
        dead_providers: set[str] = set()
        retried_server: set[str] = set()

        # 候选队列（保留原始优先级），按错误分级动态重排/跳过。
        queue = list(chain.candidates)
        while queue:
            candidate = queue.pop(0)
            if candidate.provider in dead_providers:
                attempts.append(AttemptRecord(
                    model=candidate.model_id,
                    provider=candidate.provider,
                    outcome="skip",
                    reason=f"provider {candidate.provider} blacklisted",
                ))
                continue

            started = time.perf_counter()
            result = self.adapter.spawn(
                task=packed,
                model=candidate.model_id,
                timeout_seconds=task.timeout_seconds,
            )
            duration_ms = (time.perf_counter() - started) * 1000

            if result.status != "error":
                result.model = candidate.model_id
                attempts.append(AttemptRecord(
                    model=candidate.model_id,
                    provider=candidate.provider,
                    outcome="ok",
                    status=result.status,
                    duration_ms=duration_ms,
                ))
                result.attempts = attempts
                return result

            err_class = classify_error(result)
            attempts.append(AttemptRecord(
                model=candidate.model_id,
                provider=candidate.provider,
                outcome="fail",
                status=result.status,
                error_class=err_class.value,
                error=result.error,
                duration_ms=duration_ms,
            ))

            if err_class in (ErrorClass.RATE_LIMIT, ErrorClass.AUTH):
                # 整个 provider 不可用：拉黑，余下同 provider 候选会被跳过。
                logger.warning("Blacklisting provider %s due to %s error",
                               candidate.provider, err_class.value)
                dead_providers.add(candidate.provider)
            elif err_class == ErrorClass.SERVER_ERROR and candidate.model_id not in retried_server:
                # 瞬时 5xx：同模型重试一次（插回队首）。
                logger.info("Retrying model %s after SERVER_ERROR", candidate.model_id)
                retried_server.add(candidate.model_id)
                queue.insert(0, candidate)
            elif err_class == ErrorClass.TIMEOUT:
                # 超时：优先降级到更快的候选。
                logger.debug("Re-sorting queue by speed_rank after TIMEOUT")
                queue.sort(key=lambda c: c.speed_rank)
            elif err_class == ErrorClass.CONTEXT_LENGTH:
                # 上下文超限：只留窗口更大的候选，窗口大者先、同窗更快的先。
                failed_window = candidate.context_window
                queue = [c for c in queue if c.context_window > failed_window]
                queue.sort(key=lambda c: (-c.context_window, c.speed_rank))
                logger.debug("Filtered queue to context_window > %d, %d candidates remain",
                             failed_window, len(queue))
            # UNKNOWN / 已重试过的 SERVER_ERROR：自然顺序降级。

        # 聚合摘要；完整逐次轨迹在 attempts 里。
        fail_classes: dict[str, int] = {}
        for a in attempts:
            if a.outcome == "fail" and a.error_class:
                fail_classes[a.error_class] = fail_classes.get(a.error_class, 0) + 1
        attempted = sum(1 for a in attempts if a.outcome != "skip")
        error = (f"所有候选模型均失败 (attempted={attempted}, "
                 f"blacklisted={sorted(dead_providers)}, error_classes={fail_classes})")
        logger.warning("All candidates exhausted: %s", error)
        return SpawnResult(
            run_id="", status="error",
            error=error, attempts=attempts,
        )
