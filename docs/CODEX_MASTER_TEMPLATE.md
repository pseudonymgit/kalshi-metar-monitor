CODEX MASTER TEMPLATE (EPHEMERAL-SAFE VERSION)

AUTHENTICATED PR MODE ONLY

All changes must go through a Pull Request.

A GITHUB_TOKEN secret is available in the runtime.

⸻

GIT REMOTE & AUTH HANDLING (EPHEMERAL SAFE)

Check remote:

git remote -v

If no origin exists:

git remote add origin https://github.com/pseudonymgit/kalshi-metar-monitor.git

Configure authenticated HTTPS remote:

git remote set-url origin https://x-access-token:${GITHUB_TOKEN}@github.com/pseudonymgit/kalshi-metar-monitor.git

Verify authentication:

git ls-remote origin HEAD

If git ls-remote fails:
Abort and report exact error.

Do NOT echo or print GITHUB_TOKEN.
Do NOT write token to disk.
Do NOT expose token in logs.

⸻

GITHUB AUTHENTICATION RULES
	•	Use GITHUB_TOKEN for authenticated operations.
	•	Do NOT print or expose the token.
	•	Do NOT echo environment variables.
	•	Do NOT write the token to disk.
	•	Do NOT include token in logs or PR descriptions.
	•	If push fails due to auth → abort and report exact error.

Preferred flow:
	•	Create new branch
	•	Commit changes
	•	git push -u origin 
	•	gh pr create –fill
	•	gh pr view –web
	•	Provide PR URL

Never commit directly to main.

⸻

PHASE 1 PROTECTION RULES (NON-NEGOTIABLE)

Do NOT modify:
	•	core/metar_monitor.py
	•	Alert semantics
	•	Integer floor-cross logic
	•	Station-local 11:00–19:00 window
	•	Daily reset logic
	•	Scheduler idempotency behavior
	•	Thread lifecycle logic
	•	requirements.txt (unless explicitly instructed)

If Phase 1 logic must change:
	•	Explicitly state that this is a Phase 1.x update
	•	Wait for confirmation before implementing

⸻

KALSHI RULES (WHEN APPLICABLE)

Environment variables:
	•	KALSHI_BASE_URL (RSA mode)
	•	KALSHI_KEY_ID
	•	KALSHI_KEY_PEM
	•	KALSHI_PUBLIC_BASE_URL (optional override)

Important:

If using RSA mode:
	•	Do NOT include /trade-api/v2 in helper path
	•	Signature format:
timestamp + METHOD + path + body
	•	Timestamp must be milliseconds
	•	Signing:
RSA
PKCS1v15
SHA256
Base64 encoded

Never expose key material.

Public mode default base:
https://api.elections.kalshi.com/trade-api/v2

⸻

CHANGE DISCIPLINE
	•	Minimal diff only
	•	No unrelated refactoring
	•	No formatting-only commits
	•	No renaming unless explicitly requested
	•	No dependency additions unless explicitly requested
	•	No boot behavior changes unless explicitly requested

⸻

PR RETURN FORMAT (MANDATORY)

Return:
	1.	Unified diff
	2.	Confirmation Phase 1 files untouched
	3.	Confirmation no alert semantics changed
	4.	Branch name
	5.	PR URL
	6.	Render curl test command

⸻

MERGE RULE

No PR gets merged unless ChatGPT signs off with:

“Phase 1 semantics preserved.”

⸻

OPERATING MODE

SOLO-DEV CONTROLLED RELEASE

All changes:
	•	Branch → PR → AI review → Merge

No direct main commits.

⸻

END OF TEMPLATE
