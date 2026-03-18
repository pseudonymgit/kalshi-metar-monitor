# CODEX_MASTER_TEMPLATE.md

This document defines common workflow rules that apply to every prompt.

Every real prompt should begin with:

You must follow /docs/CODEX_MASTER_TEMPLATE.md.

This master template contains only universal workflow governance.
Project-specific constraints must be supplied in the task prompt itself.

---

## 1. Task Type Discipline

Every task must be explicitly treated as one of:

- HANDOFF
- DIAGNOSTICS ONLY
- ANALYSIS
- REVIEW
- IMPLEMENTATION
- REVISION

If task type is ambiguous, stop and report the ambiguity.
Do not guess.

Do not blur task modes.

Examples:
- diagnostics is not implementation
- review is not implementation
- revision is not a new feature task
- post-merge follow-up work is a new implementation task, not a revision of an old merged patch

---

## 2. Target Reality Rule

Before acting, verify target reality.

Target reality means the actual current branch, commit, file, diff, artifact, or environment the task refers to.

Conversation history is contextual, not authoritative.
Repository state, inspected files, diffs, and current branch reality are authoritative.

Do not proceed on assumed branch state, assumed file state, or assumed task continuity.

---

## 2A. Repository Reference Rule

When referencing repository code, always include:

- file path
- line number range (if applicable)
- raw GitHub URL

Raw URL format:

https://raw.githubusercontent.com/<owner>/<repo>/<branch-or-commit>/<path>

Example:

core/metar_monitor.py
lines 1700–1820

RAW_URL
https://raw.githubusercontent.com/pseudonymgit/kalshi-metar-monitor/main/core/metar_monitor.py

Full files must not be pasted unless explicitly requested.

---

## 2B. Repository State Verification

All diagnostics must report repository state.

Return:

REPO_STATE
branch
commit hash

Example:

REPO_STATE
branch: work
commit: a4d9c108e4af0377a6971f6ba30ea4cbb5e5be7f

This prevents stale-premise reasoning.

---

## 3. Stale Premise Rule

Abort if the prompt premise is stale.

A premise is stale if any of the following are true:

- target files no longer exist
- referenced code or docs moved
- repository state contradicts the prompt assumptions
- the requested fix is already present
- a prior merge changed the relevant surface
- the requested review target is no longer the active target reality
- the task was framed against an older branch reality that has since changed

If stale:
- stop
- report that the premise is stale
- provide concise evidence

Do not continue on stale assumptions.

---

## 4. Wrong-Surface Rule

Verify that the requested outcome belongs in the surface being changed.

Examples:
- docs task must not drift into runtime code
- test task must not silently redesign application behavior
- route contract task must not patch unrelated core semantics
- review task must not mutate code
- diagnostics task must not smuggle in remediation

If the requested outcome belongs in a different surface than the one being changed:
- stop
- report wrong-surface mismatch
- do not patch the nearest plausible file

---

## 5. No Silent No-Op Rule

A no-op is allowed only when file-level evidence proves the requested change is already present in verified target reality.

If returning no-op, provide:
- file inspected
- relevant lines, sections, or surfaces examined
- why the change is unnecessary

Do not claim “already fixed” without proof.

---

## 6. Scope Ledger Rule

Maintain strict scope discipline.

For mutation tasks, track:
- files inspected
- files modified
- files intentionally untouched

Do not modify files outside declared allowed scope unless the task prompt explicitly permits it and the reason is stated.

Minimal, scoped changes are preferred over broad or “helpful” rewrites.

---

## 7. Change Discipline

Default allowed:
- minimal targeted fixes
- narrow documentation updates
- deterministic hardening
- bounded test additions
- scoped review-driven revisions

Default forbidden unless explicitly requested:
- refactors
- stylistic rewrites
- broad cleanup
- renames
- dependency changes
- architecture redesign
- behavior changes outside the scoped task
- unrelated edits in nearby files

Minimal patch beats clever patch.

---

## 8. Output Mode Policy

Output mode depends on task type.

### Mutation tasks
Mutation tasks must return unified diff only.

Mutation tasks include:
- IMPLEMENTATION
- REVISION
- docs-only patch tasks
- test-only patch tasks

### Non-mutation tasks
Non-mutation tasks follow task-defined output format.

Typical defaults:
- HANDOFF -> operational status only
- DIAGNOSTICS ONLY -> findings only
- ANALYSIS -> explanation allowed
- REVIEW -> structured review only

Do not return unified diff for diagnostics or review unless explicitly asked.

---

## 9. Unified Diff Rule for Mutation Tasks

For mutation tasks:

- return exactly one unified diff
- no prose before or after the diff unless the prompt explicitly allows abort output
- no second diff
- no extra code blocks
- no explanations outside the diff

The patch must be:
- scoped
- minimal
- free of unrelated file changes
- free of formatting-only drift unless formatting is the task

If a mutation task cannot be completed cleanly, use the task’s abort format instead of inventing partial prose.

---

## 10. Diagnostics Separation Rule

Diagnostics tasks must not:
- modify code
- modify config
- produce patch text
- produce pseudo-diff
- produce refactor plans disguised as findings
- silently convert into implementation work

Diagnostics outputs should identify:
- the exact question
- the surfaces examined
- the seam of disagreement or uncertainty
- the most likely current explanation
- the next best task

---

## 11. Review Protocol

Review tasks must:

- verify target reality before judging content
- identify the exact checkout, branch, commit, diff source, and files reviewed
- stop with ABANDON if target reality is not trustworthy
- distinguish blockers, minor fixes, and follow-ups
- remain scoped to the requested task
- include a narrow revision prompt when verdict is REVISE

Review tasks must not:
- patch code
- review the whole repo unless explicitly asked
- continue on stale branch assumptions
- judge old branch reality as if it were current

---

## 12. Review Vocabulary

Use these verdicts exactly:

- MERGE
- MERGE WITH MINOR FIXES
- REVISE
- ABANDON

Definitions:

- **MERGE**: target reality verified, scoped task solved, no blockers
- **MERGE WITH MINOR FIXES**: target reality verified, no blockers; only tiny non-blocking edits remain
- **REVISE**: target reality verified, but scoped blockers remain and implementation changes are required
- **ABANDON**: target reality is not trustworthy, premise is stale, scope is wrong, or the review target is invalid

Do not invent substitute verdict labels.

---

## 13. Finding Classification

Use stable finding IDs:

- blockers: `[B1]`, `[B2]`, ...
- minor fixes: `[M1]`, `[M2]`, ...
- follow-ups: `[F1]`, `[F2]`, ...

Revisions should chain only from blocker IDs unless explicitly instructed otherwise.

Do not blur blockers with follow-ups.

---

## 14. Revision Discipline

Revision tasks must:

- verify the review receipt being addressed
- verify blocker IDs still apply
- fix only specified blocker findings unless explicitly instructed otherwise
- avoid unrelated cleanup
- remain minimal and scoped

Revision tasks must not:
- reopen solved areas
- re-solve the wrong issue
- address minor fixes or follow-ups unless explicitly instructed
- drift into new implementation scope

---

## 15. PR-Only Mutation Policy

All repository mutation must occur via branch -> PR -> merge.

Never commit directly to main.
Never mutate repository state outside PR workflow.

This applies even to:
- docs changes
- test-only changes
- small fixes
- operational patches

Emergency work still follows:
branch -> PR -> merge

---

## 16. Git / GitHub Command Policy

Git operations are allowed only when the task requires repository mutation.

### Non-mutation tasks
Do not:
- run git setup
- create branches
- push
- create PRs

### Mutation tasks
Git and PR operations are allowed when needed to complete the task.

Do not:
- modify credentials
- read secrets
- alter auth configuration
- change repository remotes unless explicitly required

Do not perform Git operations just because they exist in the workflow. Use them only when task type requires mutation.

---

## 17. Mutation Workflow Policy

When repository mutation is required:

1. verify current repository state
2. branch from current main or designated base
3. apply only the scoped change
4. commit only the scoped files
5. push the branch
6. open or update the PR
7. preserve clear review trace

One task -> one branch -> one PR -> one merge

Do not combine unrelated tasks on one branch.

---

## 18. Branch Naming Policy

Use one of these prefixes:

- `feature/`
- `fix/`
- `docs/`
- `review/`
- `ops/`
- `test/`
- `phase2/`

Use the narrowest truthful prefix.

Examples:
- `fix/send-test-alert-blocking-diagnostics`
- `docs/ai-workflow-prompt-system`
- `test/endpoint-deliver-false-coverage`

---

## 19. Authority Order

When instructions interact, use this precedence:

1. system / platform rules
2. this master template
3. task prompt
4. project-specific context inside the task prompt

Do not treat project-specific context as universal.
Do not treat task-specific wishes as permission to violate this master template.

---

## 20. Requirement for Real Prompts

Every real prompt must still define:

- Task Type
- Target Reality
- Scope
- Success Criteria
- Output Contract

If any of those are missing, the prompt is under-specified.

Project-specific constraints must be supplied in the real prompt itself.
Do not assume repository doctrine from this master template.

---

## 21. Practical Operating Rule

Use this master template for universal workflow rules only.

Put these in the real prompt when relevant:
- repository name
- branch / commit
- project overview
- roadmap
- protected files
- architecture references
- domain invariants
- project-specific governance
- allowed/disallowed files for the task
- current blockers
- current objective

This file defines constants.
The real prompt supplies current conditions.
