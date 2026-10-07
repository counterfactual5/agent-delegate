"""Tests for RESTAdapter wait=True terminal polling."""
import sys
sys.path.insert(0, ".")

from unittest.mock import Mock, patch
from src.adapters.rest import RESTAdapter


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
