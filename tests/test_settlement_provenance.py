from core.settlement_provenance import (
    SettlementRole,
    SettlementSource,
    adapt_ghcn_proxy,
    adapt_kalshi_json,
    adapt_metar_epoch,
)


def test_kalshi_json_is_authoritative_and_preserves_payload():
    payload = {"station": "KDEN", "target_date": "2026-07-27", "event_ticker": "KXHIGHDEN-26JUL27", "expiration_value": "91"}
    record = adapt_kalshi_json(payload)
    assert record.source is SettlementSource.KALSHI_JSON
    assert record.role is SettlementRole.KALSHI_AUTHORITATIVE
    assert record.is_authoritative_outcome is True
    assert record.value_f == 91.0
    assert record.source_payload == payload


def test_metar_epoch_is_observation_not_outcome():
    record = adapt_metar_epoch({"station": "KDEN", "local_trading_date": "2026-07-27", "id": 7, "settlement_bucket": 90, "market_type": "HIGH"})
    assert record.source is SettlementSource.METAR_EPOCH
    assert record.role is SettlementRole.OBSERVED_EPOCH
    assert record.is_authoritative_outcome is False


def test_ghcn_is_proxy_not_outcome():
    record = adapt_ghcn_proxy({"station_id": "USW00003017", "date": "2026-07-27", "tmax_f": 89})
    assert record.source is SettlementSource.GHCN_PROXY
    assert record.role is SettlementRole.PROXY
    assert record.is_authoritative_outcome is False


def test_acceptance_maps_representative_records_and_filters_authoritative_only():
    kalshi_payload = {
        "station": "KDEN",
        "target_date": "2026-07-27",
        "event_ticker": "KXHIGHDEN-26JUL27",
        "expiration_value": "91",
        "settled_at": "2026-07-28T12:00:00Z",
    }
    metar_payload = {
        "station": "KDEN",
        "local_trading_date": "2026-07-27",
        "id": 7,
        "settlement_bucket": 90,
        "market_type": "HIGH",
    }
    ghcn_payload = {
        "station_id": "USW00003017",
        "date": "2026-07-27",
        "tmax_f": 89,
    }

    kalshi = adapt_kalshi_json(kalshi_payload)
    metar = adapt_metar_epoch(metar_payload)
    ghcn = adapt_ghcn_proxy(ghcn_payload)

    assert (kalshi.role, kalshi.source_id, kalshi.source_payload) == (
        SettlementRole.KALSHI_AUTHORITATIVE,
        "KXHIGHDEN-26JUL27",
        kalshi_payload,
    )
    assert (metar.role, metar.source_id, metar.source_payload) == (
        SettlementRole.OBSERVED_EPOCH,
        "7",
        metar_payload,
    )
    assert (ghcn.role, ghcn.source_id, ghcn.source_payload) == (
        SettlementRole.PROXY,
        "USW00003017",
        ghcn_payload,
    )

    records = (kalshi, metar, ghcn)
    authoritative_only = [
        record for record in records if record.is_authoritative_outcome
    ]
    assert authoritative_only == [kalshi]
    assert all(record.role is SettlementRole.KALSHI_AUTHORITATIVE for record in authoritative_only)


def test_missing_required_fields_fail_closed():
    try:
        adapt_kalshi_json({"target_date": "2026-07-27", "event_ticker": "x", "expiration_value": 1})
    except ValueError as exc:
        assert "station" in str(exc)
    else:
        raise AssertionError("missing station must fail closed")
