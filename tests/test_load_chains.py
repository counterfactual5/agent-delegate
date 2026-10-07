"""Tests for load_chains config loading."""
import sys
sys.path.insert(0, ".")

import json
import os
import tempfile

from src.models.base import load_chains, TaskType


def _write(content: str, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def test_load_json_chains():
    """JSON config loads into TaskType-keyed FallbackChain dict."""
    path = _write(json.dumps({
        "coding": [
            {"model_id": "m1", "provider": "p1", "speed_rank": 1, "cost_rank": 1},
            {"model_id": "m2", "provider": "p2", "speed_rank": 2, "cost_rank": 2},
        ],
        "trivial": [
            {"model_id": "m3", "provider": "p3"},
        ],
    }), ".json")
    try:
        chains = load_chains(path)
        assert TaskType.CODING in chains
        assert TaskType.TRIVIAL in chains
        assert len(chains[TaskType.CODING].candidates) == 2
        assert chains[TaskType.CODING].candidates[0].model_id == "m1"
        assert chains[TaskType.CODING].candidates[1].provider == "p2"
        # 未提供的字段走 dataclass 默认值
        assert chains[TaskType.TRIVIAL].candidates[0].speed_rank == 5
    finally:
        os.unlink(path)


def test_load_chains_usable_by_router():
    """Loaded chains plug straight into Router."""
    from src.router.router import Router
    from src.models.base import Task, SpawnResult
    from unittest.mock import Mock

    path = _write(json.dumps({
        "coding": [{"model_id": "only-model", "provider": "p"}],
    }), ".json")
    try:
        adapter = Mock()
        adapter.spawn.return_value = SpawnResult(run_id="r", status="completed")
        router = Router(adapter=adapter, chains=load_chains(path))
        router.dispatch_with_fallback(Task(description="x", task_type=TaskType.CODING))
        assert adapter.spawn.call_args.kwargs["model"] == "only-model"
    finally:
        os.unlink(path)


def test_load_chains_missing_file():
    """Missing file surfaces a clear error, not a silent empty dict."""
    try:
        load_chains("/nonexistent/path/chains.json")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")


def test_load_chains_unknown_task_type():
    """Unknown task type key is rejected rather than silently dropped."""
    path = _write(json.dumps({"not_a_task_type": [{"model_id": "m", "provider": "p"}]}), ".json")
    try:
        try:
            load_chains(path)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for unknown task type")
    finally:
        os.unlink(path)


def test_load_yaml_chains():
    """YAML config path (skipped if pyyaml not installed)."""
    try:
        import yaml  # noqa: F401
    except ImportError:
        import pytest
        pytest.skip("pyyaml not installed")

    path = _write("""
coding:
  - model_id: yaml-model
    provider: p1
    speed_rank: 2
    cost_rank: 3
""", ".yaml")
    try:
        chains = load_chains(path)
        assert chains[TaskType.CODING].candidates[0].model_id == "yaml-model"
        assert chains[TaskType.CODING].candidates[0].cost_rank == 3
    finally:
        os.unlink(path)
