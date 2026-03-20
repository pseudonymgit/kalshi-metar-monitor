# AI Workflow Prompt System

## Purpose

This system governs a delivery loop:

`handoff -> implementation -> review -> revision -> merge`

It is optimized for:

* small surgical diffs
* deterministic scope control
* fewer review / revise loops
* branch / checkout awareness
* stale-premise detection
* no-op revision prevention
* trustworthy merge decisions
* operational debugging clarity

This system assumes a universal master workflow doc exists at:

`/docs/CODEX_MASTER_TEMPLATE.md`

This repository also defines a project-specific collaboration doctrine at:

`/docs/DAN_WORKING_STYLE.md`

Every real prompt should begin with:

```text
You must follow /docs/CODEX_MASTER_TEMPLATE.md.
You must follow /docs/DAN_WORKING_STYLE.md.
```

The master template holds common rules that apply across all prompts.
The Dan working style doc adds project-local collaboration defaults without changing the universal template.

This document defines the actual prompt system and task templates that the orchestrator or operator uses.

---

## Merge Vocabulary

Use these verdicts exactly:

* **MERGE**: target reality verified, scoped task solved, no blockers
* **MERGE WITH MINOR FIXES**: target reality verified, no blockers; only tiny non-blocking edits remain
* **REVISE**: target reality verified, but scoped blockers remain and implementation changes are required
* **ABANDON**: target reality is not trustworthy, premise is stale, scope is wrong, or the review target is invalid

Do not invent substitute verdict labels.

---

## Core Workflow Diagnosis

Failures usually happen when prompts do not force reality checks at the right seams.

The most common causes are:

* task premise is stale, but nobody stops
* the agent works in the wrong surface
* review judges the wrong checkout or stale branch
* revision drifts beyond blocker findings
* diagnostics quietly mutate into implementation
* unrelated work contaminates a mutation branch
* branch reality and conversation reality diverge

To prevent that:

* every prompt must define task type explicitly
* every prompt must define target reality explicitly
* every prompt must define scope explicitly
* every prompt must define success criteria explicitly
* every prompt must define output contract explicitly

If one of those is missing, the prompt is under-specified.

---

## Failure Modes to Prevent

This system is designed to reduce:

* wrong-scope work
* wrong checkout / stale review
* fake no-op revision
* large diffs for tiny fixes
* technically detailed but operationally useless reviews
* preview-only vs delivery-capable confusion
* persisted vs live state confusion
* queue vs cache confusion
* requested vs attempted vs succeeded confusion
* mixed-purpose branches
* review of the wrong target reality
* post-merge work incorrectly treated as revision of an old task

---

## Global Rules Assumed from the Master Template

The master template is assumed to enforce the following across all tasks:

* task type discipline
* target-reality verification
* stale-premise abort rule
* wrong-surface detection
* no silent no-op
* scope-ledger discipline
* review vocabulary
* blocker / minor / follow-up IDs
* mutation tasks return unified diff only
* diagnostics remain diagnostics-only
* review remains review-only
* PR-only mutation workflow
* Git operations only when mutation is required
* branch naming policy
* authority order

Because those live in the master template, the prompt templates below do not need to restate them in full.

---

## Wrong-Surface Detection

Before proposing a patch, Codex must verify that
the failing behavior originates from the inspected code.

Diagnostics must report:

FAILURE_SOURCE_CONFIDENCE
HIGH / MEDIUM / LOW

If confidence is LOW, additional inspection is required
before proposing modifications.

---

## Patch Discipline

All patches must satisfy:

* smallest possible change
* no refactors
* no formatting-only edits
* no unrelated logic changes

Default patch scope:

single function unless explicitly required.

---

## Diagnostic Output Structure

All diagnostics must return the following fields:

REPO_STATE
FAILURE_CAUSE
CODE_LOCATION
RAW_URL
MINIMAL_PATCH_REQUIRED

This structure ensures consistent orchestration review.

---

## Five-Slot Prompt Framework

Every serious prompt must define:

1. **Task Type**
2. **Target Reality**
3. **Scope**
4. **Success Criteria**
5. **Output Contract**

If any slot is missing, the prompt is under-specified.

Project-specific rules do **not** belong in the master template.
They belong in the real prompt, and project-specific collaboration doctrine can be attached by referencing `/docs/DAN_WORKING_STYLE.md`, under a section like:

* `PROJECT-SPECIFIC CONTEXT`
* `PROJECT GOVERNANCE / CONSTRAINTS`

That keeps the master universal and the task prompt grounded.

---

## Reusable Prompt Templates

## 1. Handoff Prompt

Use this when handing work to a new chat and you want continuity without replaying the entire project history.

```text
You must follow /docs/CODEX_MASTER_TEMPLATE.md.
You must follow /docs/DAN_WORKING_STYLE.md.

TASK TYPE: HANDOFF
OUTPUT MODE: OPERATIONAL STATUS ONLY

TARGET REALITY
- Branch: <branch>
- Commit: <commit>
- Status validated at: <timestamp/reference>
- Active objective: <single current objective>

PROJECT-SPECIFIC CONTEXT
- Repository: <repo name>
- Relevant governance / architecture references:
  - <doc/path>
  - <doc/path>
- Governing constraints:
  - <constraint>
  - <constraint>
  - <constraint>

CURRENT TRUTH (VALIDATED)
- Solved:
  - <validated completed item>
  - <validated completed item>
- Open blockers:
  - [B1] <validated unresolved blocker>
  - [B2] <validated unresolved blocker>

IMMEDIATE NEXT TASK
- <single next action only>

PROJECT OVERVIEW
- Project: <name>
- Purpose: <1-2 sentence operational purpose>
- Success condition:
  - <what done means at project level>

ROADMAP
- Current phase: <phase>
- Priority queue:
  1. <active item>
  2. <next item>
  3. <later item>
- Rule: work only on item 1 unless explicitly redirected.

KNOWN FAILURE PATTERNS
- <specific stale-premise risk>
- <specific wrong-scope risk>
- <specific checkout/review risk>
- <specific runtime-vs-persisted truth risk, if relevant>

SCOPE NOTES
- Allowed files/surfaces for immediate next task: <paths or n/a>
- Disallowed without explicit justification: <paths or n/a>

STALE-PREMISE WARNINGS
- <fact that must be rechecked>
- <fact that must not be assumed>

SUCCESS CRITERIA
- Solved/open lists reflect validated current truth.
- Runtime-sensitive facts are clearly separated from facts requiring recheck.
- Immediate next task is singular and executable.
- Overview and roadmap guide execution without expanding scope.

OUTPUT CONTRACT
- Return only operational status blocks.
- Do not replay project lore.
- Do not reopen solved work without new contradictory evidence.
- Do not broaden scope.
```

---

## 2. Implementation Prompt

Use this for a new scoped change on current reality.

```text
You must follow /docs/CODEX_MASTER_TEMPLATE.md.

TASK TYPE: IMPLEMENTATION
OUTPUT MODE: UNIFIED DIFF ONLY

TARGET REALITY
- Branch/commit to modify: <branch>@<commit or base>
- Target artifact/surface: <file(s), endpoint, doc, test, etc.>
- Task premise: <claim that must be true before patching>

PROJECT-SPECIFIC CONTEXT
- Repository: <repo name>
- Relevant governance / architecture references:
  - <doc/path>
  - <doc/path>
- Immutable invariants / protected semantics:
  - <rule>
  - <rule>
- Protected files/surfaces:
  - <path>
  - <path>

SCOPE
- Goal: <exact narrow change>
- In scope:
  - <specific behavior / artifact>
  - <specific behavior / artifact>
- Out of scope:
  - <explicitly excluded area>
  - <explicitly excluded area>

SCOPE LEDGER
- ALLOWED FILES:
  - <path>
  - <path>
- DISALLOWED WITHOUT EXPLICIT JUSTIFICATION:
  - <path/surface>
  - <path/surface>

SUCCESS CRITERIA
- <explicit condition>
- <explicit condition>
- No unrelated files changed.
- Patch remains minimal and scoped.

OUTPUT CONTRACT
- Return unified diff only.
- No prose unless aborting.

TASK INSTRUCTIONS
- Make the minimum number of lines change necessary.
- Preserve deterministic behavior and stated invariants.
- Do not refactor.
- Do not perform unrelated cleanup.
- Do not broaden scope.
- Prefer a test-only or docs-only patch when sufficient.

ABORT PATH
If preflight fails, return exactly:

ABORT_REASON:
- <reason>

EVIDENCE:
- <concise proof>
```

---

## 3. Review Prompt

Use this to judge whether an existing diff or branch is merge-ready.

```text
You must follow /docs/CODEX_MASTER_TEMPLATE.md.

TASK TYPE: REVIEW
OUTPUT MODE: STRUCTURED REVIEW ONLY

TARGET REALITY
- Review target branch: <branch>
- Review target commit or diff source: <commit / PR / diff reference>
- Scoped task under review: <single task description>

PROJECT-SPECIFIC CONTEXT
- Repository: <repo name>
- Relevant governance / architecture references:
  - <doc/path>
  - <doc/path>
- Immutable invariants / protected semantics:
  - <rule>
  - <rule>

SCOPE
- Review only:
  - <file/surface>
  - <file/surface>
- Do not review:
  - <out-of-scope area>
  - <out-of-scope area>

FILES EXPECTED IN SCOPE
- <path>
- <path>

SUCCESS CRITERIA
- Review is grounded in verified target reality.
- Review covers only the scoped task/files.
- Findings are actionable and classified as [B#], [M#], or [F#].
- REVISE includes a narrow revision prompt tied only to blocker IDs.

OUTPUT CONTRACT
- Return structured review only.
- Do not patch code.
- Do not generate unified diff.

REQUIRED REVIEW CHECKS
1. Verify target reality before judging content.
2. Confirm scoped files match the requested task.
3. Check for scope creep.
4. Check that stated invariants were preserved.
5. Distinguish blockers from minor fixes and follow-ups.
6. Stop with ABANDON if target reality is not trustworthy.

REQUIRED OUTPUT
REVIEW RECEIPT
- checkout: <...>
- branch: <...>
- commit: <...>
- diff source: <...>
- files actually reviewed: <...>

TARGET_VERIFICATION
- target present: yes/no
- scoped task match: yes/no
- premise stale: yes/no

VERDICT
- MERGE
or
- MERGE WITH MINOR FIXES
or
- REVISE
or
- ABANDON

SUMMARY
- <concise operational summary>

BLOCKERS
- [B1] <issue>
- [B2] <issue>

MINOR FIXES
- [M1] <issue>
- [M2] <issue>

FOLLOW-UPS
- [F1] <issue>
- [F2] <issue>

MERGE BASIS
- <why this verdict follows>

REVISION PROMPT
- <required only when VERDICT is REVISE>
```

---

## 4. Revision Prompt

Use this when review findings exist and you want only those blockers fixed.

```text
You must follow /docs/CODEX_MASTER_TEMPLATE.md.

TASK TYPE: REVISION
OUTPUT MODE: UNIFIED DIFF ONLY

TARGET REALITY
- Branch/commit to modify: <branch>@<commit or base>
- Source review receipt: <review reference>
- Source blocker IDs to fix: <[B1], [B2], ...>

PROJECT-SPECIFIC CONTEXT
- Repository: <repo name>
- Relevant governance / architecture references:
  - <doc/path>
  - <doc/path>
- Immutable invariants / protected semantics:
  - <rule>
  - <rule>

SCOPE
- Fix only these blocker findings:
  - [B1] <issue>
  - [B2] <issue>
- Explicitly out of scope:
  - <minor fix not requested>
  - <follow-up not requested>
  - <unrelated cleanup>

SCOPE LEDGER
- ALLOWED FILES:
  - <path>
  - <path>
- DISALLOWED WITHOUT EXPLICIT JUSTIFICATION:
  - <path/surface>
  - <path/surface>

SUCCESS CRITERIA
- All listed blocker IDs are resolved.
- No unrelated changes are introduced.
- No minor fixes or follow-ups are addressed unless explicitly instructed.
- Patch remains minimal and scoped.

OUTPUT CONTRACT
- Return unified diff only.
- No prose unless aborting.

REVISION RULES
- Resolve only specified blocker findings ([B#]).
- Do not reopen solved areas.
- Do not broaden scope.
- Do not introduce unrelated cleanup.
- Do not address minor fixes or follow-ups unless explicitly instructed.

NO-OP RULE
- If no patch is needed, prove why with file-level evidence in the abort format.

ABORT PATH
If preflight fails, return exactly:

ABORT_REASON:
- <reason>

EVIDENCE:
- <concise proof>
```

---

## 5. Diagnostics-Only Prompt

Use this when truth is unclear and you need to understand what is happening before deciding whether code should change.

```text
You must follow /docs/CODEX_MASTER_TEMPLATE.md.

TASK TYPE: DIAGNOSTICS ONLY
OUTPUT MODE: FINDINGS ONLY

TARGET REALITY
- Branch/commit or environment under diagnosis: <target>
- Observed symptom: <exact symptom/question>

PROJECT-SPECIFIC CONTEXT
- Repository: <repo name>
- Relevant governance / architecture references:
  - <doc/path>
  - <doc/path>
- Relevant invariants / operating assumptions:
  - <rule>
  - <rule>

SCOPE
- Diagnose only:
  - <surface/question>
  - <surface/question>
- Do not implement:
  - code changes
  - config changes
  - refactor plans

SUCCESS CRITERIA
- Identify the actual seam of disagreement or uncertainty.
- Surfaces examined are explicit.
- Conclusion distinguishes confirmed cause from remaining uncertainty.
- Output remains diagnostic-only.
- Next best task is singular and actionable.

OUTPUT CONTRACT
- Return findings only.
- No code changes.
- No patch text.
- No pseudo-diff.
- No remediation disguised as diagnostics.

DIAGNOSTIC OUTPUT STRUCTURE
- REPO_STATE
- FAILURE_CAUSE
- CODE_LOCATION
- RAW_URL
- MINIMAL_PATCH_REQUIRED
- FAILURE_SOURCE_CONFIDENCE

QUESTION
- <exact operational question>

REQUIRED ANALYSIS
- requested vs attempted vs succeeded, if relevant
- persisted vs live state, if relevant
- queue vs cache seam, if relevant
- preview-only vs delivery-capable route/path labeling, if relevant
- exact surfaces/functions/files involved

REQUIRED OUTPUT
QUESTION
- <restated question>

SURFACES EXAMINED
- <file/function/route>
- <file/function/route>

FINDINGS
- [F1] <finding>
- [F2] <finding>
- [F3] <finding>

STATE-SEAM ANALYSIS
- <where truth diverges or becomes unclear>

CONCLUSION
- <most likely current explanation>

NEXT BEST TASK
- <single next action>
```

---

## Prompt Selection Rules

Use:

* **Handoff** for moving work to a new chat
* **Implementation** for a new scoped change on current reality
* **Review** for judging an existing diff / branch / PR
* **Revision** for fixing only review blockers
* **Diagnostics-only** for determining current truth before deciding whether change is needed

Important distinctions:

* post-merge follow-up work is a **new implementation**, not a revision
* diagnostics is not implementation
* review is not implementation
* revision is not a new feature task
* mixed-purpose branches should be repaired before serious review

---

## Practical Operating Rule

Each real prompt should stay lean because `/docs/CODEX_MASTER_TEMPLATE.md` already carries the universal rules.

So the real prompt should focus on:

* current task type
* current target reality
* project-specific context
* actual scope
* success criteria
* output contract

That is the working system.

If you want, I can also give you a **shorter, more token-efficient version** of this same AI Workflow Prompt System that is easier to store as a repo doc.
