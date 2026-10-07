"""测试 Phase 2 增强功能：上下文长度错误处理"""

import sys
sys.path.insert(0, ".")

from unittest.mock import Mock
from src.router.router import Router
from src.models.base import (
    Task, TaskType, SpawnResult, 
    FallbackChain, ModelCandidate,
    classify_error, ErrorClass,
)


def test_context_length_error_classification():
    """测试上下文长度错误能正确识别"""
    test_cases = [
        ("context length exceeded", ErrorClass.CONTEXT_LENGTH),
        ("maximum context window is 128000", ErrorClass.CONTEXT_LENGTH),
        ("token limit exceeded", ErrorClass.CONTEXT_LENGTH),
        ("上下文长度超限", ErrorClass.CONTEXT_LENGTH),
        ("context_length error", ErrorClass.CONTEXT_LENGTH),
        ("input too long", ErrorClass.CONTEXT_LENGTH),
        ("prompt exceeds context window", ErrorClass.CONTEXT_LENGTH),
    ]
    
    for error_text, expected_class in test_cases:
        result = SpawnResult(
            run_id='test',
            status='error',
            error=error_text
        )
        actual_class = classify_error(result)
        assert actual_class == expected_class, \
            f"'{error_text}' should classify as {expected_class}, got {actual_class}"
    
    print(f"✓ All {len(test_cases)} context length error classifications passed")


def test_context_length_fallback():
    """测试上下文长度超限时自动降级到更大窗口的模型"""
    adapter = Mock()
    
    # 自定义链：小窗口模型 -> 中窗口模型 -> 大窗口模型
    custom_chains = {
        TaskType.CODING: FallbackChain(candidates=[
            ModelCandidate("small-model", "provider-a", context_window=32000),
            ModelCandidate("medium-model", "provider-b", context_window=128000),
            ModelCandidate("large-model", "provider-c", context_window=1000000),
        ])
    }
    
    router = Router(adapter=adapter, chains=custom_chains)
    
    # 模拟第一次调用：小窗口模型返回上下文长度错误
    # 第二次调用：应该优先尝试大窗口模型（large-model）而不是中窗口
    call_count = [0]
    def spawn_side_effect(*args, **kwargs):
        call_count[0] += 1
        model = kwargs.get('model')
        
        if model == "small-model":
            return SpawnResult(
                status='error',
                run_id=f'run-{call_count[0]}',
                error='context length exceeded: maximum 32000 tokens',
                model=model
            )
        elif model == "large-model":
            # 大窗口模型成功
            return SpawnResult(
                status='completed',
                run_id=f'run-{call_count[0]}',
                error=None,
                model=model
            )
        else:
            # 不应该调用到这里
            return SpawnResult(
                status='error',
                run_id=f'run-{call_count[0]}',
                error='unexpected call',
                model=model
            )
    
    adapter.spawn.side_effect = spawn_side_effect
    
    task = Task(
        description='process large codebase',
        task_type=TaskType.CODING,
    )
    
    result = router.dispatch_with_fallback(task)
    
    # 验证：第一次失败后，应该直接跳到最大窗口的模型
    assert result.status == 'completed'
    assert result.model == 'large-model'
    assert call_count[0] == 2  # small-model 失败 + large-model 成功
    
    # 检查 attempts 日志
    assert result.attempts[0].error_class == 'context_length'
    assert result.attempts[1].model == 'large-model'
    
    print("✓ Context length fallback test passed")


def test_all_error_classifications():
    """测试所有错误类型的分类"""
    test_cases = [
        ("context length exceeded", ErrorClass.CONTEXT_LENGTH),
        ("maximum context window is 128000", ErrorClass.CONTEXT_LENGTH),
        ("token limit exceeded", ErrorClass.CONTEXT_LENGTH),
        ("上下文长度超限", ErrorClass.CONTEXT_LENGTH),
        ("rate limit exceeded", ErrorClass.RATE_LIMIT),
        ("429 too many requests", ErrorClass.RATE_LIMIT),
        ("401 unauthorized", ErrorClass.AUTH),
        ("invalid api key", ErrorClass.AUTH),
        ("500 internal server error", ErrorClass.SERVER_ERROR),
        ("503 service unavailable", ErrorClass.SERVER_ERROR),
        ("timeout after 30s", ErrorClass.TIMEOUT),
        ("request timed out", ErrorClass.TIMEOUT),
        ("something weird happened", ErrorClass.UNKNOWN),
    ]
    
    for error_text, expected_class in test_cases:
        result = SpawnResult(
            run_id='test',
            status='error',
            error=error_text
        )
        actual_class = classify_error(result)
        assert actual_class == expected_class, \
            f"'{error_text}' should classify as {expected_class}, got {actual_class}"
    
    print(f"✓ All {len(test_cases)} error classifications passed")


def test_exceeds_classification_tightened():
    """验证移除裸 'exceeds' 后的负向用例，避免配额类或其它非上下文错误被误判为 CONTEXT_LENGTH。"""
    # 裸 "exceeds" 不再匹配 CONTEXT_LENGTH
    res_generic = SpawnResult(run_id='t1', status='error', error="exceeds limit")
    assert classify_error(res_generic) == ErrorClass.UNKNOWN
    assert classify_error(res_generic) != ErrorClass.CONTEXT_LENGTH

    # "quota exceeds limit" 包含 quota 关键字，归为 RATE_LIMIT，且绝非 CONTEXT_LENGTH
    res_quota = SpawnResult(run_id='t2', status='error', error="quota exceeds limit")
    assert classify_error(res_quota) == ErrorClass.RATE_LIMIT
    assert classify_error(res_quota) != ErrorClass.CONTEXT_LENGTH


if __name__ == "__main__":
    test_context_length_error_classification()
    test_context_length_fallback()
    test_all_error_classifications()
    test_exceeds_classification_tightened()
    print("\n✅ All Phase 2 tests passed!")
