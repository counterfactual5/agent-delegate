"""测试 OpenClawAdapter 的 spawn → listen 契约（不调用真实 CLI）。"""

import subprocess

from agent_delegate.adapters.openclaw import OpenClawAdapter


def _fake_run(returncode=0, stdout="", stderr="", raises=None):
    def run(cmd, **kwargs):
        if raises:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)
    return run


def test_listen_returns_stored_summary(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="wrote PLAN.md\nall good\nrun-42\n"))
    adapter = OpenClawAdapter()
    spawned = adapter.spawn("task", "m")
    assert spawned.status == "completed" and spawned.run_id == "run-42"
    out = adapter.listen("run-42")
    assert out.success and out.summary == "wrote PLAN.md\nall good"


def test_listen_consumes_result_once(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="run-1\n"))
    adapter = OpenClawAdapter()
    adapter.spawn("task", "m")
    assert adapter.listen("run-1").summary == "Run run-1 completed"
    assert not adapter.listen("run-1").success


def test_empty_stdout_gets_unique_run_ids(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout=""))
    adapter = OpenClawAdapter()
    a, b = adapter.spawn("t", "m"), adapter.spawn("t", "m")
    assert a.run_id != b.run_id
    assert adapter.listen(a.run_id).success and adapter.listen(b.run_id).success


def test_nonzero_exit_is_spawn_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(returncode=1, stderr="429 rate limited"))
    spawned = OpenClawAdapter().spawn("t", "m")
    assert spawned.status == "error" and "429" in spawned.error


def test_timeout_is_spawn_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(raises=subprocess.TimeoutExpired("openclaw", 1)))
    assert OpenClawAdapter().spawn("t", "m").error == "Timeout"


def test_listen_unknown_run_fails():
    assert not OpenClawAdapter().listen("nope").success
