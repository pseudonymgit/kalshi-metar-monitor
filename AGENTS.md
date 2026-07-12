Repository Purpose
------------------

Deterministic METAR monitoring system that converts observations into
temperature ladder transitions and evaluates Kalshi weather markets.

Determinism Rules
-----------------

• No datetime.now() or time.time() in deterministic logic
• No randomness in runtime decision paths
• Replay outputs must be identical for identical inputs

Execution Discipline (B-MODE ACTIVE — permanent until Dan changes it)
---------------------------------------------------------------------

• Scripts only. No AI in the execution loop.
• You write scripts. You do not run backtests, experiments, paper trading,
  NWP integration, or alert delivery inside an AI session.
• All execution happens via scripts run from the host terminal, cron, or
  dedicated runners — never inside an AI model session.
• Captured terminal stdout/stderr is the only accepted evidence.
  Synthesized progress narratives without run logs do not count.
• Impossible metrics (100% accuracy, Sharpe=1000) are proof of
  non-execution. Real metrics: 60-75% accuracy, 0.1-0.5 Sharpe.
• If you didn't run it, say so. "Script written, not yet run" is honest.
  "100% accuracy" without a run log is fabrication.

Observability Rules
-------------------

Observability endpoints must be strictly read-only.

They must NOT:
• trigger hydration workers
• evaluate alerts
• call Kalshi APIs
• mutate queues
• mutate transition history
• mutate alert history

Change Task Rules
-----------------

Changes must preserve deterministic behavior and include tests when
new endpoints or logic are introduced.

Review Task Rules
-----------------

Reviews are read-only. Review prompts must not modify code.

Preferred Prompt Structure
--------------------------

1. Role
2. Objective
3. Relevant files/functions
4. Invariants that must not change
5. Required tests
6. Expected output
