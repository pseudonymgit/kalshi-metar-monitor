# PR Review Checklist — Kalshi METAR Monitor

No PR may be merged unless all items below are satisfied.

------------------------------------------------------------
SEMANTICS + GOVERNANCE
------------------------------------------------------------

[ ] Alert logic unchanged unless explicitly scoped
[ ] Integer floor-cross detection preserved
[ ] Symmetric HIGH/LOW monitoring preserved where configured
[ ] Transition-driven alerting preserved (no temp-only alerts)
[ ] Station-local alert window + reset semantics unchanged unless intentionally versioned
[ ] Replay parity expectations preserved
[ ] Observability remains non-causal and does not call Kalshi

If behavior was intentionally changed:

[ ] Canonical docs updated (`docs/OPERATING_MODE.md`, `docs/ARCHITECTURE.md`)
[ ] Semantic change clearly documented in PR

------------------------------------------------------------
SECURITY
------------------------------------------------------------

[ ] No secrets exposed in code
[ ] No secrets exposed in logs
[ ] No secrets returned in API responses
[ ] No private keys printed or serialized
[ ] No token or credential echoed

------------------------------------------------------------
ENGINEERING DISCIPLINE
------------------------------------------------------------

[ ] No refactor outside requested scope
[ ] No renamed functions unless explicitly requested
[ ] No new dependencies unless approved
[ ] No direct commits to protected branches

------------------------------------------------------------
DOCS CONSISTENCY
------------------------------------------------------------

[ ] API details live only in `docs/API_REFERENCE.md`
[ ] Runbooks/deployment notes live in `docs/OPERATIONS.md`
[ ] Architecture and invariants live in `docs/ARCHITECTURE.md`
[ ] Governance and semantic authority live in `docs/OPERATING_MODE.md`
[ ] Roadmap updates tracked in `docs/ROLLING_TODO.md`
