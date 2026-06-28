# Agent Delegate

A multi-agent dispatcher that decides whether to handle a task itself or delegate it to a specialized worker, with automatic model fallback when things go wrong.

This was extracted from a production AI assistant system. The focus is on scheduling strategy, not framework ceremony.

## Features

- **Smart routing**: Judges context dependency. Weak dependency tasks go to sub-agents; strong dependency tasks stay local.
- **Task decomposition**: Parallel tasks spawn concurrently; serial tasks get bundled to reduce round-trips.
- **Context isolation**: XML tags separate context / task / constraints so sub-agents aren't misled by data content.
- **Model fallback chains**: Each task tier has 2-4 candidate models. On 429 / 500 / timeout, automatically switch. One provider down doesn't take down the whole system.
- **Audit trail**: Every spawn logs attempts[] to memory for later tracing.
- **Workspace isolation**: Sub-agents write only to their own directory, never the root.

## Built-in Workers

| Worker | Stages | Description |
|--------|--------|-------------|
| Coding | Planner → Builder → Reviewer → Consultant | Code pipeline with review |
| Research | Searcher → Synthesizer → Fact-Checker → Reporter | Research pipeline with cross-validation |
| Doc | Scanner → Planner → Expander → Editor → Render | Documentation pipeline |
| QA | Global scan → High-risk sweep → Test execution | Security audit and testing |
| Deploy | Environment check → Script execution → Asset inventory | Automated deployment |

## Install

```bash
pip install agent-delegate
```

## Usage

```python
from agent_delegate import Router

router = Router(
    adapter="openclaw",  # or "langchain", "openai", "custom"
    models={
        "light": "gemini-3-flash",
        "standard": "gemini-3.1-pro",
        "heavy": "gpt-5.1-codex",
    }
)

result = router.dispatch("write me a Python scraper")
# weak context dependency + coding task → delegated to Coding worker

result = router.dispatch("make this function async")
# strong context dependency → handled by main agent
```

## Custom Runtime

```python
from agent_delegate import RuntimeAdapter

class MyAdapter(RuntimeAdapter):
    def spawn(self, task: str, model: str, **kwargs) -> str:
        """Create a sub-agent, return run_id"""
        ...

    def listen(self, run_id: str) -> str:
        """Wait for sub-agent to finish, return result"""
        ...

    def send(self, message: str, channel: str = "default") -> None:
        """Send message to user"""
        ...

router = Router(adapter=MyAdapter())
```

## Project Structure

```
agent-delegate/
├── src/
│   ├── router/          # Dispatcher
│   │   ├── decision.py  # Context dependency analysis + task classification
│   │   ├── fallback.py  # Model candidate chain + degradation strategy
│   │   └── context.py   # Context packing protocol
│   ├── workers/         # Worker definitions
│   │   ├── coding/
│   │   ├── research/
│   │   ├── doc/
│   │   └── qa/
│   ├── adapters/        # Runtime adapter layer
│   │   ├── base.py
│   │   ├── openclaw.py
│   │   ├── langchain.py
│   │   └── openai.py
│   └── models/          # Data models
│       ├── task.py
│       └── pipeline.py
├── workers/             # Standalone worker scripts (can run directly)
├── scripts/             # Pipeline execution scripts
├── examples/            # Usage examples
└── docs/                # Detailed documentation
```

## License

MIT
