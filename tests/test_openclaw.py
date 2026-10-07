import sys
sys.path.insert(0, ".")

import subprocess
from unittest.mock import MagicMock, patch

from agent_delegate.adapters.openclaw import OpenClawAdapter, _MAX_CACHED_RUNS
from agent_delegate.models.base import SpawnResult, WorkerOutput


def test_spawn_success_returns_summary():
    """spawn 成功时返回的 SpawnResult 应携带解析出的 summary"""
    adapter = OpenClawAdapter()
    fake = MagicMock(returncode=0, stdout="Summary line 1\nSummary line 2\nrun-123")
    with patch("agent_delegate.adapters.openclaw.subprocess.run", return_value=fake):
        result = adapter.spawn(task="t", model="m")
    assert result.status == "completed"
    assert result.run_id == "run-123"
    assert result.summary == "Summary line 1\nSummary line 2"


def test_send_timeout_does_not_raise():
    """send 是尽力通知：超时/二进制缺失只记日志，不向调用方抛异常"""
    adapter = OpenClawAdapter()
    with patch("agent_delegate.adapters.openclaw.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="openclaw", timeout=30)):
        adapter.send("hello")  # 不应抛出


def test_spawn_success_with_run_id_caches_and_listen_hits():
    adapter = OpenClawAdapter()
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = "Task finished\nrun-abc-123"
    mock_res.stderr = ""

    with patch("subprocess.run", return_value=mock_res) as mock_run:
        result = adapter.spawn(task="do something", model="claude-3-opus")

        mock_run.assert_called_once()
        assert result.status == "completed"
        assert result.run_id == "run-abc-123"

        # listen should hit cache
        worker_output = adapter.listen("run-abc-123")
        assert worker_output.success is True
        assert worker_output.summary == "Task finished"


def test_spawn_success_with_empty_output_does_not_cache():
    adapter = OpenClawAdapter()
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = ""
    mock_res.stderr = ""

    with patch("subprocess.run", return_value=mock_res):
        result = adapter.spawn(task="empty output task", model="gpt-4o")

        assert result.status == "completed"
        assert result.run_id == "unknown"
        assert "unknown" not in adapter._completed_runs
        assert len(adapter._completed_runs) == 0

        # listen for unknown should return unhit fallback
        worker_output = adapter.listen("unknown")
        assert worker_output.success is False
        assert "not found in completed cache" in worker_output.summary


def test_spawn_failure_non_zero_exit_returns_error_result():
    adapter = OpenClawAdapter()
    mock_res = MagicMock()
    mock_res.returncode = 1
    mock_res.stdout = ""
    mock_res.stderr = "CLI execution failed: invalid flag"

    with patch("subprocess.run", return_value=mock_res):
        result = adapter.spawn(task="fail task", model="gpt-4o")

        assert result.status == "error"
        assert result.run_id == ""
        assert result.error == "CLI execution failed: invalid flag"
        assert len(adapter._completed_runs) == 0


def test_completed_runs_fifo_eviction():
    adapter = OpenClawAdapter()

    # Fill cache up to capacity + 1
    for i in range(_MAX_CACHED_RUNS + 1):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = f"summary {i}\nrun-{i}"
        mock_res.stderr = ""
        with patch("subprocess.run", return_value=mock_res):
            adapter.spawn(task="t", model="m")

    assert len(adapter._completed_runs) == _MAX_CACHED_RUNS
    # The oldest entry (run-0) should have been evicted
    assert "run-0" not in adapter._completed_runs
    assert adapter.listen("run-0").success is False
    # The newest entries should still be cached
    assert "run-1" in adapter._completed_runs
    assert f"run-{_MAX_CACHED_RUNS}" in adapter._completed_runs
    assert adapter.listen(f"run-{_MAX_CACHED_RUNS}").success is True
