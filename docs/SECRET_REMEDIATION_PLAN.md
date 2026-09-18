# Secret remediation and history rewrite plan

Prepared 2026-08-17 UTC. This file is a plan only; no remote history or force-push was changed.

## Current findings

- Remote `origin` was inspected; local `main` and `origin/main` were both at the same inspected head. No remote mutation was performed.
- Tracked credential paths prepared for removal from the future tree: `.env.webhooks` and `data/kalshi_private_key.pem`. Their ignored local copies remain in place.
- `config.json` contains endpoint metadata, an empty key id, and a boolean key-presence flag only; no private key or token was found. Keep it as non-secret runtime metadata.
- Existing history contains three `.env.webhooks` revisions, one private-key revision, and six `config.json` revisions. A history rewrite is therefore required to remove the credentials from all reachable commits.
- `ALERT_WEBHOOK_URL` is the runtime production target. Canonical per-instance names are `DISCORD_WEBHOOK_PROD`, `DISCORD_WEBHOOK_DEV`, and `DISCORD_WEBHOOK_SBOX`; `WEBHOOK_*` is accepted only by local loaders for compatibility.
- `CDSAPI_KEY` is canonical, with `ECDS_KEY` retained as an optional fallback for historical experiments.
- Legacy Kalshi API secret names are not active runtime names.

## Pre-push history rewrite (requires explicit parent/user confirmation)

1. Freeze scheduled/deployment writers and take a fresh remote snapshot.
2. Verify the remote URL, expected protected branch, and current remote head. Do not proceed if the remote moved unexpectedly.
3. Use a fresh clone or a disposable mirror, not the dirty working tree:

```sh
git clone --mirror <REMOTE_URL> weather-engine-secret-scrub.git
cd weather-engine-secret-scrub.git
git filter-repo --force \
  --path .env.webhooks \
  --path data/kalshi_private_key.pem \
  --invert-paths
```

4. Add the sanitized current-tree commit/branch containing the redacted example and validator. Inspect `git log --all --name-status` and run the history scanner before publishing.
5. Revoke/rotate every exposed credential (Render rotation was confirmed); confirm `ALERT_WEBHOOK_URL` points to the new production value, without placing that value in git.
6. Publish only after parent/user approval, using an explicit lease matching the verified remote head:

```sh
git push --force-with-lease=refs/heads/main:<VERIFIED_REMOTE_HEAD> origin refs/heads/main
```

7. Verify from a brand-new clone: forbidden paths absent from all refs; secret scanner clean; expected tree files present; no webhook delivery or scheduler activity performed.
8. Coordinate a new clone for every consumer. Old clones, bundles, caches, CI artifacts, and forks may still contain the old objects.

## Rollback

The rollback bundle and mode-600 local-sensitive archive are outside the repository under `/home/node/.openclaw/backups/weather-engine-secret-remediation-20260817T021545Z/`. To restore local work, clone or fetch from `repo-all-refs.bundle`, check out the recorded head, and restore ignored files only from the protected archive. To recover the remote branch after an approved rewrite, push the recorded pre-rewrite ref with an explicit `--force-with-lease` after confirming the current remote state; do not use an unconditional force push.

## Verification report (pre-push)

- `main` = `48587dfab16ed238af3047846e3da3fbdda0b16c`
- `origin/main` = `48587dfab16ed238af3047846e3da3fbdda0b16c`
- Current-tree validator: `SECRET_HYGIENE_PASS`.
- Python compile and shell syntax gate: passed for changed loaders, validator, and runtime modules.
- Targeted tests: 14 passed, 1 failed in existing alert schema behavior (`alert_schema_version` was `1.0`, expected `2`); this is unrelated to secret remediation and was not changed.
- History counts before rewrite: `.env.webhooks` 3, `data/kalshi_private_key.pem` 1, `config.json` 6.
- No scheduler, Graphify, research-data deletion, real sweep, webhook delivery, or remote push was run.

## Accuracy improvement suggestion

Have CI run the history scanner against every reachable ref and a fresh clone, not only the working-tree/index scanner. Add a small fixture repository containing a fake webhook URL and PEM marker so the scrub gate is tested against realistic historical blobs without ever using production credentials.
