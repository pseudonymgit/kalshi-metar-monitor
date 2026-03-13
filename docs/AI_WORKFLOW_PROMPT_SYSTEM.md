# AI Workflow Prompt System

## Purpose
This system governs handoff -> implementation -> review -> revision -> merge.

Optimization goals:
- small surgical diffs
- deterministic scope control
- fewer revise/review loops
- branch/checkout awareness
- stale-premise detection
- no-op revision prevention
- trustworthy merge decisions
- operational debugging clarity

## Merge Vocabulary
- **MERGE**: target reality verified, scoped task solved, no blockers.
- **MERGE WITH MINOR FIXES**: target reality verified, no blockers; only tiny non-blocking edits remain.
- **REVISE**: target reality verified, but scoped blockers remain and implementation changes are required.
- **ABANDON**: target reality is not trustworthy, premise is stale, scope is wrong, or the review target is invalid.

## Core Workflow Diagnosis
- Prompts fail when they do not force reality checks at key seams.
- Every prompt type starts with a preflight gate.
- Every prompt type ends with a constrained output contract.
- Diagnosis and implementation are separate tasks.
- Review verifies target reality before judging content.

## Failure Modes to Prevent
- wrong-scope work
- wrong checkout / stale review
- fake no-op revision
- large diffs for tiny fixes
- technically detailed but operationally useless reviews
- preview-only vs delivery-capable confusion
- persisted vs live state confusion
- queue vs cache confusion
- requested vs attempted vs succeeded confusion

## Guardrails
- **Premise Staleness Rule**: re-validate branch, files, and assumptions before action.
- **Scope Ledger**: explicitly list allowed/disallowed files and honor it.
- **Review Finding Classification**: stable IDs by severity: **[B#]** blocker, **[M#]** minor, **[F#]** follow-up.
- **No Silent No-Op**: a no-op is allowed only when file-level evidence proves the requested fix is already present in verified target reality.
- **Wrong-Surface Suspicion Rule**: abort when requested outcome and touched surfaces do not align.
- **Route Capability Labeling for Ops/Debug**: label endpoints/actions as preview-only vs delivery-capable.
- **Review Target Receipt**: reviewer must echo exact commit/branch/files reviewed.

## Five-Slot Prompt Framework
Every serious prompt must define:
1. Task Type
2. Target Reality
3. Scope
4. Success Criteria
5. Output Contract

If any slot is missing, the prompt is under-specified.

## Reusable Prompt Templates

### Handoff Template
```md
TASK TYPE: HANDOFF
OUTPUT MODE: OPERATIONAL STATUS ONLY

TARGET REALITY
- Branch, commit, and active objective.

CURRENT TRUTH (VALIDATED)
- Solved:
  - <validated completed items only>
- Open blockers:
  - <validated unresolved items only>

KNOWN FAILURE PATTERNS
- <specific pitfalls seen in this workstream>

IMMEDIATE NEXT TASK
- <single next action>

SUCCESS CRITERIA
- solved/open lists reflect validated current truth.
- runtime-sensitive facts are clearly separated from facts that must be rechecked.
- immediate next task is singular and executable.

OUTPUT CONTRACT
- Report only operational status for next operator action.
- Preserve strict solved/open separation.
- Do not replay project lore.
- Do not reopen solved work without new contradictory evidence.
```

### Implementation Template
```md
TASK TYPE: IMPLEMENTATION
OUTPUT MODE: UNIFIED DIFF ONLY

TARGET REALITY
- Branch/commit and exact artifact to change.

SCOPE LEDGER
- ALLOWED FILES:
  - <paths>
- DISALLOWED WITHOUT EXPLICIT JUSTIFICATION:
  - <paths/surfaces>

PREFLIGHT (MANDATORY)
1. Confirm premise is still true in target reality.
2. Confirm issue is not already fixed.
3. Confirm target files/surfaces are correct and current.
4. Confirm requested outcome belongs in this surface.
5. Confirm scope ledger is satisfiable without broadening.
6. Confirm no runtime/test/config changes are required unless explicitly in scope.

IMPLEMENTATION RULES
- Make minimal, surgical edits.
- Preserve determinism and declared invariants.
- Abort on wrong-surface signals.

SUCCESS CRITERIA
- Required behavior/docs state achieved within allowed scope.

ABORT PATH
ABORT_REASON:
- wrong-surface request, or
- scope ledger violation required, or
- stale/ambiguous target reality
EVIDENCE:
- concise proof
```

### Review Template
```md
TASK TYPE: REVIEW
OUTPUT MODE: STRUCTURED REVIEW ONLY

TARGET VERIFICATION (MANDATORY BEFORE REVIEW)
- Verify checkout, branch, commit, diff source, files actually reviewed, scoped task match, and premise stale check.

STOP CONDITION
- If target reality is not trustworthy, stop and return ABANDON with evidence.

SUCCESS CRITERIA
- Review is grounded in verified target reality.
- Findings are actionable, scoped, and classified as [B#], [M#], or [F#].
- REVISE includes a narrow revision prompt tied only to blocker IDs.

REQUIRED OUTPUT
- REVIEW RECEIPT
- TARGET_VERIFICATION
- VERDICT: MERGE | MERGE WITH MINOR FIXES | REVISE | ABANDON
- SUMMARY
- BLOCKERS ([B#])
- MINOR FIXES ([M#])
- FOLLOW-UPS ([F#])
- MERGE BASIS
- REVISION PROMPT
```

### Revision Template
```md
TASK TYPE: REVISION
OUTPUT MODE: UNIFIED DIFF ONLY

TARGET REALITY
- Branch/commit and findings to address.

SCOPE LEDGER
- ALLOWED FILES:
  - <paths>
- DISALLOWED WITHOUT EXPLICIT JUSTIFICATION:
  - <paths/surfaces>

PREFLIGHT (MANDATORY)
1. Verify review receipt and finding IDs being addressed.
2. Verify target branch/commit still matches review basis.
3. Verify requested fixes fit allowed scope.

REVISION RULES
- Resolve only specified blocker findings ([B#]).
- Do not address minor fixes or follow-ups unless explicitly instructed.
- Do not introduce unrelated cleanup.
- Do not broaden scope.

SUCCESS CRITERIA
- Targeted findings resolved; no new scope introduced.

NO-OP RULE
- If requested revision produces no material diff, return explicit no-op with reason and evidence.

ABORT PATH
ABORT_REASON:
- stale premise, or
- wrong surface, or
- scope expansion required
EVIDENCE:
- concise proof
```

### Diagnostics-Only Template
```md
TASK TYPE: DIAGNOSTICS ONLY
OUTPUT MODE: FINDINGS ONLY

RULES
- No code or config changes.
- No remediation disguised as diagnostics.
- No patch text, pseudo-diff, or refactor plan.
- Use NEXT BEST TASK for follow-up only.

QUESTION
- <exact operational question>

TARGET REALITY
- Environment, branch/commit, and observed symptoms.

REQUIRED ANALYSIS
- Requested vs attempted vs succeeded.
- Persisted vs live state seams.
- Queue vs cache seams.
- Preview-only vs delivery-capable route/path labeling.

SUCCESS CRITERIA
- Produces operationally usable diagnosis and next action.

REQUIRED OUTPUT
- QUESTION
- FINDINGS
- SURFACES EXAMINED
- STATE-SEAM ANALYSIS
- CONCLUSION
- NEXT BEST TASK
```
