# Agent Delegate

A multi-agent dispatcher that decides whether to handle a task itself or delegate it to a specialized worker, with automatic model fallback when things go wrong.

This was extracted from a production AI assistant system. The focus is on scheduling strategy, not framework ceremony.

## Features

- **Smart routing**: Judges context dependency. Weak dependency tasks go to sub-agents; strong dependency tasks stay local.
- **Context isolation**: XML tags separate context / task / constraints so sub-agents aren't misled by data content.
- **Model fallback chains**: Each task tier has 2-3 candidate models. On 429 / 500 / timeout, automatically switch. One provider down doesn't take down the whole system.
- **Error classification & adaptive fallback**: Distinguishes rate limits, auth failures, server errors, timeouts, and context-length overflow (`ErrorClass.CONTEXT_LENGTH` automatically falls back to larger-window models). Router blacklists failing providers on 429/auth, retries 5xx once, and favors faster candidates on timeout.
- **Structured audit trail (`AttemptRecord`)**: Tracks every attempt with structured metadata (`model`, `provider`, `outcome`, `status`, `error_class`, `reason`), preserving clean readable string representations via `__str__`.
- **Pipeline execution (`PipelineRunner`)**: Orchestrates multi-stage workflows (Coding, Research, Doc) with dependency input gates, cascading skips, stage retries, and comprehensive execution tracking (`PipelineRun`).

## Built-in Workers

| Worker | Stages | Description |
|--------|--------|-------------|
| Coding | Planner → Builder → Reviewer → Consultant | Code pipeline with review |
| Research | Searcher → Synthesizer → Fact-Checker → Reporter | Research pipeline with cross-validation |
| Doc | Scanner → Planner → Section Expander → Merger → Quality Gate → Editor → Kami Brief → Render | Documentation pipeline (8 stages) |

**Note**: Only 3 workers are currently implemented (Coding, Research, Doc). QA and Deploy workers are planned but not yet available.

## Install

```bash
pip install agent-delegate
```

**Note**: After installation, import from `src` directly:
```python
from src.router.router import Router
from src.adapters.openclaw import OpenClawAdapter
```

Package name configuration is in progress to enable `from agent_delegate import ...` imports.

## Usage

```python
from src.router.router import Router
from src.adapters.openclaw import OpenClawAdapter
from src.adapters.rest import RESTAdapter

# Using OpenClaw adapter
router = Router(adapter=OpenClawAdapter())

# Or using REST adapter
router = Router(adapter=RESTAdapter(
    base_url="http://localhost:8000",
    spawn_endpoint="/agents/spawn",
    listen_endpoint="/agents/{run_id}/status",
    send_endpoint="/agents/message"
))

result = router.dispatch("implement a retry decorator with exponential backoff")
# coding keywords detected → routed to coding model tier

result = router.dispatch("compare the last two outputs")
# strong context dependency ("last two") → handled by main agent
```

### PipelineRunner

Execute predefined multi-stage pipelines with dependency gate validation and stage retries:

```python
from src.router.router import Router
from src.adapters.openclaw import OpenClawAdapter
from src.workers.runner import PipelineRunner
from src.workers.pipelines import StageStatus

router = Router(adapter=OpenClawAdapter())
runner = PipelineRunner(router)

# Run the 4-stage coding pipeline (Planner → Builder → Reviewer → Consultant)
run = runner.run("coding", context="Build a rate-limited HTTP client")

if run.status == StageStatus.COMPLETED:
    print(f"Pipeline finished successfully. Artifacts: {run.artifacts}")
else:
    print(f"Pipeline failed ({run.status.value}): {run.error}")
```

## Custom Runtime

```python
from src.models.base import RuntimeAdapter, SpawnResult, WorkerOutput

class MyAdapter(RuntimeAdapter):
    def spawn(self, task: str, model: str, **kwargs) -> SpawnResult:
        """Create a sub-agent, return SpawnResult"""
        # Your implementation here
        return SpawnResult(run_id="...", status="pending")

    def listen(self, run_id: str, timeout_ms: int = 30000) -> WorkerOutput:
        """Wait for sub-agent to finish, return result"""
        # Your implementation here
        return WorkerOutput(success=True, summary="...")

    def send(self, message: str, **kwargs) -> None:
        """Send message to run_id or channel"""
        # Your implementation here
        pass
    
    def list_runs(self, **kwargs) -> list:
        """List active runs"""
        # Your implementation here
        return []

router = Router(adapter=MyAdapter())
```

## Project Structure

```
agent-delegate/
├── src/
│   ├── router/
│   │   └── router.py        # Dispatcher, context analysis, task classification, fallback logic
│   ├── workers/
│   │   ├── pipelines.py     # Pipeline and stage definitions (coding, research, doc)
│   │   └── runner.py        # Pipeline execution engine (gates, retries, cascade skips)
│   ├── adapters/
│   │   ├── openclaw.py      # OpenClaw CLI adapter
│   │   └── rest.py          # Generic REST adapter
│   ├── models/
│   │   └── base.py          # Data models (Task, Pipeline, RuntimeAdapter, etc.)
│   └── __init__.py
├── tests/
│   ├── test_router.py
│   └── test_fallback.py
├── pyproject.toml
└── README.md
```

## License

MIT
