# WORKFLOW

## Purpose
Single source of truth for task execution and review flow.

## Task Identity Rules (No Parallel Drift)
1. Every change starts with one `Task ID`: `TASK_<YYYY-MM-DD>_<slug>`.
2. Exactly one active CHANGE task per branch.
3. Exactly one review packet per CHANGE task.
4. Revisions do **not** create a new CHANGE task; they append to the same packet.
5. REVIEW tasks evaluate one CHANGE task at a time and reference that task's packet.
6. Do not start NEXT TASK until current task reaches `MERGE` or `ABANDON`.

## Task Types and Tracking
- **CHANGE task**: implements code/docs changes for one scoped objective.
- **REVIEW task**: evaluates the CHANGE task packet and decides `MERGE`, `REVISE`, or `ABANDON`.

Tracking requirements:
- CHANGE task is tracked by `Task ID` + branch + single packet path.
- REVIEW task is tracked by `REVIEW of <Task ID>` and must point to the same packet.
- Never mix CHANGE implementation steps into REVIEW decisions.

## Canonical State Machine
`CHANGE -> PACKET -> REVIEW -> (REVISE -> PACKET -> REVIEW)* -> MERGE -> NEXT TASK`

Rules:
- `PACKET` is mandatory before review.
- `REVISE` loops on the same `Task ID` and same packet file.
- `MERGE` closes current CHANGE task; only then create NEXT TASK.

## Review Packet Rule (Canonical Path)
Use exactly one packet per CHANGE task at:

`docs/review_packets/REVIEW_PACKET_<YYYY-MM-DD>_<task_slug>.md`

- Create once during first packetization.
- All revision updates append new dated sections in this same file.
- Do not create v2/v3 packet files for the same CHANGE task.

## Packet-First Review Rules
- Human and assistant operate packet-first.
- No giant diffs pasted in chat.
- Packet contains summary, scope, evidence, risks, and validation commands.
- Review decisions are made from the packet plus targeted file inspection.

## Packet Format (Required)
Use this template and fill every field:

```md
# REVIEW PACKET

## Task Identity
- Task ID: TASK_<YYYY-MM-DD>_<slug>
- Task Type: CHANGE | REVIEW
- Branch: <branch_name>
- Canonical Packet Path: docs/review_packets/REVIEW_PACKET_<YYYY-MM-DD>_<task_slug>.md

## Scope
- In scope:
- Out of scope:

## Files Changed
- <relative/path>
- <relative/path>

## Determinism / Governance Checklist
- [ ] Phase 1 semantics unchanged (immutable)
- [ ] Execution-domain guard preserved
- [ ] Replay equivalence preserved
- [ ] No live trading behavior introduced

## Evidence (with timestamps)
- Endpoints/logs checked:
  - `<endpoint or log source>` — `<UTC timestamp>` — `<result>`
  - `<endpoint or log source>` — `<UTC timestamp>` — `<result>`

## Risk + Rollback
- Risk assessment:
- Rollback note:

## Test / Validation
- Commands run:
  - `<command>` — `<pass/fail>`
- Observability endpoints validated:
  - `<endpoint>` — `<what was verified>`

## Docs / Link Pointers
- `<relative/path/to/doc>`
- `<relative/path/to/doc>`
```

## Operator Rules
- Humans never paste large diffs into chat.
- Assistant reviews the packet and outputs only: `MERGE` / `REVISE` / `ABANDON` + required next-step prompt.
- If `REVISE`: provide exactly one Codex revision prompt.
- If `MERGE`: include NEXT TASK and its Codex prompt.
- Repo docs are source of truth; no canvas.

## Shortcut Prompts (Copy/Paste)

### 1) Change task implementation prompt
```text
You are implementing CHANGE task TASK_<YYYY-MM-DD>_<slug>.
Scope:
- In: <items>
- Out: <items>

Requirements:
- Keep changes minimal and deterministic.
- Update only files required by scope.
- Return and apply a unified diff only.
- After edits, list files changed and validation commands run.
```

### 2) Review packet generation prompt
```text
Generate/append the canonical review packet for TASK_<YYYY-MM-DD>_<slug> at:
docs/review_packets/REVIEW_PACKET_<YYYY-MM-DD>_<task_slug>.md

Requirements:
- Use the repository WORKFLOW Packet Format exactly.
- Include scope, files changed, determinism/governance checklist, evidence with UTC timestamps, risk/rollback, and validation commands.
- Summarize code/docs changes using unified diff references (no large pasted diffs).
- Return a unified diff only.
```

### 3) Revision task prompt (address review feedback)
```text
Execute REVISION for TASK_<YYYY-MM-DD>_<slug> based on review feedback.

Requirements:
- Apply only requested fixes; no scope expansion.
- Append revision evidence to existing canonical packet file (do not create a new packet).
- Keep determinism/governance constraints explicit.
- Return and apply a unified diff only.
```

### 4) Optional merge checklist prompt
```text
Perform MERGE readiness check for TASK_<YYYY-MM-DD>_<slug> using its canonical review packet.

Output format:
- Decision: MERGE or REVISE or ABANDON
- If REVISE: provide exactly one Codex revision prompt.
- If MERGE: provide NEXT TASK_<YYYY-MM-DD>_<slug> and one Codex implementation prompt.

Constraint:
- Reference unified diffs and packet evidence; do not request large diff pastes.
```
