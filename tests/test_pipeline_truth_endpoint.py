def test_pipeline_truth_missing_station(client):
    r = client.get("/observability/pipeline-truth")
    assert r.status_code == 400


def test_pipeline_truth_unknown_station(client):
    r = client.get("/observability/pipeline-truth?station=XXXX")
    assert r.status_code == 200
    data = r.get_json()

    assert "station" in data
    assert "blocking_stage" in data
    assert "pipeline_status" in data
    assert "signal_type" in data
    assert "suppression_reason" in data
    assert "cooldown_state" in data
