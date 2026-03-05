# Workflow Operations (Canonical)

## 1) Purpose

This workflow exists to keep execution fast, deterministic, and unambiguous while minimizing token burn and re-explanation overhead.

Core principles:
- **Repository is source of truth.** Canonical state must live in committed repo artifacts, not chat memory, canvas, or out-of-band notes.
- **Packet-first review.** Reviews should begin from the latest repo-generated review packet and canonical docs, not giant pasted raw diffs.
- **Clear task identity.** Change, review, and handoff/update tasks are distinct so merge intent and execution intent are never conflated.
- **Lean operator loop.** Standardized prompts and artifacts reduce setup friction on both desktop and mobile.

## 2) Task Types

### A. Change Task
- **What it does:** Implements or modifies runtime behavior, tests, docs, or operational code to satisfy a scoped objective.
- **Primary artifact:** A code/doc patch set (commit/PR), plus any required tests and implementation notes.
- **Merged?:** **Yes** (if approved).
- **Changes runtime behavior?:** **Often yes** (unless docs-only or non-runtime maintenance).

### B. Review Task
- **What it does:** Evaluates the latest completed change task using evidence (preferably latest review packet) and issues a decision.
- **Primary artifact:** Review decision artifact (MERGE / REVISE / ABANDON) with rationale, risks, and next-task prompt.
- **Merged?:** Optional (merge only if useful as durable repository history).
- **Changes runtime behavior?:** **No.** Review artifacts are evidence/decision records.

### C. Handoff / State Update Task
- **What it does:** Updates canonical state and handoff docs after accepted work, preserving continuity across chats/operators.
- **Primary artifact:** Updated `PROJECT_STATE.md`, `docs/ROLLING_TODO.md`, handoff template/doc updates, and cross-links.
- **Merged?:** **Yes** (recommended when state materially changed).
- **Changes runtime behavior?:** **No** (documentation/state continuity only).

### D. Optional Doc Cleanup / Canonicalization Task
- **What it does:** Reduces documentation drift/bloat and clarifies canonical-vs-secondary-vs-archive structure.
- **Primary artifact:** Docs cleanup patch including index/cross-link updates and archive references.
- **Merged?:** **Yes** (when beneficial).
- **Changes runtime behavior?:** **No** (unless explicitly paired with implementation tasks).

## 3) Merge Decision Rule

- **Merge decisions apply to change tasks.**
- Review tasks **do not** replace change-task approval; they inform it.
- If review decision is **REVISE**, revise the **change task implementation**.
- If review decision is **MERGE**, merge the **change task**.
- Review artifacts may be merged for history/traceability, but this is separate from merging implementation changes.
- Unless a change is extremely tiny and obviously safe, run a review task before merge.

## 4) Packet-First Review Rule

- Start review from the newest repo-generated review packet.
- Use raw diffs only when packet evidence is insufficient, stale, ambiguous, or missing material facts.
- Every review must state its basis explicitly (packet path/version/commit, plus any supplemental files).
- Every review must clearly separate:
  1. what changed
  2. why it changed
  3. doctrine/roadmap alignment
  4. risks/omissions/drift
  5. decision (**MERGE / REVISE / ABANDON**)
  6. next highest-value task
  7. next Codex prompt

## 5) Standard Operating Loop

1. **Run change task** (implementation patch).
2. **Run review task** (packet-first evidence).
3. **Make merge decision for the change** (MERGE / REVISE / ABANDON).
4. If approved, **merge the change task**.
5. **Run state update/handoff task** to refresh canonical continuity artifacts.
6. Move to next highest-value task from review/state outputs.

Operator checklist:
- Keep task identity explicit in prompt title and first line.
- Keep review packet current after each meaningful change.
- Keep canonical indexes pointing at current doctrine/workflow docs.
- Avoid raw-diff dumping unless packet-first path is blocked.

## 6) Full Prompt Library

> All prompts below are copy/paste-ready. Use exactly as written, then adjust only task-specific scope details.

### Prompt A — State Update / Canonical Repo Update
**When to use:** After a completed task is merged or otherwise accepted, to refresh canonical state and continuity docs.

```text
Task Type: Handoff / State Update Task (Canonical Repo Update)

Use the repository as the source of truth and inspect the latest merged/accepted state.

Required actions:
1) Update PROJECT_STATE.md to reflect the latest accepted outcomes, current constraints, and active status.
2) Update docs/ROLLING_TODO.md to remove completed items, re-prioritize remaining items, and add any newly discovered follow-ups.
3) Update handoff-related docs/templates as needed (including docs/CODEX_HANDOFF_TEMPLATE.md and any active handoff file if present) so a new chat can resume without ambiguity.
4) Update any canonical docs affected by the completed task.
5) If any docs are clearly superseded, archive/cross-reference/mark stale appropriately; do not delete useful history without replacement.

Output requirements:
- Produce unified diffs only.
- Keep edits lean, explicit, and cross-referenced.
- Briefly summarize what was updated and why.
```

### Prompt B — Review Latest Task Packet-First
**When to use:** To review the latest completed change task using repo-grounded evidence with packet-first default.

```text
Task Type: Review Task (Packet-First)

Review the latest completed change task.

Review method requirements:
1) Use the newest repo-generated review packet first.
2) Only fall back to raw diffs if the packet is insufficient, stale, ambiguous, or missing material facts.
3) Explicitly identify review basis (packet path/version/commit + any supplemental artifacts).

Your review output must clearly separate:
- What changed
- Why it changed
- Doctrine/roadmap alignment
- Risks, omissions, or drift
- Decision: MERGE / REVISE / ABANDON
- Next highest-value task
- Next task prompt (copy/paste-ready)

If appropriate, create or refresh a compact in-repo review artifact documenting this decision.
Keep the review concise, operator-usable, and evidence-based.
```

### Prompt C — Revise Latest Change Task From Review
**When to use:** Only when a review decision for a change task is REVISE.

```text
Task Type: Change Task Revision (From Review Findings)

Revise the latest implementation change task using the latest review findings as authoritative input.

Required behavior:
1) Revise the implementation change itself, not the review artifact.
2) Treat review findings as required constraints unless explicitly superseded by canonical doctrine.
3) Preserve deterministic/execution-domain guardrails and existing doctrine constraints.
4) Update tests and documentation as needed to match the revised implementation.
5) Provide a clear summary mapping each review finding to the exact change that addressed it.

Output requirements:
- Produce unified diffs only.
- Keep scope tightly focused on addressing the review findings.
```

### Prompt D — Generate / Refresh Handoff
**When to use:** Before moving to a new chat/operator or ending a session.

```text
Task Type: Handoff / State Continuity

Generate or refresh the canonical handoff artifact for this repository.

Required handoff content:
- Current state snapshot
- Current active task
- Latest accepted changes
- Open risks / unresolved decisions
- Next recommended task
- Current workflow conventions in force
- Canonical file references needed for immediate continuation

Quality requirements:
- Keep handoff lean, current, and immediately operator-usable.
- Remove stale handoff clutter where safe.
- Preserve important historical context by cross-reference rather than silent deletion.

Output requirements:
- Produce unified diffs only.
```

### Prompt E — Doc Cleanup / Canonicalization Pass
**When to use:** When docs drift, bloat, conflict, or lose clear canonical structure.

```text
Task Type: Documentation Cleanup / Canonicalization

Inspect docs/ for redundancy, stale guidance, conflicting guidance, and non-canonical duplicate content.

Required actions:
1) Propose or implement a clear canon/secondary/archive structure.
2) Preserve useful historical documents where appropriate (archive or cross-reference).
3) Update documentation indexes and cross-links so operators can find canonical guidance quickly.
4) Do not remove important doctrine/history without replacement or traceable reference.

Output requirements:
- Produce unified diffs only.
- Keep edits minimal but decisive.
- Include a short rationale for structural changes.
```

### Prompt F — Diff-Based Review Fallback
**When to use:** Only when packet-first review basis is missing, stale, or insufficient.

```text
Task Type: Review Task (Diff-Based Fallback)

Packet-first review is unavailable or insufficient. Perform a direct raw-diff review of the latest change.

Required actions:
1) Review raw diffs directly and identify implementation intent, behavior impact, and risk.
2) Create a compact repo-grounded review packet artifact summarizing findings.
3) Explicitly state why packet-first review was insufficient in this case.
4) Classify decision: MERGE / REVISE / ABANDON.
5) Provide next-task prompt (copy/paste-ready).

Output requirements:
- For any repository artifact changes, produce unified diffs only.
- Keep the decision artifact concise and evidence-based.
```

### Prompt G — Mobile Minimal Review
**When to use:** On mobile where operator friction and token budget must stay low.

```text
Task Type: Review Task (Mobile Minimal)

Run a minimal-friction review suitable for mobile use.

Requirements:
1) Prefer the latest repo-generated review packet and canonical docs as primary basis.
2) Avoid requesting giant raw diffs unless absolutely necessary.
3) Return a very concise operator decision with explicit classification: MERGE / REVISE / ABANDON.
4) Include only the next immediate task prompt (copy/paste-ready), not a long plan.
5) If packet evidence is insufficient, state the minimum additional artifact needed.

Keep output brief, clear, and actionable.
```

### Prompt H — Desktop Full Review
**When to use:** On desktop when deeper inspection and broader consistency checks are practical.

```text
Task Type: Review Task (Desktop Full)

Perform a full review of the latest completed change task with deeper repository inspection.

Required review basis:
1) Inspect latest review packet first.
2) Inspect related canonical docs and any necessary supporting files.
3) Run a deeper consistency check across doctrine, roadmap, implementation, and review evidence.

Required output structure:
- What changed
- Why it changed
- Doctrine/roadmap consistency findings
- Risks/omissions/drift
- Decision: MERGE / REVISE / ABANDON
- Next best task
- Full next-task prompt (copy/paste-ready)

Keep the review structured, evidence-based, and operator-efficient.
```

## 7) Mobile vs Desktop Guidance

### Mobile
- Use packet-first review and canonical file paths.
- Prefer concise decisions + one immediate next prompt.
- Avoid giant pasted diffs unless packet-first basis is blocked.
- Focus on decision throughput and low setup overhead.

### Desktop
- Use packet + canonical docs + targeted file inspection.
- Use repo search/history/commit references to validate alignment.
- Produce richer evidence when needed, but keep operator output structured.
- Raw diffs remain fallback, not default.

## 8) Canonical Artifacts

Keep these current as the operating backbone:
- `PROJECT_STATE.md` — canonical project state snapshot.
- `docs/ROLLING_TODO.md` — active prioritized work queue.
- `docs/CODEX_HANDOFF_TEMPLATE.md` — standard continuity structure for session transitions.
- `docs/WORKFLOW_OPERATIONS.md` — this workflow operating model and reusable prompt library.
- `docs/review_packets/` — packet-first review evidence artifacts.
- `docs/INDEX.md` and `docs/DOCUMENTATION_INDEX.md` — navigation map for canonical doctrine and workflow docs.

## 9) Guardrails

- Do not conflate review tasks with change tasks.
- Do not treat chat history as higher authority than repository state.
- Do not default to raw pasted diffs when repo artifacts already provide basis.
- Do not remove deterministic/replay/execution-domain guardrails.
- Do not introduce ambiguity in merge decision ownership (merge decisions govern change tasks).

## 10) Recommended Next-Step Shortcut Phrases

- **“Run the state update task.”** → Use **Prompt A** in [Prompt A — State Update / Canonical Repo Update](#prompt-a--state-update--canonical-repo-update).
- **“Review the latest task using the repo-generated review packet.”** → Use **Prompt B** in [Prompt B — Review Latest Task Packet-First](#prompt-b--review-latest-task-packet-first).
- **“Revise the latest change task from the review.”** → Use **Prompt C** in [Prompt C — Revise Latest Change Task From Review](#prompt-c--revise-latest-change-task-from-review).
- **“Generate the handoff.”** → Use **Prompt D** in [Prompt D — Generate / Refresh Handoff](#prompt-d--generate--refresh-handoff).
- **“Run doc cleanup.”** → Use **Prompt E** in [Prompt E — Doc Cleanup / Canonicalization Pass](#prompt-e--doc-cleanup--canonicalization-pass).
- **“Run fallback diff review.”** → Use **Prompt F** in [Prompt F — Diff-Based Review Fallback](#prompt-f--diff-based-review-fallback).
- **“Run mobile minimal review.”** → Use **Prompt G** in [Prompt G — Mobile Minimal Review](#prompt-g--mobile-minimal-review).
- **“Run desktop full review.”** → Use **Prompt H** in [Prompt H — Desktop Full Review](#prompt-h--desktop-full-review).
