"""Acceptance checks for index-search. No sockets."""

from __future__ import annotations

import html
import importlib
import importlib.util
import json
import os
import sys
import types
import urllib.request
from pathlib import Path

import pytest
import yaml

import client
import service
import store

ROOT = Path(__file__).resolve().parents[1]
SECRET = "test-key-value-not-a-real-secret"
FORBIDDEN = "届いて効いた可能性がある"


class Script:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        if not self.steps:
            raise AssertionError("unexpected HTTP")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def page(hits, total=None, status=200):
    if total is None:
        total = len(hits)
    raw = json.dumps({
        "hits": hits,
        "estimatedTotalHits": total,
        "offset": 0,
        "limit": 20,
        "processingTimeMs": 1,
        "query": "hermes",
    }).encode()
    return status, {"content-type": "application/json"}, raw


def hit(sku="s1", title="one", **extra):
    body = {"sku": sku, "title": title, "body": "secret-body"}
    body.update(extra)
    return body


def rt(tmp_path, script, **kw):
    env = {"MEILI_HOST": "http://127.0.0.1:9", "MEILI_API_KEY": SECRET}
    env.update(kw.pop("env", {}))
    return service.Runtime(
        env=env,
        data_dir=tmp_path,
        exchange=script,
        now=kw.pop("now", lambda: 1_700_000_000.0),
        **kw,
    )


def body_of(text):
    return json.loads(text)


def seed(tmp_path, **over):
    state = {
        "index": "probes",
        "q": "hermes",
        "id_field": "sku",
        "title_field": "title",
        "deliver": "local",
        "job_id": "seed",
        "scheduled": True,
        "baseline_pending": False,
        "seen": [{"t": "s", "v": "s1"}],
        "last_empty": False,
        "fail": None,
    }
    state.update(over)
    store.save(tmp_path, state, lambda path: None)
    return state


def test_register_lists_three_tools_and_does_not_import_tests():
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    assert "import tests" not in source
    assert "register_command" not in source
    assert "__package__" in source
    assert "except ImportError" not in source
    name = "index_search_plugin"
    spec = importlib.util.spec_from_file_location(
        name,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
        seen = []

        class Ctx:
            def register_tool(self, **kwargs):
                seen.append(kwargs["name"])

        mod.register(Ctx())
        assert seen == ["index_search", "index_health", "index_watch"]
        assert mod.__package__ == name
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)


def test_description_is_the_catalog_draft():
    desc = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))["description"]
    assert 700 <= len(desc) <= 1500
    for tool in ("index_search", "index_health", "index_watch"):
        assert tool in desc.split("Disclosure —", 1)[0]
    assert desc.count("Disclosure —") == 1
    assert "Disclosure:" not in desc
    assert "*/15" not in desc
    assert "<=" not in desc
    for banned in ("host process", "private module", "untried", "Unofficial", "not affiliated"):
        assert banned not in desc
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for phrase in (
        "<HERMES_HOME>/plugin-data/index-search",
        "empty",
        "title",
        "cli",
        "cron",
        "api_server",
        "bot-chat",
        "MEILI_NO_ANALYTICS",
        "telemetry",
        "Community Edition",
        "getmeili/meilisearch:v1.12",
        "1.12.8",
        "500",
        "96",
        "not truncated",
        "index_search",
        "every 15 minutes",
        "tools.approval_context._is_cron_approval_context",
        "movies_en_us",
        "created_at_id",
        "repeats once a day",
        "cannot silence the next cron report",
        "prints only `cron list` and `cron status`",
        "hermes cron remove <id>",
        "hermes -p <name> cron remove <id>",
    ):
        assert phrase in readme
    assert "*/15" not in readme
    assert "pip install" not in readme
    assert "--yes-deps" not in readme
    assert FORBIDDEN not in readme
    assert "Unofficial" not in readme
    assert not (ROOT / "NOTICE").exists()


def test_empty_query_and_extra_args_do_not_call_http(tmp_path):
    script = Script([page([hit()])])
    runtime = rt(tmp_path, script)
    for query in ("", "   ", "x" * 401, 12):
        out = body_of(service.index_search(runtime, {"index": "probes", "q": query, "id_field": "sku"}))
        assert out["message"] == service.MSG_QUERY
        assert script.calls == []
    out = body_of(service.index_search(runtime, {"index": "probes", "q": "hermes", "id_field": "sku", "filter": "a"}))
    assert out["message"] == service.MSG_SEARCH_ARGS
    assert script.calls == []
    out = body_of(service.index_search(runtime, {"index": "../x", "q": "hermes", "id_field": "sku"}))
    assert out["message"] == service.MSG_UID
    assert script.calls == []


def test_host_and_key_are_refused_before_http(tmp_path):
    script = Script([page([hit()])])
    missing = rt(tmp_path, script, env={"MEILI_HOST": "", "MEILI_API_KEY": SECRET})
    assert body_of(service.index_search(missing, {"index": "probes", "q": "hermes", "id_field": "sku"}))["message"] == service.MSG_HOST_REQUIRED
    bad = rt(tmp_path, script, env={"MEILI_HOST": "http://user:pass@127.0.0.1:9/extra", "MEILI_API_KEY": SECRET})
    assert body_of(service.index_search(bad, {"index": "probes", "q": "hermes", "id_field": "sku"}))["message"] == service.MSG_HOST_BAD
    nokey = rt(tmp_path, script, env={"MEILI_HOST": "http://127.0.0.1:9", "MEILI_API_KEY": ""})
    assert body_of(service.index_search(nokey, {"index": "probes", "q": "hermes", "id_field": "sku"}))["message"] == service.MSG_KEY_REQUIRED
    assert script.calls == []


def test_search_posts_only_the_search_route_and_returns_id_and_title(tmp_path):
    script = Script([page([hit(title="t" * 201)])])
    out = body_of(service.index_search(rt(tmp_path, script), {"index": "probes", "q": "hermes", "id_field": "sku", "title_field": "title"}))
    call = script.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://127.0.0.1:9/indexes/probes/search"
    assert json.loads(call["body"]) == {"q": "hermes", "limit": 20}
    assert call["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert call["timeout"] == 30
    assert len(script.calls) == 1
    assert out["hits"] == [{"id": "s1", "title": "t" * 200}]
    assert "secret-body" not in json.dumps(out)
    assert out["notify"] is False


def test_health_sends_no_key_and_requires_available(tmp_path):
    script = Script([
        (200, {"content-type": "application/json"}, b'{"status":"available","extra":1}'),
        (200, {"content-type": "application/json"}, b'{"status":"mustRestart"}'),
    ])
    runtime = rt(tmp_path, script)
    assert body_of(service.index_health(runtime, {}))["healthy"] is True
    assert "Authorization" not in script.calls[0]["headers"]
    assert SECRET not in json.dumps(script.calls[0]["headers"])
    assert script.calls[0]["method"] == "GET"
    assert script.calls[0]["url"].endswith("/health")
    assert body_of(service.index_health(runtime, {}))["healthy"] is False
    assert body_of(service.index_health(runtime, {"verbose": True}))["message"] == service.MSG_HEALTH_ARGS


def test_ids_stay_distinct_and_bool_is_not_an_id(tmp_path):
    script = Script([page([{"sku": 1, "title": "n"}, {"sku": "1", "title": "s"}])])
    out = body_of(service.index_search(rt(tmp_path, script), {"index": "probes", "q": "hermes", "id_field": "sku", "title_field": "title"}))
    assert out["hits"] == [{"id": 1, "title": "n"}, {"id": "1", "title": "s"}]
    script = Script([page([{"sku": True}])])
    out = body_of(service.index_search(rt(tmp_path, script), {"index": "probes", "q": "hermes", "id_field": "sku"}))
    assert out["ok"] is False
    assert out["message"] == service.MSG_NO_ID


def test_bad_pages_are_not_zero(tmp_path):
    cases = [
        (200, b'{"code":"bad","message":"no"}', service.MSG_BAD_PAGE),
        (200, b"", service.MSG_INCOMPLETE),
        (200, json.dumps({"hits": [], "estimatedTotalHits": 5}).encode(), service.MSG_BAD_PAGE),
        (200, b"<html>no</html>", service.MSG_BAD_PAGE),
        (200, b"[]", service.MSG_BAD_PAGE),
        (200, b'{"hits":{"sku":"s1"},"estimatedTotalHits":1}', service.MSG_BAD_PAGE),
        (200, json.dumps({"hits": [hit()], "estimatedTotalHits": 0}).encode(), service.MSG_BAD_PAGE),
        (200, json.dumps({"hits": [hit(), hit("s2")], "estimatedTotalHits": 1}).encode(), service.MSG_BAD_PAGE),
        (200, json.dumps({"hits": [hit()]}).encode(), service.MSG_BAD_PAGE),
    ]
    for status, raw, message in cases:
        script = Script([(status, {"content-type": "application/json"}, raw)])
        out = body_of(service.index_search(rt(tmp_path, script), {"index": "probes", "q": "hermes", "id_field": "sku"}))
        assert out["message"] == message
        assert out["ok"] is False


def test_real_zero_is_not_an_error(tmp_path):
    raw = json.dumps({"hits": [], "estimatedTotalHits": 0, "query": "hermes"}).encode()
    script = Script([(200, {}, raw)])
    out = body_of(service.index_search(rt(tmp_path, script), {"index": "probes", "q": "hermes", "id_field": "sku"}))
    assert out["ok"] is True
    assert out["hits"] == []


def test_too_many_hits_do_not_trim(tmp_path):
    hits = [hit(f"s{i}") for i in range(21)]
    script = Script([page(hits, 21)])
    out = body_of(service.index_search(rt(tmp_path, script), {"index": "probes", "q": "hermes", "id_field": "sku"}))
    assert out["message"] == service.MSG_TOO_MANY


def test_404_hides_the_server_message_and_the_key(tmp_path):
    raw = json.dumps({
        "message": f"Index `probes` not found. {SECRET}",
        "code": "index_not_found",
        "type": "invalid_request",
        "link": "https://docs.meilisearch.com/errors",
    }).encode()
    script = Script([(404, {}, raw)])
    out = body_of(service.index_search(rt(tmp_path, script), {"index": "probes", "q": "hermes", "id_field": "sku"}))
    assert out["message"] == "The search failed (HTTP 404, index_not_found). Seen ids were not advanced."
    assert SECRET not in out["message"]
    assert "docs.meilisearch.com" not in out["message"]
    assert "probes" not in out["message"]


def test_incomplete_does_not_retry(tmp_path):
    script = Script([(500, {}, b"no"), client.Incomplete()])
    runtime = rt(tmp_path, script)
    assert body_of(service.index_search(runtime, {"index": "probes", "q": "hermes", "id_field": "sku"}))["message"] == service.MSG_INCOMPLETE
    assert body_of(service.index_search(runtime, {"index": "probes", "q": "hermes", "id_field": "sku"}))["message"] == service.MSG_INCOMPLETE
    assert len(script.calls) == 2
    recorded = json.dumps(script.calls, default=lambda item: item.decode("utf-8", "replace"))
    assert FORBIDDEN not in recorded


def test_redirect_handler_does_not_follow_and_search_reports_it(tmp_path):
    handler = client._NoRedirect()
    with pytest.raises(client.Redirected):
        handler.redirect_request(None, None, 307, "Temporary Redirect", {}, "http://localhost:1/x")
    with pytest.raises(client.Redirected):
        handler.http_error_307(None, None, 307, "Temporary Redirect", {})
    script = Script([client.Redirected(307)])
    out = body_of(service.index_search(rt(tmp_path, script), {"index": "probes", "q": "hermes", "id_field": "sku"}))
    assert out["message"] == service.MSG_REDIRECT
    assert len(script.calls) == 1


def test_perform_treats_a_short_body_as_incomplete():
    def opener(method, url, headers, body, timeout):
        return 200, {"content-length": "100"}, b"{}"

    with pytest.raises(client.Incomplete):
        client.perform("POST", "http://127.0.0.1:9/indexes/probes/search", {"Authorization": "Bearer x"}, b"{}", open_fn=opener)


def test_manual_search_from_cron_does_not_write_seen_ids(tmp_path):
    seed(tmp_path)
    before = (tmp_path / "state.json").read_bytes()
    script = Script([page([hit(), hit("s2")])])
    runtime = rt(tmp_path, script, cron=lambda: "cron")
    os.environ["HERMES_CRON_SESSION"] = "1"
    try:
        service.index_search(runtime, {"index": "probes", "q": "hermes", "id_field": "sku"})
    finally:
        os.environ.pop("HERMES_CRON_SESSION", None)
    assert (tmp_path / "state.json").read_bytes() == before


def test_cron_check_then_manual_check_then_cron_check(tmp_path):
    seed(tmp_path, baseline_pending=True, seen=[])
    script = Script([
        page([hit("s1")]),
        page([hit("s1"), hit("s2")]),
        page([hit("s1"), hit("s2")]),
    ])
    cron = rt(tmp_path, script, cron=lambda: "cron")
    manual = rt(tmp_path, script, cron=lambda: "not_cron")
    first = body_of(service.index_watch(cron, {"action": "check"}))
    assert first["notify"] is False
    assert first["message"].startswith(service.MSG_BASELINE)
    assert "estimatedTotalHits is 1" in first["message"]
    mid_bytes = (tmp_path / "state.json").read_bytes()
    manual_out = body_of(service.index_watch(manual, {"action": "check"}))
    assert "did not write seen ids" in manual_out["message"]
    assert (tmp_path / "state.json").read_bytes() == mid_bytes
    second = body_of(service.index_watch(cron, {"action": "check"}))
    assert second["notify"] is True
    assert "s2" in second["message"]
    saved = json.loads((tmp_path / "state.json").read_text())
    assert {"t": "s", "v": "s2"} in saved["seen"]
    assert SECRET not in (tmp_path / "state.json").read_text()


def test_empty_args_follow_the_cron_marker_not_the_process_env(tmp_path):
    seed(tmp_path, baseline_pending=True, seen=[])
    script = Script([page([hit("s1")]), page([hit("s1")])])
    os.environ["HERMES_CRON_SESSION"] = "1"
    try:
        blocked = rt(tmp_path, script, cron=lambda: "not_cron")
        service.index_watch(blocked, {})
        assert script.calls == []
        assert json.loads((tmp_path / "state.json").read_text())["seen"] == []
        allowed = rt(tmp_path, script, cron=lambda: "cron", host=lambda: None)
        os.environ.pop("HERMES_CRON_SESSION", None)
        out = body_of(service.index_watch(allowed, {}))
        assert out["message"].startswith(service.MSG_BASELINE)
        assert json.loads((tmp_path / "state.json").read_text())["seen"] == [{"t": "s", "v": "s1"}]
    finally:
        os.environ.pop("HERMES_CRON_SESSION", None)


def test_host_process_does_not_search_or_schedule(tmp_path):
    seed(tmp_path)
    before = (tmp_path / "state.json").read_bytes()
    script = Script([page([hit()])])
    asked = []
    runtime = rt(
        tmp_path, script, host=lambda: "1", cron=lambda: "cron",
        request_approval=lambda question: asked.append(question) or {"approved": True},
    )
    out = body_of(service.index_watch(runtime, {"action": "check"}))
    assert out["message"] == service.MSG_CANNOT
    assert out["notify"] is True
    assert script.calls == []
    again = body_of(service.index_watch(runtime, {}))
    assert again["notify"] is True
    assert again["message"] == service.MSG_CANNOT
    blocked = body_of(service.index_watch(runtime, {
        "action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku", "deliver": "local",
    }))
    assert blocked["message"] == service.MSG_CANNOT
    stopped = body_of(service.index_watch(runtime, {"action": "unschedule"}))
    assert "This form cannot unschedule. The cron job is still there." in stopped["message"]
    assert "hermes cron remove seed" in stopped["message"]
    assert "<id>" not in stopped["message"]
    assert asked == []
    assert (tmp_path / "state.json").read_bytes() == before
    # Unset, empty, and 0 are the normal v0.21.4 cases.
    open_runtime = rt(tmp_path, script, host=lambda: None, cron=lambda: "not_cron")
    service.index_search(open_runtime, {"index": "probes", "q": "hermes", "id_field": "sku"})
    assert script.calls


def test_unknown_cron_marker_does_not_advance(tmp_path):
    seed(tmp_path, baseline_pending=True, seen=[])
    before = (tmp_path / "state.json").read_bytes()
    script = Script([page([hit()])])
    runtime = rt(tmp_path, script, cron=lambda: "unknown")
    out = body_of(service.index_watch(runtime, {"action": "check"}))
    assert out["message"] == service.MSG_CANNOT
    assert out["notify"] is True
    assert script.calls == []
    assert (tmp_path / "state.json").read_bytes() == before


def test_schedule_blocks_before_approval(tmp_path):
    script = Script([page([hit()])])
    asked = []

    def approve(question):
        asked.append(question)
        return {"approved": True}

    payload = {"action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku", "deliver": "local"}
    for gates in (
        {"cron": True, "yolo": False, "approvals": "manual", "single_query": False, "unattended": False, "approval_loaded": True},
        {"cron": False, "yolo": True, "approvals": "manual", "single_query": False, "unattended": False, "approval_loaded": True},
        {"cron": False, "yolo": False, "approvals": "off", "single_query": False, "unattended": False, "approval_loaded": True},
        {"cron": False, "yolo": False, "approvals": "manual", "single_query": True, "unattended": False, "approval_loaded": True},
        {"cron": False, "yolo": False, "approvals": "manual", "single_query": False, "unattended": True, "approval_loaded": True},
        {"unreadable": True},
    ):
        asked.clear()
        runtime = rt(tmp_path, script, gates=lambda g=gates: g, request_approval=approve, cron=lambda: "not_cron")
        service.index_watch(runtime, payload)
        assert asked == []
    assert script.calls == []


def test_schedule_asks_every_time_and_refuses_a_long_or_rewritten_query(tmp_path):
    script = Script([])
    asked = []

    def approve(question):
        asked.append(question)
        return {"approved": True}

    jobs = []

    def create_job(**kwargs):
        jobs.append(kwargs)
        return {"id": "job1"}

    runtime = rt(tmp_path, script, request_approval=approve, create_job=create_job, profile=lambda: "work")
    payload = {"action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku", "deliver": "local"}
    service.index_watch(runtime, payload)
    service.index_watch(runtime, payload)
    assert len(asked) == 2
    assert "schedule" in asked[0]
    assert "hermes" in asked[0]
    assert "every 15 minutes" in asked[0]
    assert "model" in asked[0]
    assert jobs[0]["no_agent"] is False
    assert jobs[0]["schedule"] == service.SCHEDULE_EXPR
    assert service.SCHEDULE_EXPR not in jobs[0]["prompt"]
    out = body_of(service.index_watch(runtime, payload))
    assert "hermes -p work cron remove" in out["message"]
    assert "hermes cron remove -p work" not in out["message"]
    assert "cron create" not in out["message"]
    assert "estimatedTotalHits" in out["message"]
    assert service.SCHEDULE_EXPR not in out["message"]
    long = dict(payload, q="h" * 400)
    before = len(asked)
    refused = body_of(service.index_watch(runtime, long))
    assert refused["message"] == service.MSG_APPROVAL_FIT
    assert refused["retryable"] is False
    assert len(asked) == before
    starred = dict(payload, q="hello *world*")
    refused = body_of(service.index_watch(runtime, starred))
    assert refused["retryable"] is False
    assert len(asked) == before
    assert service.make_rule_key() != service.make_rule_key()


def test_delivery_rules_and_deliver_only_change(tmp_path):
    script = Script([])
    created = []

    def create_job(**kwargs):
        created.append(kwargs)
        return {"id": f"job{len(created)}"}

    runtime = rt(
        tmp_path, script,
        request_approval=lambda question: {"approved": True},
        create_job=create_job,
        known_platform=lambda name: True if name.lower() == "slack" else False,
    )
    base = {"action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku"}
    for deliver in ("", "   ", "bot-chat", "cli", "cron", "api_server", "every", "5m", "monday", "9am", "*", "12", "2026-10-08T00:00:00"):
        out = body_of(service.index_watch(runtime, dict(base, deliver=deliver)))
        assert out["ok"] is False
        assert created == []
    for deliver in ("local", "origin", "all", "slack", "slack:C123"):
        out = body_of(service.index_watch(runtime, dict(base, deliver=deliver)))
        assert out["ok"] is True
    seed(tmp_path, deliver="local", seen=[{"t": "s", "v": "s1"}], baseline_pending=False)
    out = body_of(service.index_watch(runtime, dict(base, deliver="origin")))
    assert "local" in out["message"]
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["seen"] == [{"t": "s", "v": "s1"}]
    assert saved["baseline_pending"] is False


def test_failed_schedule_and_identity_change_keep_seen_ids(tmp_path):
    seed(tmp_path, seen=[{"t": "s", "v": "s1"}])
    before = (tmp_path / "state.json").read_bytes()
    script = Script([page([hit("s9")])])

    def boom(**kwargs):
        raise RuntimeError("croniter missing")

    runtime = rt(tmp_path, script, request_approval=lambda question: {"approved": True}, create_job=boom)
    out = body_of(service.index_watch(runtime, {
        "action": "schedule", "index": "other", "q": "hermes", "id_field": "sku", "deliver": "local",
    }))
    assert out["ok"] is False
    assert (tmp_path / "state.json").read_bytes() == before
    assert script.calls == []
    runtime = rt(tmp_path, script, request_approval=lambda question: {"approved": True}, create_job=lambda **kw: {"id": "new"})
    out = body_of(service.index_watch(runtime, {
        "action": "schedule", "index": "other", "q": "next", "id_field": "sku", "deliver": "local",
    }))
    assert "did not search" in out["message"]
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["seen"] == [{"t": "s", "v": "s1"}]
    assert saved["baseline_pending"] is True
    assert saved["q"] == "next"
    assert script.calls == []


def test_unschedule_without_a_job_does_not_delete_state(tmp_path):
    seed(tmp_path, scheduled=False, job_id="")
    before = (tmp_path / "state.json").read_bytes()
    runtime = rt(tmp_path, Script([]), request_approval=lambda question: {"approved": True})
    out = body_of(service.index_watch(runtime, {"action": "unschedule"}))
    assert out["ok"] is True
    assert "nothing to unschedule" in out["message"]
    assert (tmp_path / "state.json").read_bytes() == before


def test_check_uses_the_state_file_query(tmp_path):
    seed(tmp_path, q="from-state", baseline_pending=True, seen=[])
    script = Script([page([hit()])])
    service.index_watch(rt(tmp_path, script, cron=lambda: "cron"), {"action": "check"})
    assert json.loads(script.calls[0]["body"])["q"] == "from-state"


def test_zero_keeps_seen_ids_and_notifies_once(tmp_path):
    seed(tmp_path, seen=[{"t": "s", "v": "s1"}], last_empty=False)
    raw = json.dumps({"hits": [], "estimatedTotalHits": 0}).encode()
    script = Script([(200, {}, raw), (200, {}, raw)])
    runtime = rt(tmp_path, script, cron=lambda: "cron")
    first = body_of(service.index_watch(runtime, {"action": "check"}))
    assert first["notify"] is True
    assert first["message"].startswith(service.MSG_ZERO)
    assert "estimatedTotalHits is 0" in first["message"]
    assert json.loads((tmp_path / "state.json").read_text())["seen"] == [{"t": "s", "v": "s1"}]
    second = body_of(service.index_watch(runtime, {"action": "check"}))
    assert second["notify"] is False
    assert "nothing new" not in second["message"].lower()
    assert "no change" not in second["message"].lower()


def test_first_empty_page_is_a_baseline(tmp_path):
    seed(tmp_path, baseline_pending=True, seen=[])
    raw = json.dumps({"hits": [], "estimatedTotalHits": 0}).encode()
    script = Script([(200, {}, raw)])
    out = body_of(service.index_watch(rt(tmp_path, script, cron=lambda: "cron"), {"action": "check"}))
    assert out["message"].startswith(service.MSG_BASELINE)
    assert "estimatedTotalHits is 0" in out["message"]
    assert out["notify"] is False


def test_duplicate_ids_do_not_advance(tmp_path):
    seed(tmp_path, seen=[{"t": "s", "v": "s1"}])
    script = Script([page([hit("s2"), hit("s2")])])
    out = body_of(service.index_watch(rt(tmp_path, script, cron=lambda: "cron"), {"action": "check"}))
    assert out["message"] == service.MSG_DUP
    assert json.loads((tmp_path / "state.json").read_text())["seen"] == [{"t": "s", "v": "s1"}]


def test_failure_cadence_ignores_argument_errors(tmp_path):
    seed(tmp_path, seen=[{"t": "s", "v": "s1"}])
    raw = json.dumps({"message": "no", "code": "index_not_found", "link": "https://docs.example"}).encode()
    other = json.dumps({"message": "no", "code": "invalid_api_key"}).encode()
    script = Script([(404, {}, raw), (404, {}, raw), (403, {}, other)])
    clock = {"t": 1_700_000_000.0}
    runtime = rt(tmp_path, script, cron=lambda: "cron", now=lambda: clock["t"])
    assert body_of(service.index_watch(runtime, {"action": "check"}))["notify"] is True
    assert body_of(service.index_watch(runtime, {"action": "check"}))["notify"] is False
    assert body_of(service.index_watch(runtime, {"action": "check", "q": "nope"}))["notify"] is True
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["fail"]["cause"].startswith("http_404")
    assert body_of(service.index_watch(runtime, {"action": "check"}))["notify"] is True
    clock["t"] += service.DAY
    script.steps.append((404, {}, raw))
    # cause is now 403; push another 404 after the clock jump by resetting via a 404 step already consumed.
    # The last call was 403. Add a 404 after 24h.
    script.steps.append((404, {}, raw))
    out = body_of(service.index_watch(runtime, {"action": "check"}))
    assert out["notify"] is True


def test_seen_cap_drops_the_oldest(tmp_path):
    seen = [{"t": "s", "v": f"s{i}"} for i in range(500)]
    seed(tmp_path, seen=seen, baseline_pending=False)
    script = Script([page([hit("s0"), hit("fresh")])])
    service.index_watch(rt(tmp_path, script, cron=lambda: "cron"), {"action": "check"})
    saved = json.loads((tmp_path / "state.json").read_text())["seen"]
    assert len(saved) == 500
    assert {"t": "s", "v": "s0"} not in saved
    assert saved[-1] == {"t": "s", "v": "fresh"}


def test_corrupt_file_is_not_overwritten(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b"{not json")
    before = path.read_bytes()
    script = Script([page([hit()])])
    out = body_of(service.index_watch(rt(tmp_path, script, cron=lambda: "cron"), {"action": "check"}))
    assert out["notify"] is True
    assert path.read_bytes() == before
    assert script.calls == []


def test_missing_write_guard_does_not_advance(tmp_path):
    seed(tmp_path, baseline_pending=True, seen=[])
    before = (tmp_path / "state.json").read_bytes()
    script = Script([page([hit("s1")])])
    runtime = rt(tmp_path, script, cron=lambda: "cron", write_guard=None)
    out = body_of(service.index_watch(runtime, {"action": "check"}))
    assert out["message"] == service.MSG_PAGE_NOT_SAVED
    assert out["notify"] is True
    assert (tmp_path / "state.json").read_bytes() == before


def test_non_string_index_is_refused_before_http_and_is_not_saved(tmp_path):
    script = Script([page([hit()])])
    created = []
    runtime = rt(
        tmp_path, script,
        request_approval=lambda question: {"approved": True},
        create_job=lambda **kwargs: created.append(kwargs) or {"id": "job"},
    )
    for index in (1, True, False, None):
        searched = body_of(service.index_search(runtime, {"index": index, "q": "hermes", "id_field": "sku"}))
        assert searched["message"] == service.MSG_UID
        scheduled = body_of(service.index_watch(runtime, {
            "action": "schedule", "index": index, "q": "hermes", "id_field": "sku", "deliver": "local",
        }))
        assert scheduled["message"] == service.MSG_UID
    assert script.calls == []
    assert created == []
    assert not (tmp_path / "state.json").exists()


def test_plain_tokens_are_lowercase_and_origin_is_passed_or_disclosed(tmp_path):
    script = Script([])
    created = []

    def create_job(**kwargs):
        created.append(kwargs)
        return {"id": f"job{len(created)}"}

    captured = {"platform": "telegram", "chat_id": "4242", "thread_id": None}
    runtime = rt(
        tmp_path, script,
        request_approval=lambda question: {"approved": True},
        create_job=create_job,
        known_platform=lambda name: name == "Telegram",
        session_origin=lambda schedule: captured,
    )
    base = {"action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku"}
    out = body_of(service.index_watch(runtime, dict(base, deliver="Origin")))
    assert out["ok"] is True
    assert created[-1]["deliver"] == "origin"
    assert created[-1]["origin"] == captured
    assert "telegram" in out["message"]
    assert "Delivery returns to this session" in out["message"]
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["deliver"] == "origin"
    for token, stored in (("LOCAL", "local"), ("ALL", "all"), ("Telegram", "Telegram")):
        service.index_watch(runtime, dict(base, deliver=token))
        assert created[-1]["deliver"] == stored
        assert "origin" not in created[-1]
    asked = []

    def refuse_origin(schedule):
        raise RuntimeError("missing")

    blind = rt(
        tmp_path, script,
        request_approval=lambda question: asked.append(question) or {"approved": True},
        create_job=create_job,
        session_origin=refuse_origin,
    )
    before = len(created)
    refused = body_of(service.index_watch(blind, dict(base, deliver="origin")))
    assert refused["message"] == service.MSG_ORIGIN_UNREADABLE
    assert refused["ok"] is False
    assert len(created) == before
    assert asked == []
    disclosed = rt(
        tmp_path, script,
        request_approval=lambda question: {"approved": True},
        create_job=create_job,
        session_origin=lambda schedule: None,
    )
    out = body_of(service.index_watch(disclosed, dict(base, deliver="origin")))
    assert out["ok"] is True
    assert "origin" not in created[-1]
    assert "home channel" in out["message"]
    assert "nowhere" in out["message"]
    assert "does not return to this session" in out["message"]


def test_ipv6_origin_keeps_brackets(tmp_path):
    script = Script([page([hit()])])
    runtime = rt(tmp_path, script, env={"MEILI_HOST": "http://[::1]:7700", "MEILI_API_KEY": SECRET})
    out = body_of(service.index_search(runtime, {"index": "probes", "q": "hermes", "id_field": "sku"}))
    assert out["ok"] is True
    assert script.calls[0]["url"].startswith("http://[::1]:7700/indexes/probes/search")


def test_opener_has_an_empty_proxy_and_bodies_over_the_cap_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    default = urllib.request.build_opener()
    default_proxies = [
        dict(handler.proxies)
        for handler in default.handlers
        if isinstance(handler, urllib.request.ProxyHandler)
    ]
    assert default_proxies and default_proxies[0].get("http")
    built = client.opener()
    assert not any(isinstance(handler, urllib.request.ProxyHandler) for handler in built.handlers)
    assert not any(getattr(handler, "proxies", None) for handler in built.handlers)
    assert any(isinstance(handler, client._NoRedirect) for handler in built.handlers)
    huge = b"x" * (client.MAX_BODY + 1)
    with pytest.raises(client.Incomplete):
        client.perform("GET", "http://127.0.0.1:9/health", {}, None, open_fn=lambda *args: (200, {}, huge))
    with pytest.raises(client.Incomplete):
        client.perform(
            "GET", "http://127.0.0.1:9/health", {}, None,
            open_fn=lambda *args: (200, {"content-length": str(client.MAX_BODY + 5)}, b"{}"),
        )
    seen = {}

    class Resp:
        headers = {"content-length": str(client.MAX_BODY + 50)}
        status = 200

        def read(self, n=-1):
            seen["n"] = n
            return b"z" * (client.MAX_BODY + 50)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_open(self, request, timeout=None):
        return Resp()

    original = urllib.request.OpenerDirector.open
    urllib.request.OpenerDirector.open = fake_open
    try:
        with pytest.raises(client.Incomplete):
            client._open("GET", "http://127.0.0.1:9/x", {}, None, 1)
    finally:
        urllib.request.OpenerDirector.open = original
    assert "n" not in seen


def test_save_failures_do_not_say_the_search_failed(tmp_path):
    seed(tmp_path, seen=[{"t": "s", "v": "s1"}])
    before = (tmp_path / "state.json").read_bytes()
    removed = []
    runtime = rt(
        tmp_path, Script([]),
        request_approval=lambda question: {"approved": True},
        remove_job=lambda job_id: removed.append(job_id) or True,
        write_guard=lambda path: "denied",
    )
    out = body_of(service.index_watch(runtime, {"action": "unschedule"}))
    assert out["ok"] is False
    assert service.MSG_INCOMPLETE not in out["message"]
    assert "seed" in out["message"]
    assert "still says a watch is scheduled" in out["message"]
    assert removed == ["seed"]
    assert (tmp_path / "state.json").read_bytes() == before
    created = []
    fresh = rt(
        tmp_path, Script([]),
        request_approval=lambda question: {"approved": True},
        create_job=lambda **kwargs: created.append(kwargs) or {"id": "new-job"},
        remove_job=lambda job_id: removed.append(job_id) or True,
        write_guard=lambda path: "denied",
    )
    scheduled = body_of(service.index_watch(fresh, {
        "action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku", "deliver": "local",
    }))
    assert scheduled["ok"] is False
    assert scheduled["message"] == service.MSG_SCHEDULE_NOT_WRITTEN
    assert "new-job" in removed
    assert (tmp_path / "state.json").read_bytes() == before


def test_positive_total_with_no_hits_does_not_advance(tmp_path):
    seed(tmp_path, seen=[{"t": "s", "v": "s1"}])
    before = (tmp_path / "state.json").read_bytes()
    raw = json.dumps({"hits": [], "estimatedTotalHits": 5}).encode()
    script = Script([(200, {}, raw)])
    out = body_of(service.index_watch(rt(tmp_path, script, cron=lambda: "cron"), {"action": "check"}))
    assert out["ok"] is False
    assert out["message"] == service.MSG_BAD_PAGE
    assert "0 hits" not in out["message"]
    assert json.loads((tmp_path / "state.json").read_text())["seen"] == [{"t": "s", "v": "s1"}]
    assert before != (tmp_path / "state.json").read_bytes() or out["message"] == service.MSG_BAD_PAGE


def test_manual_check_does_not_claim_a_baseline_was_saved(tmp_path):
    seed(tmp_path, baseline_pending=True, seen=[])
    before = (tmp_path / "state.json").read_bytes()
    script = Script([page([hit("s1")]), (200, {}, json.dumps({"hits": [], "estimatedTotalHits": 0}).encode())])
    runtime = rt(tmp_path, script, cron=lambda: "not_cron")
    hits = body_of(service.index_watch(runtime, {"action": "check"}))
    assert hits["ok"] is False
    assert hits["message"] == service.MSG_NOT_CRON_BASELINE
    assert "Baseline saved" not in hits["message"]
    assert (tmp_path / "state.json").read_bytes() == before
    empty = body_of(service.index_watch(runtime, {"action": "check"}))
    assert empty["message"] == service.MSG_NOT_CRON_BASELINE
    assert (tmp_path / "state.json").read_bytes() == before
    assert script.calls


def test_rewrite_refusal_is_not_the_length_message(tmp_path):
    script = Script([])
    asked = []
    runtime = rt(
        tmp_path, script,
        request_approval=lambda question: asked.append(question) or {"approved": True},
        create_job=lambda **kwargs: {"id": "job"},
    )
    payload = {"action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku", "deliver": "local"}
    marked = body_of(service.index_watch(runtime, dict(payload, q="say _bold_ now")))
    assert marked["message"] == service.rewrite_message("q", "schedule")
    assert "query" in marked["message"]
    assert marked["retryable"] is False
    starred = body_of(service.index_watch(runtime, dict(payload, q="hello *world*")))
    assert starred["message"] == service.rewrite_message("q", "schedule")
    long = body_of(service.index_watch(runtime, dict(payload, q="h" * 400)))
    assert long["message"] == service.MSG_APPROVAL_FIT
    assert asked == []
    assert not (tmp_path / "state.json").exists()


def test_quote_dense_approval_is_not_sent(tmp_path):
    asked = []
    runtime = rt(
        tmp_path, Script([]),
        request_approval=lambda question: asked.append(question) or {"approved": True},
        create_job=lambda **kwargs: {"id": "job"},
    )
    payload = {"action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku", "deliver": "local"}
    dense = '"' * 80
    question = service._approval_question("schedule", "probes", dense, "sku", "local")
    assert len(question) <= service.REASON_BUDGET
    assert len(html.escape(question, quote=True)) > service.ESCAPED_REASON_BUDGET
    refused = body_of(service.index_watch(runtime, dict(payload, q=dense)))
    assert refused["message"] == service.MSG_APPROVAL_FIT
    assert refused["retryable"] is False
    ordinary = body_of(service.index_watch(runtime, dict(payload, q='say "hi"')))
    assert ordinary["ok"] is True
    assert len(asked) == 1
    assert 'say "hi"' in asked[0]


def test_quoted_key_is_absent_from_the_tool_result(tmp_path):
    secret = 'sek"ret\\key'
    raw = json.dumps({
        "hits": [{"sku": secret, "title": f"prefix {secret} suffix"}],
        "estimatedTotalHits": 1,
    }).encode()
    script = Script([(200, {}, raw)])
    runtime = rt(tmp_path, script, env={"MEILI_API_KEY": secret})
    text = service.index_search(runtime, {
        "index": "probes", "q": "hermes", "id_field": "sku", "title_field": "title",
    })
    escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
    doubled = json.dumps(escaped, ensure_ascii=False)[1:-1]
    assert secret not in text
    assert escaped not in text
    assert doubled not in text
    out = json.loads(text)
    blob = json.dumps(out, ensure_ascii=False)
    assert secret not in blob
    assert "[redacted]" in out["message"]
    assert out["hits"] == [{"id": "[redacted]", "title": "prefix [redacted] suffix"}]


def test_job_prompt_tells_a_quiet_round_to_reply_silent():
    assert "reply with exactly [SILENT]" in service.JOB_PROMPT
    assert "say nothing" not in service.JOB_PROMPT


def test_unschedule_question_removes_the_job_and_skips_approval_when_nothing_is_scheduled(tmp_path):
    asked = []

    def approve(question):
        asked.append(question)
        return {"approved": False}

    idle = rt(tmp_path, Script([]), request_approval=approve)
    out = body_of(service.index_watch(idle, {"action": "unschedule"}))
    assert "nothing to unschedule" in out["message"]
    assert asked == []
    seed(tmp_path, job_id="job-9", scheduled=True)
    runtime = rt(
        tmp_path, Script([]),
        request_approval=approve,
        get_job=lambda job_id: {"id": job_id},
        remove_job=lambda job_id: True,
    )
    body_of(service.index_watch(runtime, {"action": "unschedule"}))
    assert len(asked) == 1
    assert asked[0].endswith("This removes cron job job-9. Seen ids are kept.")
    assert "This creates a cron job" not in asked[0]
    schedule = rt(
        tmp_path, Script([]),
        request_approval=approve,
        create_job=lambda **kwargs: {"id": "job-new"},
    )
    service.index_watch(schedule, {
        "action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku", "deliver": "local",
    })
    assert asked[-1].endswith("This creates a cron job every 15 minutes that calls the model.")


def test_unschedule_does_not_print_remove_for_the_job_it_just_deleted(tmp_path):
    seed(tmp_path, job_id="job-9", scheduled=True)
    runtime = rt(
        tmp_path, Script([]),
        request_approval=lambda question: {"approved": True},
        get_job=lambda job_id: {"id": job_id},
        remove_job=lambda job_id: True,
        profile=lambda: "work",
    )
    out = body_of(service.index_watch(runtime, {"action": "unschedule"}))
    assert "The cron job job-9 was removed." in out["message"]
    assert "cron remove" not in out["message"]
    assert "hermes -p work cron list" in out["message"]
    assert "hermes -p work cron status" in out["message"]
    assert "cron create" not in out["message"]


def test_a_missing_cron_job_is_not_described_as_scheduled_or_removed(tmp_path):
    seed(tmp_path, job_id="job-9")
    before = json.loads((tmp_path / "state.json").read_text())["seen"]
    script = Script([page([hit("s9")])])
    asked = []
    runtime = rt(
        tmp_path, script,
        cron=lambda: "cron",
        request_approval=lambda question: asked.append(question) or {"approved": True},
        get_job=lambda job_id: None,
        remove_job=lambda job_id: False,
    )
    status = body_of(service.index_watch(runtime, {"action": "status"}))
    assert "job is gone" in status["message"]
    assert "A watch is scheduled" not in status["message"]
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["job_id"] == ""
    assert saved["scheduled"] is False
    assert saved["job_gone"] is True
    assert saved["seen"] == before
    checked = body_of(service.index_watch(runtime, {"action": "check"}))
    assert "job is gone" in checked["message"]
    assert script.calls == []
    removed = body_of(service.index_watch(runtime, {"action": "unschedule"}))
    assert "job is gone" in removed["message"]
    assert "was removed" not in removed["message"]
    assert asked == []
    seed(tmp_path, job_id="job-9")
    present = rt(
        tmp_path, Script([]),
        request_approval=lambda question: asked.append(question) or {"approved": True},
        get_job=lambda job_id: {"id": job_id},
        remove_job=lambda job_id: False,
    )
    lied = body_of(service.index_watch(present, {"action": "unschedule"}))
    assert "not confirmed removed" in lied["message"]
    assert "was removed" not in lied["message"]
    assert "job is gone" not in lied["message"]
    assert len(asked) == 1


def test_every_approval_field_is_refused_when_it_would_be_rewritten(tmp_path):
    asked = []
    runtime = rt(
        tmp_path, Script([]),
        request_approval=lambda question: asked.append(question) or {"approved": True},
        create_job=lambda **kwargs: {"id": "job"},
    )
    base = {"action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku", "deliver": "local"}
    marked = body_of(service.index_watch(runtime, dict(base, id_field="_bold_")))
    assert marked["message"] == service.rewrite_message("id_field", "schedule")
    indexed = body_of(service.index_watch(runtime, dict(base, index="_a-b_")))
    assert indexed["message"] == service.rewrite_message("index", "schedule")
    assert asked == []
    seed(tmp_path, job_id="bad<id", q="hermes", scheduled=True)
    watched = rt(
        tmp_path, Script([]),
        request_approval=lambda question: asked.append(question) or {"approved": True},
        get_job=lambda job_id: {"id": job_id},
        remove_job=lambda job_id: True,
    )
    out = body_of(service.index_watch(watched, {"action": "unschedule"}))
    assert out["message"] == service.rewrite_message("job_id", "unschedule")
    assert "unschedule was not asked" in out["message"]
    assert asked == []


def test_an_underscore_inside_a_word_is_not_an_emphasis_mark(tmp_path):
    """movies_en_us and created_at_id are shown unchanged, so they are not refused."""
    assert service._rewrite_risk("movies_en_us") is False
    assert service._rewrite_risk("created_at_id") is False
    assert service._rewrite_risk("a_b_c") is False
    assert service._rewrite_risk("_bold_") is True
    assert service._rewrite_risk("say _bold_ now") is True
    assert service._rewrite_risk("hello *world*") is True
    asked = []
    runtime = rt(
        tmp_path, Script([]),
        request_approval=lambda question: asked.append(question) or {"approved": True},
        create_job=lambda **kwargs: {"id": "job-9"},
    )
    out = body_of(service.index_watch(runtime, {
        "action": "schedule", "index": "movies_en_us", "q": "a_b_c",
        "id_field": "created_at_id", "deliver": "local",
    }))
    assert out["ok"] is True
    assert len(asked) == 1
    assert "Index: movies_en_us. Query: a_b_c. Id field: created_at_id." in asked[0]
    assert "cannot be shown unchanged" not in out["message"]


def test_a_key_inside_the_url_is_not_an_incomplete_search(tmp_path):
    script = Script([page([hit()])])
    runtime = rt(tmp_path, script, env={"MEILI_HOST": "http://127.0.0.1:9", "MEILI_API_KEY": "search"})
    out = body_of(service.index_search(runtime, {"index": "probes", "q": "hermes", "id_field": "sku"}))
    assert out["message"] == service.MSG_KEY_IN_URL
    assert service.MSG_INCOMPLETE not in out["message"]
    assert script.calls == []


def test_an_unreadable_profile_is_not_printed_as_default(tmp_path):
    def boom():
        raise RuntimeError("profile")

    runtime = rt(
        tmp_path, Script([]),
        request_approval=lambda question: {"approved": True},
        create_job=lambda **kwargs: {"id": "job-9"},
        profile=boom,
    )
    out = body_of(service.index_watch(runtime, {
        "action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku", "deliver": "local",
    }))
    assert "profile unknown" in out["message"]
    assert "hermes cron" not in out["message"]
    blank = rt(
        tmp_path, Script([]),
        request_approval=lambda question: {"approved": True},
        create_job=lambda **kwargs: {"id": "job-8"},
        profile=lambda: None,
    )
    out = body_of(service.index_watch(blank, {
        "action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku", "deliver": "local",
    }))
    assert "profile unknown" in out["message"]
    assert "hermes cron" not in out["message"]


def test_matches_past_twenty_warn_and_a_later_id_is_not_on_the_page(tmp_path):
    first = [hit(f"s{i}") for i in range(20)]
    script = Script([page(first, 21), page(first, 22)])
    seed(tmp_path, baseline_pending=True, seen=[], job_id="")
    runtime = rt(tmp_path, script, cron=lambda: "cron")
    opened = body_of(service.index_watch(runtime, {"action": "check"}))
    assert opened["message"].startswith(service.MSG_BASELINE)
    assert "estimatedTotalHits is 21" in opened["message"]
    assert "never reported" in opened["message"]
    # A quiet baseline would keep this warning inside cron, where [SILENT] drops it.
    assert opened["notify"] is True
    added = body_of(service.index_watch(runtime, {"action": "check"}))
    assert "s20" not in added["message"]
    assert "New ids" not in added["message"]
    assert "estimatedTotalHits is 22" in added["message"]
    assert "never reported" in added["message"]
    assert added["notify"] is False
    saved = json.loads((tmp_path / "state.json").read_text())["seen"]
    assert {"t": "s", "v": "s20"} not in saved
    assert len(saved) == 20


def test_the_full_page_warning_repeats_once_a_day_and_restarts_after_it_clears(tmp_path):
    full = [hit(f"s{i}") for i in range(20)]
    clock = {"t": 1_700_000_000.0}
    script = Script([page(full, 25), page(full, 25), page(full, 25), page(full, 20), page(full, 25)])
    seed(tmp_path, baseline_pending=True, seen=[], job_id="")
    runtime = rt(tmp_path, script, cron=lambda: "cron", now=lambda: clock["t"])
    first = body_of(service.index_watch(runtime, {"action": "check"}))
    assert first["notify"] is True
    assert "estimatedTotalHits is 25" in first["message"]
    clock["t"] += 900.0
    quiet = body_of(service.index_watch(runtime, {"action": "check"}))
    assert quiet["notify"] is False
    clock["t"] += service.DAY
    daily = body_of(service.index_watch(runtime, {"action": "check"}))
    assert daily["notify"] is True
    assert "never reported" in daily["message"]
    clock["t"] += 900.0
    under = body_of(service.index_watch(runtime, {"action": "check"}))
    assert under["notify"] is False
    assert "never reported" not in under["message"]
    assert json.loads((tmp_path / "state.json").read_text())["over"] is None
    clock["t"] += 900.0
    again = body_of(service.index_watch(runtime, {"action": "check"}))
    assert again["notify"] is True
    assert "never reported" in again["message"]


def test_a_manual_check_in_the_isolated_form_does_not_silence_the_next_cron(tmp_path):
    """The form cannot tell cron from a hand-typed check, so the clock never moves here."""
    seed(tmp_path, seen=[{"t": "s", "v": "s1"}])
    before = (tmp_path / "state.json").read_bytes()
    clock = {"t": 1_700_000_000.0}
    script = Script([page([hit("s9")])])
    runtime = rt(tmp_path, script, host=lambda: "1", cron=lambda: "cron", now=lambda: clock["t"])
    manual = body_of(service.index_watch(runtime, {"action": "check"}))
    assert manual["notify"] is True
    assert manual["message"] == service.MSG_CANNOT
    assert (tmp_path / "state.json").read_bytes() == before
    clock["t"] += 900.0
    later = body_of(service.index_watch(runtime, {"action": "check"}))
    assert later["notify"] is True
    assert later["message"] == service.MSG_CANNOT
    assert script.calls == []
    assert (tmp_path / "state.json").read_bytes() == before


def _install_hermes_fakes(monkeypatch, tmp_path):
    def put(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)
        if "." in name:
            parent_name, child = name.rsplit(".", 1)
            parent = sys.modules.get(parent_name)
            if parent is None:
                parent = types.ModuleType(parent_name)
                monkeypatch.setitem(sys.modules, parent_name, parent)
            setattr(parent, child, mod)
        return mod

    put("plugins")
    put("plugins.plugin_storage", plugin_data_dir=lambda name: tmp_path)
    put("tools")
    put(
        "tools.approval_context",
        _is_cron_approval_context=lambda: False,
        _get_approval_mode=lambda: "manual",
        _is_single_query_approval_context=lambda: False,
        _is_unattended_platform_approval_context=lambda: False,
    )
    put(
        "tools.approval",
        _yolo_active=lambda: False,
        request_tool_approval=lambda *args, **kwargs: {"approved": True},
    )
    put("agent")
    put("agent.file_safety", get_write_denied_error=lambda path: None)
    put("cron")
    put(
        "cron.jobs",
        create_job=lambda **kwargs: {"id": "job-live"},
        remove_job=lambda job_id: True,
        get_job=lambda job_id: {"id": job_id},
    )
    put("tools.cronjob_job_args", _origin_from_env=lambda schedule: {"platform": "desk", "chat_id": "1"})
    put("hermes_cli")
    put("hermes_cli.profiles", get_active_profile_name=lambda: "work")
    put("cron.scheduler_delivery", _is_known_delivery_platform=lambda name: False)


def _break_helper(monkeypatch, module, name, mode):
    if mode == "import":
        real = importlib.import_module

        def guarded(mod_name, package=None):
            if mod_name == module:
                raise ModuleNotFoundError(mod_name)
            return real(mod_name, package)

        monkeypatch.setattr(service.importlib, "import_module", guarded)
        monkeypatch.delitem(sys.modules, module, raising=False)
        return
    mod = sys.modules[module]
    if mode == "missing":
        delattr(mod, name)
        return
    def boom(*args, **kwargs):
        raise RuntimeError("renamed")
    setattr(mod, name, boom)


HELPERS = [
    ("plugins.plugin_storage", "plugin_data_dir", "data_dir"),
    ("tools.approval_context", "_is_cron_approval_context", "cron"),
    ("agent.file_safety", "get_write_denied_error", "guard"),
    ("tools.approval", "_yolo_active", "gates"),
    ("tools.approval_context", "_get_approval_mode", "gates"),
    ("tools.approval_context", "_is_single_query_approval_context", "gates"),
    ("tools.approval_context", "_is_unattended_platform_approval_context", "gates"),
    ("tools.approval", "request_tool_approval", "approval"),
    ("cron.jobs", "create_job", "create"),
    ("cron.jobs", "remove_job", "remove"),
    ("cron.jobs", "get_job", "get"),
    ("tools.cronjob_job_args", "_origin_from_env", "origin"),
    ("hermes_cli.profiles", "get_active_profile_name", "profile"),
    ("cron.scheduler_delivery", "_is_known_delivery_platform", "platform"),
]


@pytest.mark.parametrize("module,name,kind", HELPERS)
@pytest.mark.parametrize("mode", ["import", "missing", "raise"])
def test_each_hermes_helper_fails_closed(monkeypatch, tmp_path, module, name, kind, mode):
    _install_hermes_fakes(monkeypatch, tmp_path)
    _break_helper(monkeypatch, module, name, mode)
    if kind == "data_dir":
        with pytest.raises(RuntimeError):
            service.live_runtime()
        return
    runtime = service.live_runtime()
    calls = []

    def exchange(*args, **kwargs):
        calls.append(args)
        return page([hit()])

    runtime.exchange = exchange
    if kind == "cron":
        seed(tmp_path, baseline_pending=True, seen=[])
        out = body_of(service.index_watch(runtime, {"action": "check"}))
        assert out["message"] == service.MSG_CANNOT
        assert calls == []
        assert json.loads((tmp_path / "state.json").read_text())["seen"] == []
        return
    if kind == "guard":
        sys.modules["tools.approval_context"]._is_cron_approval_context = lambda: True
        seed(tmp_path, baseline_pending=True, seen=[], job_id="")
        out = body_of(service.index_watch(runtime, {"action": "check"}))
        assert out["message"] == service.MSG_PAGE_NOT_SAVED
        assert json.loads((tmp_path / "state.json").read_text())["seen"] == []
        return
    if kind == "gates":
        out = body_of(service.index_watch(runtime, {
            "action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku", "deliver": "local",
        }))
        assert out["message"] == service.MSG_CANNOT
        assert not (tmp_path / "state.json").exists()
        return
    if kind == "approval":
        out = body_of(service.index_watch(runtime, {
            "action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku", "deliver": "local",
        }))
        assert out["message"] == service.MSG_CANNOT
        assert not (tmp_path / "state.json").exists()
        return
    if kind == "create":
        out = body_of(service.index_watch(runtime, {
            "action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku", "deliver": "local",
        }))
        assert "was not created" in out["message"]
        assert out["ok"] is False
        return
    if kind == "remove":
        seed(tmp_path, job_id="job-9", scheduled=True)
        out = body_of(service.index_watch(runtime, {"action": "unschedule"}))
        assert "The cron job job-9 was removed" not in out["message"]
        if mode == "import":
            # The same module provides get_job, so the lookup fails closed first.
            assert out["message"] == service.MSG_JOB_UNKNOWN
        else:
            assert "was not removed" in out["message"]
        return
    if kind == "get":
        seed(tmp_path, job_id="job-9")
        out = body_of(service.index_watch(runtime, {"action": "status"}))
        assert "A watch is scheduled" not in out["message"]
        checked = body_of(service.index_watch(runtime, {"action": "check"}))
        assert checked["message"] == service.MSG_JOB_UNKNOWN
        assert calls == []
        return
    if kind == "origin":
        out = body_of(service.index_watch(runtime, {
            "action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku", "deliver": "origin",
        }))
        assert out["message"] == service.MSG_ORIGIN_UNREADABLE
        assert not (tmp_path / "state.json").exists()
        return
    if kind == "profile":
        out = body_of(service.index_watch(runtime, {
            "action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku", "deliver": "local",
        }))
        assert "profile unknown" in out["message"]
        assert "hermes cron" not in out["message"]
        return
    out = body_of(service.index_watch(runtime, {
        "action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku", "deliver": "desk",
    }))
    assert out["message"] == "Delivery could not be checked, so no job was created."
    assert not (tmp_path / "state.json").exists()


def test_a_full_page_with_no_saved_id_says_matches_may_have_been_missed(tmp_path):
    seed(tmp_path, seen=[{"t": "s", "v": "old"}])
    page_ids = [hit(f"n{i}") for i in range(20)]
    script = Script([page(page_ids, 40)])
    out = body_of(service.index_watch(rt(tmp_path, script, cron=lambda: "cron"), {"action": "check"}))
    assert "New ids" in out["message"]
    assert service.MSG_GAP.strip() in out["message"]
    assert out["notify"] is True


def test_unschedule_block_names_unschedule(tmp_path):
    seed(tmp_path, job_id="job-9")
    runtime = rt(
        tmp_path, Script([]),
        gates=lambda: {"cron": False, "yolo": True, "approvals": "manual", "single_query": False, "unattended": False, "approval_loaded": True},
        get_job=lambda job_id: {"id": job_id},
        request_approval=lambda question: {"approved": True},
    )
    out = body_of(service.index_watch(runtime, {"action": "unschedule"}))
    assert "so unschedule was not asked" in out["message"]
    assert "so schedule was not asked" not in out["message"]


def test_public_sources_have_no_cli_or_engine_routes():
    texts = []
    for path in ROOT.glob("*.py"):
        texts.append(path.read_text(encoding="utf-8"))
    blob = "\n".join(texts)
    assert "add_argument" not in blob
    assert "subprocess" not in blob
    assert "/version" not in blob
    assert "/mcp" not in blob
    assert "config_schema" not in blob
    for name in ("slack", "telegram", "discord", "matrix"):
        assert name not in blob
