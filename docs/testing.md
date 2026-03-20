# Testing

This document intentionally avoids endpoint inventories.

For all API routes, request/response shapes, and canonical test surfaces, use `docs/API_REFERENCE.md`.

Operational runbooks and deployment checks live in `docs/OPERATIONS.md`.

## Pytest invocation

Run pytest from the repository root and prefer `python -m pytest ...` as the canonical entrypoint for this repo.

Plain `pytest ...` may fail in some environments because the import-path context differs from the repository-root module context.

If you cannot use `python -m pytest ...`, use `PYTHONPATH=. pytest ...` only as a fallback.

Current known break: `python -m pytest tests/test_pipeline_truth_endpoint.py` is still red because the `client` fixture is missing from the real test environment. This is a test-surface issue, not a shell entrypoint issue.
