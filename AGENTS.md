# AGENTS.md

## Repository Purpose
This repository implements a deterministic event-processing pipeline that:
- ingests METAR observations,
- detects ladder transitions,
- evaluates Kalshi markets, and
- emits alerts.

## Determinism Rules
Agents must **NOT** introduce:
- `datetime.now()`
- `time.time()`
- nondeterministic randomness

Replay outputs must remain identical for identical input streams.

## Observability Rules
Observability endpoints must:
- be read-only,
- never trigger hydration,
- never mutate transitions,
- never mutate alerts,
- never call external APIs.

## Change Task Rules
CHANGE tasks may modify runtime logic, but must preserve deterministic behavior and include tests.

## Review Task Rules
REVIEW tasks are strictly read-only and must not modify code.

## Preferred Prompt Structure
Prompts should include:
- role,
- objective,
- relevant files/functions,
- invariants that must not change,
- required tests,
- expected output format.
