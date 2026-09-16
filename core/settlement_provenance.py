"""Explicit settlement provenance adapters.

This module is an ingestion boundary only.  It does not declare that METAR or
GHCN values are Kalshi outcomes: each source keeps its own semantic role and
source payload.  Consumers must opt into the role they support.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional


class SettlementSource(str, Enum):
    KALSHI_JSON = "kalshi_json"
    METAR_EPOCH = "metar_settlement_epoch"
    GHCN_PROXY = "ghcn_proxy"


class SettlementRole(str, Enum):
    KALSHI_AUTHORITATIVE = "kalshi_authoritative"
    OBSERVED_EPOCH = "metar_observed_epoch"
    PROXY = "ghcn_proxy"


@dataclass(frozen=True)
class SettlementRecord:
    source: SettlementSource
    role: SettlementRole
    station: str
    local_trading_date: str
    market_type: Optional[str]
    value_f: float
    source_id: str
    source_payload: Mapping[str, Any]
    settlement_timestamp_utc: Optional[str] = None
    provenance_note: str = ""

    @property
    def is_authoritative_outcome(self) -> bool:
        return self.role is SettlementRole.KALSHI_AUTHORITATIVE


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} is required")
    return result


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if result != result or result in (float("inf"), float("-inf")):
        raise ValueError(f"{field} must be finite")
    return result


def adapt_kalshi_json(payload: Mapping[str, Any]) -> SettlementRecord:
    """Adapt a Kalshi settlement artifact without changing its outcome."""
    value = payload.get("expiration_value", payload.get("kalshi_temp"))
    if value is None:
        raise ValueError("Kalshi payload requires expiration_value or kalshi_temp")
    station = payload.get("station") or payload.get("settlement_station")
    date = payload.get("target_date") or payload.get("local_trading_date")
    source_id = payload.get("event_ticker") or payload.get("market_ticker")
    return SettlementRecord(
        source=SettlementSource.KALSHI_JSON,
        role=SettlementRole.KALSHI_AUTHORITATIVE,
        station=_text(station, "station").upper(),
        local_trading_date=_text(date, "target_date"),
        market_type=str(payload.get("market_type", "HIGH")).upper(),
        value_f=_number(value, "expiration_value"),
        source_id=_text(source_id, "event_ticker"),
        source_payload=dict(payload),
        settlement_timestamp_utc=payload.get("settled_at") or payload.get("fetched_at"),
        provenance_note="Kalshi JSON expiration value; authoritative market outcome.",
    )


def adapt_metar_epoch(payload: Mapping[str, Any]) -> SettlementRecord:
    """Adapt a locally derived METAR settlement epoch as an observation."""
    value = payload.get("settlement_bucket")
    source_id = payload.get("id") or payload.get("settlement_transition_event_id")
    return SettlementRecord(
        source=SettlementSource.METAR_EPOCH,
        role=SettlementRole.OBSERVED_EPOCH,
        station=_text(payload.get("station"), "station").upper(),
        local_trading_date=_text(payload.get("local_trading_date"), "local_trading_date"),
        market_type=(str(payload["market_type"]).upper() if payload.get("market_type") else None),
        value_f=_number(value, "settlement_bucket"),
        source_id=_text(source_id, "epoch id"),
        source_payload=dict(payload),
        settlement_timestamp_utc=payload.get("settlement_timestamp_utc"),
        provenance_note="METAR-derived settlement epoch; observed transition state, not a Kalshi outcome.",
    )


def adapt_ghcn_proxy(payload: Mapping[str, Any]) -> SettlementRecord:
    """Adapt a GHCN daily value as a calibration proxy, never as settlement."""
    value = payload.get("value_f", payload.get("tmax_f", payload.get("tmin_f")))
    source_id = payload.get("station_id") or payload.get("source_id")
    return SettlementRecord(
        source=SettlementSource.GHCN_PROXY,
        role=SettlementRole.PROXY,
        station=_text(payload.get("station") or payload.get("station_id"), "station").upper(),
        local_trading_date=_text(payload.get("date") or payload.get("local_trading_date"), "date"),
        market_type=(str(payload["market_type"]).upper() if payload.get("market_type") else None),
        value_f=_number(value, "value_f"),
        source_id=_text(source_id, "station_id"),
        source_payload=dict(payload),
        provenance_note="GHCN daily proxy for calibration; not a market settlement outcome.",
    )


def adapt(source: SettlementSource, payload: Mapping[str, Any]) -> SettlementRecord:
    adapters = {
        SettlementSource.KALSHI_JSON: adapt_kalshi_json,
        SettlementSource.METAR_EPOCH: adapt_metar_epoch,
        SettlementSource.GHCN_PROXY: adapt_ghcn_proxy,
    }
    return adapters[source](payload)
