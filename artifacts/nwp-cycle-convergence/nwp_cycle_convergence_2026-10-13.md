# NWP cycle convergence — early (06Z-12Z) vs late (18Z-00Z) cycles

Window: 2026-04-01 → 2026-10-09. Read-only; truth = settlement_truth HIGH (trustworthy=1).
Early hours [5, 6, 10, 12], late hours [0, 1, 16, 17, 18, 22, 23].

**Design caveat:** The archive stores ~one fetch per model per fetch_date, so fetch HOUR (cycle) is perfectly confounded with fetch DATE (lead time): an early-hour vintage is from an earlier day and therefore a longer lead. A lead-matched early-vs-late pairing is therefore structurally impossible in this archive (0 same-lead pairs). The literal per-station-day test (design A) compares phases at DIFFERENT leads and its sign is unreliable for a pure cycle-age question.

| model | stn-days | vintages (E/L) | MAE early | MAE late | ΔE−L pooled |
|---|---:|---:|---:|---:|---:|
| gfs | 2960 | 11298/3593 | 2.9117 | 3.2351 | -0.3234 |
| gem | 2520 | 6277/3636 | 3.1278 | 3.1157 | 0.0121 |
| icon | 2960 | 11020/3640 | 2.8568 | 3.002 | -0.1452 |
| gefs_ens | 3060 | 0/4210 | None | 4.58 | None |

## Paired tests (Δ = early_err − late_err; positive ⇒ late closer)

| model | design | n | Δ (F) | s.e. | t | p | sig 2s.e. | late ≥0.1F |
|---|---|---:|---:|---:|---:|---:|:---:|:---:|
| gfs | literal | 283 | 0.1995 | 0.1442 | 1.383 | 0.16655 | False | True |
| gfs | lead-matched | 0 | None | None | None | None | False | False |
| gem | literal | 264 | -0.0463 | 0.12272 | -0.377 | 0.70586 | False | False |
| gem | lead-matched | 0 | None | None | None | None | False | False |
| icon | literal | 260 | -0.1051 | 0.12103 | -0.869 | 0.38497 | False | False |
| icon | lead-matched | 0 | None | None | None | None | False | False |
| gefs_ens | literal | 0 | None | None | None | None | False | False |
| gefs_ens | lead-matched | 0 | None | None | None | None | False | False |

## MAE by phase × lead (days)

### gfs
| lead | early MAE | late MAE |
|---:|---:|---:|
| 0 | 2.2771 | 1.7192 |
| 1 | 2.6156 | 2.4242 |
| 2 | 3.1278 | 3.2562 |
| 3 | 3.4325 | 3.5662 |
| 4 | 3.5134 | 3.6526 |
| 5 | 3.8475 | 3.931 |
| 6 | 4.2142 | 4.0949 |
| 15 | 1.886 | None |
| 16 | 3.366 | None |
| 17 | 2.513 | None |
| 18 | 2.009 | None |
| 19 | 3.188 | None |
| 20 | 1.29 | None |
| 21 | 1.63 | None |
| 22 | 2.053 | None |
| 23 | 2.563 | None |
| 24 | 3.467 | None |
| 25 | 2.342 | None |
| 26 | 2.454 | None |
| 27 | 2.104 | None |
| 28 | 1.939 | None |
| 29 | 2.038 | None |
| 30 | 1.493 | None |
| 31 | 1.524 | None |
| 32 | 1.542 | None |
| 33 | 2.475 | None |
| 35 | 2.636 | None |
| 36 | 1.882 | None |
| 37 | 1.547 | None |
| 38 | 1.365 | None |
| 39 | 1.152 | None |
| 40 | 1.702 | None |
| 41 | 1.585 | None |
| 42 | 1.825 | None |
| 43 | 1.776 | None |
| 44 | 1.544 | None |
| 45 | 1.746 | None |
| 46 | 1.361 | None |
| 47 | 1.31 | None |
| 48 | 2.469 | None |
| 49 | 2.35 | None |
| 50 | 2.148 | None |
| 51 | 2.899 | None |
| 52 | 1.894 | None |
| 53 | 1.98 | None |
| 54 | 1.523 | None |
| 55 | 1.899 | None |
| 56 | 2.671 | None |
| 57 | 1.939 | None |
| 58 | 1.323 | None |
| 59 | 1.447 | None |
| 60 | 1.614 | None |
| 61 | 1.645 | None |
| 62 | 2.098 | None |
| 63 | 1.612 | None |
| 64 | 1.286 | None |
| 65 | 1.05 | None |
| 66 | 0.97 | None |
| 67 | 1.511 | None |
| 68 | 1.312 | None |
| 69 | 1.607 | None |
| 70 | 1.261 | None |
| 71 | 1.578 | None |
| 72 | 2.076 | None |
| 73 | 1.707 | None |
| 74 | 1.874 | None |
| 75 | 1.234 | None |
| 76 | 1.22 | None |
| 77 | 0.935 | None |
| 78 | 1.629 | None |
| 79 | 1.799 | None |
| 80 | 1.632 | None |
| 81 | 1.665 | None |
| 82 | 1.85 | None |
| 83 | 2.509 | None |
| 84 | 1.916 | None |
| 85 | 1.705 | None |
| 86 | 1.305 | None |
| 87 | 1.196 | None |
| 88 | 1.676 | None |
| 89 | 3.377 | None |
| 90 | 2.911 | None |
| 91 | 1.41 | None |
| 92 | 2.186 | None |
| 93 | 1.669 | None |
| 94 | 3.314 | None |
| 95 | 5.337 | None |
| 96 | 4.825 | None |
| 97 | 4.919 | None |
| 98 | 5.369 | None |

### gem
| lead | early MAE | late MAE |
|---:|---:|---:|
| 0 | 2.6661 | 2.4352 |
| 1 | 2.9612 | 2.6252 |
| 2 | 3.0406 | 2.8098 |
| 3 | 3.3056 | 2.9884 |
| 4 | 3.402 | 3.4135 |
| 5 | 3.7177 | 3.6749 |
| 6 | 4.1727 | 3.8656 |
| 29 | 2.989 | None |
| 30 | 2.251 | None |
| 31 | 2.107 | None |
| 32 | 2.586 | None |
| 33 | 3.964 | None |
| 35 | 2.906 | None |
| 36 | 2.271 | None |
| 37 | 2.592 | None |
| 38 | 1.712 | None |
| 39 | 2.128 | None |
| 40 | 3.282 | None |
| 41 | 2.118 | None |
| 42 | 3.169 | None |
| 43 | 2.09 | None |
| 44 | 1.929 | None |
| 45 | 2.779 | None |
| 46 | 2.345 | None |
| 47 | 2.353 | None |
| 48 | 2.998 | None |
| 49 | 2.0 | None |
| 50 | 2.447 | None |
| 51 | 2.775 | None |
| 52 | 2.066 | None |
| 53 | 3.108 | None |
| 54 | 2.358 | None |
| 55 | 2.1 | None |
| 56 | 2.574 | None |
| 57 | 2.501 | None |
| 58 | 2.484 | None |
| 59 | 2.319 | None |
| 60 | 2.401 | None |
| 61 | 2.876 | None |
| 62 | 3.152 | None |
| 63 | 3.059 | None |
| 64 | 2.66 | None |
| 65 | 2.24 | None |
| 66 | 1.92 | None |
| 67 | 2.284 | None |
| 68 | 2.732 | None |
| 69 | 2.166 | None |
| 70 | 2.183 | None |
| 71 | 2.083 | None |
| 72 | 3.269 | None |
| 73 | 3.059 | None |
| 74 | 2.966 | None |
| 75 | 2.536 | None |
| 76 | 2.46 | None |
| 77 | 2.112 | None |
| 78 | 1.628 | None |
| 79 | 2.322 | None |
| 80 | 2.26 | None |
| 81 | 2.284 | None |
| 82 | 2.471 | None |
| 83 | 3.154 | None |
| 84 | 2.016 | None |
| 85 | 1.797 | None |
| 86 | 1.791 | None |
| 87 | 1.686 | None |
| 88 | 2.899 | None |
| 89 | 2.518 | None |
| 90 | 2.916 | None |
| 91 | 2.274 | None |
| 92 | 3.288 | None |
| 93 | 2.739 | None |
| 94 | 2.824 | None |
| 95 | 3.067 | None |
| 96 | 2.329 | None |
| 97 | 2.914 | None |
| 98 | 3.544 | None |

### icon
| lead | early MAE | late MAE |
|---:|---:|---:|
| 0 | 2.4178 | 2.0329 |
| 1 | 2.6048 | 2.3395 |
| 2 | 2.8971 | 2.6572 |
| 3 | 3.3051 | 2.9242 |
| 4 | 3.1466 | 3.3045 |
| 5 | 3.574 | 3.6798 |
| 6 | 3.944 | 4.0759 |
| 31 | 1.414 | None |
| 32 | 1.543 | None |
| 33 | 2.992 | None |
| 35 | 2.366 | None |
| 36 | 1.689 | None |
| 37 | 1.77 | None |
| 38 | 1.726 | None |
| 39 | 1.942 | None |
| 40 | 2.123 | None |
| 41 | 1.814 | None |
| 42 | 2.098 | None |
| 43 | 1.897 | None |
| 44 | 1.925 | None |
| 45 | 2.332 | None |
| 46 | 1.465 | None |
| 47 | 2.331 | None |
| 48 | 2.954 | None |
| 49 | 3.342 | None |
| 50 | 2.447 | None |
| 51 | 3.208 | None |
| 52 | 2.237 | None |
| 53 | 2.349 | None |
| 54 | 1.745 | None |
| 55 | 2.639 | None |
| 56 | 2.754 | None |
| 57 | 2.027 | None |
| 58 | 1.9 | None |
| 59 | 2.1 | None |
| 60 | 2.279 | None |
| 61 | 2.104 | None |
| 62 | 2.835 | None |
| 63 | 1.677 | None |
| 64 | 1.946 | None |
| 65 | 1.19 | None |
| 66 | 1.215 | None |
| 67 | 1.974 | None |
| 68 | 1.678 | None |
| 69 | 1.973 | None |
| 70 | 1.39 | None |
| 71 | 1.717 | None |
| 72 | 2.621 | None |
| 73 | 2.267 | None |
| 74 | 1.938 | None |
| 75 | 1.455 | None |
| 76 | 1.336 | None |
| 77 | 1.615 | None |
| 78 | 1.971 | None |
| 79 | 1.91 | None |
| 80 | 2.117 | None |
| 81 | 1.904 | None |
| 82 | 2.605 | None |
| 83 | 3.108 | None |
| 84 | 2.078 | None |
| 85 | 2.857 | None |
| 86 | 1.935 | None |
| 87 | 1.358 | None |
| 88 | 2.558 | None |
| 89 | 3.04 | None |
| 90 | 2.303 | None |
| 91 | 1.809 | None |
| 92 | 2.268 | None |
| 93 | 2.0 | None |
| 94 | 2.618 | None |
| 95 | 3.37 | None |
| 96 | 2.557 | None |
| 97 | 2.674 | None |
| 98 | 3.799 | None |

### gefs_ens
| lead | early MAE | late MAE |
|---:|---:|---:|
| -118 | None | 5.5531 |
| -117 | None | 9.0274 |
| -116 | None | 8.3367 |
| -115 | None | 5.7727 |
| -114 | None | 8.7479 |
| -113 | None | 4.1743 |
| -112 | None | 5.5388 |
| -111 | None | 6.7179 |
| -110 | None | 5.2119 |
| -109 | None | 6.5686 |
| -108 | None | 3.3196 |
| -107 | None | 6.148 |
| -106 | None | 6.7563 |
| -105 | None | 6.0346 |
| -104 | None | 4.2755 |
| -103 | None | 5.0179 |
| -102 | None | 7.1028 |
| -101 | None | 8.0451 |
| -100 | None | 9.0211 |
| -99 | None | 4.603 |
| -98 | None | 7.3102 |
| -97 | None | 7.3728 |
| -96 | None | 7.7758 |
| -95 | None | 5.7248 |
| -94 | None | 6.7441 |
| -93 | None | 5.5718 |
| -92 | None | 5.9469 |
| -91 | None | 5.9522 |
| -90 | None | 6.1985 |
| -89 | None | 5.2251 |
| -88 | None | 5.958 |
| -87 | None | 7.3909 |
| -86 | None | 5.9841 |
| -85 | None | 6.8264 |
| -84 | None | 8.3622 |
| -83 | None | 7.3702 |
| -82 | None | 8.2086 |
| -81 | None | 5.5298 |
| -80 | None | 5.5843 |
| -79 | None | 6.6881 |
| -78 | None | 9.2723 |
| -77 | None | 6.5809 |
| -76 | None | 6.1376 |
| -75 | None | 4.4435 |
| -74 | None | 5.0505 |
| -73 | None | 7.0367 |
| -72 | None | 5.2722 |
| -71 | None | 5.635 |
| -70 | None | 5.8301 |
| -69 | None | 4.5996 |
| -68 | None | 8.5535 |
| -67 | None | 6.0367 |
| -66 | None | 5.8755 |
| -65 | None | 5.4756 |
| -64 | None | 5.8817 |
| -63 | None | 3.6146 |
| -62 | None | 5.3535 |
| -60 | None | 5.472 |
| -59 | None | 5.2184 |
| -58 | None | 6.7791 |
| -57 | None | 5.0485 |
| -56 | None | 5.5154 |
| -55 | None | 6.7476 |
| -54 | None | 4.6865 |
| -53 | None | 4.0553 |
| -52 | None | 2.1174 |
| -51 | None | 3.2486 |
| -50 | None | 3.724 |
| -49 | None | 3.7801 |
| -48 | None | 4.935 |
| -47 | None | 5.9848 |
| -46 | None | 4.7385 |
| -45 | None | 4.1045 |
| -44 | None | 5.9447 |
| -43 | None | 7.1544 |
| -42 | None | 5.8239 |
| -41 | None | 4.5091 |
| -40 | None | 4.2803 |
| -39 | None | 5.503 |
| -38 | None | 4.8014 |
| -37 | None | 3.3299 |
| -36 | None | 4.2378 |
| -35 | None | 5.3135 |
| -34 | None | 5.1698 |
| -33 | None | 5.2281 |
| -32 | None | 4.098 |
| -31 | None | 4.0415 |
| -30 | None | 3.1116 |
| -29 | None | 4.7501 |
| -28 | None | 3.8267 |
| -27 | None | 3.8953 |
| -26 | None | 4.7906 |
| -25 | None | 2.8235 |
| -24 | None | 2.9245 |
| -23 | None | 6.3875 |
| -22 | None | 5.6826 |
| -21 | None | 4.2692 |
| -20 | None | 4.247 |
| -19 | None | 4.0368 |
| -18 | None | 3.3276 |
| -17 | None | 3.7798 |
| -16 | None | 3.44 |
| -15 | None | 4.2896 |
| -14 | None | 5.415 |
| -13 | None | 4.7154 |
| -12 | None | 5.2393 |
| -11 | None | 3.5239 |
| -10 | None | 3.8366 |
| -9 | None | 3.4783 |
| -8 | None | 3.4198 |
| -7 | None | 5.1208 |
| -6 | None | 3.735 |
| -5 | None | 6.4295 |
| -4 | None | 4.4528 |
| -3 | None | 4.9739 |
| -2 | None | 4.0792 |
| -1 | None | 3.3284 |
| 0 | None | 2.8659 |
| 1 | None | 3.6709 |
| 2 | None | 3.0864 |
| 3 | None | 3.2227 |
| 4 | None | 3.6246 |
| 5 | None | 3.9031 |
| 6 | None | 4.6218 |
| 7 | None | 3.4983 |
| 8 | None | 2.2675 |
| 9 | None | 3.1537 |
| 10 | None | 2.4376 |

## Verdict

- **gfs**: lead-matched pairs n=0 → actionable=False. Literal Δ=0.1995 F (n=283, sig@2s.e.=False).
- **gem**: lead-matched pairs n=0 → actionable=False. Literal Δ=-0.0463 F (n=264, sig@2s.e.=False).
- **icon**: lead-matched pairs n=0 → actionable=False. Literal Δ=-0.1051 F (n=260, sig@2s.e.=False).
- **gefs_ens**: lead-matched pairs n=0 → actionable=False. Literal Δ=None F (n=0, sig@2s.e.=False).

Because the archive fetches ~once per model per day, cycle phase and lead time are confounded and no lead-matched early-vs-late comparison exists. The literal Δ therefore measures the lead-time gradient, not cycle-age skill: do NOT weight by cycle-age on this evidence. `actionable` is true only when the lead-matched test has pairs and clears both the 2 s.e. and the 0.1 F bars.
