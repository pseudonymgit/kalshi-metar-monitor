# Concurrent-committer trace — `pseudonymgit` (2026-10-12)

**Owner:** Bertram Gilfoyle (CTIO) · **Repo:** `prototypes/weather-engine-source/`
**Question (Dan):** "pseudonymgit is committing seconds after your agents. I'm not
doing it." — who/what is this?

## Verdict

**Not a concurrent actor, not a daemon, not a cron commit job.** It is a **git
identity artifact**: the OpenClaw `exec` environment injects
`GIT_AUTHOR_*` / `GIT_COMMITTER_*` / `GIT_CONFIG_*` from
`tools.github.gitAuthor` in the gateway config, so **every commit made through an
agent's exec tool — including the two in this very task — is authored *and*
committed as `pseudonymgit`.** The "seconds after your agents" timestamps are the
agents' own commits wearing the tooling's identity.

## Evidence

### 1. The repo's committed identity is `Dan`; the live identity is `pseudonymgit`

`.git/config`:

```
[user]
	email = dan@openclaw.ai
	name = Dan
```

But a plain `git var GIT_AUTHOR_IDENT` in the exec environment returns
`pseudonymgit <127955822+pseudonymgit@users.noreply.github.com>`. Running the
same with a **clean** environment (`env -i PATH=… HOME=… git var …`) returns
`Dan <dan@openclaw.ai>`. The difference is the injected environment, not the repo
config.

### 2. The injection source: `openclaw.json → tools.github.gitAuthor`

`/home/node/.openclaw/openclaw.json`:

```json
"github": {
  "profileId": "ghp_…",
  "kind": "oauth",
  "gitAuthor": {
    "name": "pseudonymgit",
    "email": "127955822+pseudonymgit@users.noreply.github.com"
  }
}
```

The exec process environment carries the derived overrides:

```
GIT_AUTHOR_NAME=pseudonymgit
GIT_AUTHOR_EMAIL=127955822+pseudonymgit@users.noreply.github.com
GIT_COMMITTER_NAME=pseudonymgit
GIT_COMMITTER_EMAIL=127955822+pseudonymgit@users.noreply.github.com
GIT_CONFIG_COUNT=2
GIT_CONFIG_KEY_0=user.name
GIT_CONFIG_VALUE_0=pseudonymgit
GIT_CONFIG_KEY_1=user.email
GIT_CONFIG_VALUE_1=127955822+pseudonymgit@users.noreply.github.com
```

`git config --list --show-origin` confirms the same two keys arrive as
`command line:` entries — i.e. OpenClaw hands git the identity out-of-band, which
is why the committed `.git/config` still says `Dan` while the commits say
`pseudonymgit`.

### 3. The recent "agent" commits are all `pseudonymgit`

`git log` for the Oct 11–12 agent work (look-200 battery, Track A, E-series
fixes) shows **both author and committer** = `pseudonymgit` on every row. The two
commits created during *this* task are no exception:

```
f4e65706b | A:pseudonymgit | C:pseudonymgit | fix(governance): close look-200 integrity gaps F-1/F-2/F-3/F-5/F-6
8d97a1496 | A:pseudonymgit | C:pseudonymgit | fix(sizing): cap positions at $25 on the $200 book
```

The only commits carrying `Dan <dan@openclaw.ai>` as committer are the ones made
**outside** the injected exec env (e.g. by hand / GitHub UI).

### 4. Ruled out: hooks, daemons, cron

- **Hooks:** `.git/config` sets `core.hooksPath = .githooks`, but **`.githooks/`
  does not exist**, and `.git/hooks/` contains only `*.sample` files — no
  `post-commit`/`pre-commit` is active. No hook fabricates a second commit.
- **Daemons:** two long-running processes touch the repo
  (`scripts/intraday_daemon.py --interval-min 5`, `scripts/run_daemon.py`), but
  **neither calls git at all** (grepped for `git`/`commit` — no matches).
- **Cron:** 11 jobs; the only data-adjacent producer is *"NWP Forecast Collection
  Daily"*, which runs `scripts/nwp_collect.py` (no commit). Separately,
  `scripts/update_weather_data.sh` (run by **host** cron every 6h) *does*
  `git commit -m "auto: weather data update …"` for `data/` — but its
  `auto:` commits do not appear in the recent history, so it is not the actor in
  the observed "seconds after" windows.
- **Processes:** no concurrent `git commit` process was observable.

### 5. Author/committer tallies (whole repo)

```
author:     pseudonymgit 884 | Dan 681 | Codex 21 | daniel.gabriel 5
committer:  Dan 681 | GitHub 464 | pseudonymgit 420 | Codex 21 | daniel.gabriel 5
```

The `pseudonymgit` author count (884) far exceeds its committer count (420): the
former includes every commit *authored* through the tooling, the latter only
those where the tooling also stamped the committer.

## Interpretation & recommendation

- It is a **git config / tooling identity artifact**, not an intruder. Dan is not
  doing it, and no bot is racing the agents — the agents' commits simply *are*
  `pseudonymgit`.
- The GitHub token profile (`tools.github.profileId`, an OAuth credential) maps
  to the `pseudonymgit` account so pushes authenticate there.
- If Dan wants agent commits attributed to a different identity (or to a distinct
  service identity so they are easy to separate from manual commits), the change
  is in `tools.github.gitAuthor` in `openclaw.json`. That is a **gateway-config
  change**, owned by Dan — not edited here.

### Method note

Per the task constraint, the core evidence came from **reading file content**
(`.git/config`, `/home/node/.openclaw/openclaw.json`, hook samples, daemon
sources, cron job store). The `git var` / environment / process probes were
read-only confirmations of the runtime injection, and no git config was
modified.
