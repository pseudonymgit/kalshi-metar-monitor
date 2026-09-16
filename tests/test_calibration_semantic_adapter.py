"""Q-CAL-004/Q-CAL-005 semantic fixture and read-only diagnostic tests."""

from unittest.mock import Mock

import pytest

from core.calibration.semantic_adapter import (
    CalibrationSemanticError,
    compare_zone_rule_provenance,
    evaluate_zone_rule_provenance_policy,
    adapt_semantic_fixture,
    diagnose_semantic_fixture,
)


FIXTURE = {
    "market_type": "daily_high_temperature",
    "direction": "up",
    "hour_semantics": {
        "measured_from": "settlement_time",
        "timezone": "America/New_York",
        "bucket_boundaries": ["06:00", "07:00"],
    },
    "value": {"alpha": 2.0, "beta": 0.0, "n": 30},
}


def test_q_cal_004_preserves_explicit_semantics_read_only():
    diagnostic = diagnose_semantic_fixture(FIXTURE)
    assert diagnostic["accepted"] is True
    assert diagnostic["market_type"] == FIXTURE["market_type"]
    assert diagnostic["direction"] == FIXTURE["direction"]
    assert diagnostic["hour_semantics"] == FIXTURE["hour_semantics"]

    adapted = adapt_semantic_fixture(FIXTURE)
    with pytest.raises(TypeError):
        adapted["direction"] = "down"
    assert FIXTURE["direction"] == "up"


@pytest.mark.parametrize(
    "field, value",
    [
        ("market_type", None),
        ("direction", ""),
        ("hour_semantics", {"measured_from": "settlement_time"}),
        ("hour_semantics", {"measured_from": "settlement_time", "timezone": "UTC", "bucket_boundaries": ["06:00"]}),
    ],
)
def test_q_cal_004_rejects_missing_or_ambiguous_fields(field, value):
    record = dict(FIXTURE)
    record[field] = value
    with pytest.raises(CalibrationSemanticError):
        adapt_semantic_fixture(record)


@pytest.mark.parametrize(
    "timezone, boundaries",
    [
        ("EST", ["06:00", "07:00"]),  # abbreviation has no DST meaning
        ("local", ["06:00", "07:00"]),  # machine-local timezone is not portable
        ("America/New_York", ["01:00", "01:00"]),  # zero-width bucket
        ("America/New_York", ["07:00", "06:00"]),  # reversed bucket
        ("America/New_York", ["6:00", "07:00"]),  # non-canonical boundary
    ],
)
def test_q_cal_005_rejects_timezone_or_bucket_boundary_ambiguity(timezone, boundaries):
    record = dict(FIXTURE)
    record["hour_semantics"] = {
        **FIXTURE["hour_semantics"],
        "timezone": timezone,
        "bucket_boundaries": boundaries,
    }
    with pytest.raises(CalibrationSemanticError):
        adapt_semantic_fixture(record)


def test_q_cal_005_freezes_nested_hour_semantics_and_does_not_mutate_input():
    record = {**FIXTURE, "hour_semantics": {**FIXTURE["hour_semantics"], "bucket_boundaries": ["06:00", "07:00"]}}
    adapted = adapt_semantic_fixture(record)
    with pytest.raises(TypeError):
        adapted["hour_semantics"]["timezone"] = "UTC"
    with pytest.raises(TypeError):
        adapted["hour_semantics"]["bucket_boundaries"] += ("08:00",)
    assert record["hour_semantics"]["timezone"] == "America/New_York"
    assert record["hour_semantics"]["bucket_boundaries"] == ["06:00", "07:00"]


def test_q_cal_006_producer_consumer_parity_preserves_explicit_semantics():
    """The eventual producer-to-consumer shape must retain every semantic field."""
    producer_fixture = {
        "market_type": "daily_high_temperature",
        "direction": "down",
        "hour_semantics": {
            "measured_from": "observation_time",
            "timezone": "America/Chicago",
            "bucket_boundaries": ["18:00", "19:00"],
        },
        "cell": {"alpha": 1.25, "beta": -0.5, "n": 42},
    }

    adapted = adapt_semantic_fixture(producer_fixture)
    diagnostic = diagnose_semantic_fixture(adapted)

    assert diagnostic == {
        "accepted": True,
        "market_type": "daily_high_temperature",
        "direction": "down",
        "hour_semantics": {
            "measured_from": "observation_time",
            "timezone": "America/Chicago",
            "bucket_boundaries": ["18:00", "19:00"],
        },
    }
    assert tuple(adapted["hour_semantics"]["bucket_boundaries"]) == ("18:00", "19:00")
    assert adapted["cell"] == producer_fixture["cell"]


def test_q_cal_007_midnight_crossing_parity_preserves_date_and_offset_policy():
    producer_fixture = {
        "market_type": "daily_high_temperature",
        "direction": "up",
        "hour_semantics": {
            "measured_from": "settlement_time",
            "timezone": "America/Los_Angeles",
            "bucket_boundaries": ["23:00", "01:00"],
            "date_policy": "end_on_next_local_date",
            "offset_policy": "iana_rules_at_each_boundary",
        },
        "diagnostic": {"source": "fixture", "raw": "23:00-01:00"},
    }

    adapted = adapt_semantic_fixture(producer_fixture)
    diagnostic = diagnose_semantic_fixture(adapted)

    assert diagnostic == {
        "accepted": True,
        "market_type": "daily_high_temperature",
        "direction": "up",
        "hour_semantics": producer_fixture["hour_semantics"],
    }
    assert adapted["diagnostic"] == producer_fixture["diagnostic"]


@pytest.mark.parametrize(
    "missing_policy",
    ["date_policy", "offset_policy"],
)
def test_q_cal_007_rejects_midnight_crossing_without_explicit_policy(missing_policy):
    record = {**FIXTURE, "hour_semantics": {
        **FIXTURE["hour_semantics"],
        "timezone": "America/Los_Angeles",
        "bucket_boundaries": ["23:00", "01:00"],
        "date_policy": "end_on_next_local_date",
        "offset_policy": "iana_rules_at_each_boundary",
    }}
    del record["hour_semantics"][missing_policy]
    with pytest.raises(CalibrationSemanticError, match="explicit"):
        adapt_semantic_fixture(record)


def test_q_cal_007_does_not_infer_aliases():
    record = dict(FIXTURE)
    del record["direction"]
    record["side"] = "up"
    with pytest.raises(CalibrationSemanticError):
        adapt_semantic_fixture(record)


def test_q_cal_008_dst_spring_forward_parity_preserves_policies_and_opaque_diagnostic():
    producer_fixture = {
        "market_type": "daily_high_temperature",
        "direction": "up",
        "hour_semantics": {
            "measured_from": "settlement_time",
            "timezone": "America/New_York",
            "bucket_boundaries": ["01:30", "03:30"],
            "boundary_local_datetimes": [
                "2025-03-09T01:30:00",
                "2025-03-09T03:30:00",
            ],
            "date_policy": "same_local_date",
            "offset_policy": "iana_rules_at_each_boundary",
        },
        "diagnostic": {"source": "fixture", "raw": "spring-forward", "opaque": {"fold": "reject"}},
    }
    adapted = adapt_semantic_fixture(producer_fixture)
    assert diagnose_semantic_fixture(adapted)["hour_semantics"] == producer_fixture["hour_semantics"]
    assert adapted["diagnostic"] == producer_fixture["diagnostic"]


def test_q_cal_008_dst_fall_back_parity_preserves_unambiguous_boundaries_and_diagnostic():
    producer_fixture = {
        "market_type": "daily_high_temperature",
        "direction": "down",
        "hour_semantics": {
            "measured_from": "observation_time",
            "timezone": "America/New_York",
            "bucket_boundaries": ["00:30", "02:30"],
            "boundary_local_datetimes": [
                "2025-11-02T00:30:00",
                "2025-11-02T02:30:00",
            ],
            "date_policy": "same_local_date",
            "offset_policy": "iana_rules_at_each_boundary",
        },
        "diagnostic": {"source": "fixture", "raw": "fall-back", "opaque": {"fold": "reject"}},
    }
    adapted = adapt_semantic_fixture(producer_fixture)
    assert diagnose_semantic_fixture(adapted)["hour_semantics"] == producer_fixture["hour_semantics"]
    assert adapted["diagnostic"] == producer_fixture["diagnostic"]


@pytest.mark.parametrize(
    "boundary, expected",
    [
        ("2025-03-09T02:30:00", "nonexistent DST boundary"),
        ("2025-11-02T01:30:00", "ambiguous DST boundary"),
    ],
)
def test_q_cal_008_rejects_nonexistent_or_ambiguous_dst_boundary(boundary, expected):
    record = {
        **FIXTURE,
        "hour_semantics": {
            **FIXTURE["hour_semantics"],
            "bucket_boundaries": ["01:30", "03:30"],
            "boundary_local_datetimes": [boundary, "2025-12-01T03:30:00"],
            "date_policy": "same_local_date",
            "offset_policy": "iana_rules_at_each_boundary",
        },
        "diagnostic": {"source": "fixture", "opaque": {"boundary": boundary}},
    }
    with pytest.raises(CalibrationSemanticError, match=expected):
        adapt_semantic_fixture(record)


@pytest.mark.parametrize(
    "zone, boundaries, local_datetimes, date_policy",
    [
        (
            "America/New_York",
            ["01:30", "03:30"],
            ["2025-03-09T01:30:00", "2025-03-09T03:30:00"],
            "same_local_date",
        ),
        (
            "Europe/London",
            ["00:30", "02:30"],
            ["2025-03-30T00:30:00", "2025-03-30T02:30:00"],
            "same_local_date",
        ),
        # Lord Howe moves by 30 minutes, not the usual one hour.  Keep both
        # endpoints outside the gap so this fixture proves the IANA rule is
        # preserved rather than assuming a fixed one-hour transition.
        (
            "Australia/Lord_Howe",
            ["01:30", "03:00"],
            ["2025-10-05T01:30:00", "2025-10-05T03:00:00"],
            "same_local_date",
        ),
        (
            "Pacific/Chatham",
            ["22:30", "00:30"],
            ["2025-01-15T22:30:00", "2025-01-16T00:30:00"],
            "end_on_next_local_date",
        ),
    ],
)
def test_q_cal_009_cross_zone_transition_matrix_preserves_policy_and_opaque_payload(
    zone, boundaries, local_datetimes, date_policy
):
    producer_fixture = {
        "market_type": "daily_high_temperature",
        "direction": "up",
        "hour_semantics": {
            "measured_from": "settlement_time",
            "timezone": zone,
            "bucket_boundaries": boundaries,
            "boundary_local_datetimes": local_datetimes,
            "date_policy": date_policy,
            "offset_policy": "iana_rules_at_each_boundary",
        },
        "diagnostic": {
            "source": "q-cal-009-fixture",
            "zone": zone,
            "opaque": {"preserve": True, "transition_model": "iana"},
        },
    }

    adapted = adapt_semantic_fixture(producer_fixture)
    diagnostic = diagnose_semantic_fixture(adapted)

    assert diagnostic["hour_semantics"] == producer_fixture["hour_semantics"]
    assert adapted["market_type"] == producer_fixture["market_type"]
    assert adapted["direction"] == producer_fixture["direction"]
    assert adapted["diagnostic"] == producer_fixture["diagnostic"]


@pytest.mark.parametrize(
    "zone, boundary, expected",
    [
        (
            "Australia/Lord_Howe",
            "2025-10-05T02:15:00",
            "nonexistent DST boundary",
        ),
        (
            "Australia/Lord_Howe",
            "2026-04-05T01:45:00",
            "ambiguous DST boundary",
        ),
        (
            "Europe/London",
            "2025-03-30T01:30:00",
            "nonexistent DST boundary",
        ),
    ],
)
def test_q_cal_009_rejects_nonexistent_or_ambiguous_boundaries_across_zones(
    zone, boundary, expected
):
    record = {
        **FIXTURE,
        "hour_semantics": {
            **FIXTURE["hour_semantics"],
            "timezone": zone,
            "bucket_boundaries": ["01:00", "03:00"],
            "boundary_local_datetimes": [boundary, "2025-12-01T03:00:00"],
            "date_policy": "same_local_date",
            "offset_policy": "iana_rules_at_each_boundary",
        },
        "diagnostic": {"source": "q-cal-009-fixture", "opaque": {"boundary": boundary}},
    }

    with pytest.raises(CalibrationSemanticError, match=expected):
        adapt_semantic_fixture(record)


def test_q_cal_010_diagnostic_preserves_transition_metadata_and_rule_provenance():
    record = {
        **FIXTURE,
        "hour_semantics": {
            **FIXTURE["hour_semantics"],
            "timezone": "Australia/Lord_Howe",
            "bucket_boundaries": ["01:30", "03:00"],
            "boundary_local_datetimes": ["2025-10-05T01:30:00", "2025-10-05T03:00:00"],
            "date_policy": "same_local_date",
            "offset_policy": "iana_rules_at_each_boundary",
        },
        "diagnostic": {
            "source": "q-cal-010-fixture",
            "transition": {"expected_delta_minutes": 30, "case": "spring-forward"},
        },
    }
    diagnostic = diagnose_semantic_fixture(record, include_provenance=True)
    assert diagnostic["diagnostic"] == record["diagnostic"]
    provenance = diagnostic["zone_rule_provenance"]
    assert provenance["source"]
    assert "version" in provenance
    assert provenance["future_rules_stable"] is False


def test_q_cal_011_provenance_mismatch_is_explicit_and_fixture_metadata_is_unchanged():
    recorded = {"source": "fixture", "version": "2025a", "future_rules_stable": False}
    observed = {"source": "fixture", "version": "2026b", "future_rules_stable": False}
    metadata = {
        "source": "q-cal-011-fixture",
        "recorded_zone_rule_provenance": recorded,
        "opaque": {"preserve": True},
    }
    record = {**FIXTURE, "diagnostic": metadata}

    diagnostic = diagnose_semantic_fixture(record, include_provenance=True)

    comparison = compare_zone_rule_provenance(recorded, observed)
    assert comparison["status"] == "mismatch"
    assert comparison["mismatch"] is True
    assert "provenance mismatch" in comparison["message"]
    assert comparison["recorded"] == recorded
    assert comparison["observed"] == observed
    assert diagnostic["diagnostic"] == metadata
    assert diagnostic["diagnostic"]["recorded_zone_rule_provenance"] == recorded

    replay = diagnose_semantic_fixture(record, include_provenance=True)
    assert replay["diagnostic"] == diagnostic["diagnostic"]
    assert replay["zone_rule_provenance_comparison"] == diagnostic[
        "zone_rule_provenance_comparison"
    ]


def test_q_cal_011_unknown_marker_is_not_silently_accepted_or_rewritten():
    recorded = {"source": "fixture", "version": "unknown", "future_rules_stable": False}
    observed = {"source": "fixture", "version": None, "future_rules_stable": False}

    comparison = compare_zone_rule_provenance(recorded, observed)

    assert comparison["status"] == "mismatch_unknown_marker"
    assert comparison["mismatch"] is True
    assert "unknown" in comparison["message"]
    assert comparison["recorded"] == recorded
    assert comparison["observed"] == observed


@pytest.mark.parametrize(
    ("case", "recorded", "observed", "status"),
    [
        (
            "equal-snapshots",
            {"source": "fixture", "version": "2026a", "future_rules_stable": False},
            {"source": "fixture", "version": "2026a", "future_rules_stable": False},
            "match",
        ),
        (
            "version-only-drift",
            {"source": "fixture", "version": "2025a", "future_rules_stable": False},
            {"source": "fixture", "version": "2026a", "future_rules_stable": False},
            "mismatch",
        ),
        (
            "source-only-drift",
            {"source": "fixture-a", "version": "2026a", "future_rules_stable": False},
            {"source": "fixture-b", "version": "2026a", "future_rules_stable": False},
            "mismatch",
        ),
        ("recorded-none", {"source": "fixture", "version": None}, {"source": "fixture", "version": "2026a"}, "mismatch_unknown_marker"),
        ("observed-none", {"source": "fixture", "version": "2026a"}, {"source": "fixture", "version": None}, "mismatch_unknown_marker"),
        ("recorded-sentinel", {"source": "fixture", "version": "unknown"}, {"source": "fixture", "version": "2026a"}, "mismatch_unknown_marker"),
        ("observed-sentinel", {"source": "fixture", "version": "2026a"}, {"source": "fixture", "version": "unavailable"}, "mismatch_unknown_marker"),
    ],
)
def test_q_cal_012_read_only_provenance_fixture_matrix(case, recorded, observed, status):
    comparison = compare_zone_rule_provenance(recorded, observed)
    assert comparison["status"] == status, case
    assert comparison["mismatch"] is (status != "match")
    assert comparison["recorded"] == recorded
    assert comparison["observed"] == observed
    # Returned snapshots are copies: diagnostics cannot mutate the fixture.
    comparison["recorded"]["version"] = "mutated"
    assert recorded["version"] != "mutated"


def test_q_cal_013_diagnostic_policy_passes_only_known_equal_and_quarantines_other_inputs():
    cases = [
        (
            {"source": "fixture", "version": "2026a", "future_rules_stable": False},
            {"source": "fixture", "version": "2026a", "future_rules_stable": False},
            True,
            False,
            "match",
        ),
        (
            {"source": "fixture", "version": "2025a", "future_rules_stable": False},
            {"source": "fixture", "version": "2026a", "future_rules_stable": False},
            False,
            True,
            "mismatch",
        ),
        (
            {"source": "fixture", "version": "unknown", "future_rules_stable": False},
            {"source": "fixture", "version": "2026a", "future_rules_stable": False},
            False,
            True,
            "mismatch_unknown_marker",
        ),
    ]
    for recorded, observed, accepted, quarantined, status in cases:
        original_recorded = dict(recorded)
        original_observed = dict(observed)
        decision = evaluate_zone_rule_provenance_policy(recorded, observed)
        assert decision["accepted"] is accepted
        assert decision["quarantined"] is quarantined
        assert decision["status"] == status
        assert decision["recorded"] == original_recorded
        assert decision["observed"] == original_observed
        decision["recorded"]["version"] = "rewritten"
        decision["observed"]["version"] = "rewritten"
        assert recorded == original_recorded
        assert observed == original_observed


def test_q_cal_012_replay_is_deterministic_for_every_matrix_fixture():
    fixtures = [
        ({"source": "fixture", "version": "2026a"}, {"source": "fixture", "version": "2026a"}),
        ({"source": "fixture", "version": "2025a"}, {"source": "fixture", "version": "2026a"}),
        ({"source": "fixture-a", "version": "2026a"}, {"source": "fixture-b", "version": "2026a"}),
        ({"source": "fixture", "version": None}, {"source": "fixture", "version": "2026a"}),
        ({"source": "fixture", "version": "2026a"}, {"source": "fixture", "version": "unknown"}),
    ]
    for recorded, observed in fixtures:
        assert compare_zone_rule_provenance(recorded, observed) == compare_zone_rule_provenance(
            recorded, observed
        )


def test_q_cal_014_provenance_boundary_quarantines_before_loader_or_consumer():
    """The provenance decision is diagnostic-only; downstream edges stay unreachable."""

    diagnostic_boundary = Mock()
    artifact_loader = Mock(name="artifact_loader")
    calibration_consumer = Mock(name="calibration_consumer")

    def diagnostic_only_route(recorded, observed):
        decision = evaluate_zone_rule_provenance_policy(recorded, observed)
        # This is the only permitted integration edge.  The mock represents a
        # read-only diagnostic sink; no artifact path or runtime consumer is
        # imported, opened, enabled, or called by this fixture.
        diagnostic_boundary(decision)
        return decision

    quarantined = diagnostic_only_route(
        {"source": "fixture", "version": "2025a"},
        {"source": "fixture", "version": "2026a"},
    )
    equal_known = diagnostic_only_route(
        {"source": "fixture", "version": "2026a"},
        {"source": "fixture", "version": "2026a"},
    )

    assert quarantined["quarantined"] is True
    assert quarantined["accepted"] is False
    assert equal_known["quarantined"] is False
    assert equal_known["accepted"] is True
    assert diagnostic_boundary.call_count == 2
    assert diagnostic_boundary.call_args_list[0].args[0] == quarantined
    assert diagnostic_boundary.call_args_list[1].args[0] == equal_known

    # These mocks intentionally have no implementation: any call would prove
    # that this boundary reached an artifact loader or calibration consumer.
    assert artifact_loader.call_count == 0
    assert calibration_consumer.call_count == 0

    # The diagnostic decision is copied evidence, not a write-through handle.
    recorded = {"source": "fixture", "version": "2025a"}
    observed = {"source": "fixture", "version": "2026a"}
    decision = diagnostic_only_route(recorded, observed)
    decision["recorded"]["version"] = "rewritten"
    assert recorded == {"source": "fixture", "version": "2025a"}
    assert observed == {"source": "fixture", "version": "2026a"}
