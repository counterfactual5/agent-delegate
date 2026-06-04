"""测试按错误类型降级的 dispatch_with_fallback。"""

import sys
sys.path.insert(0, ".")

from src.router.router import Router
from src.models.base import (
    Task, TaskType, SpawnResult, WorkerOutput, RuntimeAdapter,
    ErrorClass, classify_error,
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
def _coding_task():
    return Task(description="写一个完整的电商后端")


def test_rate_limit_skips_whole_provider():
    """gemini 429 → 跳过整个 gemini，落到 openai 首选。"""
    adapter = ScriptedAdapter({
        "gemini-pro-high": _err("429 rate limit"),
    })
    router = Router(adapter)
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
    router = Router(adapter)
    result = router.dispatch_with_fallback(_coding_task())
    assert result.status == "completed"
    assert result.model == "gemini-pro-high"
    assert adapter.calls == ["gemini-pro-high", "gemini-pro-high"]


def test_timeout_prefers_faster_candidate():
    """超时 → 重排剩余候选，优先 speed_rank 最小者 (gpt-codex-mini, rank=3)。"""
    adapter = ScriptedAdapter({
        "gemini-pro-high": _err("request timed out"),
    })
    router = Router(adapter)
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
    router = Router(adapter)
    result = router.dispatch_with_fallback(_coding_task())
    assert result.status == "error"
    assert "所有候选模型均失败" in result.error
    assert result.attempts  # 审计轨迹非空


def test_success_records_attempts():
    router = Router(ScriptedAdapter({}))
    result = router.dispatch_with_fallback(_coding_task())
    assert result.status == "completed"
    assert result.attempts[-1].startswith("ok ")
