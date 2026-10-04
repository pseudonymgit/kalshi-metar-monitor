"""
Guard test: signal taxonomy separation (SIGNAL-TAXONOMY-SPEC-2026-10-03).

The sweep previously reported "Signals with 0 trades (non-functional): 23" as a
single bucket, conflating three structurally different things. Five of those 23
signals are execution gates / modulators that can NEVER trade by design, so
counting them as broken predictors is a category error that hides the real
defects behind noise.

These guards pin the reporting frame:
  1. every registered signal carries a non-null taxonomy class;
  2. every class value is one of the three valid classes;
  3. a modulator or execution gate is NEVER counted in `predictors_zero_trade`,
     even when it produced 0 trades;
  4. the counting helper classifies a fully-zero result set into the right
     buckets (the acceptance shape: only genuine predictors land in the
     zero-trade failure bucket).

Deterministic, no disk writes, no live DB.
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from scripts.sweep_engine import (  # noqa: E402
        build_signal_registry,
        signal_class_of,
        count_zero_trade_by_class,
        SIGNAL_CLASSES,
        VALID_SIGNAL_CLASSES,
    )
    _IMPORT_ERR = None
except Exception as exc:  # pragma: no cover - dependency gap, not a logic fault
    _IMPORT_ERR = exc


def _zero_result():
    return {"aggregate": {"n_trades": 0, "accuracy": 0.0}}


class TaxonomyMapTests(unittest.TestCase):
    """Static-map guards — no registry import needed."""

    def test_valid_classes_are_the_three_spec_classes(self):
        self.assertEqual(
            set(VALID_SIGNAL_CLASSES),
            {"predictor", "modulator", "gate"},
        )

    def test_every_mapped_class_is_valid(self):
        bad = {k: v for k, v in SIGNAL_CLASSES.items()
               if v not in VALID_SIGNAL_CLASSES}
        self.assertEqual(bad, {}, f"invalid taxonomy class values: {bad}")

    def test_spec_classifications_are_pinned(self):
        # The 10 structurally non-directional signals from the spec must be
        # non-predictors; misclassifying one re-introduces the category error.
        spec_modulators = {
            "volume_momentum", "dewpoint_depression", "cloud_cover_index",
            "feels_like_delta", "cross_model_divergence",
        }
        spec_gates = {
            "settlement_arbitrage", "spread_based_entry",
            "frontal_passage_nowcast", "intraday_metar_confirmation",
            "frontal_passage_detector",
        }
        for name in spec_modulators:
            self.assertEqual(signal_class_of(name), "modulator", name)
        for name in spec_gates:
            self.assertEqual(signal_class_of(name), "gate", name)


@unittest.skipIf(_IMPORT_ERR is not None,
                 f"registry import unavailable: {_IMPORT_ERR}")
class RegistryTaxonomyTests(unittest.TestCase):
    """Live-registry guards (require the sweep engine's deps)."""

    def test_every_registry_key_has_a_non_null_class(self):
        registry = build_signal_registry()
        self.assertGreater(len(registry), 0)
        unclassified = [k for k in registry if signal_class_of(k) is None]
        self.assertEqual(
            unclassified, [],
            f"registry signals missing a taxonomy class: {unclassified}",
        )

    def test_registry_classes_are_all_valid(self):
        registry = build_signal_registry()
        bad = {k: signal_class_of(k) for k in registry
               if signal_class_of(k) not in VALID_SIGNAL_CLASSES}
        self.assertEqual(bad, {}, f"invalid classes for registry keys: {bad}")


class CountingTests(unittest.TestCase):
    """Guard: modulators/gates never counted as zero-trade predictors."""

    def test_modulator_and_gate_never_in_predictors_zero_trade(self):
        # Construct an all-zero result set using the real class map.
        results = {name: _zero_result() for name in SIGNAL_CLASSES}
        buckets = count_zero_trade_by_class(results)

        for name in buckets["predictors_zero_trade"]:
            self.assertEqual(
                signal_class_of(name), "predictor",
                f"non-predictor {name!r} counted as a zero-trade predictor",
            )

        # Every modulator/gate is reported separately, none in the failure set.
        expected_mods = {n for n, c in SIGNAL_CLASSES.items() if c == "modulator"}
        expected_gates = {n for n, c in SIGNAL_CLASSES.items() if c == "gate"}
        self.assertEqual(set(buckets["modulators"]), expected_mods)
        self.assertEqual(set(buckets["gates"]), expected_gates)
        self.assertEqual(
            set(buckets["predictors_zero_trade"]) & (expected_mods | expected_gates),
            set(),
        )

    def test_zero_trade_predictors_are_the_only_failures(self):
        results = {name: _zero_result() for name in SIGNAL_CLASSES}
        buckets = count_zero_trade_by_class(results)
        expected = {n for n, c in SIGNAL_CLASSES.items() if c == "predictor"}
        self.assertEqual(set(buckets["predictors_zero_trade"]), expected)
        self.assertEqual(buckets["predictors_trading"], [])
        self.assertEqual(buckets["unclassified"], [])

    def test_trading_predictor_and_zero_predictor_split(self):
        buckets = count_zero_trade_by_class({
            "gaussian": {"aggregate": {"n_trades": 5}},
            "esdr": {"aggregate": {"n_trades": 0}},
            "volume_momentum": {"aggregate": {"n_trades": 0}},
        })
        self.assertEqual(buckets["predictors_trading"], ["gaussian"])
        self.assertEqual(buckets["predictors_zero_trade"], ["esdr"])
        self.assertEqual(buckets["modulators"], ["volume_momentum"])

    def test_unknown_signal_is_unclassified_not_a_predictor_failure(self):
        buckets = count_zero_trade_by_class({"totally_new_signal": _zero_result()})
        self.assertEqual(buckets["unclassified"], ["totally_new_signal"])
        self.assertEqual(buckets["predictors_zero_trade"], [])

    def test_missing_or_odd_trade_count_is_treated_as_zero(self):
        buckets = count_zero_trade_by_class({
            "gaussian": {},                                  # no aggregate
            "esdr": {"aggregate": {"n_trades": None}},       # None
            "regime": {"aggregate": {}},                     # no key
        })
        self.assertEqual(
            set(buckets["predictors_zero_trade"]),
            {"gaussian", "esdr", "regime"},
        )


if __name__ == "__main__":
    unittest.main()
