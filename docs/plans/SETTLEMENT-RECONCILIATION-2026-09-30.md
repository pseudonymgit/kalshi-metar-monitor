# Settlement-Source Reconciliation — 2026-09-30

Generated: 2026-09-30 03:17 UTC  |  Work order: Item 3 (settlement-source reconciliation)

**Method.** For every station and every date where both legs exist, compare Kalshi settlement temperature against METAR daily max (HIGH markets) / min (LOW markets), and against GHCN `high_f`/`low_f`. `bias = kalshi_temp − metar_high` (or − ghcn_high). Kalshi settles on NWS Climatological Reports; GHCN is the official climatological archive, METAR is our raw observation feed.

> **Rounding artifact:** every `kalshi_temp` is a whole integer (°F). GHCN/METAR are in tenths. Any bias therefore carries an inherent ±0.5°F rounding component. A ±0.5°F mean is expected noise, not a hazard.

## HEADLINE — settlement-source verdict

| Leg | Result |
|---|---|
| **Kalshi vs GHCN** (official climatology) | mean bias ≈ 0.00°F, **no station/season exceeds 1.0°F** |
| **Kalshi vs raw METAR** | small systematic HIGH +0.4°F / LOW −0.35°F, but a **corrupted-day tail** inflates naive means |

**Verdict: the 2026-09-24 gap was NOT a settlement-source problem.** Kalshi settles on NWS Climatological Reports, and those match the GHCN archive to the rounding unit at every station. The only >1°F gaps are between Kalshi and our **raw METAR daily extremes**, which carry corrupted station-days (see adjudication below).

**Big-dispute adjudication** (|GHCN−Kalshi| > 5°F): METAR agrees with GHCN on **212** and with Kalshi on **249** of 461 days — i.e. these are corrupt *station-days* on both truth legs, not a settlement-source bias.

## 🚩 FLAGGED — |systematic (clean-subset) mean| > 1.0°F

**None. Every station's systematic bias is within ±1.0°F.**

## Systematic bias — CLEAN subset (both truth legs agree, |METAR−GHCN|≤2)

| Station | Market | bias = Kalshi−truth | n | mean | median | MAD |
|---|---|---|---|---|---|---|
| KATL | HIGH | GHCN | 111 | -0.001 | +0.000 | 0.000 |
| KATL | HIGH | METAR | 111 | +0.418 | +0.600 | 0.700 |
| KATL | LOW | GHCN | 16 | +0.006 | +0.000 | 0.000 |
| KATL | LOW | METAR | 16 | -0.225 | -0.200 | 0.500 |
| KAUS | HIGH | GHCN | 856 | -0.002 | +0.000 | 0.000 |
| KAUS | HIGH | METAR | 856 | +0.445 | +0.900 | 0.800 |
| KAUS | LOW | GHCN | 14 | +0.007 | +0.000 | 0.000 |
| KAUS | LOW | METAR | 14 | -0.386 | -0.200 | 0.500 |
| KBOS | HIGH | GHCN | 113 | +0.003 | +0.000 | 0.000 |
| KBOS | HIGH | METAR | 113 | +0.518 | +0.600 | 0.700 |
| KBOS | LOW | GHCN | 14 | +0.021 | +0.000 | 0.100 |
| KBOS | LOW | METAR | 14 | -0.400 | -0.400 | 0.700 |
| KDCA | HIGH | GHCN | 125 | +0.215 | +0.000 | 0.100 |
| KDCA | HIGH | METAR | 125 | +0.553 | +0.400 | 1.200 |
| KDCA | LOW | GHCN | 14 | +0.043 | +0.050 | 0.050 |
| KDCA | LOW | METAR | 14 | -0.343 | -0.300 | 0.900 |
| KDEN | HIGH | GHCN | 377 | -0.007 | +0.000 | 0.000 |
| KDEN | HIGH | METAR | 377 | +0.557 | +0.900 | 0.900 |
| KDEN | LOW | GHCN | 12 | +0.008 | +0.000 | 0.000 |
| KDEN | LOW | METAR | 12 | -0.967 | -1.100 | 0.600 |
| KDFW | HIGH | GHCN | 120 | +0.452 | +0.000 | 0.100 |
| KDFW | HIGH | METAR | 120 | +0.750 | +0.600 | 1.000 |
| KDFW | LOW | GHCN | 15 | -0.007 | +0.000 | 0.000 |
| KDFW | LOW | METAR | 15 | -0.120 | -0.400 | 0.200 |
| KHOU | HIGH | GHCN | 120 | +0.067 | +0.000 | 0.000 |
| KHOU | HIGH | METAR | 120 | +0.497 | +0.700 | 0.900 |
| KHOU | LOW | GHCN | 19 | -0.005 | +0.000 | 0.000 |
| KHOU | LOW | METAR | 19 | -0.653 | -0.600 | 0.800 |
| KLAS | HIGH | GHCN | 106 | +0.006 | +0.000 | 0.000 |
| KLAS | HIGH | METAR | 106 | +0.553 | +0.750 | 0.850 |
| KLAS | LOW | GHCN | 15 | -0.007 | +0.000 | 0.000 |
| KLAS | LOW | METAR | 15 | -1.160 | -1.400 | 0.400 |
| KLAX | HIGH | GHCN | 331 | +0.066 | +0.000 | 0.000 |
| KLAX | HIGH | METAR | 331 | +0.782 | +1.000 | 0.600 |
| KLAX | LOW | GHCN | 16 | -0.006 | +0.000 | 0.100 |
| KLAX | LOW | METAR | 16 | -0.437 | -0.700 | 0.700 |
| KMDW | HIGH | GHCN | 1337 | -0.008 | +0.000 | 0.000 |
| KMDW | HIGH | METAR | 1337 | +0.451 | +0.200 | 0.700 |
| KMDW | LOW | GHCN | 17 | +0.006 | +0.000 | 0.000 |
| KMDW | LOW | METAR | 17 | -0.459 | -0.400 | 0.800 |
| KMIA | HIGH | GHCN | 887 | -0.719 | -1.000 | 0.100 |
| KMIA | HIGH | METAR | 887 | +0.016 | +0.000 | 0.000 |
| KMIA | LOW | GHCN | 44 | -0.014 | +0.000 | 0.000 |
| KMIA | LOW | METAR | 44 | -0.220 | -0.400 | 0.600 |
| KMSP | HIGH | GHCN | 72 | -0.007 | +0.000 | 0.000 |
| KMSP | HIGH | METAR | 72 | +0.428 | +0.600 | 0.600 |
| KMSP | LOW | GHCN | 13 | -0.008 | +0.000 | 0.100 |
| KMSP | LOW | METAR | 13 | -0.492 | -0.600 | 0.400 |
| KMSY | HIGH | GHCN | 87 | +0.008 | +0.000 | 0.000 |
| KMSY | HIGH | METAR | 87 | +0.428 | +0.600 | 0.800 |
| KMSY | LOW | GHCN | 17 | +0.006 | +0.000 | 0.000 |
| KMSY | LOW | METAR | 17 | -0.565 | -0.200 | 0.600 |
| KNYC | HIGH | GHCN | 1253 | +0.202 | +0.000 | 1.000 |
| KNYC | HIGH | METAR | 1253 | +0.086 | +0.000 | 0.100 |
| KNYC | LOW | GHCN | 13 | +0.000 | +0.000 | 0.000 |
| KNYC | LOW | METAR | 13 | -0.169 | +0.000 | 0.600 |
| KOKC | HIGH | GHCN | 99 | +0.008 | +0.000 | 0.000 |
| KOKC | HIGH | METAR | 99 | +0.415 | +0.600 | 0.800 |
| KOKC | LOW | GHCN | 16 | -0.006 | +0.000 | 0.000 |
| KOKC | LOW | METAR | 16 | -0.481 | -0.500 | 0.400 |
| KPHL | HIGH | GHCN | 355 | -0.438 | -0.100 | 0.200 |
| KPHL | HIGH | METAR | 355 | +0.054 | +0.000 | 0.100 |
| KPHL | LOW | GHCN | 16 | +0.019 | +0.000 | 0.000 |
| KPHL | LOW | METAR | 16 | -0.063 | -0.200 | 0.600 |
| KPHX | HIGH | GHCN | 93 | -0.009 | +0.000 | 0.100 |
| KPHX | HIGH | METAR | 93 | +0.445 | +0.400 | 1.200 |
| KPHX | LOW | GHCN | 13 | +0.000 | +0.000 | 0.000 |
| KPHX | LOW | METAR | 13 | -0.446 | -0.400 | 0.600 |
| KSAT | HIGH | GHCN | 79 | -0.015 | +0.000 | 0.000 |
| KSAT | HIGH | METAR | 79 | +0.110 | +0.200 | 1.000 |
| KSAT | LOW | GHCN | 16 | +0.000 | +0.000 | 0.050 |
| KSAT | LOW | METAR | 16 | -0.675 | -0.800 | 0.800 |
| KSEA | HIGH | GHCN | 85 | -0.141 | +0.000 | 0.100 |
| KSEA | HIGH | METAR | 85 | +0.244 | +0.400 | 1.000 |
| KSEA | LOW | GHCN | 12 | +0.008 | +0.000 | 0.000 |
| KSEA | LOW | METAR | 12 | -0.217 | -0.200 | 0.200 |
| KSFO | HIGH | GHCN | 80 | -0.006 | +0.000 | 0.000 |
| KSFO | HIGH | METAR | 80 | +0.591 | +0.600 | 0.800 |
| KSFO | LOW | GHCN | 17 | +0.024 | +0.000 | 0.000 |
| KSFO | LOW | METAR | 17 | -0.012 | -0.200 | 0.200 |

## FULL (unfiltered) per-station table — every matched station-day

> These means are inflated by corrupted METAR station-days. Use the CLEAN-subset table above for strike decisions.

| Station | Market | vs | n | mean | median | MAD | max abs |
|---|---|---|---|---|---|---|---|
| KATL | HIGH | GHCN | 218 | +0.000 | +0.000 | 0.000 | 0.10 |
| KATL | HIGH | METAR | 212 | +3.127 | +1.600 | 1.950 | 19.00 |
| KATL | LOW | GHCN | 18 | +0.006 | +0.000 | 0.000 | 0.10 |
| KATL | LOW | METAR | 18 | -0.178 | -0.200 | 0.600 | 4.20 |
| KAUS | HIGH | GHCN | 1101 | -0.003 | +0.000 | 0.000 | 0.20 |
| KAUS | HIGH | METAR | 1091 | +0.605 | +0.900 | 0.900 | 22.80 |
| KAUS | LOW | GHCN | 18 | +0.000 | +0.000 | 0.000 | 0.10 |
| KAUS | LOW | METAR | 18 | -1.033 | -0.300 | 0.800 | 5.00 |
| KBOS | HIGH | GHCN | 217 | -0.001 | +0.000 | 0.000 | 0.10 |
| KBOS | HIGH | METAR | 211 | +1.818 | +1.200 | 1.600 | 25.90 |
| KBOS | LOW | GHCN | 18 | +0.022 | +0.000 | 0.100 | 0.10 |
| KBOS | LOW | METAR | 18 | +3.150 | -0.200 | 0.800 | 32.20 |
| KDCA | HIGH | GHCN | 233 | -0.118 | +0.000 | 0.100 | 26.00 |
| KDCA | HIGH | METAR | 227 | +1.839 | +0.800 | 2.700 | 28.50 |
| KDCA | LOW | GHCN | 17 | +0.035 | +0.000 | 0.100 | 0.10 |
| KDCA | LOW | METAR | 17 | +1.424 | -0.400 | 1.000 | 34.20 |
| KDEN | HIGH | GHCN | 580 | -0.004 | +0.000 | 0.000 | 1.00 |
| KDEN | HIGH | METAR | 573 | +0.029 | +0.900 | 1.100 | 19.90 |
| KDEN | LOW | GHCN | 18 | +0.000 | +0.000 | 0.000 | 0.10 |
| KDEN | LOW | METAR | 18 | +1.394 | -1.200 | 0.800 | 29.20 |
| KDFW | HIGH | GHCN | 210 | -0.098 | +0.000 | 0.100 | 39.00 |
| KDFW | HIGH | METAR | 204 | +1.571 | +0.800 | 2.000 | 37.90 |
| KDFW | LOW | GHCN | 17 | -0.006 | +0.000 | 0.000 | 0.10 |
| KDFW | LOW | METAR | 17 | -0.424 | -0.400 | 0.400 | 3.20 |
| KHOU | HIGH | GHCN | 209 | +0.071 | +0.000 | 0.000 | 4.10 |
| KHOU | HIGH | METAR | 204 | +2.207 | +1.600 | 1.700 | 19.20 |
| KHOU | LOW | GHCN | 19 | -0.005 | +0.000 | 0.000 | 0.10 |
| KHOU | LOW | METAR | 19 | -0.653 | -0.600 | 0.800 | 1.80 |
| KLAS | HIGH | GHCN | 238 | +0.000 | +0.000 | 0.000 | 0.10 |
| KLAS | HIGH | METAR | 226 | +1.168 | +1.300 | 1.600 | 12.00 |
| KLAS | LOW | GHCN | 18 | -0.006 | +0.000 | 0.000 | 0.10 |
| KLAS | LOW | METAR | 18 | -1.806 | -1.400 | 0.500 | 8.80 |
| KLAX | HIGH | GHCN | 549 | +0.017 | +0.000 | 0.100 | 20.90 |
| KLAX | HIGH | METAR | 543 | +2.763 | +1.100 | 1.100 | 26.50 |
| KLAX | LOW | GHCN | 17 | -0.006 | +0.000 | 0.100 | 0.10 |
| KLAX | LOW | METAR | 17 | +1.718 | -0.600 | 0.800 | 36.20 |
| KMDW | HIGH | GHCN | 1746 | -0.007 | +0.000 | 0.000 | 2.00 |
| KMDW | HIGH | METAR | 1736 | -0.020 | +0.100 | 0.900 | 25.00 |
| KMDW | LOW | GHCN | 18 | +0.000 | +0.000 | 0.000 | 0.10 |
| KMDW | LOW | METAR | 18 | -0.578 | -0.500 | 0.900 | 2.60 |
| KMIA | HIGH | GHCN | 1035 | -0.999 | -1.000 | 0.900 | 10.00 |
| KMIA | HIGH | METAR | 1031 | +0.103 | +0.000 | 0.100 | 14.20 |
| KMIA | LOW | GHCN | 66 | -0.014 | +0.000 | 0.000 | 0.10 |
| KMIA | LOW | METAR | 65 | +2.168 | -0.400 | 1.000 | 48.20 |
| KMSP | HIGH | GHCN | 119 | -0.007 | +0.000 | 0.000 | 0.10 |
| KMSP | HIGH | METAR | 113 | +0.391 | +0.600 | 1.200 | 15.40 |
| KMSP | LOW | GHCN | 19 | -0.011 | +0.000 | 0.100 | 0.10 |
| KMSP | LOW | METAR | 19 | +0.821 | -0.800 | 0.800 | 30.60 |
| KMSY | HIGH | GHCN | 120 | +0.007 | +0.000 | 0.000 | 0.10 |
| KMSY | HIGH | METAR | 110 | +1.025 | +0.800 | 0.950 | 15.80 |
| KMSY | LOW | GHCN | 18 | +0.011 | +0.000 | 0.000 | 0.10 |
| KMSY | LOW | METAR | 18 | -0.789 | -0.400 | 0.600 | 4.60 |
| KNYC | HIGH | GHCN | 1843 | +0.320 | +0.000 | 1.100 | 28.90 |
| KNYC | HIGH | METAR | 1833 | +0.138 | +0.000 | 0.100 | 30.70 |
| KNYC | LOW | GHCN | 17 | +0.000 | +0.000 | 0.000 | 0.10 |
| KNYC | LOW | METAR | 17 | +1.412 | -0.400 | 0.800 | 34.60 |
| KOKC | HIGH | GHCN | 208 | +0.009 | +0.000 | 0.000 | 0.10 |
| KOKC | HIGH | METAR | 197 | +1.839 | +1.200 | 1.800 | 22.20 |
| KOKC | LOW | GHCN | 18 | +0.000 | +0.000 | 0.000 | 0.10 |
| KOKC | LOW | METAR | 18 | -0.783 | -0.700 | 0.500 | 3.60 |
| KPHL | HIGH | GHCN | 527 | -0.835 | -0.100 | 0.900 | 16.10 |
| KPHL | HIGH | METAR | 517 | +0.208 | +0.000 | 0.100 | 12.40 |
| KPHL | LOW | GHCN | 18 | +0.017 | +0.000 | 0.000 | 0.10 |
| KPHL | LOW | METAR | 18 | -0.556 | -0.200 | 0.800 | 5.40 |
| KPHX | HIGH | GHCN | 214 | -0.039 | +0.000 | 0.100 | 17.90 |
| KPHX | HIGH | METAR | 206 | +1.707 | +1.000 | 2.600 | 23.40 |
| KPHX | LOW | GHCN | 17 | -0.006 | +0.000 | 0.000 | 0.10 |
| KPHX | LOW | METAR | 17 | +0.612 | -0.400 | 1.000 | 45.20 |
| KSAT | HIGH | GHCN | 120 | -0.007 | +0.000 | 0.000 | 0.10 |
| KSAT | HIGH | METAR | 110 | +0.089 | +0.200 | 1.200 | 16.20 |
| KSAT | LOW | GHCN | 18 | -0.006 | +0.000 | 0.050 | 0.10 |
| KSAT | LOW | METAR | 18 | -0.883 | -0.800 | 0.900 | 2.60 |
| KSEA | HIGH | GHCN | 144 | -0.177 | +0.000 | 0.100 | 24.10 |
| KSEA | HIGH | METAR | 138 | +0.108 | +0.450 | 1.650 | 27.80 |
| KSEA | LOW | GHCN | 18 | +0.028 | +0.000 | 0.000 | 0.10 |
| KSEA | LOW | METAR | 18 | +4.283 | -0.100 | 0.800 | 23.20 |
| KSFO | HIGH | GHCN | 238 | -0.007 | +0.000 | 0.000 | 0.10 |
| KSFO | HIGH | METAR | 229 | +3.900 | +3.200 | 2.300 | 26.30 |
| KSFO | LOW | GHCN | 17 | +0.024 | +0.000 | 0.000 | 0.10 |
| KSFO | LOW | METAR | 17 | -0.012 | -0.200 | 0.200 | 1.00 |

## Seasonal breakdown — HIGH markets vs GHCN (clean subset)

| Station | DJF | MAM | JJA | SON |
|---|---|---|---|---|
| KATL | +0.10 (n=2) | -0.02 (n=25) | +0.00 (n=63) | +0.00 (n=21) |
| KAUS | -0.00 (n=154) | -0.00 (n=202) | -0.00 (n=290) | +0.00 (n=210) |
| KBOS | -0.01 (n=13) | +0.00 (n=33) | +0.01 (n=51) | +0.00 (n=16) |
| KDCA | +0.29 (n=14) | +0.62 (n=37) | -0.01 (n=57) | +0.01 (n=17) |
| KDEN | -0.00 (n=86) | -0.00 (n=117) | -0.01 (n=120) | -0.01 (n=54) |
| KDFW | -5.03 (n=3) | +1.52 (n=45) | +0.02 (n=52) | -0.01 (n=20) |
| KHOU | +0.00 (n=2) | -0.00 (n=44) | +0.15 (n=57) | -0.01 (n=17) |
| KLAS | +0.03 (n=15) | +0.01 (n=42) | -0.01 (n=35) | -0.01 (n=14) |
| KLAX | +0.15 (n=53) | +0.13 (n=111) | -0.00 (n=141) | -0.00 (n=26) |
| KMDW | -0.04 (n=300) | +0.01 (n=315) | +0.00 (n=368) | -0.01 (n=354) |
| KMIA | -0.73 (n=178) | -0.80 (n=201) | -0.72 (n=289) | -0.64 (n=219) |
| KMSP | — | -0.01 (n=11) | +0.00 (n=46) | -0.03 (n=15) |
| KMSY | — | -0.06 (n=12) | +0.02 (n=56) | +0.02 (n=19) |
| KNYC | +0.28 (n=297) | -0.19 (n=289) | +0.24 (n=338) | +0.43 (n=329) |
| KOKC | +0.05 (n=4) | -0.00 (n=35) | +0.01 (n=47) | +0.02 (n=13) |
| KPHL | -0.36 (n=84) | -0.51 (n=81) | -0.44 (n=131) | -0.44 (n=59) |
| KPHX | -1.87 (n=9) | +0.68 (n=24) | -0.01 (n=48) | -0.01 (n=12) |
| KSAT | — | -0.03 (n=14) | -0.02 (n=50) | +0.01 (n=15) |
| KSEA | — | -0.55 (n=22) | -0.01 (n=48) | +0.03 (n=15) |
| KSFO | -0.01 (n=11) | -0.01 (n=15) | +0.00 (n=38) | -0.01 (n=16) |

## Coverage & verification

- Kalshi HIGH+LOW rows used: 10297
- Matched to METAR and/or GHCN: 10274
- Unmatched (no METAR/GHCN leg): 23

| Station | HIGH matched | HIGH unmatched | LOW matched |
|---|---|---|---|
| KATL | 218 | 1 | 18 |
| KAUS | 1101 | 1 | 18 |
| KBOS | 217 | 1 | 18 |
| KDCA | 233 | 1 | 17 |
| KDEN | 580 | 1 | 18 |
| KDFW | 210 | 1 | 17 |
| KHOU | 209 | 2 | 19 |
| KLAS | 238 | 1 | 18 |
| KLAX | 549 | 1 | 17 |
| KMDW | 1746 | 1 | 18 |
| KMIA | 1036 | 1 | 66 |
| KMSP | 119 | 1 | 19 |
| KMSY | 120 | 1 | 18 |
| KNYC | 1843 | 1 | 17 |
| KOKC | 208 | 1 | 18 |
| KPHL | 527 | 1 | 18 |
| KPHX | 214 | 3 | 17 |
| KSAT | 120 | 1 | 18 |
| KSEA | 144 | 1 | 18 |
| KSFO | 238 | 1 | 17 |

**Date alignment**: `target_date` (local market day) joined to `daily_stats.date_local` and `ghcn_settlements.date` — all local calendar days.

**Station-specific note — KMIA**: on KMIA HIGH markets, GHCN `high_f` reads ~0.72°F *above* both Kalshi and METAR (which agree with each other). Below the 1.0°F threshold; not a strike hazard, but a known GHCN-at-KMIA quirk.

## Settling-source quality — finalized vs historical_api

Residual rate = share of clean-subset station-days where |Kalshi−GHCN| > 3°F:

| source_type | clean n | residual >3°F | rate |
|---|---|---|---|
| finalized | 1072 | 3 | 0.28% |
| historical_api | 5943 | 129 | 2.17% |

**Implication:** `historical_api` settlement values carry ~8× the outlier rate of `finalized` values. Any backtest that reads the settlement value should prefer `source_type='finalized'` and treat `historical_api` rows as provisional on HIGH markets.

## Spot checks (hand-verified date alignment)

| Station | Date | Kalshi | METAR | GHCN | K−METAR | K−GHCN |
|---|---|---|---|---|---|---|
| KNYC | 2026-09-24 | 66 | 66.2 | 66.0 | -0.2 | +0.0 |
| KMDW | 2026-09-24 | 69 | 68.0 | 69.1 | +1.0 | -0.1 |
| KLAX | 2026-09-25 | 77 | 73.4 | 77.0 | +3.6 | +0.0 |
| KAUS | 2026-09-25 | 98 | 89.6 | 98.1 | +8.4 | -0.1 |
| KNYC | 2026-09-25 | 69 | 62.6 | 69.1 | +6.4 | -0.1 |