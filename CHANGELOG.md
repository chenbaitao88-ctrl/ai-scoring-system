# Changelog

## 2026-09-08 — Authoritative file exports

- Route XLSX, DOCX and CSV through the same task-scoped adoption/final-lock derivation.
- Reject unresolved cases, missing facts, incomplete task manifests and stale result versions; never fall back to legacy weighted scores.
- Add task preview, explicit selection, version-aware downloads and mobile navigation.
- Keep legacy download URLs as task_id-required compatibility routes (409 without a task).
- Add durable-fact and real-file regression tests, offline browser checks and export documentation.

## 1.0.2 - 2026-09-01

- Refreshed the six-model multimodal catalog shown in the scoring interface.
- Aligned backend defaults, frontend choices, API defaults, and related tests.
- Kept the portable demo offline: model names describe selectable product options and do not claim that six live Provider connections were validated.

## 1.0.1 - 2026-09-01

- Added complete AI process comments for all 12 synthetic demo cases.
- Added realistic fictional work titles and three fully synthetic sample work packages.
- Added safe in-place enrichment for an existing offline demo runtime.
- Added regression coverage for comments, sample archives, idempotence, and runtime upgrades.

## 1.0.0 - 2026-08-31

- Created a fresh privacy-safe product repository from the Phase 11 production baseline.
- Added deterministic synthetic demo data for local and onsite demonstrations.
- Added isolated demo runtime, Intel Mac setup/start/stop/acceptance scripts, and Windows helper scripts.
- Added locked Python and frontend dependencies.
- Excluded real project data, databases, runtime sidecars, works, logs, exports, and credentials.
