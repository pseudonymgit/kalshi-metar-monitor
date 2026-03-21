# CODE_CHANGE_RETURN_PACKET

Canonical operator-facing return packet for code-change and repo-report tasks when a task contract asks for a reviewable delivery/report packet.

This packet does not replace mutation-task `UNIFIED DIFF ONLY` output rules from `/docs/CODEX_MASTER_TEMPLATE.md`. Use it only when the active task contract explicitly asks for a reviewable return packet.

---

## Rules

- Raw URLs must be GitHub-visible commit-pinned URLs when possible.
- Verified public-branch raw URLs are acceptable only when commit-pinned URLs are not available.
- Raw URLs must be verified before return when possible.
- Unchecked local branch-name raw refs must not be used.
- If a required raw URL cannot be produced or verified, return `UNAVAILABLE` with the reason.
- The packet is incomplete unless every required changed file and report artifact has a verified commit-pinned raw URL, a verified public-branch raw URL, or `UNAVAILABLE` with reason.
- Large pasted diffs are fallback only.
- `UNIFIED_DIFF` is required only when `RAW_URLS` are unavailable.

---

## Canonical Packet

```text
REPO_STATE
branch: <branch>
commit: <commit>

PATCH_SUMMARY
- <one-line scoped change>
- <one-line scoped change>

CHANGED_FILES
- <path>
- <path>

REPORT_ARTIFACT
- <artifact name>: <commit-pinned raw URL | verified public-branch raw URL | UNAVAILABLE: reason>

RAW_URLS
- <path>: <commit-pinned raw URL | verified public-branch raw URL>
- <path>: <commit-pinned raw URL | verified public-branch raw URL>
- If unavailable for any required item: UNAVAILABLE: <reason>

DIFF_LINK
- <commit diff URL | compare URL | UNAVAILABLE: reason>

UNIFIED_DIFF
- Required only when RAW_URLS are unavailable.
- If not required: OMIT.

TEST_RESULTS
- <command>: PASS
- <command>: FAIL
- <command>: NOT RUN (<reason>)
```
