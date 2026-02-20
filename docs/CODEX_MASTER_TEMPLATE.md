# CODEX MASTER TEMPLATE (AUTH PERSISTED VIA SETUP SCRIPT)

AUTHENTICATED PR MODE ONLY
All changes must go through a Pull Request.

Git authentication is preconfigured in the setup script via credential helper.
Do NOT attempt to read or use GITHUB_TOKEN during agent execution.

---

GIT PRECHECK (IDEMPOTENT + AUTH SAFE)

Before making any edits, run:

# Ensure origin exists (safe to run every task)
if ! git remote | grep -q origin; then
  git remote add origin https://github.com/pseudonymgit/kalshi-metar-monitor.git
fi

# Show remotes for visibility
git remote -v

# Verify authenticated access
if ! git ls-remote origin HEAD > /dev/null 2>&1; then
  echo "ERROR: git authentication failed or origin misconfigured."
  exit 1
fi

Rules:

If origin is missing, add it automatically.

If git ls-remote origin HEAD fails, abort and report exact error.

Do NOT modify credential helper.

Do NOT modify ~/.git-credentials.

Do NOT reference or use GITHUB_TOKEN during agent execution.

Authentication is handled by setup script and must not be reconfigured here.

BRANCH RULES

Never commit directly to main.

Always create a new branch.

Branch naming format:

feature/<short-description>

fix/<short-description>

phase2/<short-description>

test/<short-description> (allowed for pipeline validation only)

---

PHASE 1 PROTECTION (NON-NEGOTIABLE)

Do NOT modify:

* core/metar_monitor.py
* Alert semantics
* Integer floor-cross logic
* Station-local 11:00–19:00 window
* Station-local daily reset
* Scheduler idempotency
* Thread lifecycle
* requirements.txt (unless explicitly requested)

If Phase 1 must change:

* Explicitly declare Phase 1.x
* Stop and request confirmation before proceeding

---

KALSHI RULES

Public API default:
[https://api.elections.kalshi.com/trade-api/v2](https://api.elections.kalshi.com/trade-api/v2)

RSA mode (dormant) rules:

* Do NOT include /trade-api/v2 in helper path
* Signature format:
  timestamp + METHOD + path + body
* Timestamp in milliseconds
* RSA PKCS1v15 + SHA256
* Base64 encoded
* Never expose key material

---

CHANGE DISCIPLINE

* Minimal diff only
* No formatting-only commits
* No refactoring unrelated code
* No renaming unless explicitly requested
* No dependency additions unless explicitly requested
* No boot behavior changes unless explicitly requested

---

PR CREATION FLOW

After implementing changes:

1. git checkout -b <branch-name>
2. git add relevant files
3. git commit -m "<clear message>"
4. git push -u origin <branch-name>
5. gh pr create --fill
6. gh pr view --web

Return:

1. Unified diff
2. Confirmation Phase 1 files untouched
3. Confirmation no alert semantics changed
4. Branch name
5. PR URL
6. Render curl test command

---

MERGE RULE

No PR gets merged unless ChatGPT signs off with:

“Phase 1 semantics preserved.”

---

OPERATING MODE

SOLO-DEV CONTROLLED RELEASE

All changes:
Branch → PR → AI review → Merge

No direct commits to main.

---

END OF TEMPLATE

---
