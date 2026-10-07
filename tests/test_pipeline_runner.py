import sys
sys.path.insert(0, ".")

"""
Tests for PipelineRunner engine.
"""

from unittest.mock import Mock

import pytest

from agent_delegate.models.base import RuntimeAdapter, SpawnResult
from agent_delegate.router.router import Router
from agent_delegate.workers.pipelines import PIPELINES, Pipeline, Stage, StageStatus
from agent_delegate.workers.runner import PipelineRun, PipelineRunner, StageRun


class MockAdapter(RuntimeAdapter):
    """用于测试的 Mock Adapter"""
    def __init__(self):
        self.spawn_calls = []

    def spawn(self, task: str, model: str, **kwargs) -> SpawnResult:
        self.spawn_calls.append({"task": task, "model": model, "kwargs": kwargs})
        return SpawnResult(run_id="run-123", status="completed")

    def listen(self, run_id: str, timeout_ms: int = 30000):
        pass

    def send(self, message: str, **kwargs) -> None:
        pass

    def list_runs(self, **kwargs) -> list:
        return []


def test_pipeline_runner_full_success(monkeypatch):
    """测试完整流水线所有阶段顺利执行成功"""
    mock_router = Mock(spec=Router)
    mock_router.dispatch_with_fallback.return_value = SpawnResult(
        run_id="run-ok",
        status="completed",
        model="heavy-model",
    )

    runner = PipelineRunner(router=mock_router)
    result = runner.run("coding", context="需求描述")

    assert isinstance(result, PipelineRun)
    assert result.status == StageStatus.COMPLETED
    assert result.pipeline_name == "coding"
    assert len(result.stage_runs) == len(PIPELINES["coding"].stages)

    # 验证每一个阶段都标记为 COMPLETED
    for stage_run in result.stage_runs:
        assert stage_run.status == StageStatus.COMPLETED
        assert stage_run.retries == 0
        assert stage_run.spawn_result is not None
        assert stage_run.spawn_result.status == "completed"

    # 验证产出产物累计
    assert "PLAN.md" in result.artifacts
    assert "src/" in result.artifacts
    assert "REVIEW.md" in result.artifacts
    assert "CONSULTANT.md" in result.artifacts

    # 验证调用 dispatch_with_fallback 时带有 wait=True
    assert mock_router.dispatch_with_fallback.call_count == len(PIPELINES["coding"].stages)
    for call in mock_router.dispatch_with_fallback.call_args_list:
        assert call.kwargs.get("wait") is True


def test_pipeline_runner_gate_cascade_skip():
    """测试当前置产物不满足时，触发级联跳过 (StageStatus.SKIPPED)"""
    mock_router = Mock(spec=Router)
    # Planner 成功，但如果某些原因后续依赖缺失（模拟自定义 pipeline）
    test_pipeline = Pipeline(
        name="test_cascade",
        description="测试级联跳过",
        stages=[
            Stage(
                name="Stage1",
                role_prompt="P1",
                input_gates=["non_existent_gate.md"],
                output_artifacts=["art1.md"],
            ),
            Stage(
                name="Stage2",
                role_prompt="P2",
                input_gates=["art1.md"],
                output_artifacts=["art2.md"],
            ),
        ],
    )

    # 临时注册到 PIPELINES
    PIPELINES["test_cascade"] = test_pipeline
    try:
        runner = PipelineRunner(router=mock_router)
        result = runner.run("test_cascade")

        assert result.status == StageStatus.SKIPPED
        assert len(result.stage_runs) == 2
        assert result.stage_runs[0].status == StageStatus.SKIPPED
        assert result.stage_runs[1].status == StageStatus.SKIPPED
        # 门禁未满足时不应调用 router
        assert mock_router.dispatch_with_fallback.call_count == 0
    finally:
        del PIPELINES["test_cascade"]


def test_pipeline_runner_retry_success():
    """测试阶段失败后在 max_retries 内重试成功"""
    mock_router = Mock(spec=Router)
    # 模拟 Planner: 第一次失败，第二次重试成功
    call_count = 0

    def mock_dispatch(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return SpawnResult(run_id="run-fail", status="error", error="API error")
        return SpawnResult(run_id="run-success", status="completed")

    mock_router.dispatch_with_fallback.side_effect = mock_dispatch

    test_pipeline = Pipeline(
        name="test_retry",
        description="测试重试成功",
        stages=[
            Stage(
                name="Planner",
                role_prompt="Plan something",
                input_gates=[],
                output_artifacts=["PLAN.md"],
                max_retries=2,
            )
        ],
    )
    PIPELINES["test_retry"] = test_pipeline
    try:
        runner = PipelineRunner(router=mock_router)
        result = runner.run("test_retry")

        assert result.status == StageStatus.COMPLETED
        assert len(result.stage_runs) == 1
        assert result.stage_runs[0].status == StageStatus.COMPLETED
        assert result.stage_runs[0].retries == 1  # 经历了 1 次重试后成功
        assert "PLAN.md" in result.artifacts
        assert call_count == 2
    finally:
        del PIPELINES["test_retry"]


def test_stage_run_preserves_rounds_and_duration():
    """阶段审计应逐轮保留 SpawnResult（spawn_attempts）并记录耗时"""
    mock_router = Mock(spec=Router)
    call_count = 0

    def mock_dispatch(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return SpawnResult(run_id="run-fail", status="error", error="API error")
        return SpawnResult(run_id="run-success", status="completed")

    mock_router.dispatch_with_fallback.side_effect = mock_dispatch

    test_pipeline = Pipeline(
        name="test_rounds",
        description="测试逐轮保留",
        stages=[
            Stage(
                name="Planner",
                role_prompt="Plan something",
                input_gates=[],
                output_artifacts=["PLAN.md"],
                max_retries=2,
            )
        ],
    )
    PIPELINES["test_rounds"] = test_pipeline
    try:
        runner = PipelineRunner(router=mock_router)
        result = runner.run("test_rounds")

        stage_run = result.stage_runs[0]
        assert stage_run.status == StageStatus.COMPLETED
        assert len(stage_run.spawn_attempts) == 2  # 两轮 SpawnResult 都在
        assert stage_run.spawn_attempts[0].run_id == "run-fail"
        assert stage_run.spawn_attempts[1].run_id == "run-success"
        assert stage_run.duration_ms is not None
        assert stage_run.duration_ms >= 0
    finally:
        del PIPELINES["test_rounds"]


def test_skipped_stage_duration_is_zero_and_summable():
    """SKIPPED 阶段报 duration_ms=0.0，下游聚合不会见到 None。"""
    test_pipeline = Pipeline(
        name="test_skip_dur",
        description="测试跳过阶段耗时",
        stages=[
            Stage(name="A", role_prompt="a", input_gates=["MISSING.md"],
                  output_artifacts=["A.md"]),
            Stage(name="B", role_prompt="b", input_gates=["A.md"],
                  output_artifacts=["B.md"]),
        ],
    )
    PIPELINES["test_skip_dur"] = test_pipeline
    try:
        result = PipelineRunner(router=Mock()).run("test_skip_dur")
        assert all(sr.status == StageStatus.SKIPPED for sr in result.stage_runs)
        assert all(sr.duration_ms == 0.0 for sr in result.stage_runs)
        total = sum(sr.duration_ms for sr in result.stage_runs)  # must not raise
        assert total == 0.0
    finally:
        del PIPELINES["test_skip_dur"]


def test_pipeline_runner_retry_exhausted_and_cascade_fail():
    """测试重试耗尽导致失败，后续阶段自动级联跳过"""
    mock_router = Mock(spec=Router)
    # 一直返回 error
    mock_router.dispatch_with_fallback.return_value = SpawnResult(
        run_id="run-fail",
        status="error",
        error="Persistent failure",
    )

    test_pipeline = Pipeline(
        name="test_exhausted",
        description="测试重试耗尽并级联",
        stages=[
            Stage(
                name="Stage1",
                role_prompt="P1",
                input_gates=[],
                output_artifacts=["art1.md"],
                max_retries=2,
            ),
            Stage(
                name="Stage2",
                role_prompt="P2",
                input_gates=["art1.md"],
                output_artifacts=["art2.md"],
            ),
        ],
    )
    PIPELINES["test_exhausted"] = test_pipeline
    try:
        runner = PipelineRunner(router=mock_router)
        result = runner.run("test_exhausted")

        assert result.status == StageStatus.FAILED
        assert len(result.stage_runs) == 2

        # Stage1 失败，重试了 2 次（总尝试 1+2=3 次）
        assert result.stage_runs[0].status == StageStatus.FAILED
        assert result.stage_runs[0].retries == 2
        assert mock_router.dispatch_with_fallback.call_count == 3

        # Stage2 因 Stage1 未产出 art1.md 级联跳过
        assert result.stage_runs[1].status == StageStatus.SKIPPED
        assert "art1.md" not in result.artifacts
    finally:
        del PIPELINES["test_exhausted"]


def test_pipeline_runner_with_existing_artifacts():
    """测试预先传入 existing_artifacts 时能正确通过对应门禁"""
    mock_router = Mock(spec=Router)
    mock_router.dispatch_with_fallback.return_value = SpawnResult(
        run_id="run-ok",
        status="completed",
    )

    test_pipeline = Pipeline(
        name="test_existing",
        description="测试已有产物",
        stages=[
            Stage(
                name="Builder",
                role_prompt="Build code",
                input_gates=["PLAN.md"],
                output_artifacts=["src/"],
            ),
        ],
    )
    PIPELINES["test_existing"] = test_pipeline
    try:
        runner = PipelineRunner(router=mock_router)
        # 提供已有的 PLAN.md
        result = runner.run("test_existing", existing_artifacts={"PLAN.md"})

        assert result.status == StageStatus.COMPLETED
        assert result.stage_runs[0].status == StageStatus.COMPLETED
        assert "PLAN.md" in result.artifacts
        assert "src/" in result.artifacts
    finally:
        del PIPELINES["test_existing"]
