from icamfind.eval.evaluate import score


def test_score_matches_by_type_time_and_identity():
    truth = [
        {"t": 10.0, "type": "placed", "object": "keys", "zone": "desk"},
        {"t": 30.0, "type": "removed", "object": "keys", "zone": "desk"},
        {"t": 40.0, "type": "moved", "object": "keys", "zone": "shelf", "from_zone": "desk"},
    ]
    pred = [
        {"type": "placed", "ts": 11.5, "zone": "desk", "object_id": "a", "label": "keys"},
        {"type": "removed", "ts": 31.0, "zone": "desk", "object_id": "a", "label": "keys"},
        {"type": "moved", "ts": 41.2, "zone": "shelf", "object_id": "a", "label": "keys"},
        {"type": "placed", "ts": 80.0, "zone": "desk", "object_id": "b", "label": "object"},
    ]
    s = score(pred, truth)
    assert s["true_positives"] == 3
    assert s["recall"] == 1.0
    assert s["precision"] == 0.75
    assert s["zone_accuracy"] == 1.0
    assert s["identity_consistency"] == "1/1"
    assert len(s["false_positives"]) == 1


def test_late_detection_is_a_miss():
    truth = [{"t": 10.0, "type": "placed", "object": "keys", "zone": "desk"}]
    pred = [{"type": "placed", "ts": 30.0, "zone": "desk", "object_id": "a", "label": "keys"}]
    s = score(pred, truth)
    assert s["true_positives"] == 0 and s["recall"] == 0.0
