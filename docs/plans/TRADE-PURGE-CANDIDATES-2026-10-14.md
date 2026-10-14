# Trade Purge Candidates — 2026-10-14

**Status:** CANDIDATES ONLY — **nothing deleted**. Awaiting Dan's approval of the criterion.

**Repo:** `prototypes/weather-engine-source/` · **Book:** `data/paper_trading_sbox.db`
**Tool:** `scripts/purge_candidates_2026-10-14.py` · **Artifact:** `artifacts/settlement-axis-truth/purge_candidates_2026-10-14.json`

## 0. Dan's directive

> "purge any trade history that doesn't actually represent an informative and accurate trade."

## 1. Proposed purge criterion (composable; a row qualifies on ANY hold)

| tag | meaning |
|---|---|
| **A_saturated_selection** | entered on the OLD saturated fused probability (`p_fused_raw >= 0.99`) — the deep-OTM lottery-ticket selection the register already flags as broken. The old p_fused told us nothing about the pick. |
| **B_unreconstructable** | the honest reconstruction cannot produce a defensible `calibrated_p` (no companion row, or `calibrated_p` missing/NaN/out of (0,1)). |
| **C_zero_size** | `position_size_usd == 0` — the honest ladder never sized it; it should not have been placed. |

A row is **informative** only if its decision-time probability is defensible (not saturated, reconstructable) and it was **sized** (> $0). Rows failing both A and C are the strongest purge candidates.

## 2. Result

- book rows: **91**
- purge candidates: **45**
  - A (saturated selection): **39**
  - B (unreconstructable): **0**
  - C (zero size): **6**

## 3. Candidate list

| id | station | ticker | status | size$ | old_size$ | p_fused_raw | calibrated_p | outcome | pnl(1c) | reasons |
|---:|---|---|---|---:|---:|---:|---:|---|---:|---|
| 187 | KSEA | KXHIGHTSEA-26OCT09-T59 | closed | 10.0 | 25.0 | 0.99 | 0.8599 | correct | 0.94 | A_saturated_selection |
| 188 | KMDW | KXHIGHCHI-26OCT09-T71 | closed | 10.0 | 5.0 | 0.99 | 0.8441 | incorrect | -0.55 | A_saturated_selection |
| 189 | KDFW | KXHIGHTDAL-26OCT09-T93 | closed | 0.0 | 5.0 | 0.2499 | 0.3087 | incorrect | -0.055 | C_zero_size |
| 190 | KSFO | KXHIGHTSFO-26OCT09-T70 | closed | 10.0 | 25.0 | 0.99 | 0.8518 | incorrect | -0.7 | A_saturated_selection |
| 197 | KATL | KXHIGHTATL-26AUG01-T82 | closed | 10.0 | 25.0 | 0.99 | 0.7836 | incorrect | -0.14500000000000002 | A_saturated_selection |
| 198 | KATL | KXHIGHTATL-26AUG04-T84 | closed | 10.0 | 25.0 | 0.99 | 0.7836 | incorrect | -0.135 | A_saturated_selection |
| 199 | KATL | KXHIGHTATL-26AUG05-T84 | closed | 10.0 | 25.0 | 0.99 | 0.7836 | incorrect | -0.065 | A_saturated_selection |
| 201 | KATL | KXHIGHTATL-26AUG10-T88 | closed | 10.0 | 25.0 | 0.99 | 0.7836 | incorrect | -0.035 | A_saturated_selection |
| 202 | KATL | KXHIGHTATL-26AUG11-T89 | closed | 10.0 | 25.0 | 0.99 | 0.7836 | incorrect | -0.025 | A_saturated_selection |
| 204 | KATL | KXHIGHTATL-26AUG13-T92 | closed | 10.0 | 25.0 | 0.99 | 0.7836 | incorrect | -0.045 | A_saturated_selection |
| 205 | KATL | KXHIGHTATL-26AUG14-T93 | closed | 10.0 | 25.0 | 0.99 | 0.7836 | incorrect | -0.065 | A_saturated_selection |
| 211 | KAUS | KXHIGHAUS-26SEP15-T98 | closed | 10.0 | 25.0 | 0.99 | 0.799 | incorrect | -0.025 | A_saturated_selection |
| 213 | KAUS | KXHIGHAUS-26SEP29-T93 | closed | 10.0 | 25.0 | 0.99 | 0.799 | incorrect | -0.08 | A_saturated_selection |
| 214 | KBOS | KXHIGHTBOS-26AUG04-T81 | closed | 10.0 | 25.0 | 0.99 | 0.7894 | incorrect | -0.045 | A_saturated_selection |
| 215 | KBOS | KXHIGHTBOS-26AUG11-T85 | closed | 10.0 | 25.0 | 0.99 | 0.7894 | incorrect | -0.055 | A_saturated_selection |
| 217 | KBOS | KXHIGHTBOS-26AUG13-T81 | closed | 10.0 | 25.0 | 0.99 | 0.7894 | incorrect | -0.11499999999999999 | A_saturated_selection |
| 218 | KBOS | KXHIGHTBOS-26AUG14-T80 | closed | 10.0 | 25.0 | 0.99 | 0.7894 | incorrect | -0.055 | A_saturated_selection |
| 219 | KBOS | KXHIGHTBOS-26SEP05-T70 | closed | 10.0 | 25.0 | 0.99 | 0.7894 | incorrect | -0.07500000000000001 | A_saturated_selection |
| 220 | KBOS | KXHIGHTBOS-26SEP08-T78 | closed | 10.0 | 25.0 | 0.99 | 0.7894 | incorrect | -0.155 | A_saturated_selection |
| 221 | KBOS | KXHIGHTBOS-26SEP15-T65 | closed | 10.0 | 25.0 | 0.99 | 0.7894 | incorrect | -0.035 | A_saturated_selection |
| 222 | KDCA | KXHIGHTDC-26AUG05-T84 | closed | 10.0 | 25.0 | 0.99 | 0.8062 | incorrect | -0.055 | A_saturated_selection |
| 223 | KDCA | KXHIGHTDC-26AUG09-T90 | closed | 10.0 | 25.0 | 0.99 | 0.8062 | incorrect | -0.035 | A_saturated_selection |
| 227 | KDCA | KXHIGHTDC-26AUG18-T86 | closed | 10.0 | 25.0 | 0.99 | 0.8062 | incorrect | -0.065 | A_saturated_selection |
| 228 | KDCA | KXHIGHTDC-26SEP05-T82 | closed | 10.0 | 25.0 | 0.99 | 0.8062 | incorrect | -0.14500000000000002 | A_saturated_selection |
| 231 | KDEN | KXHIGHDEN-26AUG04-T82 | closed | 10.0 | 25.0 | 0.99 | 0.8148 | incorrect | -0.025 | A_saturated_selection |
| 232 | KDEN | KXHIGHDEN-26AUG09-T94 | closed | 10.0 | 25.0 | 0.99 | 0.8148 | incorrect | -0.035 | A_saturated_selection |
| 233 | KDEN | KXHIGHDEN-26AUG11-T93 | closed | 10.0 | 25.0 | 0.99 | 0.8148 | incorrect | -0.025 | A_saturated_selection |
| 236 | KDEN | KXHIGHDEN-26SEP15-T70 | closed | 10.0 | 25.0 | 0.99 | 0.8148 | incorrect | -0.05500000000000001 | A_saturated_selection |
| 240 | KDFW | KXHIGHTDAL-26AUG01-T95 | closed | 10.0 | 25.0 | 0.99 | 0.8018 | incorrect | -0.03 | A_saturated_selection |
| 243 | KDFW | KXHIGHTDAL-26SEP04-T93 | closed | 10.0 | 25.0 | 0.99 | 0.8018 | incorrect | -0.075 | A_saturated_selection |
| 244 | KHOU | KXHIGHTHOU-26SEP08-T91 | closed | 10.0 | 25.0 | 0.99 | 0.8402 | incorrect | -0.025 | A_saturated_selection |
| 245 | KLAX | KXHIGHLAX-26SEP14-T82 | closed | 0.0 | 5.0 | 0.2354 | 0.2976 | incorrect | -0.08499999999999999 | C_zero_size |
| 248 | KMIA | KXHIGHMIA-26SEP08-T86 | closed | 10.0 | 25.0 | 0.99 | 0.8738 | incorrect | -0.025 | A_saturated_selection |
| 249 | KMIA | KXHIGHMIA-26SEP17-T84 | closed | 10.0 | 25.0 | 0.99 | 0.8738 | incorrect | -0.74 | A_saturated_selection |
| 253 | KPHL | KXHIGHPHIL-26SEP20-T75 | closed | 10.0 | 25.0 | 0.99 | 0.847 | incorrect | -0.065 | A_saturated_selection |
| 254 | KPHX | KXHIGHTPHX-26SEP14-T101 | closed | 10.0 | 25.0 | 0.99 | 0.7735 | incorrect | -0.055 | A_saturated_selection |
| 258 | KSFO | KXHIGHTSFO-26SEP14-T72 | closed | 10.0 | 25.0 | 0.99 | 0.8518 | incorrect | -0.12 | A_saturated_selection |
| 264 | KLAS | KXHIGHTLV-26OCT11-T73 | closed | 10.0 | 25.0 | 0.99 | 0.7735 | incorrect | -0.815 | A_saturated_selection |
| 267 | KMDW | KXHIGHCHI-26OCT11-T73 | closed | 10.0 | 5.0 | 0.99 | 0.8441 | incorrect | -0.025 | A_saturated_selection |
| 269 | KNYC | KXHIGHNY-26OCT12-T69 | closed | 10.0 | 25.0 | 0.99 | 0.8321 | incorrect | -0.10500000000000001 | A_saturated_selection |
| 270 | KBOS | KXHIGHTBOS-26OCT12-T71 | closed | 10.0 | 25.0 | 0.99 | 0.7894 | incorrect | -0.4 | A_saturated_selection |
| 271 | KDCA | KXHIGHTDC-26OCT13-T85 | closed | 0.0 | 5.0 | 0.2481 | 0.3073 | incorrect | -0.025 | C_zero_size |
| 272 | KLAX | KXHIGHLAX-26OCT13-T75 | closed | 0.0 | 5.0 | 0.2481 | 0.3073 | incorrect | -0.035 | C_zero_size |
| 273 | KSFO | KXHIGHTSFO-26OCT13-T75 | closed | 0.0 | 5.0 | 0.2481 | 0.3073 | incorrect | -0.025 | C_zero_size |
| 274 | KOKC | KXHIGHTOKC-26OCT13-T94 | closed | 0.0 | 5.0 | 0.2481 | 0.3073 | incorrect | -0.045 | C_zero_size |

## 4. Recommendation

- **Do not delete yet.** Dan approves the criterion first (his directive).
- Before any purge: full backup (`data/paper_trading_sbox.db.bak-*`) + a pre-purge snapshot; the purge is an explicit `DELETE`/archive with the candidate ids recorded in the artifact above.
- Consider **archive-not-delete**: move candidates to a `trades_purged_2026` table so the history is auditable rather than destroyed.
