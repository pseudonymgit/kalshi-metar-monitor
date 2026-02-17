# Architecture Guardrails

Phase 1 is frozen.

The following behavior must NOT change unless explicitly versioned:

- Integer floor transition alert semantics
- Station-local 11:00–19:00 alert window
- Station-local daily reset logic
- last_observed_floor persistence behavior

If Phase 1 behavior is modified:

1. A new git tag must be created.
2. docs/PHASE1.md must be updated.
3. The behavioral change must be explicitly documented.

All future development (Phase 2+) must not alter Phase 1 behavior.
