# Operating Mode — Kalshi METAR Monitor

Project Mode: SOLO-DEV CONTROLLED RELEASE

This repository operates under strict change discipline.

------------------------------------------------------------
CHANGE POLICY
------------------------------------------------------------

- No direct commits to main.
- All changes must go through Pull Request.
- All PRs must pass docs/PR_REVIEW_CHECKLIST.md.
- No PR may be merged unless ChatGPT explicitly states:

  "Phase 1 semantics preserved."

If Phase 1 behavior is intentionally modified:

- docs/phase_current.md version must be incremented.
- Semantic change must be documented.
- ChatGPT must explicitly approve the new version.

------------------------------------------------------------
ALERT ENGINE INVARIANTS
------------------------------------------------------------

Unless version incremented, the following are frozen:

- Integer floor-cross detection
- No unchanged-repeat alerts
- Station-local 11:00–19:00 window
- Station-local daily reset
- Scheduler idempotency
- No polling drift
- No probabilistic execution paths
- No suppression of rapid reversions

------------------------------------------------------------
SECURITY POLICY
------------------------------------------------------------

- No secrets in code.
- No secrets in logs.
- No secrets returned via endpoints.
- No private key material exposed.
- GitHub tokens must never be embedded in runtime.

------------------------------------------------------------
PHILOSOPHY
------------------------------------------------------------

This system may control real capital.

Stability > speed.
Determinism > convenience.
Explicit versioning > silent drift.
Goldilocks structural-event preservation > cosmetic smoothing.
