"""Read-only semantic boundary for future calibration adapters (Q-CAL-004).

This module deliberately does not load or write calibration artifacts.  It only
validates and copies a producer record so a future adapter cannot silently lose
market, direction, or hour meaning while reshaping data.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path
import importlib.metadata


class CalibrationSemanticError(ValueError):
    """Raised when a producer record is missing or ambiguous."""


def _required_text(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CalibrationSemanticError(f"{field} must be an explicit non-empty string")
    return value


_CLOCK_BOUNDARY = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _validate_timezone(value: str) -> None:
    # Abbreviations and machine-local labels are ambiguous across producers.
    if value in {"local", "LOCAL", "system", "SYSTEM"} or "/" not in value and value != "UTC":
        raise CalibrationSemanticError("hour_semantics.timezone must be an explicit IANA zone or UTC")
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise CalibrationSemanticError("hour_semantics.timezone must name an installed IANA zone") from exc


def _validate_boundaries(boundaries: list[str] | tuple[str, ...]) -> bool:
    if any(not _CLOCK_BOUNDARY.fullmatch(boundary) for boundary in boundaries):
        raise CalibrationSemanticError("hour_semantics.bucket_boundaries must use HH:MM boundaries")
    parsed = [datetime.strptime(boundary, "%H:%M") for boundary in boundaries]
    if parsed[0] == parsed[1]:
        raise CalibrationSemanticError("hour_semantics.bucket_boundaries must be distinct")
    # A reversed pair is a local-time bucket crossing midnight.  It is valid
    # only when the producer states how the date and UTC offset are governed;
    # never infer either policy from ordering or the machine timezone.
    return parsed[0] > parsed[1]


_ALLOWED_DATE_POLICIES = {"end_on_next_local_date", "same_local_date"}
_ALLOWED_OFFSET_POLICIES = {"iana_rules_at_each_boundary"}


def _validate_local_datetimes(hour: Mapping[str, Any]) -> None:
    """Reject DST gaps/folds; never let zoneinfo silently choose a side."""
    values = hour.get("boundary_local_datetimes")
    if values is None:
        return
    if not isinstance(values, (list, tuple)) or len(values) != 2:
        raise CalibrationSemanticError(
            "hour_semantics.boundary_local_datetimes must contain exactly two values"
        )
    if hour.get("date_policy") not in _ALLOWED_DATE_POLICIES:
        raise CalibrationSemanticError(
            "explicit date_policy is required for local datetime boundaries"
        )
    if hour.get("offset_policy") not in _ALLOWED_OFFSET_POLICIES:
        raise CalibrationSemanticError(
            "explicit offset_policy is required for local datetime boundaries"
        )
    zone = ZoneInfo(hour["timezone"])
    for value in values:
        if not isinstance(value, str) or "T" not in value:
            raise CalibrationSemanticError(
                "hour_semantics.boundary_local_datetimes must use local ISO timestamps"
            )
        try:
            local = datetime.fromisoformat(value)
        except ValueError as exc:
            raise CalibrationSemanticError(
                "hour_semantics.boundary_local_datetimes must use ISO timestamps"
            ) from exc
        if local.tzinfo is not None:
            raise CalibrationSemanticError(
                "hour_semantics.boundary_local_datetimes must not include an inferred offset"
            )
        candidates = [local.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
        round_trips = [
            candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
            for candidate in candidates
        ]
        if any(round_trip != local for round_trip in round_trips):
            raise CalibrationSemanticError(
                "hour_semantics.boundary_local_datetimes contains a nonexistent DST boundary"
            )
        if candidates[0].utcoffset() != candidates[1].utcoffset():
            raise CalibrationSemanticError(
                "hour_semantics.boundary_local_datetimes contains an ambiguous DST boundary"
            )


def _validate_midnight_policies(hour: Mapping[str, Any], crosses_midnight: bool) -> None:
    if not crosses_midnight:
        return
    date_policy = hour.get("date_policy")
    offset_policy = hour.get("offset_policy")
    if date_policy not in _ALLOWED_DATE_POLICIES:
        raise CalibrationSemanticError(
            "midnight-crossing bucket requires explicit date_policy"
        )
    if offset_policy not in _ALLOWED_OFFSET_POLICIES:
        raise CalibrationSemanticError(
            "midnight-crossing bucket requires explicit offset_policy"
        )


def adapt_semantic_fixture(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate and return a read-only, semantically lossless record.

    Required ``hour_semantics`` keys are intentionally explicit: the reference
    point, timezone, and bucket boundaries must travel with the hour value.
    No aliases or key-name inference are accepted.
    """
    if not isinstance(record, Mapping):
        raise CalibrationSemanticError("record must be a mapping")

    market_type = _required_text(record, "market_type")
    direction = _required_text(record, "direction")
    hour = record.get("hour_semantics")
    if not isinstance(hour, Mapping):
        raise CalibrationSemanticError("hour_semantics must be an explicit mapping")

    required_hour_fields = ("measured_from", "timezone", "bucket_boundaries")
    missing = [field for field in required_hour_fields if field not in hour]
    if missing:
        raise CalibrationSemanticError(
            "hour_semantics missing explicit field(s): " + ", ".join(missing)
        )
    if any(not isinstance(hour[field], str) or not hour[field].strip() for field in required_hour_fields[:2]):
        raise CalibrationSemanticError("hour_semantics reference and timezone must be non-empty strings")
    _validate_timezone(hour["timezone"])
    boundaries = hour["bucket_boundaries"]
    if not isinstance(boundaries, (list, tuple)) or len(boundaries) != 2:
        raise CalibrationSemanticError("hour_semantics.bucket_boundaries must contain exactly two boundaries")
    if any(not isinstance(boundary, str) or not boundary.strip() for boundary in boundaries):
        raise CalibrationSemanticError("hour_semantics.bucket_boundaries must be explicit non-empty strings")
    crosses_midnight = _validate_boundaries(boundaries)
    _validate_midnight_policies(hour, crosses_midnight)
    _validate_local_datetimes(hour)

    # Deep-copy before freezing: caller mutation cannot alter the diagnostic.
    # Re-adapting an already adapted record must also work; mappingproxy cannot
    # be deep-copied directly, so rebuild the semantic mapping below.
    normalized = {
        key: deepcopy(value) for key, value in record.items() if key != "hour_semantics"
    }
    normalized["market_type"] = market_type
    normalized["direction"] = direction
    # Freeze the nested mapping too; the diagnostic must remain read-only.
    normalized["hour_semantics"] = MappingProxyType(
        {**deepcopy(dict(hour)), "bucket_boundaries": tuple(boundaries)}
    )
    return MappingProxyType(normalized)


def zone_rule_provenance() -> dict[str, Any]:
    """Report the rule source/version used by this process, if discoverable.

    The version is evidence attached to a diagnostic, not a promise that future
    tzdata releases retain today's transition rules.
    """
    try:
        version = importlib.metadata.version("tzdata")
        source = "python-tzdata-package"
    except importlib.metadata.PackageNotFoundError:
        version = None
        source = "system-zoneinfo"
        for root in TZPATH:
            marker = Path(root) / "tzdata.zi"
            if marker.is_file():
                first = marker.read_text(errors="replace").splitlines()[:1]
                if first and first[0].startswith("# version "):
                    version = first[0].removeprefix("# version ").strip()
                source = str(marker)
                break
    return {"source": source, "version": version, "future_rules_stable": False}


def compare_zone_rule_provenance(
    recorded: Mapping[str, Any], observed: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare recorded and observed tzdata markers without normalizing either.

    This is intentionally a diagnostic-only comparison.  A missing/unknown
    marker is evidence of unknown provenance, not permission to accept drift,
    and the returned copies preserve both original snapshots verbatim.
    """
    recorded_copy = deepcopy(dict(recorded))
    observed_copy = deepcopy(dict(observed))
    recorded_marker = recorded_copy.get("version")
    observed_marker = observed_copy.get("version")

    def _unknown_marker(value: Any) -> bool:
        # Explicit sentinels are unknown evidence too; never treat them as a
        # real version merely because the string is non-empty.
        return value is None or (
            isinstance(value, str)
            and value.strip().lower() in {"", "unknown", "unavailable", "null"}
        )

    if _unknown_marker(recorded_marker) or _unknown_marker(observed_marker):
        status = "mismatch_unknown_marker"
        message = "tzdata provenance mismatch: recorded or observed marker is unknown"
    elif recorded_copy != observed_copy:
        status = "mismatch"
        message = "tzdata provenance mismatch: recorded and observed snapshots differ"
    else:
        status = "match"
        message = "tzdata provenance matches recorded snapshot"
    return {
        "status": status,
        "mismatch": status != "match",
        "message": message,
        "recorded": recorded_copy,
        "observed": observed_copy,
    }


def evaluate_zone_rule_provenance_policy(
    recorded: Mapping[str, Any], observed: Mapping[str, Any]
) -> dict[str, Any]:
    """Apply diagnostic-boundary quarantine policy without rewriting evidence.

    Equal, known snapshots may pass. Any mismatch—including unknown markers—must
    be quarantined and must remain diagnostic-only; this helper does not persist,
    normalize, or mutate either input snapshot.
    """
    comparison = compare_zone_rule_provenance(recorded, observed)
    quarantined = comparison["status"] != "match"
    return {
        "accepted": not quarantined,
        "quarantined": quarantined,
        "status": comparison["status"],
        "reason": (
            "provenance_match"
            if not quarantined
            else "provenance_unknown_or_mismatch"
        ),
        "recorded": comparison["recorded"],
        "observed": comparison["observed"],
    }


def diagnose_semantic_fixture(
    record: Mapping[str, Any], *, include_provenance: bool = False
) -> dict[str, Any]:
    """Return a small diagnostic without mutating state or touching storage.

    Provenance is opt-in for backward-compatible fixture consumers; new
    diagnostics should request it and persist the returned evidence alongside
    the fixture rather than assuming rules remain stable.
    """
    adapted = adapt_semantic_fixture(record)
    diagnostic = {
        "accepted": True,
        "market_type": adapted["market_type"],
        "direction": adapted["direction"],
        "hour_semantics": {
            **deepcopy(dict(adapted["hour_semantics"])),
            "bucket_boundaries": list(adapted["hour_semantics"]["bucket_boundaries"]),
        },
    }
    if include_provenance:
        diagnostic["diagnostic"] = deepcopy(adapted.get("diagnostic", {}))
        diagnostic["zone_rule_provenance"] = zone_rule_provenance()
        recorded = diagnostic["diagnostic"].get("recorded_zone_rule_provenance")
        if isinstance(recorded, Mapping):
            diagnostic["zone_rule_provenance_comparison"] = compare_zone_rule_provenance(
                recorded, diagnostic["zone_rule_provenance"]
            )
    return diagnostic
