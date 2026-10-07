"""测试按错误类型降级的 dispatch_with_fallback。"""

import sys
sys.path.insert(0, ".")

from agent_delegate.router.router import Router
from agent_delegate.models.base import (
    Task, TaskType, SpawnResult, WorkerOutput, RuntimeAdapter,
    ErrorClass, classify_error, AttemptRecord, FallbackChain, ModelCandidate,
)


class ScriptedAdapter(RuntimeAdapter):
    """按 model_id → 预设结果返回；未命中默认成功。记录调用顺序。"""

    def __init__(self, results: dict[str, SpawnResult]):
        self.results = results
        self.calls: list[str] = []

    def spawn(self, task: str, model: str, **kwargs) -> SpawnResult:
        self.calls.append(model)
        return self.results.get(model, SpawnResult(run_id="ok", status="completed"))

    def listen(self, run_id: str, timeout_ms: int = 30000):
        return WorkerOutput(success=True, summary="Done")

    def send(self, message: str, **kwargs) -> None:
        pass

    def list_runs(self, **kwargs) -> list:
        return []


def _err(msg):
    return SpawnResult(run_id="", status="error", error=msg)


# ─── classify_error ───

def test_classify():
    assert classify_error(SpawnResult(run_id="x", status="completed")) == ErrorClass.NONE
    assert classify_error(_err("HTTP 429 Too Many Requests")) == ErrorClass.RATE_LIMIT
    assert classify_error(_err("quota 配额 exceeded")) == ErrorClass.RATE_LIMIT
    assert classify_error(_err("401 Unauthorized: invalid api key")) == ErrorClass.AUTH
    assert classify_error(_err("request timed out after 300s")) == ErrorClass.TIMEOUT
    assert classify_error(_err("502 Bad Gateway")) == ErrorClass.SERVER_ERROR
    assert classify_error(_err("weird thing happened")) == ErrorClass.UNKNOWN
    assert classify_error(_err("")) == ErrorClass.UNKNOWN


# CODING 链: claude-sonnet-5.5(anthropic), gpt-4o(openai), gemini-2.5-flash(openai)
def _coding_task():
    return Task(description="写一个完整的电商后端", task_type=TaskType.CODING)


def test_rate_limit_skips_whole_provider():
    """anthropic 429 → 跳过整个 anthropic，落到 openai 首选。"""
    adapter = ScriptedAdapter({
        "claude-sonnet-5.5": _err("429 rate limit"),
    })
    router = Router(adapter)
    result = router.dispatch_with_fallback(_coding_task())
    assert result.status == "completed"
    assert result.model == "gpt-5.6-sol-high"
    assert adapter.calls == ["claude-sonnet-5.5", "gpt-5.6-sol-high"]


def test_server_error_retries_same_model_once():
    """5xx → 同模型重试一次；第二次成功。"""
    flaky = iter([_err("503 unavailable"), SpawnResult(run_id="ok2", status="completed")])
    adapter = ScriptedAdapter({})

    def spawn(task, model, **kw):
        adapter.calls.append(model)
        if model == "claude-sonnet-5.5":
            return next(flaky)
        return SpawnResult(run_id="ok", status="completed")

    adapter.spawn = spawn  # type: ignore
    router = Router(adapter)
    result = router.dispatch_with_fallback(_coding_task())
    assert result.status == "completed"
    assert result.model == "claude-sonnet-5.5"
    assert adapter.calls == ["claude-sonnet-5.5", "claude-sonnet-5.5"]


def test_timeout_prefers_faster_candidate():
    """超时 → 重排剩余候选，优先 speed_rank 最小者 (gemini-2.5-flash, rank=2)。"""
    adapter = ScriptedAdapter({
        "claude-sonnet-5.5": _err("request timed out"),
    })
    router = Router(adapter)
    result = router.dispatch_with_fallback(_coding_task())
    assert result.status == "completed"
    # gpt-4o(rank5) vs gemini-2.5-flash(rank2) → mini 先跑
    assert result.model == "gemini-2.5-flash"
    assert adapter.calls == ["claude-sonnet-5.5", "gemini-2.5-flash"]


def test_all_fail_returns_error_with_audit():
    adapter = ScriptedAdapter({
        "claude-sonnet-5.5": _err("429"),
        "gpt-5.6-sol-high": _err("500"),
        "gemini-2.5-flash": _err("500"),
    })
    router = Router(adapter)
    result = router.dispatch_with_fallback(_coding_task())
    assert result.status == "error"
    assert result.error
    assert result.attempts  # 审计轨迹非空


def test_success_records_attempts():
    router = Router(ScriptedAdapter({}))
    result = router.dispatch_with_fallback(_coding_task())
    assert result.status == "completed"
    assert result.attempts[-1].outcome == "ok"
    assert str(result.attempts[-1]).startswith("ok ")
    # 验证 AttemptRecord 类型与字段
    attempt = result.attempts[-1]
    assert attempt.outcome == "ok"
    assert attempt.status == "completed"
    assert attempt.model == "claude-sonnet-5.5"
    assert attempt.provider == "anthropic"


def test_failure_and_skip_records_attempts():
    adapter = ScriptedAdapter({
        "claude-sonnet-5.5": _err("429"),
        "gpt-5.6-sol-high": _err("500"),
        "gemini-2.5-flash": _err("500"),
    })
    router = Router(adapter)
    result = router.dispatch_with_fallback(_coding_task())
    assert result.status == "error"
    assert len(result.attempts) >= 3
    first_attempt = result.attempts[0]
    assert first_attempt.outcome == "fail"
    assert first_attempt.status == "error"
    assert first_attempt.model == "claude-sonnet-5.5"
    assert first_attempt.provider == "anthropic"
    assert first_attempt.error_class == "rate_limit"
    assert "429" in (first_attempt.error or "")
    assert str(first_attempt).startswith("fail claude-sonnet-5.5 [rate_limit]")


def test_adapter_exception_handled_as_failure():
    """adapter 抛出异常时不应崩溃，应转为 error 并进入降级流程记录审计日志。"""
    class CrashingAdapter(RuntimeAdapter):
        def __init__(self):
            self.calls = []

        def spawn(self, task: str, model: str, **kwargs) -> SpawnResult:
            self.calls.append(model)
            if model == "claude-sonnet-5.5":
                raise ConnectionResetError("network dropped")
            return SpawnResult(run_id="ok-fallback", status="completed")

        def listen(self, run_id: str, timeout_ms: int = 30000) -> WorkerOutput:
            return WorkerOutput(success=True, summary="Done")

        def send(self, message: str, **kwargs) -> None:
            pass

        def list_runs(self, **kwargs) -> list:
            return []

    adapter = CrashingAdapter()
    router = Router(adapter)
    result = router.dispatch_with_fallback(_coding_task())
    assert result.status == "completed"
    assert result.model == "gpt-5.6-sol-high"
    assert adapter.calls == ["claude-sonnet-5.5", "gpt-5.6-sol-high"]
    assert len(result.attempts) >= 2
    failed_attempt = result.attempts[0]
    assert failed_attempt.outcome == "fail"
    assert failed_attempt.status == "error"
    assert failed_attempt.model == "claude-sonnet-5.5"
    assert "ConnectionResetError: network dropped" in (failed_attempt.error or "")


def test_non_terminal_status_preserved_in_attempts():
    """pending/running 等非终态 status 应在 AttemptRecord 中原样保留，且 outcome 为 incomplete。"""
    adapter = ScriptedAdapter({
        "claude-sonnet-5.5": SpawnResult(run_id="run-1", status="pending", error="still processing"),
    })
    router = Router(adapter)
    result = router.dispatch_with_fallback(_coding_task())
    assert result.status == "completed"
    first_attempt = result.attempts[0]
    assert first_attempt.outcome == "incomplete"
    assert first_attempt.status == "pending"


def test_non_terminal_status_marks_incomplete_and_continues():
    """Non-terminal status (pending/running) should be recorded as incomplete, not fail, and continue to next candidate."""
    adapter = ScriptedAdapter({
        "claude-sonnet-5.5": SpawnResult(run_id="run-pending-1", status="pending"),
    })
    router = Router(adapter)
    result = router.dispatch_with_fallback(_coding_task())
    # Should succeed with fallback candidate
    assert result.status == "completed"
    assert result.model == "gpt-5.6-sol-high"
    assert len(result.attempts) == 2
    
    # First attempt should be incomplete
    first = result.attempts[0]
    assert first.outcome == "incomplete"
    assert first.status == "pending"
    assert first.model == "claude-sonnet-5.5"
    assert "non-terminal status" in (first.reason or "")
    
    # Second should succeed
    second = result.attempts[1]
    assert second.outcome == "ok"
    assert second.model == "gpt-5.6-sol-high"


def test_all_non_terminal_preserves_last_run_id():
    """When all candidates return non-terminal status, final SpawnResult should carry the last run_id."""
    adapter = ScriptedAdapter({
        "claude-sonnet-5.5": SpawnResult(run_id="run-1", status="pending"),
        "gpt-5.6-sol-high": SpawnResult(run_id="run-2", status="running"),
        "gemini-2.5-flash": SpawnResult(run_id="run-3", status="pending"),
    })
    router = Router(adapter)
    result = router.dispatch_with_fallback(_coding_task())
    
    assert result.status == "error"
    assert result.run_id == "run-3"  # Last non-terminal run_id preserved
    assert result.error.startswith("all candidates exhausted")
    assert "non-terminal" in result.error
    assert len(result.attempts) == 3
    assert all(a.outcome == "incomplete" for a in result.attempts)


def test_all_providers_rate_limited_exhausts_with_skips():
    """End-to-end: every provider rate-limited → same-provider candidates skip,
    and the exhausted error carries compact aggregates."""
    adapter = ScriptedAdapter({
        "a1": SpawnResult(run_id="", status="error", error="HTTP 429 too many requests"),
        "a2": SpawnResult(run_id="", status="error", error="HTTP 429 too many requests"),
        "b1": SpawnResult(run_id="", status="error", error="429 rate limit"),
        "b2": SpawnResult(run_id="", status="error", error="429 rate limit"),
    })
    chain = FallbackChain(candidates=[
        ModelCandidate("a1", "p1"), ModelCandidate("a2", "p1"),
        ModelCandidate("b1", "p2"), ModelCandidate("b2", "p2"),
    ])
    router = Router(adapter, chains={TaskType.CODING: chain})
    result = router.dispatch_with_fallback(_coding_task())

    assert result.status == "error"
    assert [(a.model, a.outcome) for a in result.attempts] == [
        ("a1", "fail"), ("a2", "skip"), ("b1", "fail"), ("b2", "skip"),
    ]
    assert "attempted=2" in result.error
    assert "blacklisted=['p1', 'p2']" in result.error
    assert "error_classes={'rate_limit': 2}" in result.error


def test_incomplete_attempt_str_includes_reason():
    """AttemptRecord.__str__ surfaces status/reason for incomplete outcomes."""
    rec = AttemptRecord(model="m", provider="p", outcome="incomplete",
                        status="pending", reason="non-terminal status: pending")
    assert str(rec) == "incomplete m [pending] (non-terminal status: pending)"


def test_auth_error_blacklists_entire_provider():
    """AUTH error (401) should blacklist the entire provider, skipping subsequent candidates from same provider."""
    from agent_delegate.models.base import FallbackChain, ModelCandidate
    
    # Create chain: anthropic (2 models) + openai (1 model)
    custom_chain = FallbackChain(candidates=[
        ModelCandidate(model_id="claude-opus", provider="anthropic", speed_rank=1),
        ModelCandidate(model_id="claude-sonnet", provider="anthropic", speed_rank=2),
        ModelCandidate(model_id="gpt-4o", provider="openai", speed_rank=3),
    ])
    
    adapter = ScriptedAdapter({
        "claude-opus": _err("401 Unauthorized: invalid api key"),
    })
    router = Router(adapter, chains={TaskType.CODING: custom_chain})
    result = router.dispatch_with_fallback(_coding_task())
    
    # Should succeed with openai after blacklisting anthropic
    assert result.status == "completed"
    assert result.model == "gpt-4o"
    assert len(result.attempts) == 3
    
    # First attempt: fail with AUTH error
    assert result.attempts[0].outcome == "fail"
    assert result.attempts[0].model == "claude-opus"
    assert result.attempts[0].error_class == "auth"
    
    # Second attempt: skipped due to provider blacklist
    assert result.attempts[1].outcome == "skip"
    assert result.attempts[1].model == "claude-sonnet"
    assert "blacklisted" in (result.attempts[1].reason or "")
    
    # Third attempt: success
    assert result.attempts[2].outcome == "ok"
    assert result.attempts[2].model == "gpt-4o"
