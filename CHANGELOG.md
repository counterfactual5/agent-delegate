# Changelog

## [0.1.0] — 2026-06-05

Initial release. Production-grade multi-agent orchestration with:

### Added
- **Router**: context-dependency analysis, 6-tier task classification, XML context packing.
- **Error-class-aware fallback**: `ErrorClass` + `classify_error`; 429/auth blacklists whole
  provider; 5xx retries once; timeout prefers faster candidates; `attempts[]` audit trail.
- **Pipeline workers**: coding, research, doc, QA, deploy stage definitions.
- **RuntimeAdapter** abstraction with OpenClaw and REST adapters.

Tests: 11 passed.
