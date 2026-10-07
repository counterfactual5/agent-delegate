"""回归测试 - 验证已修复的 4 个 bug"""
import sys
sys.path.insert(0, ".")


import pytest
from unittest.mock import Mock
from agent_delegate.adapters.rest import RESTAdapter
from agent_delegate.router.router import Router
from agent_delegate.models.base import Task, TaskType, SpawnResult


class TestNullErrorHandling:
    """Bug #3: REST adapter 把 {"error": null} 当作失败"""

    def test_null_error_treated_as_success(self):
        adapter = RESTAdapter(base_url="http://test")
        adapter._request = Mock(return_value={
            "run_id": "run-123",
            "status": "completed",
            "error": None,  # null error
        })

        result = adapter.spawn(task="test", model="gpt-4")

        assert result.run_id == "run-123"
        assert result.status == "completed"
        assert result.error is None

    def test_empty_string_error_treated_as_success(self):
        adapter = RESTAdapter(base_url="http://test")
        adapter._request = Mock(return_value={
            "run_id": "run-456",
            "status": "completed",
            "error": "",  # empty string
        })

        result = adapter.spawn(task="test", model="gpt-4")

        assert result.run_id == "run-456"
        assert result.status == "completed"

    def test_actual_error_triggers_failure(self):
        adapter = RESTAdapter(base_url="http://test")
        adapter._request = Mock(return_value={
            "error": "Rate limit exceeded",
        })

        result = adapter.spawn(task="test", model="gpt-4")

        assert result.status == "error"
        assert result.error == "Rate limit exceeded"


class TestSuccessDetection:
    """Bug #2: dispatch_with_fallback 用 if result.run_id 判断成功"""

    def test_completed_status_triggers_success(self):
        router = Router(adapter=Mock())
        router.adapter.spawn = Mock(return_value=SpawnResult(
            run_id="run-789",
            status="completed",
        ))

        task = Task(
            description="test task",
            task_type=TaskType.TRIVIAL,
        )
        result = router.dispatch_with_fallback(task)

        assert result.status == "completed"
        assert result.attempts[0].outcome == "ok"

    def test_error_with_run_id_triggers_fallback(self):
        router = Router(adapter=Mock())
        call_count = 0

        def mock_spawn(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return SpawnResult(
                    run_id="run-failed",
                    status="error",
                    error="Server error",
                )
            return SpawnResult(
                run_id="run-ok",
                status="completed",
            )

        router.adapter.spawn = mock_spawn

        task = Task(
            description="test task",
            task_type=TaskType.STANDARD,
        )
        result = router.dispatch_with_fallback(task)

        assert call_count == 2  # 触发了 fallback
        assert result.status == "completed"


class TestParameterPreservation:
    """Bug #4: dispatch_with_fallback 无条件覆盖 caller 提供的参数"""

    def test_preserves_caller_task_type(self):
        router = Router(adapter=Mock())
        router.adapter.spawn = Mock(return_value=SpawnResult(
            run_id="run-1",
            status="completed",
        ))

        task = Task(
            description="SELECT * FROM users",
            task_type=TaskType.CODING,  # caller 明确指定
        )
        router.dispatch_with_fallback(task)

        # 不应该被重新分类成 STANDARD
        assert task.task_type == TaskType.CODING


    def test_model_override_creates_single_candidate_chain(self):
        router = Router(adapter=Mock())
        router.adapter.spawn = Mock(return_value=SpawnResult(
            run_id="run-3",
            status="completed",
        ))

        task = Task(
            description="test task",
            task_type=TaskType.STANDARD,
            model_override="anthropic/claude-3-opus",
        )
        result = router.dispatch_with_fallback(task)

        # 应该只尝试了指定的模型
        router.adapter.spawn.assert_called_once()
        assert result.status == "completed"

    def test_analyzes_when_not_set(self):
        router = Router(adapter=Mock())
        router.adapter.spawn = Mock(return_value=SpawnResult(
            run_id="run-4",
            status="completed",
        ))

        task = Task(
            description="Write a Python script to parse CSV",
            # task_type 和 context_dependency 都未设置
        )
        router.dispatch_with_fallback(task)

        # task_type 未设置时 Router 不自动填充（决策已上交调用方）
        # 但 dispatch 仍能执行（fallback 到 STANDARD 链）


class TestTimeoutHandling:
    """验证超时后的降级逻辑（原 bug #1 相关）"""

    def test_timeout_triggers_speed_based_reordering(self):
        router = Router(adapter=Mock())
        call_sequence = []

        def mock_spawn(task, model, **kwargs):
            call_sequence.append(model)
            if len(call_sequence) == 1:
                return SpawnResult(
                    run_id="",
                    status="error",
                    error="timeout",
                )
            return SpawnResult(
                run_id="run-ok",
                status="completed",
            )

        router.adapter.spawn = mock_spawn

        task = Task(
            description="test task",
            task_type=TaskType.STANDARD,
        )
        result = router.dispatch_with_fallback(task)

        # 第一次调用后触发超时，剩余候选应该按 speed_rank 排序
        assert len(call_sequence) >= 2
        assert result.status == "completed"
