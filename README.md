# Agent Delegate

A multi-agent dispatcher that decides whether to handle a task itself or delegate it to a specialized worker, with automatic model fallback when things go wrong.

This was extracted from a production AI assistant system. The focus is on scheduling strategy, not framework ceremony.

## Features

- **Smart routing**: Judges context dependency. Weak dependency tasks go to sub-agents; strong dependency tasks stay with the main agent.
- **Context isolation**: XML tags separate context / task / constraints so sub-agents aren't misled by data content.
- **Caller-chosen models with fallback**: The caller supplies ordered candidate models, per task (`Task.candidates`) or per task type (`Router(chains=...)`). Nothing is built in, because available models differ per runtime. Rate limits and auth errors skip the whole provider, 5xx retries the same model once, timeouts prefer faster candidates.
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
from agent_delegate import Router, RESTAdapter, Task

adapter = RESTAdapter({
    "base_url": "http://localhost:8080",
    "headers": {"Authorization": "Bearer <token>"},
})
router = Router(adapter)

# 候选模型取自运行环境实际可用的列表，按顺序尝试，失败时按错误类型降级
task = Task(
    description="implement a REST backend for a todo app",
    candidates=["provider-a/model-x", "provider-b/model-y"],
)
result = router.dispatch_with_fallback(task)
if result.status != "error":
    output = adapter.listen(result.run_id, timeout_ms=300_000)
    if output.incomplete:
        ...  # 远端仍在运行：稍后用同一个 run_id 再 listen，不要重派
```

`dispatch_with_fallback` returns a `SpawnResult` whose `model` and `attempts` show which candidate succeeded. Candidates are resolved caller-first: an explicit `chain` argument, then `Task.candidates`, then `Task.model_override`, then the caller's `Task.task_type` looked up in `Router(chains=...)`. Keyword classification is only used when none of these is given. If no chain is found, `ChainNotConfigured` is raised; the library ships no model list.

Pick candidate IDs from the models your runtime actually offers, not from memory. In a model ID of the form `provider/model`, the prefix is the provider, so a rate limit or auth error skips the rest of that provider's candidates. Scripts that have no LLM caller can configure chains per task type, e.g. `Router(adapter, chains={TaskType.CODING: FallbackChain.from_ids([...])})`.

`Router.dispatch(description)` is kept for compatibility but not recommended: it guesses the task type from keywords, returns advice text for context-dependent tasks, and spawns only the first candidate without fallback.

### Running a pipeline

```python
from agent_delegate import PIPELINES, FallbackChain, PipelineRunner, Router, OpenClawAdapter

tiers = {
    "light": FallbackChain.from_ids(["provider-a/fast-model"]),
    "standard": FallbackChain.from_ids(["provider-a/mid-model", "provider-b/mid-model"]),
    "heavy": FallbackChain.from_ids(["provider-b/strong-model", "provider-a/strong-model"]),
}
runner = PipelineRunner(Router(OpenClawAdapter()), workdir="./run", tier_chains=tiers)
result = runner.run(PIPELINES["coding"], "build a CLI todo app")

if not result.success:
    print(result.failed_stage.name, result.failed_stage.error)
```

Each stage is dispatched through the router's fallback chain for its `model_tier` (`light`, `standard`, `heavy`). The chain comes from `tier_chains`, or from `Router.select_model()` when `tier_chains` is not given, so `Router(chains=...)` is respected. Model and provider failures are handled by the chain; a worker that reports failure or skips its output files is re-dispatched up to the stage's `max_retries`. The run stops at the first failed stage. If `listen()` gives up before the worker reaches a final state, the stage is not re-dispatched: its record is marked `incomplete` with the `run_id` (see `result.incomplete_stage`), since the worker may still be running.

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
