# Template Drift Audit — 2026-03-21

## Scope

Inspected only:
- `docs/AI_WORKFLOW_PROMPT_SYSTEM.md`
- `docs/CODE_CHANGE_RETURN_PACKET.md`
- `docs/DAN_WORKING_STYLE.md`
- task-time reference copies of:
  - `docs/CODEX_MASTER_TEMPLATE.md`
  - `docs/DAN_WORKING_STYLE.md`

Task type: diagnostics-only, read-only audit of template consistency after the CODE_CHANGE_RETURN_PACKET wiring.

## Repository Reality

- Branch: `work`
- Commit: `d1680b403cd7af7a6027468ae1bb69af6f776e41`

## Question

Is there any remaining prompt-template drift in `docs/AI_WORKFLOW_PROMPT_SYSTEM.md` after the packet wiring, especially for operator-facing repo-report/diagnostics tasks?

## Short Answer

Yes. The packet doctrine is **not fully wired** yet.

There is still **one missing reusable template** for operator-facing repo-report diagnostics, and `AI_WORKFLOW_PROMPT_SYSTEM.md` still contains **legacy output-contract text that competes with the packet** when diagnostics/review returns are meant to be operator-facing.

## Findings

### F1 — Diagnostics template still has a competing legacy output contract

`docs/AI_WORKFLOW_PROMPT_SYSTEM.md` correctly says to use `/docs/CODE_CHANGE_RETURN_PACKET.md` when a diagnostics return must be reviewable/operator-facing, but it immediately keeps the legacy diagnostics structure as the template's required output.

Why this is drift:
- The diagnostics template says both:
  - use the packet when the return must be reviewable/operator-facing
  - return findings only / use the legacy diagnostics structure
- The legacy required output block (`QUESTION`, `SURFACES EXAMINED`, `FINDINGS`, `STATE-SEAM ANALYSIS`, `CONCLUSION`, `NEXT BEST TASK`) remains the only concrete reusable diagnostics template in the file.
- That makes packet usage advisory rather than structurally authoritative for operator-facing diagnostics/repo-report tasks.

Operational effect:
- An agent can plausibly produce the old findings schema and omit packet fields like `REPORT_ARTIFACT`, `RAW_URLS`, or packet-style repo state framing even when the operator clearly wants a reviewable repo report.

## F2 — Review template has the same packet-vs-legacy competition

The review template also says to use `/docs/CODE_CHANGE_RETURN_PACKET.md` for a reviewable/operator-facing return, but it still defines a full legacy `REQUIRED OUTPUT` schema as the concrete template.

Why it matters here:
- This is the same ambiguity pattern seen in diagnostics.
- Even though the current audit is focused on repo-report/diagnostics use, the same wording style confirms that the packet doctrine is still attached as an overlay rather than replacing the old operator-facing reusable template.

Operational effect:
- Reviewable outputs can still drift back to the pre-packet review receipt format instead of the packet.

## F3 — The packet was broadened into handoff, which exceeds the packet's own stated scope

`docs/CODE_CHANGE_RETURN_PACKET.md` defines itself as the canonical operator-facing return packet for **code-change and repo-report tasks**.

But `docs/AI_WORKFLOW_PROMPT_SYSTEM.md` also instructs handoff prompts to use the packet when the handoff must be reviewable/operator-facing.

Why this is drift:
- That broadens packet applicability beyond the packet doc's own scope statement.
- It creates unnecessary ambiguity about whether the packet is meant for all operator-facing non-mutation tasks or specifically code-change/repo-report tasks.

Operational effect:
- The packet doctrine is no longer scoped cleanly.
- Operators may infer that any operator-facing status output should use the packet, even though the packet is documented more narrowly.

## F4 — Implementation and revision templates were not accidentally broadened

No remaining drift was found in the mutation-only reusable templates.

- `Implementation` still requires `UNIFIED DIFF ONLY`.
- `Revision` still requires `UNIFIED DIFF ONLY`.
- Neither template was broadened to packet-style output.

This part of the wiring is consistent with the master-template mutation rules.

## Assessment

### Is the reviewable packet doctrine fully wired?

No.

### What is still missing?

One reusable template is still missing:
- a **dedicated operator-facing repo-report / diagnostics template** inside `docs/AI_WORKFLOW_PROMPT_SYSTEM.md` that makes the packet the concrete output contract instead of an optional overlay on top of the legacy findings schema.

Without that reusable template, operator-facing diagnostics tasks still have two plausible output shapes:
1. the legacy findings structure
2. the packet

That is the remaining template drift.

## Recommended Next Action

Add **one dedicated reusable "Repo-Report / Operator Diagnostics" template** to `docs/AI_WORKFLOW_PROMPT_SYSTEM.md` that explicitly uses `/docs/CODE_CHANGE_RETURN_PACKET.md` as the concrete output contract, and in the existing diagnostics/review templates clarify that the legacy required-output blocks apply only when a packet is **not** requested.

## Bottom Line

The packet doctrine is **partially wired but not fully normalized**.

The main remaining issue is not mutation-template drift; it is that operator-facing diagnostics/repo-report work still lacks a single concrete reusable packet-first template, so legacy findings/review schemas can still win by default.
