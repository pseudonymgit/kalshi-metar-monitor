"""Read-only review gate for Q-B2-PROD-PRICE-CONTRACT.

This test validates the decision packet and synthetic fixture only. It does not
call the network, open a live DB, mutate schema/data, or approve the contract.
"""
import json
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "q-b2-prod-price-contract.json"
PACKET = Path(__file__).parents[1] / "docs" / "Q-B2-PROD-PRICE-CONTRACT-DECISION-PACKET.md"


def load_fixture():
    return json.loads(FIXTURE.read_text())


def test_fixture_is_pending_and_has_explicit_owner_decisions():
    data = load_fixture()
    assert data["status"] == "approved_monitor_contract"
    assert data["api_contract_version"] == "kalshi-predictions-rest-v2"
    assert set(data["owner_decision_fields"]) == {
        "permitted_source_fields", "source_field_units", "fallback_policy",
        "mixed_source_policy", "price_unit", "bid_source_field",
        "ask_source_field", "api_contract_version",
    }


def test_only_suffixed_dollar_path_is_reviewable_accepted_example():
    accepted = load_fixture()["accepted_example"]
    assert accepted["snapshot"] == {
        "price_unit": "cents",
        "bid_source_field": "yes_bid",
        "ask_source_field": "yes_ask",
        "mid_price": 34.0,
        "api_contract_version": "kalshi-predictions-rest-v2",
    }
    assert accepted["expected"] == "accepted_verified_monitor_row"


def test_unproven_unsuffixed_and_incomplete_metadata_are_rejected():
    rejected = load_fixture()["rejected_examples"]
    assert rejected[0]["expected"] == "reject_unproven_unsuffixed_unit"
    assert rejected[1]["expected"] == "reject_mixed_or_unapproved_source"
    assert rejected[2]["expected"] == "reject_missing_api_contract_metadata"
    for row in rejected:
        assert row["expected"].startswith("reject_")


def test_packet_states_no_inference_and_next_action():
    packet = PACKET.read_text()
    for required in (
        "APPROVED_MONITOR_CONTRACT",
        "unit inference",
        "reject_suffixed_or_unpaired",
        "price_unit",
        "bid_source_field",
        "ask_source_field",
        "api_contract_version",
        "https://api.elections.kalshi.com/trade-api/v2",
        "GET /trade-api/v2/markets",
    ):
        assert required in packet
