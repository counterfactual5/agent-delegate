# Agent Delegate

A multi-agent dispatcher that decides whether to handle a task itself or delegate it to a specialized worker, with automatic model fallback when things go wrong.

This was extracted from a production AI assistant system. The focus is on scheduling strategy, not framework ceremony.

## Features

- **Smart routing**: Judges context dependency. Weak dependency tasks go to sub-agents; strong dependency tasks stay with the main agent.
- **Context isolation**: XML tags separate context / task / constraints so sub-agents aren't misled by data content.
- **Model fallback chains**: Each task tier has candidate models across providers. Rate limits and auth errors skip the whole provider, 5xx retries the same model once, timeouts prefer faster candidates.
- **Audit trail**: Every dispatch records its `attempts` list for later tracing.
- **Pipelines with artifact gates**: Multi-stage workers check that each stage's input files exist before it runs and that its output files exist after it finishes.

## Built-in Pipelines

| Pipeline | Stages |
|----------|--------|
| `coding` | Planner → Builder → Reviewer → Consultant |
| `research` | Searcher → Synthesizer → Fact-Checker → Reporter |
| `doc` | Scanner → Planner → Section Expander → Merger → Quality Gate → Editor → Kami Brief → Render |

They are available as `agent_delegate.PIPELINES`.

## Install

The package is not published to PyPI yet. Install from a checkout:

```bash
pip install -e ".[dev]"
```

## Usage

```python
from agent_delegate import Router, RESTAdapter

adapter = RESTAdapter({
    "base_url": "http://localhost:8080",
    "headers": {"Authorization": "Bearer <token>"},
})
router = Router(adapter)

# 弱上下文依赖 → 派发给子 agent，返回 SpawnResult
result = router.dispatch("implement a REST backend for a todo app")
if result.status != "error":
    output = adapter.listen(result.run_id, timeout_ms=300_000)  # 取结果

# 强上下文依赖 → 返回字符串，建议主 agent 自己处理
advice = router.dispatch("continue the previous refactor")
```

`Router.dispatch_with_fallback(task)` takes a `Task` and walks the fallback chain for its task type, returning a `SpawnResult` whose `model` and `attempts` show which candidate succeeded.

### Running a pipeline

```python
from agent_delegate import PIPELINES, PipelineRunner, Router, OpenClawAdapter

runner = PipelineRunner(Router(OpenClawAdapter()), workdir="./run")
result = runner.run(PIPELINES["coding"], "build a CLI todo app")

if not result.success:
    print(result.failed_stage.name, result.failed_stage.error)
```

Each stage is dispatched through the router's fallback chain for its `model_tier` (`light`, `standard`, `heavy`). The chain comes from `Router.select_model()`, so a custom `Router(chains=...)` is respected. Model and provider failures are handled by the chain; a worker that reports failure or skips its output files is re-dispatched up to the stage's `max_retries`. The run stops at the first failed stage.

Output artifacts must be created or modified during the current attempt — pre-existing files and leftovers from failed tries don't count as delivery. Paths ending in `/` must be non-empty directories.

## Custom Runtime

```python
from agent_delegate import RuntimeAdapter, SpawnResult, WorkerOutput

class MyAdapter(RuntimeAdapter):
    def spawn(self, task: str, model: str, **kwargs) -> SpawnResult:
        """Submit the task and return a handle (run_id)."""
        ...

    def listen(self, run_id: str, timeout_ms: int = 30000) -> WorkerOutput:
        """Return the result for run_id, waiting if needed."""
        ...

    def send(self, message: str, **kwargs) -> None:
        """Send a message to the user."""
        ...

    def list_runs(self, **kwargs) -> list:
        """List active runs."""
        ...
```

Callers always call `listen()` after a successful `spawn()`. Runtimes that execute synchronously store the result in `spawn()` and return it from `listen()`, as `OpenClawAdapter` does. `Router.dispatch()` returns a `SpawnResult`; check `status` before calling `listen()`.

## Project Structure

```
agent-delegate/
├── src/agent_delegate/
│   ├── __init__.py        # Public API
│   ├── models/base.py     # Task, fallback chains, error classes, RuntimeAdapter
│   ├── router/router.py   # Context analysis, classification, packing, fallback dispatch
│   ├── workers/
│   │   ├── pipelines.py   # Stage and pipeline definitions
│   │   └── runner.py      # PipelineRunner
│   └── adapters/
│       ├── openclaw.py    # OpenClaw CLI runtime
│       └── rest.py        # Generic REST runtime
└── tests/
```

## Development

```bash
pytest
ruff check .
```

## License

MIT
