IMPORTANT — AUTHENTICATED PR MODE ONLY

You must create a new branch and open a Pull Request using authenticated GitHub access.

A fine-grained GITHUB_TOKEN secret is available in the runtime.

DO NOT:
- Modify or create the main branch
- Commit without pushing
- Use unauthenticated git
- Print or expose GITHUB_TOKEN
- Echo environment variables
- Write the token to any file
- Include the token in logs, diffs, or PR descriptions
- Rewrite the git remote URL
- Persist credentials to disk

If authentication fails at any step, abort and report the exact error.

All changes must go through a Pull Request.

----------------------------------------------------------------
MANDATORY AUTHENTICATION SEQUENCE (RUN BEFORE PUSH)
----------------------------------------------------------------

1) Authenticate GitHub CLI using the token:

   gh auth login --with-token <<< "$GITHUB_TOKEN"

2) Verify authentication:

   gh auth status

3) Verify remote connectivity:

   git ls-remote origin HEAD

If any of the above fails:
- Abort immediately
- Report exact error
- Do NOT attempt git push

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
