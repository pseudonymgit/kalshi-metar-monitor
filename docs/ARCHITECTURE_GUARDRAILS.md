# Architecture Guardrails

Phase 1 is frozen.

The following files must not have behavior changes:
- core/metar_monitor.py (alert logic)
- Station-local alert window logic
- Daily reset logic
- Integer-cross semantics

Permitted changes:
- Bug fixes only
- Logging improvements
- Performance optimizations that do not change behavior

Any change to Phase 1 behavior requires:
1. New git tag
2. Update to PHASE1.md
3. Explicit documentation of behavioral delta
