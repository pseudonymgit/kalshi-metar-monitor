# CODEX HANDOFF TEMPLATE

## 1) Mandatory header binding to /docs/CODEX_MASTER_TEMPLATE.md

**MANDATORY BINDING:** This handoff must comply with `/docs/CODEX_MASTER_TEMPLATE.md` before any implementation action.

## 2) Project Identity

- Repository: `kalshi-metar-monitor`
- Branch: `<feature/fix/phase/test branch>`
- Session date (UTC): `<YYYY-MM-DD>`
- Session objective: `<concise objective>`

## 3) Current Stability Declaration

- Phase 1 semantics status: `preserved` / `blocked` (must include rationale)
- Execution logic mutation status: `no mutation` / `mutation proposed but not applied`
- Determinism status: `preserved` / `risk identified`
- Observability-domain boundary status: `preserved` / `risk identified`

## 4) Current Roadmap Snapshot

- Active P0 items:
  - `<item>`
- Active P1 items:
  - `<item>`
- Milestone target in progress:
  - `<tag or milestone name>`

## 5) Mutation Guardrails

- No execution authority changes outside approved scope.
- No ingestion ordering changes.
- No transition emission semantic changes.
- No replay-equivalence behavior changes.
- No dependency additions unless explicitly approved.

## 6) Replay & Determinism Invariants

- Replay must reproduce live transition outcomes for identical ordered inputs.
- Transition-driven alert semantics remain authoritative.
- Alert absence alone is non-authoritative for hydration/discovery diagnosis.
- Observability remains read-only and non-causal.

## 7) Deliverable Format Requirements

- Provide exact files changed.
- Provide unified diffs for modified/moved files.
- Provide full content for new files.
- Include validation commands run and outcomes.
- Explicitly state whether logic changes occurred.

## 8) Milestone Reference Section

- Current milestone doc: `<docs/MILESTONE_...md>`
- Prior milestone docs consulted:
  - `<doc>`
- Tag readiness statement:
  - `<ready/not ready + reason>`

---

**MANDATORY USE POLICY:** This template is required for all future Codex sessions in this repository.
