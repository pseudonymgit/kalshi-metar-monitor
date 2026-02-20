# CODEX MASTER TEMPLATE (AUTH PERSISTED VIA SETUP SCRIPT)

AUTHENTICATED PR MODE ONLY
All changes must go through a Pull Request.

Git authentication is preconfigured in the setup script via credential helper.
Do NOT attempt to read or use GITHUB_TOKEN during agent execution.

---

GIT PRECHECK (FINAL VERSION)

Check if origin exists:

git remote | grep -q origin || git remote add origin https://github.com/pseudonymgit/kalshi-metar-monitor.git

Verify access:

git ls-remote origin HEAD

If ls-remote fails → abort and report exact error.

Do NOT touch credential helper.
Do NOT touch ~/.git-credentials.
Do NOT reference GITHUB_TOKEN.---

BRANCH RULES

* Never commit directly to main.
* Always create a new branch.
* Branch naming format:
  feature/<short-description>
  fix/<short-description>
  phase2/<short-description>

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

You now have:

• Setup-script-based authentication
• Persistent git auth
• Clean agent behavior
• Locked Phase 1
• Controlled PR workflow
