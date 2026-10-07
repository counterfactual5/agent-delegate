"""Tests for RESTAdapter wait=True terminal polling."""
import sys
sys.path.insert(0, ".")

import http.client
from unittest.mock import Mock, patch
import pytest
import urllib.error
from agent_delegate.adapters.rest import RESTAdapter


def _adapter():
    return RESTAdapter(config={
        "base_url": "http://test",
        "spawn_endpoint": "/spawn",
        "listen_endpoint": "/listen/{run_id}",
        "send_endpoint": "/send",
    })


class TestWaitTruePolling:
    def test_pending_polled_to_completed(self):
        """wait=True: pending status should be polled until terminal."""
        a = _adapter()
        a._request = Mock(side_effect=[
            {"run_id": "r1", "status": "pending"},       # POST /spawn
            {"status": "running"},                        # poll 1
            {"status": "completed", "summary": "done"},   # poll 2
        ])
        with patch("time.sleep"):
            result = a.spawn(task="t", model="m", timeout_seconds=5)
        assert result.status == "completed"
        assert result.run_id == "r1"

    def test_pending_polled_to_error(self):
        """wait=True: polling to error returns error SpawnResult."""
        a = _adapter()
        a._request = Mock(side_effect=[
            {"run_id": "r1", "status": "pending"},
            {"status": "error", "error": "model refused"},
        ])
        with patch("time.sleep"):
            result = a.spawn(task="t", model="m", timeout_seconds=5)
        assert result.status == "error"
        assert "model refused" in result.error

    def test_wait_false_returns_pending_immediately(self):
        """wait=False: return current status without polling."""
        a = _adapter()
        a._request = Mock(return_value={"run_id": "r1", "status": "pending"})
        result = a.spawn(task="t", model="m", wait=False)
        assert result.status == "pending"
        assert result.run_id == "r1"
        assert a._request.call_count == 1  # no polling

    def test_summary_artifacts_passthrough(self):
        """wait=True: WorkerOutput fields transparently passed to SpawnResult."""
        a = _adapter()
        a._request = Mock(side_effect=[
            {"run_id": "r1", "status": "pending"},
            {"status": "completed", "summary": "wrote PLAN.md",
             "artifacts": ["PLAN.md"], "output_path": "PLAN.md"},
        ])
        with patch("time.sleep"):
            result = a.spawn(task="t", model="m", timeout_seconds=5)
        assert result.status == "completed"
        assert result.summary == "wrote PLAN.md"
        assert result.artifacts == ["PLAN.md"]
        assert result.output_path == "PLAN.md"

    def test_immediate_terminal_no_polling(self):
        """Already terminal status returns immediately even with wait=True."""
        a = _adapter()
        a._request = Mock(return_value={"run_id": "r1", "status": "completed"})
        result = a.spawn(task="t", model="m")
        assert result.status == "completed"
        assert a._request.call_count == 1  # no polling needed

    def test_polling_timeout_preserves_run_id(self):
        """When polling times out, SpawnResult should preserve the run_id."""
        a = _adapter()
        a._request = Mock(return_value={"run_id": "r-timeout", "status": "pending"})
        
        # Mock listen to return failure (timeout)
        from agent_delegate.models.base import WorkerOutput
        a.listen = Mock(return_value=WorkerOutput(success=False, summary="polling timeout after 5000ms"))
        
        result = a.spawn(task="t", model="m", wait=True, timeout_seconds=5)
        
        assert result.status == "error"
        assert result.run_id == "r-timeout"  # run_id preserved
        assert "timeout" in result.error.lower()
        a.listen.assert_called_once()

    def test_urlopen_network_error_returns_error_payload(self):
        """urlopen 网络级异常应转为 error 字典，spawn 返回 status=error"""
        a = _adapter()
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("conn refused")):
            resp = a._request("GET", "/listen/r1")
            assert "error" in resp
            result = a.spawn(task="t", model="m")
        assert result.status == "error"

    def test_programming_error_propagates(self):
        """编程类异常（TypeError）不再被 _request 吞掉，应向外抛出"""
        a = _adapter()
        with patch("urllib.request.urlopen", side_effect=TypeError("bad type")):
            with pytest.raises(TypeError):
                a._request("GET", "/listen/r1")

    def test_http_client_exception_returns_error_payload(self):
        """http.client.HTTPException (BadStatusLine) should be caught and return error dict."""
        a = _adapter()
        with patch("urllib.request.urlopen",
                   side_effect=http.client.BadStatusLine("garbage")):
            resp = a._request("GET", "/listen/r1")
            assert "error" in resp
            result = a.spawn(task="t", model="m")
        assert result.status == "error"

    def test_spawn_timeout_passthrough(self):
        """spawn() should pass timeout_seconds to _request as timeout parameter."""
        a = _adapter()
        mock_resp = {"run_id": "r1", "status": "completed"}
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = Mock(return_value=Mock(read=lambda: b'{"run_id": "r1", "status": "completed"}'))
            mock_urlopen.return_value.__exit__ = Mock(return_value=False)
            result = a.spawn(task="t", model="m", timeout_seconds=5)
        
        # Verify urlopen was called with timeout=5
        assert mock_urlopen.called
        call_kwargs = mock_urlopen.call_args
        assert call_kwargs[1]["timeout"] == 5
