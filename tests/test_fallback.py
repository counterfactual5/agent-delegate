"""测试按错误类型降级的 dispatch_with_fallback。"""

import pytest

from agent_delegate.router.router import Router
from agent_delegate.models.base import (
    Task, TaskType, ChainNotConfigured, SpawnResult, WorkerOutput, RuntimeAdapter,
    ErrorClass, classify_error, FallbackChain, ModelCandidate, AttemptRecord,
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


# CODING 链: gemini-pro-high(gemini), gpt-codex(openai), gpt-codex-mini(openai)
CODING_CHAINS = {TaskType.CODING: FallbackChain(candidates=[
    ModelCandidate("gemini-pro-high", "gemini", speed_rank=6),
    ModelCandidate("gpt-codex", "openai", speed_rank=5),
    ModelCandidate("gpt-codex-mini", "openai", speed_rank=3),
])}


def _coding_task():
    return Task(description="写一个完整的电商后端")


def test_rate_limit_skips_whole_provider():
    """gemini 429 → 跳过整个 gemini，落到 openai 首选。"""
    adapter = ScriptedAdapter({
        "gemini-pro-high": _err("429 rate limit"),
    })
    router = Router(adapter, chains=CODING_CHAINS)
    result = router.dispatch_with_fallback(_coding_task())
    assert result.status == "completed"
    assert result.model == "gpt-codex"
    assert adapter.calls == ["gemini-pro-high", "gpt-codex"]


def test_server_error_retries_same_model_once():
    """5xx → 同模型重试一次；第二次成功。"""
    flaky = iter([_err("503 unavailable"), SpawnResult(run_id="ok2", status="completed")])
    adapter = ScriptedAdapter({})

    def spawn(task, model, **kw):
        adapter.calls.append(model)
        if model == "gemini-pro-high":
            return next(flaky)
        return SpawnResult(run_id="ok", status="completed")

    adapter.spawn = spawn  # type: ignore
    router = Router(adapter, chains=CODING_CHAINS)
    result = router.dispatch_with_fallback(_coding_task())
    assert result.status == "completed"
    assert result.model == "gemini-pro-high"
    assert adapter.calls == ["gemini-pro-high", "gemini-pro-high"]


def test_timeout_prefers_faster_candidate():
    """超时 → 重排剩余候选，优先 speed_rank 最小者 (gpt-codex-mini, rank=3)。"""
    adapter = ScriptedAdapter({
        "gemini-pro-high": _err("request timed out"),
    })
    router = Router(adapter, chains=CODING_CHAINS)
    result = router.dispatch_with_fallback(_coding_task())
    assert result.status == "completed"
    # gpt-codex(rank5) vs gpt-codex-mini(rank3) → mini 先跑
    assert result.model == "gpt-codex-mini"
    assert adapter.calls == ["gemini-pro-high", "gpt-codex-mini"]


def test_all_fail_returns_error_with_audit():
    adapter = ScriptedAdapter({
        "gemini-pro-high": _err("429"),
        "gpt-codex": _err("500"),
        "gpt-codex-mini": _err("500"),
    })
    router = Router(adapter, chains=CODING_CHAINS)
    result = router.dispatch_with_fallback(_coding_task())
    assert result.status == "error"
    assert "所有候选模型均失败" in result.error
    assert result.attempts  # 审计轨迹非空


def test_success_records_attempts():
    router = Router(ScriptedAdapter({}), chains=CODING_CHAINS)
    result = router.dispatch_with_fallback(_coding_task())
    assert result.status == "completed"
    assert result.attempts[-1].outcome == "ok"


# ─── classify 精度（B2 嫁接） ───

def test_classify_numeric_digit_boundaries():
    """裸数字签名带数字边界：1503/4290/端口 5000 不误触发。"""
    assert classify_error(_err("request 1503 failed")) == ErrorClass.UNKNOWN
    assert classify_error(_err("error code 4290 while calling model")) == ErrorClass.UNKNOWN
    assert classify_error(_err("connection to port 5000 refused")) == ErrorClass.UNKNOWN
    assert classify_error(_err("HTTP 500 internal error")) == ErrorClass.SERVER_ERROR
    assert classify_error(_err("HTTP 429 too many requests")) == ErrorClass.RATE_LIMIT


def test_classify_context_length():
    """真实厂商上下文超长措辞命中 CONTEXT_LENGTH。"""
    for msg in [
        "this model's maximum context length is 8192 tokens",
        "prompt is too long: 200000 tokens > 190000 maximum",
        "input too long",
        "request entity too large",
        "上下文长度超限",
    ]:
        assert classify_error(_err(msg)) == ErrorClass.CONTEXT_LENGTH, msg


def test_classify_took_too_long_is_timeout():
    """裸 "too long" 不再误判为超长；"took too long" 归 TIMEOUT。"""
    assert classify_error(_err("request took too long")) == ErrorClass.TIMEOUT
    assert classify_error(_err("response latency too long")) == ErrorClass.UNKNOWN


def test_attempt_record_str():
    rec = AttemptRecord(model="m", provider="p", outcome="fail",
                        error_class="rate_limit", error="429")
    assert str(rec) == "fail m [rate_limit] 429"
    assert str(AttemptRecord(model="m", provider="p", outcome="ok")) == "ok m"


# ─── AttemptRecord 审计 + CONTEXT_LENGTH 降级（B2 嫁接） ───

def _chain(*candidates):
    return FallbackChain(candidates=list(candidates))


def test_attempts_are_structured_with_timing():
    """降级轨迹是 AttemptRecord，ok/fail 都带耗时。"""
    adapter = ScriptedAdapter({"m1": _err("429")})
    router = Router(adapter)
    result = router.dispatch_with_fallback(
        Task(description="query"), chain=_chain(
            ModelCandidate("m1", "p1"), ModelCandidate("m2", "p2")))
    assert result.status == "completed"
    assert result.attempts[0].outcome == "fail"
    assert result.attempts[0].error_class == "rate_limit"
    assert result.attempts[0].duration_ms is not None
    assert result.attempts[1].outcome == "ok"
    assert result.attempts[1].duration_ms >= 0


def test_context_length_falls_back_to_larger_window():
    """上下文超限 → 只留窗口更大的候选。"""
    adapter = ScriptedAdapter({"small": _err("prompt is too long: 200000 tokens")})
    router = Router(adapter)
    result = router.dispatch_with_fallback(
        Task(description="query"), chain=_chain(
            ModelCandidate("small", "p1", context_window=128000),
            ModelCandidate("big", "p2", context_window=1000000)))
    assert result.status == "completed"
    assert result.model == "big"
    assert adapter.calls == ["small", "big"]


def test_context_length_exhausts_when_no_larger_window():
    """同窗候选耗尽：a 上下文超限后 b 无更大窗口可用，直接耗尽。"""
    adapter = ScriptedAdapter({
        "a": _err("prompt is too long: 200000 tokens"),
        "b": _err("prompt is too long: 200000 tokens"),
    })
    router = Router(adapter)
    result = router.dispatch_with_fallback(
        Task(description="query"), chain=_chain(
            ModelCandidate("a", "p1", context_window=128000),
            ModelCandidate("b", "p2", context_window=128000)))
    assert result.status == "error"
    assert "所有候选模型均失败" in result.error
    assert "context_length" in result.error
    # a 记 fail 后，b 从未被 spawn（窗口过滤直接清空队列）
    assert adapter.calls == ["a"]


# ─── 调用方优先选模型 ───

def test_caller_task_type_is_not_reclassified():
    """调用方说是 RESEARCH，描述里的 "api" 不能把它改成 CODING。"""
    adapter = ScriptedAdapter({})
    router = Router(adapter, chains={
        TaskType.RESEARCH: FallbackChain.from_ids(["r/research-model"]),
        TaskType.CODING: FallbackChain.from_ids(["c/coding-model"]),
    })
    task = Task(description="调研一下这个库的 API 设计", task_type=TaskType.RESEARCH)
    result = router.dispatch_with_fallback(task)
    assert result.model == "r/research-model"
    assert task.task_type == TaskType.RESEARCH


def test_candidates_are_tried_in_order_with_provider_blacklist():
    adapter = ScriptedAdapter({"a/x": _err("429 rate limit")})
    router = Router(adapter)
    result = router.dispatch_with_fallback(
        Task(description="q", candidates=["a/x", "a/y", "b/z"]))
    assert result.model == "b/z"
    assert adapter.calls == ["a/x", "b/z"]
    assert [a.outcome for a in result.attempts] == ["fail", "skip", "ok"]


def test_candidates_take_precedence_over_task_type_and_override():
    adapter = ScriptedAdapter({})
    router = Router(adapter, chains={TaskType.CODING: FallbackChain.from_ids(["c/coding-model"])})
    result = router.dispatch_with_fallback(Task(
        description="q", task_type=TaskType.CODING, model_override="o/override",
        candidates=["p/picked"]))
    assert adapter.calls == ["p/picked"]


def test_model_override_needs_no_configured_chain():
    adapter = ScriptedAdapter({})
    result = Router(adapter).dispatch_with_fallback(
        Task(description="写一个完整的电商后端", model_override="b/z"))
    assert result.status == "completed"
    assert adapter.calls == ["b/z"]


def test_ids_without_provider_prefix_are_not_blacklisted_together():
    adapter = ScriptedAdapter({"x": _err("429")})
    result = Router(adapter).dispatch_with_fallback(Task(description="q", candidates=["x", "y"]))
    assert result.model == "y"
    assert adapter.calls == ["x", "y"]


def test_unconfigured_chain_raises_instead_of_falling_back():
    """没有候选、没有配置：报错并说明缺哪个类型，不悄悄换用 STANDARD 链。"""
    adapter = ScriptedAdapter({})
    router = Router(adapter, chains={TaskType.STANDARD: FallbackChain.from_ids(["s/std"])})
    with pytest.raises(ChainNotConfigured, match="coding"):
        router.dispatch_with_fallback(_coding_task())
    assert adapter.calls == []
