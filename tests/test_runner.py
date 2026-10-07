"""测试 PipelineRunner：产物关卡、结果重派、复用 Router 降级链。"""

import copy

from agent_delegate.models.base import (
    FallbackChain, ModelCandidate, RuntimeAdapter, SpawnResult, WorkerOutput,
)
from agent_delegate.router.router import Router
from agent_delegate.workers.pipelines import CODING_PIPELINE, Pipeline, Stage, StageStatus
from agent_delegate.workers.runner import PipelineRunner


class FileWritingAdapter(RuntimeAdapter):
    """
    模拟会写文件的 worker：spawn 时把脚本指定的产物写进 workdir。

    script: 按调用顺序消费的动作列表，每项是 dict：
      - error: 让 spawn 直接失败
      - write: 要创建或改写的相对路径列表（以 / 结尾的建目录并在其中写一个文件）
      - summary: listen 返回的 summary（默认 "done <run_id>"）
      - ok:    listen 返回的 success（默认 True）
    脚本用尽后默认成功但什么都不写。
    """

    def __init__(self, workdir, script):
        self.workdir = workdir
        self.script = list(script)
        self.calls: list[str] = []
        self.tasks: list[str] = []
        self._outputs: dict[str, WorkerOutput] = {}

    def spawn(self, task: str, model: str, **kwargs) -> SpawnResult:
        self.calls.append(model)
        self.tasks.append(task)
        step = self.script.pop(0) if self.script else {}
        if "error" in step:
            return SpawnResult(run_id="", status="error", error=step["error"])
        run_id = f"run-{len(self.calls)}"
        for rel in step.get("write", []):
            path = self.workdir / rel
            if rel.endswith("/"):
                path.mkdir(parents=True, exist_ok=True)
                path = path / "file.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(run_id)  # 内容随 run 变化，改写同一文件也能被识别为新产出
        summary = step.get("summary", f"done {run_id}")
        self._outputs[run_id] = WorkerOutput(success=step.get("ok", True), summary=summary)
        return SpawnResult(run_id=run_id, status="completed")

    def listen(self, run_id: str, timeout_ms: int = 30000) -> WorkerOutput:
        return self._outputs.pop(run_id)

    def send(self, message: str, **kwargs) -> None:
        pass

    def list_runs(self, **kwargs) -> list:
        return []


TIERS = {
    "light": FallbackChain([ModelCandidate("light-a", "p1")]),
    "heavy": FallbackChain([ModelCandidate("heavy-a", "p1"), ModelCandidate("heavy-b", "p2")]),
}


def _two_stage(max_retries=1):
    return Pipeline(name="t", description="", stages=[
        Stage(name="Plan", role_prompt="plan", input_gates=[], output_artifacts=["PLAN.md"],
              model_tier="heavy", max_retries=max_retries),
        Stage(name="Build", role_prompt="build", input_gates=["PLAN.md"], output_artifacts=["out/"],
              model_tier="light", max_retries=max_retries),
    ])


def _runner(tmp_path, script):
    adapter = FileWritingAdapter(tmp_path, script)
    return PipelineRunner(Router(adapter), tmp_path, tier_chains=TIERS), adapter


def test_all_stages_complete(tmp_path):
    runner, adapter = _runner(tmp_path, [{"write": ["PLAN.md"]}, {"write": ["out/"]}])
    result = runner.run(_two_stage(), "build a thing")
    assert result.success
    assert [r.status for r in result.records] == [StageStatus.COMPLETED] * 2
    # 按阶段档位选链，而不是按任务描述的关键词分类
    assert adapter.calls == ["heavy-a", "light-a"]
    assert "前序阶段已产出：PLAN.md" in adapter.tasks[1]
    assert "前序阶段已产出" not in adapter.tasks[0]


def test_missing_artifact_retries_then_fails(tmp_path):
    runner, adapter = _runner(tmp_path, [{}, {}])
    result = runner.run(_two_stage(max_retries=1), "x")
    assert not result.success
    plan = result.records[0]
    assert plan.status == StageStatus.FAILED and plan.tries == 2
    assert "PLAN.md" in plan.error
    assert len(result.records) == 1  # 失败即停，后续阶段不执行
    assert result.pipeline.stages[1].status == StageStatus.PENDING


def test_worker_failure_then_success(tmp_path):
    runner, _ = _runner(tmp_path, [{"ok": False}, {"write": ["PLAN.md"]}, {"write": ["out/"]}])
    result = runner.run(_two_stage(max_retries=1), "x")
    assert result.success
    assert result.records[0].tries == 2


def test_input_gate_blocks_stage(tmp_path):
    pipeline = Pipeline(name="t", description="", stages=[
        Stage(name="Review", role_prompt="r", input_gates=["src/"], output_artifacts=["REVIEW.md"],
              model_tier="light"),
    ])
    runner, adapter = _runner(tmp_path, [])
    result = runner.run(pipeline, "x")
    assert result.failed_stage.name == "Review"
    assert "src/" in result.failed_stage.error
    assert adapter.calls == []  # 前置产物不全时不应派发


def test_reuses_router_fallback_chain(tmp_path):
    runner, adapter = _runner(tmp_path, [
        {"error": "HTTP 429 Too Many Requests"},
        {"write": ["PLAN.md"]},
        {"write": ["out/"]},
    ])
    result = runner.run(_two_stage(), "x")
    assert result.success
    assert adapter.calls[:2] == ["heavy-a", "heavy-b"]
    assert result.records[0].model == "heavy-b"
    assert any(a.error_class == "rate_limit" for a in result.records[0].attempts)


def test_exhausted_chain_does_not_retry_stage(tmp_path):
    runner, adapter = _runner(tmp_path, [{"error": "401 Unauthorized"}, {"error": "401 Unauthorized"}])
    result = runner.run(_two_stage(max_retries=3), "x")
    assert not result.success
    assert result.records[0].tries == 1
    assert adapter.calls == ["heavy-a", "heavy-b"]


def test_unknown_tier_fails(tmp_path):
    pipeline = Pipeline(name="t", description="", stages=[
        Stage(name="S", role_prompt="s", input_gates=[], output_artifacts=[], model_tier="ultra"),
    ])
    runner, _ = _runner(tmp_path, [])
    result = runner.run(pipeline, "x")
    assert "ultra" in result.failed_stage.error


def test_predefined_pipeline_is_not_mutated(tmp_path):
    before = copy.deepcopy(CODING_PIPELINE)
    runner, _ = _runner(tmp_path, [{"write": ["PLAN.md"]}])
    runner.tier_chains = {**TIERS, "standard": TIERS["light"]}
    result = runner.run(CODING_PIPELINE, "x")
    assert result.records[0].status == StageStatus.COMPLETED
    assert CODING_PIPELINE == before


# ─── 产物必须是本次产出 ───

def _single(output, input_gates=(), max_retries=0):
    return Pipeline(name="t", description="", stages=[
        Stage(name="S", role_prompt="s", input_gates=list(input_gates), output_artifacts=[output],
              model_tier="light", max_retries=max_retries),
    ])


def test_preexisting_artifact_is_not_delivery(tmp_path):
    (tmp_path / "PLAN.md").write_text("old")
    runner, _ = _runner(tmp_path, [{}])
    result = runner.run(_single("PLAN.md"), "x")
    assert not result.success
    assert "PLAN.md" in result.failed_stage.error


def test_rewriting_input_artifact_counts(tmp_path):
    """Editor 类阶段：输出同时也是输入，必须真的改写过才算完成。"""
    (tmp_path / "DOC.md").write_text("draft")
    runner, _ = _runner(tmp_path, [{}, {"write": ["DOC.md"]}])
    result = runner.run(_single("DOC.md", input_gates=["DOC.md"], max_retries=1), "x")
    assert result.success
    assert result.records[0].tries == 2


def test_partial_output_from_failed_try_is_not_reused(tmp_path):
    runner, _ = _runner(tmp_path, [{"write": ["PLAN.md"], "ok": False}, {}])
    result = runner.run(_single("PLAN.md", max_retries=1), "x")
    assert not result.success


def test_dir_artifact_rejects_file_and_empty_dir(tmp_path):
    runner, _ = _runner(tmp_path, [{"write": ["out"]}])
    assert not runner.run(_single("out/"), "x").success  # 同名普通文件
    (tmp_path / "out").unlink()
    (tmp_path / "out").mkdir()
    runner, _ = _runner(tmp_path, [{}])
    assert not runner.run(_single("out/"), "x").success  # 空目录


def test_empty_dir_does_not_satisfy_input_gate(tmp_path):
    (tmp_path / "src").mkdir()
    runner, adapter = _runner(tmp_path, [])
    result = runner.run(_single("R.md", input_gates=["src/"]), "x")
    assert "src/" in result.failed_stage.error
    assert adapter.calls == []


# ─── 模型链来源 ───

def test_uses_router_chains_by_default(tmp_path):
    from agent_delegate.models.base import TaskType
    adapter = FileWritingAdapter(tmp_path, [{"write": ["PLAN.md"]}, {"write": ["out/"]}])
    chains = {t: FallbackChain([ModelCandidate(f"mine-{t.value}", "p")]) for t in TaskType}
    result = PipelineRunner(Router(adapter, chains=chains), tmp_path).run(_two_stage(), "x")
    assert result.success
    assert adapter.calls == ["mine-coding", "mine-trivial"]


def test_empty_tier_chains_is_not_replaced_by_default(tmp_path):
    adapter = FileWritingAdapter(tmp_path, [])
    result = PipelineRunner(Router(adapter), tmp_path, tier_chains={}).run(_two_stage(), "x")
    assert "heavy" in result.failed_stage.error
    assert adapter.calls == []


# ─── 多次重试下的 record 字段 ───

def test_record_reflects_last_try_and_accumulates_attempts(tmp_path):
    runner, adapter = _runner(tmp_path, [
        {"ok": False, "summary": "first"},                      # try 1: heavy-a
        {"error": "HTTP 429 Too Many Requests"},                # try 2: heavy-a 限流
        {"write": ["PLAN.md"], "summary": "second"},           # try 2: heavy-b
    ])
    record = runner.run(_two_stage(max_retries=1), "x").records[0]
    assert record.status == StageStatus.COMPLETED
    assert record.tries == 2
    assert record.model == "heavy-b"
    assert record.summary == "second"
    assert str(record.attempts[0]) == "ok heavy-a"
    assert str(record.attempts[-1]) == "ok heavy-b"
    assert len(record.attempts) == 3


def test_record_cleared_when_last_try_fails_to_dispatch(tmp_path):
    runner, _ = _runner(tmp_path, [
        {"ok": False, "summary": "stale"},
        {"error": "401 Unauthorized"},
        {"error": "401 Unauthorized"},
    ])
    record = runner.run(_two_stage(max_retries=1), "x").records[0]
    assert record.status == StageStatus.FAILED
    assert record.model is None
    assert record.summary == ""
    assert "所有候选模型均失败" in record.error


def test_worker_creating_empty_dir_fails(tmp_path):
    """worker 只建了空子目录，没有写文件，应判为未产出。"""
    class EmptyDirAdapter(FileWritingAdapter):
        def spawn(self, task, model, **kwargs):
            (self.workdir / "out" / "sub").mkdir(parents=True, exist_ok=True)
            run_id = f"run-{len(self.calls)+1}"
            self._outputs[run_id] = WorkerOutput(success=True, summary="done")
            return SpawnResult(run_id=run_id, status="completed")

    runner = PipelineRunner(Router(EmptyDirAdapter(tmp_path, [])), tmp_path, tier_chains=TIERS)
    result = runner.run(_single("out/"), "x")
    assert not result.success
    assert "out/" in result.failed_stage.error


def test_partial_router_chains_no_keyerror(tmp_path):
    """Router(chains=...) 只给部分类型时不应抛 KeyError。"""
    from agent_delegate.models.base import TaskType
    adapter = FileWritingAdapter(tmp_path, [{"write": ["PLAN.md"]}])
    chains = {TaskType.CODING: FallbackChain([ModelCandidate("only-heavy", "p")])}
    result = PipelineRunner(Router(adapter, chains=chains), tmp_path).run(
        _two_stage(), "x"
    )
    # heavy 用自定义链，light 回退到 STANDARD（不存在）→ 失败但不抛异常
    assert result.records[0].status == StageStatus.COMPLETED
    assert result.records[1].status == StageStatus.FAILED


# ─── 逐轮 SpawnResult 与阶段耗时（B2 嫁接） ───

def test_stage_record_tracks_spawn_attempts_and_duration(tmp_path):
    runner, adapter = _runner(tmp_path, [
        {"ok": False, "summary": "first"},          # try 1: listen 报失败
        {"write": ["PLAN.md"], "summary": "second"},  # try 2: 成功
    ])
    record = runner.run(_two_stage(max_retries=1), "x").records[0]
    assert record.status == StageStatus.COMPLETED
    assert len(record.spawn_attempts) == record.tries == 2
    # 每轮都是完整 SpawnResult（含降级轨迹 AttemptRecord）
    assert record.spawn_attempts[0].attempts  # 非空审计轨迹
    assert record.duration_ms is not None and record.duration_ms >= 0


# ─── listen 未等到终态 ───

def test_incomplete_listen_is_not_redispatched(tmp_path):
    """listen 超时（远端可能仍在跑）：不重派，把 run_id 交还调用方。"""
    class StillRunningAdapter(FileWritingAdapter):
        def listen(self, run_id, timeout_ms=30000):
            return WorkerOutput(success=False, summary="Timeout waiting for agent", incomplete=True)

    adapter = StillRunningAdapter(tmp_path, [])
    result = PipelineRunner(Router(adapter), tmp_path, tier_chains=TIERS).run(
        _two_stage(max_retries=2), "x")
    record = result.records[0]
    assert adapter.calls == ["heavy-a"]
    assert record.incomplete is True
    assert record.run_id == "run-1"
    assert record.tries == 1
    assert result.incomplete_stage is record
    assert result.failed_stage is None
    assert not result.success
    assert len(result.records) == 1  # 后续阶段不开始
