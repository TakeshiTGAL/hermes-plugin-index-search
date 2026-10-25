"""Five argument shapes a caller may send, and how each one is accepted or refused."""
import json

from test_acceptance import Script, body_of, hit, page, rt


def test_fullwidth_digits_in_the_index_are_sent_as_ascii(tmp_path):
    script = Script([page([hit()])])
    out = body_of(service_search(tmp_path, script, {
        "index": "notes１２",
        "q": "ｈは残す２０２４",
        "id_field": "sku",
    }))
    assert out["ok"] is True
    assert "/indexes/notes12/search" in script.calls[0]["url"]
    sent = script.calls[0]["body"].decode("utf-8")
    assert "2024" in sent
    assert "１２" not in script.calls[0]["url"]


def test_index_uid_case_is_sent_unchanged(tmp_path):
    script = Script([page([hit()])])
    out = body_of(service_search(tmp_path, script, {
        "index": "Probes",
        "q": "hermes",
        "id_field": "sku",
    }))
    assert out["ok"] is True
    assert script.calls[0]["url"].endswith("/indexes/Probes/search")


def test_extra_limit_is_refused_without_the_value(tmp_path):
    script = Script([page([hit()])])
    out = body_of(service_search(tmp_path, script, {
        "index": "notes-gate",
        "q": "ZZQUERY",
        "id_field": "sku",
        "limit": 10.0,
    }))
    text = json.dumps(out, ensure_ascii=False)
    assert out["ok"] is False
    assert out["error_type"] == "bad_args"
    assert "probes" in out["message"]
    assert "hermes" in out["message"]
    assert "sku" in out["message"]
    assert "10.0" not in text
    assert "limit" not in text
    assert "ZZQUERY" not in text
    assert "notes-gate" not in text
    assert script.calls == []


def test_missing_id_field_names_an_example(tmp_path):
    script = Script([page([hit()])])
    out = body_of(service_search(tmp_path, script, {"index": "notes-gate", "q": "ZZQUERY"}))
    text = json.dumps(out, ensure_ascii=False)
    assert out["ok"] is False
    assert out["error_type"] == "bad_args"
    assert "sku" in out["message"]
    assert "ZZQUERY" not in text
    assert "notes-gate" not in text
    assert script.calls == []


def test_float_id_field_and_overlong_query_are_not_rewritten(tmp_path):
    script = Script([page([hit()])])
    runtime_q = "x" * 401
    floated = body_of(service_search(tmp_path, script, {
        "index": "notes-gate",
        "q": "ZZQUERY",
        "id_field": 10.0,
    }))
    long = body_of(service_search(tmp_path, script, {
        "index": "probes",
        "q": runtime_q,
        "id_field": "sku",
    }))
    float_text = json.dumps(floated, ensure_ascii=False)
    long_text = json.dumps(long, ensure_ascii=False)
    assert floated["ok"] is False and floated["error_type"] == "bad_args"
    assert long["ok"] is False and long["error_type"] == "bad_args"
    assert "sku" in floated["message"]
    assert "hermes" in long["message"]
    assert "10.0" not in float_text
    assert "ZZQUERY" not in float_text
    assert runtime_q not in long_text
    assert script.calls == []


def service_search(tmp_path, script, args):
    import service
    return service.index_search(rt(tmp_path, script), args)
