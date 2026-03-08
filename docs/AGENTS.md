Repository Purpose
------------------

Deterministic METAR monitoring system that converts observations into
temperature ladder transitions and evaluates Kalshi weather markets.

Determinism Rules
-----------------

• No datetime.now() or time.time() in deterministic logic
• No randomness in runtime decision paths
• Replay outputs must be identical for identical inputs

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
