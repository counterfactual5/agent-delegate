"""
PipelineRunner - 按阶段执行流水线

每个阶段：检查前置产物 → 经 Router 带降级派发 → listen() 取结果 → 核对产出文件。
两层重试各管一件事：
- 模型 / provider 级失败（429、5xx、超时）由 Router.dispatch_with_fallback 在候选链内降级；
- 结果不合格（worker 报告失败、没交出产物）由 Stage.max_retries 控制重派次数。

产物路径以 / 结尾表示目录，必须是非空目录；否则必须是文件。输出产物还必须是本次尝试
新建或修改的：工作目录里已有的旧文件、上次失败尝试留下的半成品都不算交付。
"""

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from agent_delegate.models.base import FallbackChain, Task, TaskType
from agent_delegate.router.router import Router
from agent_delegate.workers.pipelines import Pipeline, Stage, StageStatus

#: 阶段模型档位 → 任务类型，用 router.select_model() 取候选链，从而尊重 Router(chains=...)。
TIER_TASK_TYPES: dict[str, TaskType] = {
    "light": TaskType.TRIVIAL,
    "standard": TaskType.STANDARD,
    "heavy": TaskType.CODING,
}


@dataclass
class StageRecord:
    """单个阶段的执行记录；model / summary 反映最后一次尝试"""
    name: str
    status: StageStatus
    tries: int = 0
    model: Optional[str] = None
    summary: str = ""
    error: Optional[str] = None
    attempts: list[str] = field(default_factory=list)  # 各次派发的降级轨迹，按顺序拼接


@dataclass
class PipelineResult:
    """一次流水线运行的结果"""
    pipeline: Pipeline  # 本次运行的独立副本，含各阶段最终状态
    records: list[StageRecord] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return all(s.status == StageStatus.COMPLETED for s in self.pipeline.stages)

    @property
    def failed_stage(self) -> Optional[StageRecord]:
        return next((r for r in self.records if r.status == StageStatus.FAILED), None)


class PipelineRunner:
    """顺序执行流水线各阶段，遇到失败即停止。"""

    def __init__(
        self,
        router: Router,
        workdir: str | Path,
        tier_chains: Optional[dict[str, FallbackChain]] = None,
    ):
        self.router = router
        self.workdir = Path(workdir)
        self.tier_chains = tier_chains  # 显式覆盖；None 表示按档位从 router 取链

    def run(self, pipeline: Pipeline, description: str, context: str = None) -> PipelineResult:
        # 预定义流水线是模块级单例，不能把运行状态写回去。
        pipeline = copy.deepcopy(pipeline)
        result = PipelineResult(pipeline=pipeline)
        for stage in pipeline.stages:
            record = self._run_stage(stage, description, context, pipeline)
            result.records.append(record)
            if record.status != StageStatus.COMPLETED:
                break
        return result

    def _chain_for(self, tier: str) -> Optional[FallbackChain]:
        if self.tier_chains is not None:
            return self.tier_chains.get(tier)
        task_type = TIER_TASK_TYPES.get(tier)
        return self.router.select_model(task_type) if task_type else None

    def _run_stage(
        self, stage: Stage, description: str, context: Optional[str], pipeline: Pipeline,
    ) -> StageRecord:
        record = StageRecord(name=stage.name, status=StageStatus.IN_PROGRESS)
        stage.status = StageStatus.IN_PROGRESS

        missing = [p for p in stage.input_gates if not self._present(p)]
        if missing:
            return self._fail(stage, record, f"缺少前置产物: {', '.join(missing)}")

        chain = self._chain_for(stage.model_tier)
        if chain is None:
            return self._fail(stage, record, f"未知模型档位: {stage.model_tier}")

        error = None
        for _ in range(1 + stage.max_retries):
            record.tries += 1
            record.model, record.summary = None, ""
            before = {p: self._fingerprint(p) for p in stage.output_artifacts}
            task = Task(
                description=self._stage_prompt(stage, description),
                timeout_seconds=stage.timeout_seconds,
            )
            spawned = self.router.dispatch_with_fallback(
                task, context=self._stage_context(context, pipeline), chain=chain,
            )
            record.attempts.extend(spawned.attempts)
            if spawned.status == "error":
                # 候选链已经耗尽，再重派只会重复同样的失败。
                return self._fail(stage, record, spawned.error or "派发失败")

            output = self.router.adapter.listen(
                spawned.run_id, timeout_ms=stage.timeout_seconds * 1000,
            )
            record.model = spawned.model
            record.summary = output.summary
            if not output.success:
                error = f"worker 报告失败: {output.summary}"
                continue
            stale = [
                p for p in stage.output_artifacts
                if not self._present(p) or self._fingerprint(p) == before[p]
            ]
            if stale:
                error = f"本次未产出: {', '.join(stale)}"
                continue

            stage.status = StageStatus.COMPLETED
            record.status = StageStatus.COMPLETED
            return record

        return self._fail(stage, record, error)

    # ─── helpers ────────────────────────────────────────────

    def _present(self, rel: str) -> bool:
        path = self.workdir / rel
        if rel.endswith("/"):
            return path.is_dir() and any(f.is_file() for f in path.rglob("*"))
        return path.is_file()

    def _fingerprint(self, rel: str) -> Optional[tuple]:
        """产物的内容指纹：文件取 (mtime, size)，目录取其下所有文件的 (路径, mtime, size)。"""
        path = self.workdir / rel
        if path.is_file():
            st = path.stat()
            return (st.st_mtime_ns, st.st_size)
        if path.is_dir():
            return tuple(sorted(
                (str(f.relative_to(path)), f.stat().st_mtime_ns, f.stat().st_size)
                for f in path.rglob("*") if f.is_file()
            ))
        return None

    def _stage_prompt(self, stage: Stage, description: str) -> str:
        lines = [stage.role_prompt, "", f"任务：{description}", f"工作目录：{self.workdir}"]
        if stage.input_gates:
            lines.append(f"可用输入：{', '.join(stage.input_gates)}")
        if stage.output_artifacts:
            lines.append(f"必须产出（相对工作目录）：{', '.join(stage.output_artifacts)}")
        return "\n".join(lines)

    @staticmethod
    def _stage_context(context: Optional[str], pipeline: Pipeline) -> str:
        parts = [context or "（无额外上下文）"]
        done = pipeline.completed_artifacts()
        if done:
            parts.append(f"前序阶段已产出：{', '.join(done)}")
        return "\n".join(parts)

    @staticmethod
    def _fail(stage: Stage, record: StageRecord, error: Optional[str]) -> StageRecord:
        stage.status = StageStatus.FAILED
        stage.error = error
        record.status = StageStatus.FAILED
        record.error = error
        return record
