import sys
sys.path.insert(0, ".")

import subprocess
from unittest.mock import MagicMock, patch

from src.adapters.openclaw import OpenClawAdapter, _MAX_CACHED_RUNS
from src.models.base import SpawnResult, WorkerOutput


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
