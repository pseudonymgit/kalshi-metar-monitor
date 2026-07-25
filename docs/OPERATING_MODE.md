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
- Station-local 24/7 operation (window removed)
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

------------------------------------------------------------
CURRENT OPERATING MODE (2026-07-25)
------------------------------------------------------------

**A-Mode: DISABLED (BURIED)**

The A-Mode dispatcher has been disabled and buried. It failed in too many ways across Phase 10-14 development and is no longer viable as an operational path. No effort should be made to resurrect or repair it. All historical A-Mode artifacts remain in `.meta/continuity/weather-engine/` for reference only.

**Phase dispatcher cron: DISABLED**

The automated cron job that advanced phase state has been disabled. Phase advancement is now a manual decision made by Gilfoyle after explicit verification.

**B-Mode: SOLE OPERATING MODE**

B-mode is the only operating mode for this system. Key characteristics:
- Gilfoyle runs all execution tasks — no automated execution without Gilfoyle supervision
- All Phase 10-14 modules run under B-mode governance
- The 30-day paper test executes under B-mode
- No fallback, no alternative path — B-mode is it

**Execution Ownership**
- **Gilfoyle** owns all execution tasks: cron jobs, phase advancement, module deployment, monitoring
- **Donna** handles routing, continuity, and orchestration
- **Dan** handles merges and strategic calls
- No automated agents modify production state without Gilfoyle approval

**Key operational constraints:**
- No cron jobs execute automated trading decisions
- All trade gates must be manually verified before paper test graduation
- Phase 14 production gates (`core/production_gate.py`) remain enforced even in B-mode
