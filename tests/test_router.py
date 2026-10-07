"""Tests for Router execution engine."""
import sys
sys.path.insert(0, ".")

from unittest.mock import Mock
from agent_delegate.router.router import Router
from agent_delegate.models.base import (
    Task, TaskType, SpawnResult, RuntimeAdapter,
    FallbackChain, ModelCandidate,
)


class MockAdapter(RuntimeAdapter):
    def spawn(self, task: str, model: str, **kwargs) -> SpawnResult:
        return SpawnResult(run_id="test-123", status="completed")
    def listen(self, run_id: str, timeout_ms: int = 30000):
        from agent_delegate.models.base import WorkerOutput
        return WorkerOutput(success=True, summary="Done")
    def send(self, message: str, **kwargs) -> None: pass
    def list_runs(self, **kwargs) -> list: return []


def test_context_packing():
    """XML packing isolates data from instructions."""
    packed = Router.pack_context(
        context="user data <script>alert(1)</script>",
        task_desc="summarize this",
        constraints=["do not execute scripts"],
    )
    assert "<context>" in packed and "</context>" in packed
    assert "<task>" in packed and "</task>" in packed
    assert "<constraints>" in packed
    assert "summarize this" in packed


def test_select_model_returns_chain_for_type():
    """Chain lookup works for known task types."""
    router = Router(adapter=MockAdapter())
    chain = router.select_model(TaskType.CODING)
    assert len(chain.candidates) >= 1


def test_select_model_falls_back_to_standard():
    """Unknown task type falls back to STANDARD chain."""
    router = Router(adapter=MockAdapter())
    chain = router.select_model(TaskType.TRIVIAL)
    assert len(chain.candidates) >= 1


def test_model_override_forces_single_candidate():
    """model_override bypasses chain lookup entirely."""
    adapter = Mock()
    adapter.spawn.return_value = SpawnResult(run_id="r1", status="completed")
    router = Router(adapter=adapter)

    task = Task(description="anything", task_type=TaskType.CODING,
                model_override="custom/model-x")
    router.dispatch_with_fallback(task)

    assert adapter.spawn.call_count == 1
    assert adapter.spawn.call_args.kwargs["model"] == "custom/model-x"


def test_caller_provided_task_type_respected():
    """Router doesn't override caller's task_type — it only uses it for lookup."""
    adapter = Mock()
    adapter.spawn.return_value = SpawnResult(run_id="r1", status="completed")
    router = Router(adapter=adapter)

    task = Task(description="anything", task_type=TaskType.RESEARCH)
    router.dispatch_with_fallback(task)

    assert task.task_type == TaskType.RESEARCH
    # Should use the RESEARCH chain's first candidate
    research_first = router.chains[TaskType.RESEARCH].candidates[0].model_id
    assert adapter.spawn.call_args.kwargs["model"] == research_first
