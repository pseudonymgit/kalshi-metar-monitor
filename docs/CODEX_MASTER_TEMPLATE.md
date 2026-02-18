IMPORTANT — NATIVE PR MODE ONLY

All changes must be made on a new branch and opened as a Pull Request
using Codex’s native GitHub integration (make_pr or equivalent).

DO NOT:
- Modify or commit directly to main
- Use unauthenticated shell git push
- Attempt manual gh auth
- Rewrite the git remote URL
- Persist credentials
- Expose any secrets

If branch creation or PR creation fails,
abort and report the exact error.

----------------------------------------------------------------
GITHUB WORKFLOW REQUIREMENTS
----------------------------------------------------------------

- Create a new branch.
- Commit changes to that branch.
- Push via native Codex GitHub integration.
- Create a real GitHub Pull Request.
- Return the PR URL.
- Confirm the branch exists remotely.

If PR metadata is recorded locally but no PR appears in GitHub UI,
treat this as a failure and report it.

----------------------------------------------------------------
PROJECT GUARDRAILS
----------------------------------------------------------------

- Do NOT modify Phase 1 alert semantics.
- Refer to docs/PHASE1.md and docs/ARCHITECTURE_GUARDRAILS.md.
- Preserve integer-floor cross logic exactly.
- Preserve station-local 11:00–19:00 window.
- Preserve station-local daily reset.
- Preserve scheduler idempotency behavior.
- Minimal diffs only.
- No refactoring unrelated code.
- No renaming functions unless explicitly requested.
- No new dependencies unless explicitly approved.
- Do not expose secrets in any endpoint or state output.
- Do not log private keys or sensitive auth material.
- Do not change Render boot behavior unless explicitly requested.

----------------------------------------------------------------
KALSHI RULES (WHEN APPLICABLE)
----------------------------------------------------------------

Environment variables:
- KALSHI_BASE_URL
- KALSHI_KEY_ID
- KALSHI_KEY_PEM

IMPORTANT:
KALSHI_BASE_URL already includes `/trade-api/v2`.

Paths passed to helpers MUST NOT include `/trade-api/v2` again.

Correct:
    /markets?limit=1

Signature message format:
    timestamp + METHOD + path + body

Timestamp:
    milliseconds (int(time.time() * 1000))

Signing:
    RSA
    PKCS1v15
    SHA256
    Base64 encoded

Never expose key_id or key_pem.

----------------------------------------------------------------
CHANGE SCOPE
----------------------------------------------------------------

(Describe requested implementation here.)

----------------------------------------------------------------
DELIVERABLE REQUIREMENTS
----------------------------------------------------------------

Return:

1) Exact unified diff only
2) Confirmation Phase 1 files were not modified
3) Confirmation no alert semantics changed
4) PR URL
5) Required environment variables (if applicable)
6) Local curl test command (if applicable)
