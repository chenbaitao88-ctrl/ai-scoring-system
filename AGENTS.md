# AI Scoring System AI Scoring System

This repository is the portable, privacy-safe product edition of the AI Scoring System AI-assisted scoring system.

## Scope

- Single user, local machine, single backend instance.
- SQLite plus Phase 11 file sidecars; no database schema changes without explicit approval.
- Synthetic demo data is the default. Real student data is never part of this repository.
- Live model providers are optional and must be enabled explicitly through local environment variables.

## Privacy and security

- Do not commit real names, phone numbers, registration sheets, cloud-drive links, works, media, transcripts, databases, model responses, exports, or logs.
- Do not commit `.env`, API keys, tokens, passwords, private endpoints, or account identifiers.
- Tests and demos must use unmistakably fictional identifiers and deterministic fixtures.
- Runtime files belong under `runtime/` or `data/`, both ignored by Git.

## Engineering rules

- Preserve successful `ScoreAttempt` records; retries create new attempts.
- `must_review` blocks automatic adoption.
- `ManualFinalLock` protects a confirmed human result.
- Provider changes must not change the rubric, prompt, evidence input, or response schema.
- Do not execute submitted programs, macros, installers, or unknown scripts.

## Verification

From the repository root:

```text
python -m pytest tests -v
cd frontend && npm run build
python scripts/e2e_smoke_playwright.py
```

For the portable demo, follow `docs/MACOS_INTEL.md` or `docs/RUNBOOK.md`.
