import json

from utils.json_fields import json_list


def test_json_list_accepts_current_and_legacy_values():
    expected = [{"code": "DEMO", "level": "MUST_REVIEW"}]

    assert json_list(expected) == expected
    assert json_list(json.dumps(expected)) == expected
    assert json_list(json.dumps(json.dumps(expected))) == expected


def test_json_list_fails_closed_for_non_lists():
    assert json_list(None) == []
    assert json_list("not-json") == []
    assert json_list({"code": "DEMO"}) == []
