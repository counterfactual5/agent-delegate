# Agent Delegate

> Production-grade multi-agent orchestration with intelligent task routing, model fallback chains, and pipeline-based workers.

**这不是一个框架。这是打过的仗。**

Agent Delegate 从一个 7×24 在线运行的 AI 助手系统中提炼而来。它不提供"你可以这样用"的示例 API，而是提供**已经在线上跑通的调度策略和流水线模式**。

## 核心特性

### 🧠 智能调度（Router）
- **决策矩阵**：基于上下文依赖性自动判断任务归属（主 Agent 处理 vs 子 Agent 外包）
- **任务拆解**：自动识别并行/串行依赖，独立任务并行 spawn，串行任务合并打包
- **上下文打包协议**：XML 标签隔离 context/task/constraints，防止 prompt injection

### 🔗 模型候选链（Fallback Chains）
- 6 档任务分级：极简杂活 → 标准任务 → 复杂编码 → 深度调研 → 轻量编码 → 专项审计
- 每档 2-4 个候选模型，按错误类型自动降级（429 切 provider，500 重试后切换，timeout 降级快速模型）
- Provider 级隔离：Gemini quota 耗尽不影响 GPT，反之亦然

### 🏭 流水线 Worker

| Worker | 阶段 | 说明 |
|--------|------|------|
| **Coding Agent** | Planner → Builder → Reviewer → Consultant | 4 阶段编码流水线，含代码审查和咨询 |
| **Research Agent** | Searcher → Synthesizer → Fact-Checker → Reporter | 4 阶段调研流水线，含交叉验证 |
| **Doc Agent** | Scanner → Planner → Expander → Editor → Render | 9 阶段文档流水线，产出白皮书+PPT |
| **QA Agent** | 全局扫描 → 高危排雷 → 测试执行 | 安全审计与测试 |
| **Deploy Agent** | 环境校验 → 脚本执行 → 资产盘点 | 自动化部署 |

### 🛡️ 生产级保障
- 超时控制与资源清理
- 工作区隔离（子 Agent 严禁写根目录）
- 审计日志（每次 spawn 都记录到 memory）
- 反幻觉校验（抽查产出文件是否真实存在）

## 快速开始

### 安装

```bash
pip install agent-delegate
```

### 基本使用

```python
from agent_delegate import Router, WorkerConfig

# 配置你的 LLM runtime
router = Router(
    adapter="openclaw",  # 或 "langchain", "openai", "custom"
    models={
        "light": "gemini-3-flash",
        "standard": "gemini-3.1-pro",
        "heavy": "gpt-5.1-codex",
    }
)

# 自动路由：Router 决定自己做还是外包
result = router.dispatch("帮我写一个 Python 爬虫")
# → 判定为"弱上下文依赖 + 编码任务" → 外包给 Coding Agent

result = router.dispatch("把这个函数改成异步的")
# → 判定为"强上下文依赖" → 主 Agent 自己处理
```

### 自定义 Adapter

```python
from agent_delegate import RuntimeAdapter

class MyAdapter(RuntimeAdapter):
    def spawn(self, task: str, model: str, **kwargs) -> str:
        """创建子 agent，返回 run_id"""
        ...

    def listen(self, run_id: str) -> str:
        """等待子 agent 完成，返回结果"""
        ...

    def send(self, message: str, channel: str = "default") -> None:
        """发送消息到用户"""
        ...

router = Router(adapter=MyAdapter())
```

## 项目结构

```
agent-delegate/
├── src/
│   ├── router/              # 调度大脑
│   │   ├── decision.py      # 决策矩阵（上下文依赖分析 + 任务分类）
│   │   ├── fallback.py      # 模型候选链 + 降级策略
│   │   └── context.py       # 上下文打包协议
│   ├── workers/             # Worker 定义
│   │   ├── coding/          # 编码流水线（4 阶段）
│   │   ├── research/        # 调研流水线（4 阶段）
│   │   ├── doc/             # 文档流水线（9 阶段）
│   │   └── qa/              # 审计流水线
│   ├── adapters/            # Runtime 适配层
│   │   ├── base.py          # RuntimeAdapter 抽象接口
│   │   ├── openclaw.py      # OpenClaw 适配
│   │   ├── langchain.py     # LangChain 适配
│   │   └── openai.py        # OpenAI Agents SDK 适配
│   └── models/              # 数据模型
│       ├── task.py          # Task / TaskResult
│       └── pipeline.py      # Pipeline 定义
├── workers/                 # 独立 Worker 脚本（可直接运行）
├── scripts/                 # Pipeline 执行脚本
├── examples/                # 使用示例
└── docs/                    # 详细文档
```

## 设计哲学

1. **策略 > 框架** — 不提供空壳 API，提供经过验证的调度策略
2. **容灾优先** — 每个环节都有 fallback，单个模型/provider 挂掉不影响整体
3. **上下文安全** — XML 打包协议防止子 Agent 被数据内容误导
4. **Fire and Forget** — 主 Agent 派发后立即释放，不阻塞用户交互

## 竞品对比

| | Agent Delegate | CrewAI | LangGraph | AutoGen |
|---|---|---|---|---|
| 调度决策 | ✅ 自动上下文分析 | 手动定义 | 手动定义 | 手动定义 |
| 模型降级 | ✅ 6档候选链 | ❌ | ❌ | ❌ |
| 上下文安全 | ✅ XML打包协议 | ❌ | ❌ | ❌ |
| 流水线模板 | ✅ 3种内置 | 少量 | ❌ | ❌ |
| 生产验证 | ✅ 7×24在线 | 社区使用 | 社区使用 | 研究项目 |
| Runtime绑定 | ✅ 可插拔Adapter | LangChain | LangChain | 自有 |

## License

MIT

---

*Extracted from a production AI assistant system running 7×24 since 2026.*
