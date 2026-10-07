"""
Pipeline Runner - 高健壮性流水线执行引擎

负责流水线的阶段编排、依赖门禁检查、重试与状态跟踪。
"""

import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Set

from agent_delegate.models.base import SpawnResult, Task, TaskType
from agent_delegate.router.router import Router
from agent_delegate.workers.pipelines import PIPELINES, Pipeline, Stage, StageStatus

logger = logging.getLogger(__name__)


@dataclass
class StageRun:
    """单个阶段的执行结果记录"""
    stage_name: str
    status: StageStatus
    retries: int = 0
    spawn_result: Optional[SpawnResult] = None
    error: Optional[str] = None
    output_artifacts: list[str] = field(default_factory=list)
    attempt_chain: list = field(default_factory=list)
    spawn_attempts: list[SpawnResult] = field(default_factory=list)
    duration_ms: Optional[float] = None


@dataclass
class PipelineRun:
    """整条流水线的执行结果记录"""
    pipeline_name: str
    status: StageStatus  # COMPLETED | FAILED | SKIPPED
    stage_runs: list[StageRun] = field(default_factory=list)
    artifacts: set[str] = field(default_factory=set)
    error: Optional[str] = None


class PipelineRunner:
    """
    流水线执行引擎。

    - 验证前置依赖门禁（validate_gates），依赖不满足时级联跳过（SKIPPED）。
    - 阶段派发调用 router.dispatch_with_fallback(..., wait=True)。
    - 支持 stage.max_retries 失败重试。
    - 产出详细的 PipelineRun 和 StageRun 审计记录。
    """

    def __init__(self, router: Router):
        self.router = router

    def run(
        self,
        pipeline_name: str,
        context: str = "",
        existing_artifacts: Optional[Set[str]] = None,
    ) -> PipelineRun:
        """
        执行指定名称的流水线。

        Args:
            pipeline_name: 注册在 PIPELINES 中的流水线名称，或外部直接传入
            context: 任务上下文描述
            existing_artifacts: 初始已有产物文件集合

        Returns:
            PipelineRun 包含各阶段的执行状态与最终累积产物
        """
        if pipeline_name not in PIPELINES:
            raise ValueError(f"Unknown pipeline: {pipeline_name}")

        pipeline_template = PIPELINES[pipeline_name]
        # 深拷贝 pipeline 以免不同 run 互相影响状态
        pipeline: Pipeline = copy.deepcopy(pipeline_template)

        current_artifacts: set[str] = set(existing_artifacts or set())
        stage_runs: list[StageRun] = []
        overall_status = StageStatus.COMPLETED
        pipeline_error: Optional[str] = None
        has_cascade_skip = False

        for stage in pipeline.stages:
            # 1. 当前阶段门禁不满足
            if not pipeline.validate_gates(stage, current_artifacts):
                logger.warning(
                    "Stage '%s' skipped: missing input_gates=%s",
                    stage.name, stage.input_gates
                )
                stage.status = StageStatus.SKIPPED
                stage.error = f"Input gates not satisfied: {stage.input_gates}"
                stage_run = StageRun(
                    stage_name=stage.name,
                    status=StageStatus.SKIPPED,
                    retries=0,
                    error=stage.error,
                    output_artifacts=[],
                    duration_ms=0.0,
                )
                stage_runs.append(stage_run)
                has_cascade_skip = True
                continue

            # 2. 执行当前阶段（带重试）
            stage.status = StageStatus.IN_PROGRESS
            max_retries = max(0, stage.max_retries)
            attempt = 0
            success = False
            last_spawn_result: Optional[SpawnResult] = None
            last_error: Optional[str] = None
            stage_started = time.perf_counter()
            spawn_attempts: list[SpawnResult] = []

            # 总尝试次数 = 1 次首次执行 + max_retries 次重试
            while attempt <= max_retries:
                if attempt > 0:
                    logger.info(
                        "Retrying stage '%s', attempt %d/%d",
                        stage.name, attempt, max_retries
                    )
                task = Task(
                    description=stage.role_prompt,
                    task_type=TaskType.CODING if stage.model_tier == "heavy" else TaskType.STANDARD,
                    timeout_seconds=stage.timeout_seconds,
                )

                spawn_result = self.router.dispatch_with_fallback(
                    task=task,
                    context=context,
                    wait=True,
                )

                last_spawn_result = spawn_result
                spawn_attempts.append(spawn_result)

                if spawn_result.status == "completed":
                    success = True
                    break
                else:
                    last_error = spawn_result.error or f"Spawn failed with status '{spawn_result.status}'"
                    attempt += 1

            if success:
                logger.debug("Stage '%s' completed successfully", stage.name)
                stage.status = StageStatus.COMPLETED
                stage.error = None
                current_artifacts.update(stage.output_artifacts)
                stage_run = StageRun(
                    stage_name=stage.name,
                    status=StageStatus.COMPLETED,
                    retries=attempt,  # 重试次数：第0次成功为0，第1次重试成功为1
                    spawn_result=last_spawn_result,
                    error=None,
                    output_artifacts=list(stage.output_artifacts),
                    attempt_chain=last_spawn_result.attempts if last_spawn_result else [],
                    spawn_attempts=spawn_attempts,
                    duration_ms=(time.perf_counter() - stage_started) * 1000,
                )
                stage_runs.append(stage_run)
            else:
                logger.error(
                    "Stage '%s' failed after %d attempts: %s",
                    stage.name, attempt, last_error
                )
                # Log structured failure details for debugging
                spawn_summary = ", ".join(
                    f"{i+1}:{sr.status}" for i, sr in enumerate(spawn_attempts)
                )
                logger.error(
                    "Stage '%s' spawn attempts: [%s]. Last error: %s",
                    stage.name, spawn_summary, last_error
                )
                stage.status = StageStatus.FAILED
                stage.error = last_error
                # attempt 此时为已重试次数
                retries_done = min(attempt, max_retries)
                stage_run = StageRun(
                    stage_name=stage.name,
                    status=StageStatus.FAILED,
                    retries=retries_done,
                    spawn_result=last_spawn_result,
                    error=stage.error,
                    output_artifacts=[],
                    attempt_chain=last_spawn_result.attempts if last_spawn_result else [],
                    spawn_attempts=spawn_attempts,
                    duration_ms=(time.perf_counter() - stage_started) * 1000,
                )
                stage_runs.append(stage_run)

                # 当前阶段失败，触发后续阶段级联跳过，并将整体流水线标记为失败
                has_cascade_skip = True
                overall_status = StageStatus.FAILED
                if not pipeline_error:
                    pipeline_error = f"Stage '{stage.name}' failed: {last_error}"

        # 如果没有 FAILED 但是有阶段被 SKIPPED（如初始 input_gates 就不满足）
        if overall_status != StageStatus.FAILED:
            if any(sr.status == StageStatus.SKIPPED for sr in stage_runs):
                overall_status = StageStatus.SKIPPED
                if not pipeline_error:
                    pipeline_error = "One or more stages were skipped due to unsatisfied input gates"

        return PipelineRun(
            pipeline_name=pipeline_name,
            status=overall_status,
            stage_runs=stage_runs,
            artifacts=current_artifacts,
            error=pipeline_error,
        )
