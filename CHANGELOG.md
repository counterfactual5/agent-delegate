# Changelog

## [Unreleased]

### Removed
- **Decision layer**: `analyze_context`, `classify_task`, `dispatch`, and the
  `ContextDependency` enum. Decision-making belongs to the caller (LLM or config);
  the framework is now a pure execution engine. Use `load_chains()` for
  config-driven chain selection.

### Added
- **PipelineRunner**: Multi-stage pipeline execution engine (`src/workers/runner.py`) for Coding, Research, and Doc pipelines with `input_gates` dependency validation, cascade skips, configurable `max_retries`, and `PipelineRun`/`StageRun` audit records.
- **AttemptRecord**: Structured dataclass in `src/models/base.py` tracking model fallback audit trails (`model`, `provider`, `outcome`, `error_class`, `reason`), replacing plain string concatenation; `str(record)` preserves the readable one-line summary.
- **Context Length Error Handling**: Added `ErrorClass.CONTEXT_LENGTH` to error classification signatures and adaptive router fallback to automatically switch to models with larger context windows when token limits are exceeded.
- **RESTAdapter kwargs configuration**: Flexible kwargs configuration for base URL, headers, and endpoints; `wait=True` semantics available via `RESTAdapter.spawn(wait=True)` which polls until terminal status.

### Fixed
- **OpenClaw WorkerOutput fields**: Standardized cached `WorkerOutput` construction in `OpenClawAdapter` to align with base model field expectations.
- **Router custom chains fallback**: Added robust fallback handling when custom `chains` mappings do not define a requested task type, falling back to `TaskType.STANDARD` or the first available configured chain.

## [0.1.0] — 2026-06-05

Initial release. Production-grade multi-agent orchestration with:

### Added
- **Router**: context-dependency analysis, 6-tier task classification, XML context packing.
- **Error-class-aware fallback**: `ErrorClass` + `classify_error`; 429/auth blacklists whole
  provider; 5xx retries once; timeout prefers faster candidates; `attempts[]` audit trail.
- **Pipeline workers**: coding, research, doc, QA, deploy stage definitions.
- **RuntimeAdapter** abstraction with OpenClaw and REST adapters.

Tests: 11 passed.
