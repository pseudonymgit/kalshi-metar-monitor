# CODEX MASTER TEMPLATE (AUTH PERSISTED VIA SETUP SCRIPT)

==================================================
GLOBAL EXECUTION GOVERNANCE
==================================================

AUTHENTICATED PR MODE ONLY

All repository mutations MUST occur via Pull Request.
Execution operates under deterministic governance rules.

Agent behavior is PATCH-ONLY.
Conversation output is NOT authoritative.
Git diff output IS authoritative.

Git authentication is preconfigured via setup script.

DO NOT:
- read GITHUB_TOKEN
- modify credential helpers
- modify ~/.git-credentials
- attempt authentication changes

Authentication exists outside agent authority.

--------------------------------------------------

==================================================
MANDATORY OUTPUT CONTRACT (NON-NEGOTIABLE)
==================================================

ALL IMPLEMENTATION RESPONSES MUST:

1. Produce EXACTLY ONE unified git diff
2. Contained inside EXACTLY ONE triple-backtick code block
3. Begin with:
   diff --git a/
4. Contain NO prose before or after the block
5. Contain NO markdown headings
6. Contain NO explanations
7. Contain NO secondary code blocks

If these conditions are not satisfied:

→ RESPONSE IS INVALID  
→ DISCARD AND REGENERATE

Agent MUST internally validate output before responding.

--------------------------------------------------

OUTPUT VALIDATION CHECKLIST (SELF-ENFORCED)

Before returning response verify:

- One and only one code block exists
- Diff header present
- Patch applies cleanly
- No unrelated files modified
- No formatting-only edits introduced

If validation fails:
Regenerate silently.

--------------------------------------------------

==================================================
GIT PRECHECK (IDEMPOTENT + AUTH SAFE)
==================================================

Before making edits run:

if ! git remote | grep -q origin; then
  git remote add origin https://github.com/pseudonymgit/kalshi-metar-monitor.git
fi

git remote -v

if ! git ls-remote origin HEAD > /dev/null 2>&1; then
  echo "ERROR: git authentication failed or origin misconfigured."
  exit 1
fi

Rules:

- origin must exist
- authentication failure aborts execution
- credentials MUST NOT be modified

--------------------------------------------------

==================================================
BRANCH BASE SAFETY (MANDATORY)
==================================================

Before creating branch:

git checkout main
git pull origin main

One feature → one branch → one merge.
Never stack branches.

--------------------------------------------------

==================================================
BRANCH RULES
==================================================

Never commit directly to main.

Always create:

feature/<short-description>
fix/<short-description>
phase2/<short-description>
test/<short-description>

--------------------------------------------------

==================================================
PHASE 1 PROTECTION (ABSOLUTE LAW)
==================================================

The following are IMMUTABLE:

- core/metar_monitor.py behavior
- Alert semantics
- Integer floor-cross logic
- Station-local 11:00–19:00 window
- Station-local reset
- Scheduler lifecycle
- Thread idempotency
- requirements.txt

If modification appears required:

STOP.
Declare proposed Phase 1.x change.
Request confirmation.

No implicit mutation allowed.

--------------------------------------------------

==================================================
ARCHITECTURAL SAFETY RULES
==================================================

Agent MUST NOT:

- introduce probabilistic logic
- introduce ML behavior
- change ingestion ordering
- change transition emission logic
- alter replay equivalence
- introduce hidden side effects
- move execution authority boundaries

Observability MUST remain read-only.

--------------------------------------------------

==================================================
CHANGE DISCIPLINE
==================================================

Allowed:
- minimal targeted fixes
- bounded observability additions
- deterministic hardening

Forbidden:
- refactors
- stylistic rewrites
- renames
- dependency additions
- boot-flow mutation

Unless explicitly requested.

--------------------------------------------------

==================================================
KALSHI EXECUTION RULES
==================================================

Public API:
https://api.elections.kalshi.com/trade-api/v2

RSA MODE (DORMANT):

- No /trade-api/v2 duplication
- timestamp + METHOD + path + body
- milliseconds timestamp
- RSA PKCS1v15 + SHA256
- Base64 encoded
- Never expose key material

--------------------------------------------------

==================================================
PR CREATION FLOW
==================================================

After implementation:

git checkout -b <branch>
git add relevant files
git commit -m "<clear message>"
git push -u origin <branch>
gh pr create --fill
gh pr view --web

Return ONLY unified diff.

--------------------------------------------------

==================================================
MERGE AUTHORITY RULE
==================================================

No PR may merge unless reviewer states:

"Phase 1 semantics preserved."

--------------------------------------------------

==================================================
OPERATING MODE
==================================================

SOLO-DEV CONTROLLED RELEASE

Branch → PR → AI Review → Merge

No direct commits to main.

--------------------------------------------------

END OF TEMPLATE
