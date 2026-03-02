# CODEX MASTER TEMPLATE (ACTIVE)

## Purpose

Defines mandatory engineering and governance rules when Codex
produces or modifies repository code or documentation.

This document governs tooling behavior only.
Historical templates remain archived.

------------------------------------------------------------

## Core Engineering Rules

- Preserve deterministic execution behavior.
- Do not modify execution semantics unless explicitly requested.
- Prefer architectural stability over feature expansion.
- Do not refactor outside requested scope.
- Do not rename functions or endpoints unless instructed.

------------------------------------------------------------

## Documentation Authority Model

Canonical authority hierarchy:

1. docs/API_REFERENCE.md
2. docs/ARCHITECTURE.md
3. docs/OPERATING_MODE.md
4. docs/OPERATIONS.md
5. docs/ROLLING_TODO.md

If conflicts exist, higher authority controls.

Archived documents MUST NOT be treated as requirements.

------------------------------------------------------------

## PR Output Requirements

All implementation responses MUST:

- Produce unified diffs.
- Modify only requested scope.
- Preserve replay determinism.
- Preserve transition-driven alert semantics.
- Avoid introducing probabilistic or ML behavior.

------------------------------------------------------------

## Safety Constraints

DO NOT:

- introduce automated trading
- introduce probabilistic execution paths
- smooth or suppress rapid reversions
- shift execution authority outside Execution domain

------------------------------------------------------------

## Merge Gate

A PR may be merged only if review confirms:

"Deterministic execution semantics preserved."

------------------------------------------------------------
