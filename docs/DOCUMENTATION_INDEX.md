# Documentation Index

This index defines the canonical documentation structure for the repository.

## System Doctrine
- `docs/ARCHITECTURE.md` — Core architecture, domain model, and deterministic system boundaries.
- `docs/EXECUTION_VISIBILITY_STANDARD.md` — Normative visibility and execution observability doctrine.
- `docs/OPERATING_MODE.md` — Repository governance and release authority model.
- `docs/VISIBILITY_HOOK_CONTRACT.md` — Contract for instrumentation hook semantics and stability requirements.

## Runtime Surfaces
- `docs/API_REFERENCE.md` — Runtime endpoint and interface reference.
- `docs/OPERATIONS.md` — Operational runbook and day-2 procedures.
- `docs/ALERT_SCHEMA.md` — Alert payload schema and field definitions.
- `docs/ALERT_INTEGRITY_MONITOR.md` — Alert integrity monitoring surface and checks.
- `docs/HYDRATION_HEALTH.md` — Hydration health endpoint semantics.
- `docs/LADDER_CACHE_OBSERVABILITY.md` — Ladder cache observability behavior and diagnostics.
- `docs/REPLAY_PARITY_VALIDATION.md` — Replay parity validation workflow and checks.
- `docs/env_vars.md` — Environment variable catalog and runtime configuration notes.

## Engineering Workflow
- `docs/WORKFLOW.md` — Single source-of-truth workflow: task identity rules, packet-first review protocol, canonical state machine, and operator shortcut prompts.
- `docs/WORKFLOW_OPERATIONS.md` — Canonical ChatGPT/Codex task model, packet-first review process, merge decision rules, and full reusable prompt library.
- `docs/testing.md` — Test execution entry points and verification workflow.
- `docs/PR_REVIEW_CHECKLIST.md` — Pull request validation checklist for documentation and runtime safety.
- `docs/CODEX_HANDOFF_TEMPLATE.md` — Session handoff template used for deterministic engineering continuity.
- `docs/CODEX_MASTER_TEMPLATE.md` — Master execution constraints for repository-safe agent operation.

## Archive
- `docs/archive/reviews/` — Archived review artifacts.
- `docs/archive/design_specs/` — Archived design specifications.
- `docs/archive/` — Historical diagnostics, superseded proposals, milestones, and engineering artifacts retained for traceability.
