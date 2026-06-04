"""测试 Router 决策逻辑"""

import sys
sys.path.insert(0, ".")

from src.router.router import Router
from src.models.base import (
    Task, TaskType, ContextDependency,
    SpawnResult, RuntimeAdapter,
)


class MockAdapter(RuntimeAdapter):
    """测试用 mock adapter"""
    def spawn(self, task: str, model: str, **kwargs) -> SpawnResult:
        return SpawnResult(run_id="test-123", status="completed")
    def listen(self, run_id: str, timeout_ms: int = 30000):
        from src.models.base import WorkerOutput
        return WorkerOutput(success=True, summary="Done")
    def send(self, message: str, **kwargs) -> None:
        pass
    def list_runs(self, **kwargs) -> list:
        return []


def test_context_analysis():
    router = Router(MockAdapter())
    
    # 强依赖
    assert router.analyze_context(Task(description="把这个改成异步的")) == ContextDependency.STRONG
    assert router.analyze_context(Task(description="继续刚才的工作")) == ContextDependency.STRONG
    assert router.analyze_context(Task(description="change this to async")) == ContextDependency.STRONG
    
    # 弱依赖
    assert router.analyze_context(Task(description="写一个爬虫")) == ContextDependency.WEAK
    assert router.analyze_context(Task(description="帮我搜索天气")) == ContextDependency.WEAK


def test_task_classification():
    router = Router(MockAdapter())
    
    assert router.classify_task(Task(description="写一个完整的电商后端")) == TaskType.CODING
    assert router.classify_task(Task(description="做竞品调研")) == TaskType.RESEARCH
    assert router.classify_task(Task(description="审计合约安全性")) == TaskType.AUDIT
    assert router.classify_task(Task(description="写个简单脚本")) == TaskType.LIGHT_CODING
    assert router.classify_task(Task(description="帮我翻译这段话")) == TaskType.TRIVIAL
    assert router.classify_task(Task(description="生成白皮书")) == TaskType.DOC
    assert router.classify_task(Task(description="搜索Python教程")) == TaskType.STANDARD


def test_dispatch_strong_context():
    """强上下文依赖不应外包"""
    router = Router(MockAdapter())
    result = router.dispatch("把这个函数改成异步的")
    assert isinstance(result, str)
    assert "主 Agent" in result


def test_dispatch_weak_context():
    """弱上下文依赖应该外包"""
    router = Router(MockAdapter())
    result = router.dispatch("写一个爬虫")
    assert isinstance(result, SpawnResult)
    assert result.run_id == "test-123"


def test_context_packing():
    packed = Router.pack_context(
        context="用户在做区块链项目",
        task_desc="写一个 ERC20 代币合约",
        constraints=["产出保存在 /tmp/", "3 句话总结"],
    )
    assert "<context>" in packed
    assert "<task>" in packed
    assert "<constraints>" in packed
    assert "区块链" in packed
    assert "ERC20" in packed


if __name__ == "__main__":
    test_context_analysis()
    print("✅ test_context_analysis passed")
    
    test_task_classification()
    print("✅ test_task_classification passed")
    
    test_dispatch_strong_context()
    print("✅ test_dispatch_strong_context passed")
    
    test_dispatch_weak_context()
    print("✅ test_dispatch_weak_context passed")
    
    test_context_packing()
    print("✅ test_context_packing passed")
    
    print("\n🎉 All tests passed!")
