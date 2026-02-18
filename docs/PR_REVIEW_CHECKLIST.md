# PR Review Checklist — Kalshi METAR Monitor

No PR may be merged unless all items below are satisfied.

------------------------------------------------------------
PHASE 1 SEMANTICS
------------------------------------------------------------

[ ] Alert logic unchanged
[ ] Integer floor-cross detection preserved
[ ] No alert suppression logic added
[ ] Station-local 11:00–19:00 window preserved
[ ] Station-local daily reset preserved
[ ] Scheduler idempotency preserved
[ ] No polling frequency drift

If Phase 1 behavior was intentionally changed:

[ ] PHASE1.md version incremented
[ ] Semantic change clearly documented
[ ] Merge gate phrase updated accordingly

------------------------------------------------------------
SECURITY
------------------------------------------------------------

[ ] No secrets exposed in code
[ ] No secrets exposed in logs
[ ] No secrets returned in API responses
[ ] No private keys printed or serialized
[ ] No token or credential echoed

------------------------------------------------------------
ARCHITECTURE
------------------------------------------------------------

[ ] No refactor outside requested scope
[ ] No renamed functions unless explicitly requested
[ ] No new dependencies unless approved
[ ] No main branch direct commits

------------------------------------------------------------
KALSHI (When Applicable)
------------------------------------------------------------

[ ] Signature format correct (timestamp + METHOD + path + body)
[ ] Timestamp in milliseconds
[ ] RSA PKCS1v15 + SHA256
[ ] Base64 encoded signature
[ ] KALSHI_BASE_URL does NOT duplicate /trade-api/v2
[ ] key_id and key_pem never exposed

------------------------------------------------------------
MERGE GATE
------------------------------------------------------------

ChatGPT must explicitly state:

“Phase 1 semantics preserved.”

If not stated, the PR must not be merged.
