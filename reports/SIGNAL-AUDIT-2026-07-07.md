# Signal Audit Report — DTR Trend + Regime

**Date:** 2026-07-07 00:48 UTC
**Owner:** Gilfoyle (subagent)
**Status:** COMPLETE

---

## Summary

Ran standalone accuracy, Brier, ECE analysis for dtr_trend and regime_signal.

---

## Results

### DTR Trend Signal

| Metric | Value |
|--------|-------|
| Accuracy | 0.5030 |
| Brier Score | 0.3233 |
| ECE | 0.2731 |
| Trade Count | 19536 |

### Regime Signal

| Metric | Value |
|--------|-------|
| Accuracy | 0.3500 |
| Brier Score | 0.3436 |
| ECE | 0.5470 |
| Trade Count | 40 |

---

## Thresholds

- **Accuracy threshold:** 58% directional accuracy
- **Minimum trades threshold:** 500 trades

---

## Recommendation

### DTR Trend Signal

- **DROP**
- Accuracy 0.502968877968878 < 58%

### Regime Signal

- **DROP**
- Accuracy 0.35 < 58%

---

## Notes

- Both signals use deterministic calculations only
- No AI/ML in prediction/execution loop
- Results based on real METAR settlement data (2021-08-27 to 2025-08-27)
