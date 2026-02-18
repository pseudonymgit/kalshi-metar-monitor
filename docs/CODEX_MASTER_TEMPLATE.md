IMPORTANT — PR MODE ONLY

All changes must be made on a new branch.
A Pull Request must be opened.
Never modify main directly.

DO NOT:
- Commit to main
- Modify main directly
- Print secrets
- Echo environment variables
- Log private keys
- Expose tokens in output
- Refactor unrelated code

If PR creation fails, report the exact error.

----------------------------------------------------------------
PROJECT GUARDRAILS
----------------------------------------------------------------

You must read and respect:

- docs/PHASE1.md
- docs/ARCHITECTURE_GUARDRAILS.md
- PR_REVIEW_CHECKLIST.md

Phase 1 alert semantics are frozen unless explicitly instructed.

Preserve exactly:

- Integer floor-cross detection
- Station-local 11:00–19:00 window
- Station-local daily reset
- Scheduler idempotency
- Poll cadence
- No duplicate alerts
- No unchanged-repeat alerts

If any alert logic is modified intentionally:

- Increment PHASE1.md version
- Document semantic change clearly
- State change explicitly in PR description

----------------------------------------------------------------
KALSHI RULES (When Applicable)
----------------------------------------------------------------

Environment variables:
- KALSHI_BASE_URL
- KALSHI_KEY_ID
- KALSHI_KEY_PEM

KALSHI_BASE_URL already includes:
    /trade-api/v2

Paths passed to helpers must NOT include it again.

Signature format:
    timestamp + METHOD + path + body

Timestamp:
    int(time.time() * 1000)

Signing:
    RSA
    PKCS1v15
    SHA256
    Base64 encoded

Never expose key_id or key_pem.

----------------------------------------------------------------
DELIVERABLE REQUIREMENTS
----------------------------------------------------------------

Return:

1) Unified diff only
2) Confirmation Phase 1 files were not modified (unless requested)
3) Confirmation no alert semantics changed
4) Branch name
5) PR URL
6) Required environment variables
7) Local curl test command
