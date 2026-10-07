"""测试 RESTAdapter.listen() 的错误处理与轮询逻辑。

不发起任何真实网络请求：ScriptedRESTAdapter 覆盖 _request()，按调用顺序
返回预设响应；poll_interval 设为 0，测试不会真的 sleep。
"""

import inspect
import time
from agent_delegate.adapters.rest import RESTAdapter
from agent_delegate.models.base import SpawnResult, WorkerOutput


class ScriptedRESTAdapter(RESTAdapter):
    """按调用顺序返回预设响应；脚本用完后重复最后一条。记录调用顺序。"""

    def __init__(self, responses, poll_interval=0, **config):
        super().__init__({"base_url": "http://scripted.invalid", **config},
                         poll_interval=poll_interval)
        self.responses = list(responses)
        self.calls: list[str] = []

    def _request(self, method: str, path: str, data: dict = None, **kwargs) -> dict:
        self.calls.append(f"{method} {path}")
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def _adapter(responses, poll_interval=0) -> ScriptedRESTAdapter:
    return ScriptedRESTAdapter(responses, poll_interval=poll_interval)


# ─── 分支 1：_request 的失败形态（含 "error" 键）──

def test_listen_request_error_fails_fast():
    """传输/HTTP 错误立刻失败，summary 带原始错误文本，而不是拖到 deadline。"""
    adapter = _adapter([{"error": "HTTP 503: backend unavailable"}])
    started = time.monotonic()
    out = adapter.listen("run-1", timeout_ms=1000)
    elapsed = time.monotonic() - started

    assert isinstance(out, WorkerOutput)
    assert out.success is False
    assert out.summary == "HTTP 503: backend unavailable"
    assert len(adapter.calls) == 1
    assert elapsed < 0.5  # 远没有耗到 1s deadline


def test_listen_request_error_with_empty_text():
    """空错误文本且没有 status：不算传输错误，但形状不对，仍然立即失败。"""
    adapter = _adapter([{"error": ""}])
    out = adapter.listen("run-1", timeout_ms=1000)
    assert out.success is False
    assert "Unexpected listen status" in out.summary
    assert len(adapter.calls) == 1


# ─── 分支 2：status == "completed" ──

def test_listen_completed_maps_payload():
    adapter = _adapter([{
        "status": "completed",
        "summary": "写了 3 个文件",
        "output_path": "/tmp/out",
        "artifacts": ["a.py", "b.py"],
        "issues": ["缺测试"],
    }])
    out = adapter.listen("run-1", timeout_ms=1000)

    assert out.success is True
    assert out.summary == "写了 3 个文件"
    assert out.output_path == "/tmp/out"
    assert out.artifacts == ["a.py", "b.py"]
    assert out.issues == ["缺测试"]
    assert len(adapter.calls) == 1


def test_listen_completed_without_summary_uses_default():
    """resp 里没有 summary 时给一个合理默认值，而不是空字符串。"""
    adapter = _adapter([{"status": "completed"}])
    out = adapter.listen("run-7", timeout_ms=1000)

    assert out.success is True
    assert out.summary == "Run run-7 completed"
    assert out.output_path is None
    assert out.artifacts == []
    assert out.issues == []


def test_listen_completed_tolerates_null_collections():
    adapter = _adapter([{"status": "completed", "artifacts": None, "issues": None}])
    out = adapter.listen("run-1", timeout_ms=1000)
    assert out.artifacts == [] and out.issues == []


# ─── 分支 3：status == "error" ──

def test_listen_remote_error_status():
    adapter = _adapter([{"status": "error", "error": "模型配额耗尽", "issues": ["quota"]}])
    out = adapter.listen("run-1", timeout_ms=1000)

    assert out.success is False
    assert out.summary == "模型配额耗尽"
    assert out.issues == ["quota"]  # 走 status 分支才会保留 issues
    assert len(adapter.calls) == 1


def test_listen_error_status_without_message():
    adapter = _adapter([{"status": "error"}])
    out = adapter.listen("run-1", timeout_ms=1000)
    assert out.success is False
    assert out.summary == "Unknown error"


# ─── 分支 4：进行中状态 → 继续轮询 ──

def test_listen_in_progress_statuses_keep_polling():
    """这些状态说明远端明确说「还在跑」，应继续轮询而不是判定为错误。"""
    for status in ("pending", "queued", "running", "in_progress"):
        adapter = _adapter([{"status": status}, {"status": "completed", "summary": "done"}])
        out = adapter.listen("run-1", timeout_ms=1000)

        assert out.success is True, status
        assert out.summary == "done", status
        assert len(adapter.calls) == 2, status


def test_listen_pending_then_completed():
    """轮询期间状态从 pending 变为 completed 能正常成功返回。"""
    adapter = _adapter([
        {"status": "pending"},
        {"status": "running"},
        {"status": "completed", "summary": "全部通过", "artifacts": ["report.md"]},
    ])
    out = adapter.listen("run-7", timeout_ms=1000)

    assert out.success is True
    assert out.summary == "全部通过"
    assert out.artifacts == ["report.md"]
    assert adapter.calls == ["GET /agents/run-7/status"] * 3


# ─── 分支 5：deadline 到期仍未完成 ──

def test_listen_timeout_returns_failure():
    adapter = _adapter([{"status": "running"}], poll_interval=0.01)
    started = time.monotonic()
    out = adapter.listen("run-1", timeout_ms=60)
    elapsed = time.monotonic() - started

    assert out.success is False
    assert out.summary == "Timeout waiting for agent"
    assert len(adapter.calls) >= 2  # 确实轮询过，不是第一次就放弃
    assert elapsed < 1.0


def test_listen_deadline_does_not_wait_extra_interval():
    """到达 deadline 的那一轮不再先睡满一整个 poll_interval。"""
    adapter = _adapter([{"status": "pending"}], poll_interval=5.0)
    started = time.monotonic()
    out = adapter.listen("run-1", timeout_ms=40)
    elapsed = time.monotonic() - started

    assert out.success is False
    assert out.summary == "Timeout waiting for agent"
    assert len(adapter.calls) == 2  # 首次 + deadline 时的最后一次
    assert elapsed < 1.0  # 旧实现会先睡满 5s 才退出


def test_listen_completes_on_final_poll_at_deadline():
    """远端在最后一个睡眠间隔内完成，不应被判超时。"""
    adapter = _adapter([{"status": "pending"}, {"status": "completed", "summary": "done"}],
                       poll_interval=5.0)
    out = adapter.listen("run-1", timeout_ms=40)
    assert out.success is True
    assert out.summary == "done"


def test_listen_zero_timeout_polls_once():
    adapter = _adapter([{"status": "completed", "summary": "done"}])
    assert adapter.listen("run-1", timeout_ms=0).success is True
    assert len(adapter.calls) == 1


def test_listen_completed_with_null_error_is_success():
    """远端常见写法 {"status": "completed", "error": null} 是成功。"""
    adapter = _adapter([{"status": "completed", "summary": "ok", "error": None}])
    out = adapter.listen("run-1", timeout_ms=1000)
    assert out.success is True
    assert out.summary == "ok"


def test_spawn_with_null_error_is_not_failure():
    adapter = _adapter([{"run_id": "r1", "status": "pending", "error": None}])
    result = adapter.spawn("task", "model")
    assert result.status == "pending"
    assert result.run_id == "r1"


def test_spawn_remote_error_status_keeps_message():
    adapter = _adapter([{"status": "error", "error": "bad model"}])
    result = adapter.spawn("task", "model")
    assert result.status == "error"
    assert result.error == "bad model"


# ─── 响应形状不符合预期 → 立即失败，不当作「还在跑」──

def test_listen_unknown_status_fails_immediately():
    adapter = _adapter([{"status": "half-done"}])
    out = adapter.listen("run-1", timeout_ms=1000)

    assert out.success is False
    assert "half-done" in out.summary
    assert len(adapter.calls) == 1


def test_listen_missing_status_fails_immediately():
    adapter = _adapter([{"run_id": "run-1", "progress": 0.3}])
    out = adapter.listen("run-1", timeout_ms=1000)

    assert out.success is False
    assert "Unexpected listen status" in out.summary
    assert len(adapter.calls) == 1


def test_listen_non_dict_response_fails_immediately():
    adapter = _adapter([["not", "a", "status"]])
    out = adapter.listen("run-1", timeout_ms=1000)

    assert out.success is False
    assert "Unexpected listen response" in out.summary
    assert len(adapter.calls) == 1


# ─── 对外契约与配置 ──

def test_listen_public_signature_unchanged():
    """router.py 及未来执行器按 (run_id, timeout_ms=30000) 调用，签名不能动。"""
    params = inspect.signature(RESTAdapter.listen).parameters
    assert list(params) == ["self", "run_id", "timeout_ms"]
    assert params["timeout_ms"].default == 30000


def test_poll_interval_default_and_overrides():
    assert RESTAdapter({"base_url": "http://x"}).poll_interval == 2.0
    assert RESTAdapter({"base_url": "http://x", "poll_interval": 0.25}).poll_interval == 0.25
    # 显式参数优先于 config
    assert RESTAdapter({"base_url": "http://x", "poll_interval": 9}, poll_interval=0).poll_interval == 0.0


# ─── spawn() 与 listen() 共用同一套错误响应判定 ──

def test_spawn_request_error_maps_to_error_status():
    adapter = _adapter([{"error": "HTTP 429: rate limit"}])
    result = adapter.spawn("do it", "gemini-pro")

    assert isinstance(result, SpawnResult)
    assert result.status == "error"
    assert result.run_id == ""
    assert "429" in result.error


def test_spawn_success_maps_run_id():
    adapter = _adapter([{"run_id": "run-42", "status": "pending"}])
    result = adapter.spawn("do it", "gemini-pro")

    assert result.status == "pending"
    assert result.run_id == "run-42"
    assert result.error is None

# ─── _request 异常边界与超时透传（B2 嫁接） ───

def test_http_client_exception_returns_error_payload():
    """http.client.HTTPException（BadStatusLine）转为 error 字典，spawn 返回 error。"""
    import http.client
    from unittest.mock import patch
    a = RESTAdapter(config={"base_url": "http://t"})
    with patch("urllib.request.urlopen",
               side_effect=http.client.BadStatusLine("garbage")):
        resp = a._request("GET", "/x")
        assert "error" in resp
        result = a.spawn(task="t", model="m")
    assert result.status == "error"


def test_programming_error_propagates():
    """编程错误（TypeError）不被 _request 吞掉。"""
    from unittest.mock import patch
    import pytest
    a = RESTAdapter(config={"base_url": "http://t"})
    with patch("urllib.request.urlopen", side_effect=TypeError("boom")):
        with pytest.raises(TypeError):
            a._request("GET", "/x")


def test_spawn_timeout_passthrough():
    """spawn 的 timeout_seconds 透传为单次 HTTP 请求的 timeout。"""
    from unittest.mock import Mock, patch
    a = RESTAdapter(config={"base_url": "http://t"})
    with patch("urllib.request.urlopen") as m:
        m.return_value.__enter__ = Mock(return_value=Mock(read=lambda: b'{"run_id":"r","status":"pending"}'))
        m.return_value.__exit__ = Mock(return_value=False)
        a.spawn(task="t", model="m", timeout_seconds=5)
        assert m.call_args[1]["timeout"] == 5
