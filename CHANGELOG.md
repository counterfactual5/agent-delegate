# Changelog

## [Unreleased]

### Changed
- Model selection is caller-first: `dispatch_with_fallback` uses `Task.candidates`, then
  `Task.model_override`, then a caller-set `Task.task_type` (no longer overwritten by keyword
  classification); keyword classification is only the fallback. **Breaking:** `DEFAULT_CHAINS` is now
  empty (its placeholder IDs were not real models), and `Router.select_model()` raises
  `ChainNotConfigured` instead of silently falling back to the STANDARD chain. `Router.dispatch()` is
  kept but documented as not recommended.
- Moved to a src layout: the package is now `agent_delegate` (`src/agent_delegate/`); tests import the
  package via `pythonpath = ["src"]` instead of `sys.path` hacks.
- Unified the adapter contract: callers always call `listen()` after a successful `spawn()`.
  `OpenClawAdapter` now stores the CLI output in `spawn()` and returns it from `listen()`, instead of
  always reporting success; empty output no longer collapses every run to `run_id="unknown"`.
- `Router.dispatch_with_fallback` accepts an optional `chain` to bypass task classification.
- Error signatures match numeric codes with digit boundaries (`"1503"` no longer
  triggers the `"500"` needle); `"took too long"` classifies as TIMEOUT. The
  exhaustion error now carries an aggregate summary (`attempted`, `blacklisted`,
  `error_classes`).

### Added
- `Task.candidates` (ordered `provider/model` IDs) and `FallbackChain.from_ids()`; the provider is the
  ID prefix, or the ID itself when there is no prefix.
- `WorkerOutput.incomplete`: `RESTAdapter.listen()` timeouts are reported as incomplete rather than as
  plain failures. `PipelineRunner` does not re-dispatch an incomplete stage; it records the `run_id`
  (`StageRecord.incomplete`, `PipelineResult.incomplete_stage`) so the caller can listen again.
- `AttemptRecord`: structured per-attempt audit trail (model, provider, outcome,
  error class, duration) replacing hand-formatted attempt strings; `StageRecord`
  keeps every round's `SpawnResult` in `spawn_attempts` and times each stage in
  `duration_ms`.
- `ErrorClass.CONTEXT_LENGTH`: context-overflow errors now fall back to candidates
  with a larger `context_window` (`ModelCandidate.context_window`).
- stdlib logging at key transitions: provider blacklisting, same-model 5xx retry,
  timeout re-sorting, context filtering, stage retry/failure/completion, and
  `send()` failures.
- `PipelineRunner`: runs pipelines stage by stage with input/output artifact gates, reusing the router's
  fallback chain per stage tier and re-dispatching unqualified results up to `Stage.max_retries`.

### Fixed
- `RESTAdapter._request` no longer swallows programming errors (they propagate to
  the caller); network/protocol exceptions, including `http.client.HTTPException`,
  still convert to `{"error": ...}`. The per-request timeout follows the task's
  `timeout_seconds` instead of a hardcoded 60s.
- `OpenClawAdapter.send()` and `RESTAdapter.send()` are best-effort: failures log
  a warning instead of raising.
- `RESTAdapter.listen()` returned transport errors and malformed responses only after the full timeout,
  reported as "Timeout waiting for agent". They now fail immediately with the original error. The poll
  interval is configurable and the last round no longer sleeps past the deadline.
- `RESTAdapter` treated `{"status": "completed", "error": null}` as a failure. Responses with a `status`
  field are now judged by `status`, not by the presence of an `error` key.
- `PipelineRunner` accepted pre-existing files as stage output. Artifacts must now be created or modified
  during the current attempt; directory artifacts must be non-empty.
- `PipelineRunner` ignored `Router(chains=...)`. It now resolves chains via `Router.select_model()`.
- README described files, workers, and an API that did not exist.

- `RESTAdapter.spawn()` treated `{"status": "failed", "error": "..."}` as success. Any non-empty
  `error` field now marks the response as failed unless `status` is `completed` or in-progress.
- `Router.select_model()` raised `KeyError` when `chains` was missing `TaskType.STANDARD`. It now
  falls back gracefully. `Router(chains={})` is also no longer replaced by `DEFAULT_CHAINS`.
- `PipelineRunner` accepted an empty subdirectory as a valid directory artifact. It now requires at
  least one file inside the directory.
- README `listen()` example now passes an explicit timeout matching the task timeout.

Tests: 59 passed.

## [0.1.0] — 2026-06-05

Initial release. Production-grade multi-agent orchestration with:

### Added
- **Router**: context-dependency analysis, 6-tier task classification, XML context packing.
- **Error-class-aware fallback**: `ErrorClass` + `classify_error`; 429/auth blacklists whole
  provider; 5xx retries once; timeout prefers faster candidates; `attempts[]` audit trail.
- **Pipeline workers**: coding, research, doc, QA, deploy stage definitions.
- **RuntimeAdapter** abstraction with OpenClaw and REST adapters.

Tests: 11 passed.
