"""Tests for timing and attempt chain tracking."""
import sys
import time
sys.path.insert(0, ".")

from unittest.mock import Mock
from agent_delegate.router.router import Router
from agent_delegate.models.base import (
    Task, TaskType, SpawnResult, RuntimeAdapter, AttemptRecord,
    FallbackChain, ModelCandidate,
)
# PipelineRunner integration tested separately


class TimedMockAdapter(RuntimeAdapter):
    """Mock adapter that simulates timing delays."""
    
    def __init__(self, delay_ms=50, should_fail=False):
        self.delay_ms = delay_ms
        self.should_fail = should_fail
        self.call_count = 0
    
    def spawn(self, task: str, model: str, **kwargs) -> SpawnResult:
        self.call_count += 1
        # Simulate processing time
        time.sleep(self.delay_ms / 1000.0)
        
        if self.should_fail:
            return SpawnResult(
                run_id=f"test-{self.call_count}",
                status="error",
                error="Simulated failure"
            )
        return SpawnResult(
            run_id=f"test-{self.call_count}",
            status="completed"
        )
    
    def listen(self, run_id: str, timeout_ms: int = 30000):
        from agent_delegate.models.base import WorkerOutput
        return WorkerOutput(success=True, summary="Done")
    
    def send(self, message: str, **kwargs) -> None:
        pass
    
    def list_runs(self, **kwargs) -> list:
        return []


def test_duration_captured_on_success():
    """Successful spawn should record duration_ms in AttemptRecord."""
    adapter = TimedMockAdapter(delay_ms=50, should_fail=False)
    router = Router(adapter=adapter)
    
    task = Task(
        description="test task",
        task_type=TaskType.TRIVIAL,
        model_override="test/model"
    )
    
    result = router.dispatch_with_fallback(task, wait=False)
    
    assert result.status == "completed"
    assert len(result.attempts) == 1
    
    attempt = result.attempts[0]
    assert attempt.outcome == "ok"
    assert attempt.duration_ms is not None
    assert attempt.duration_ms >= 50  # At least the simulated delay
    assert attempt.duration_ms < 200  # But not too long


def test_duration_captured_on_failure():
    """Failed spawn should record duration_ms in AttemptRecord."""
    adapter = TimedMockAdapter(delay_ms=30, should_fail=True)
    router = Router(adapter=adapter)
    
    # Use a chain with single candidate to avoid multiple attempts
    router.chains[TaskType.TRIVIAL] = FallbackChain(
        candidates=[ModelCandidate(model_id="test/model", provider="test")]
    )
    
    task = Task(
        description="test task",
        task_type=TaskType.TRIVIAL,
    )
    
    result = router.dispatch_with_fallback(task, wait=False)
    
    assert result.status == "error"
    assert len(result.attempts) == 1
    
    attempt = result.attempts[0]
    assert attempt.outcome == "fail"
    assert attempt.duration_ms is not None
    assert attempt.duration_ms >= 30


def test_fallback_chain_timing():
    """Fallback chain should capture timing for each attempt."""
    # Create adapter that fails first time, succeeds second
    call_count = [0]
    
    class FallbackAdapter(RuntimeAdapter):
        def spawn(self, task: str, model: str, **kwargs) -> SpawnResult:
            call_count[0] += 1
            time.sleep(0.02)  # 20ms delay
            
            if call_count[0] == 1:
                return SpawnResult(run_id="r1", status="error", error="First fail")
            return SpawnResult(run_id="r2", status="completed")
        
        def listen(self, run_id: str, timeout_ms: int = 30000):
            from agent_delegate.models.base import WorkerOutput
            return WorkerOutput(success=True, summary="Done")
        
        def send(self, message: str, **kwargs) -> None: pass
        def list_runs(self, **kwargs) -> list: return []
    
    adapter = FallbackAdapter()
    router = Router(adapter=adapter)
    
    # Set up a chain with two candidates
    router.chains[TaskType.TRIVIAL] = FallbackChain(
        candidates=[
            ModelCandidate(model_id="test/model1", provider="provider1"),
            ModelCandidate(model_id="test/model2", provider="provider2"),
        ]
    )
    
    task = Task(description="test", task_type=TaskType.TRIVIAL)
    result = router.dispatch_with_fallback(task, wait=False)
    
    assert result.status == "completed"
    assert len(result.attempts) == 2
    
    # First attempt failed
    assert result.attempts[0].outcome == "fail"
    assert result.attempts[0].duration_ms is not None
    assert result.attempts[0].duration_ms >= 20
    
    # Second attempt succeeded
    assert result.attempts[1].outcome == "ok"
    assert result.attempts[1].duration_ms is not None
    assert result.attempts[1].duration_ms >= 20


def test_spawn_result_includes_attempt_chain():
    """SpawnResult should include attempts with timing data."""
    adapter = TimedMockAdapter(delay_ms=10, should_fail=False)
    router = Router(adapter=adapter)
    
    task = Task(
        description="test task",
        task_type=TaskType.TRIVIAL,
        model_override="test/model"
    )
    
    result = router.dispatch_with_fallback(task, wait=False)
    
    assert result.status == "completed"
    assert hasattr(result, 'attempts')
    assert len(result.attempts) >= 1
    
    # Check that the attempt chain contains AttemptRecords with timing
    attempt = result.attempts[0]
    assert isinstance(attempt, AttemptRecord)
    assert attempt.duration_ms is not None
    assert attempt.duration_ms >= 10


def test_skip_does_not_have_duration():
    """Skipped attempts should not have duration_ms."""
    adapter = TimedMockAdapter(delay_ms=10, should_fail=True)
    router = Router(adapter=adapter)
    
    # Set up chain where first provider gets blacklisted, second succeeds
    router.chains[TaskType.TRIVIAL] = FallbackChain(
        candidates=[
            ModelCandidate(model_id="auth_fail/model1", provider="provider_a"),
            ModelCandidate(model_id="auth_fail/model2", provider="provider_a"),
            ModelCandidate(model_id="good/model3", provider="provider_b"),
        ]
    )
    
    # Mock to fail with auth error on auth_fail models
    call_count = [0]
    
    def auth_fail_spawn(task: str, model: str, **kwargs) -> SpawnResult:
        call_count[0] += 1
        time.sleep(0.01)
        if "auth_fail" in model:
            return SpawnResult(
                run_id=f"r{call_count[0]}",
                status="error",
                error="401 Unauthorized"
            )
        return SpawnResult(run_id=f"r{call_count[0]}", status="completed")
    
    adapter.spawn = auth_fail_spawn
    
    task = Task(description="test", task_type=TaskType.TRIVIAL)
    result = router.dispatch_with_fallback(task, wait=False)
    
    # Should have: fail, skip, ok
    assert len(result.attempts) == 3
    assert result.attempts[0].outcome == "fail"
    assert result.attempts[0].duration_ms is not None
    
    assert result.attempts[1].outcome == "skip"
    assert result.attempts[1].duration_ms is None  # Skipped, so no timing
    
    assert result.attempts[2].outcome == "ok"
    assert result.attempts[2].duration_ms is not None


if __name__ == "__main__":
    print("Running timing tests...")
    test_duration_captured_on_success()
    print("✓ test_duration_captured_on_success")
    
    test_duration_captured_on_failure()
    print("✓ test_duration_captured_on_failure")
    
    test_fallback_chain_timing()
    print("✓ test_fallback_chain_timing")
    
    test_spawn_result_includes_attempt_chain()
    print("✓ test_spawn_result_includes_attempt_chain")
    
    test_skip_does_not_have_duration()
    print("✓ test_skip_does_not_have_duration")
    
    print("\nAll timing tests passed!")
