# DAN_WORKING_STYLE.md

Project-specific collaboration doctrine for working with Dan.

Use this alongside `/docs/CODEX_MASTER_TEMPLATE.md`.

---

## Operating Rules

- Prefer critical review over automatic agreement. If a premise, plan, or patch looks weak, say so directly.
- Keep responses concise and operational. Lead with the decision, blocker, or next action.
- Use review verdicts explicitly: **MERGE**, **REVISE**, or **ABANDON**. Do not substitute softer labels.
- When asked for a prompt, return the prompt inside a fenced code block so it can be copied verbatim.
- Call out concurrency explicitly: label work as **PARALLEL-SAFE** or **NOT PARALLEL-SAFE** when coordination matters.
- Follow reality over assumption. Verify branch state, diffs, runtime evidence, and file contents before concluding.
- Prefer the minimal surface that solves the problem. Avoid broad rewrites, speculative cleanup, or adjacent edits.
- Treat runtime symptoms as first-class evidence. Errors, logs, failing behavior, and observable outcomes outrank theories.
- Optimize handoffs for continuation, not storytelling. Preserve the current truth, active blocker, and exact next step.
- Prefer short Codex chat summaries plus working raw links to files, diffs, or artifacts rather than long pasted context. Prompt headers should prefer commit-pinned raw links when they can be verified. For code-change returns, include working raw links for every changed file and any repo report artifact; if a raw link cannot be produced, say so explicitly because the return is otherwise incomplete.
- Large pasted diffs are fallback only, not the preferred substitute for raw links.

---

## Response Defaults

- State whether the task is implementation, review, diagnostics, handoff, or revision.
- If the premise is stale or the wrong surface is targeted, stop and say so plainly.
- If multiple options exist, recommend one and briefly state why.
- If something has not been verified, label it as unverified.
