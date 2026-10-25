"""Acceptance checks for index-search. No sockets."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
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
        "prints the `hermes cron create` line and does not delete the job",
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
        assert out["message"] == service.MSG_SEARCH_QUERY
        assert script.calls == []
    out = body_of(service.index_search(runtime, {"index": "probes", "q": "hermes", "id_field": "sku", "filter": "a"}))
    assert out["message"] == service.MSG_SEARCH_ARGS
    assert script.calls == []
    out = body_of(service.index_search(runtime, {"index": "../x", "q": "hermes", "id_field": "sku"}))
    assert out["message"] == service.MSG_SEARCH_UID
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
    created = []
    removed = []
    runtime = rt(
        tmp_path, script, host=lambda: "1", cron=lambda: "cron",
        create_job=lambda **kwargs: created.append(kwargs) or {"id": "job"},
        remove_job=lambda job_id: removed.append(job_id) or True,
        profile=lambda: "default",
    )
    out = body_of(service.index_watch(runtime, {"action": "check"}))
    assert out["message"] == service.MSG_CANNOT
    assert out["notify"] is True
    assert script.calls == []
    again = body_of(service.index_watch(runtime, {}))
    assert again["notify"] is True
    assert again["message"] == service.MSG_CANNOT
    blocked = body_of(service.index_watch(runtime, {
        "action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku",
    }))
    assert blocked["ok"] is False
    assert "does not create or remove" in blocked["message"]
    stopped = body_of(service.index_watch(runtime, {"action": "unschedule"}))
    assert stopped["ok"] is False
    assert "does not create or remove" in stopped["message"]
    assert created == [] and removed == []
    assert (tmp_path / "state.json").read_bytes() == before
    saved = body_of(service.index_watch(runtime, {
        "action": "save", "index": "probes", "q": "hermes", "id_field": "sku", "title_field": "title",
    }))
    assert saved["ok"] is True
    assert "Title field: title" in saved["message"]
    assert "did not search" in saved["message"]
    assert "did not create a cron job" in saved["message"]
    assert script.calls == []
    assert created == [] and removed == []
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


def test_schedule_does_not_ask_or_create(tmp_path):
    """Replaces the gate that asked on every schedule. The plugin no longer asks."""
    script = Script([page([hit()])])
    asked = []
    created = []
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
        created.clear()
        runtime = rt(
            tmp_path, script, gates=lambda g=gates: g,
            request_approval=lambda question: asked.append(question) or {"approved": True},
            create_job=lambda **kwargs: created.append(kwargs) or {"id": "job"},
            cron=lambda: "not_cron",
        )
        out = body_of(service.index_watch(runtime, payload))
        assert out["ok"] is False
        assert "does not create or remove" in out["message"]
        assert asked == []
        assert created == []
    assert script.calls == []
    assert not (tmp_path / "state.json").exists()


def test_save_prints_the_user_cron_line_and_does_not_create_a_job(tmp_path):
    script = Script([])
    asked = []
    jobs = []
    runtime = rt(
        tmp_path, script,
        request_approval=lambda question: asked.append(question) or {"approved": True},
        create_job=lambda **kwargs: jobs.append(kwargs) or {"id": "job1"},
        remove_job=lambda job_id: False,
        profile=lambda: "work",
    )
    payload = {"action": "save", "index": "probes", "q": "hermes", "id_field": "sku", "title_field": "title"}
    service.index_watch(runtime, payload)
    out = body_of(service.index_watch(runtime, payload))
    assert asked == []
    assert jobs == []
    assert "Title field: title" in out["message"]
    assert "hermes -p work cron create" in out["message"]
    assert "--deliver local" in out["message"]
    assert "every 15 minutes" in out["message"]
    assert "hermes cron remove -p work" not in out["message"]
    assert "cron remove" not in out["message"]
    assert "estimatedTotalHits" in out["message"]
    assert "*/15" not in out["message"]
    assert out["message"].index("hermes -p work") < out["message"].index("cron create")
    long = dict(payload, q="h" * 401)
    refused = body_of(service.index_watch(runtime, long))
    assert refused["ok"] is False
    assert service.MSG_QUERY in refused["message"]
    assert jobs == []
    starred = dict(payload, q="hello *world*")
    kept = body_of(service.index_watch(runtime, starred))
    assert kept["ok"] is True
    assert "hello *world*" not in kept["message"].split("hermes -p work cron create", 1)[-1]
    assert jobs == []


def test_deliver_is_not_a_tool_argument(tmp_path):
    script = Script([])
    created = []
    runtime = rt(
        tmp_path, script,
        create_job=lambda **kwargs: created.append(kwargs) or {"id": "job"},
        profile=lambda: "default",
    )
    base = {"action": "save", "index": "probes", "q": "hermes", "id_field": "sku"}
    for deliver in ("", "local", "origin", "all", "cli", "cron", "api_server", "bot-chat"):
        out = body_of(service.index_watch(runtime, dict(base, deliver=deliver)))
        assert out["ok"] is False
        assert "Delivery is not a tool argument" in out["message"]
        assert "--deliver" in out["message"]
        assert created == []
        assert not (tmp_path / "state.json").exists()
    saved = body_of(service.index_watch(runtime, base))
    assert saved["ok"] is True
    assert "--deliver local" in saved["message"]
    assert created == []
    stored = json.loads((tmp_path / "state.json").read_text())
    assert stored["deliver"] == ""
    assert stored["q"] == "hermes"
    seed(tmp_path, deliver="local", seen=[{"t": "s", "v": "s1"}], baseline_pending=False, job_id="seed")
    out = body_of(service.index_watch(runtime, base))
    assert "Title field:" in out["message"]
    assert "This plugin did not remove cron job seed." in out["message"]
    assert "hermes cron remove seed" in out["message"]
    assert "hermes cron remove -p" not in out["message"]
    assert created == []
    kept = json.loads((tmp_path / "state.json").read_text())
    assert kept["seen"] == [{"t": "s", "v": "s1"}]
    assert kept["baseline_pending"] is False
    assert kept["job_id"] == "seed"


def test_failed_save_and_identity_change_keep_seen_ids(tmp_path):
    seed(tmp_path, seen=[{"t": "s", "v": "s1"}], job_id="seed")
    before = (tmp_path / "state.json").read_bytes()
    script = Script([page([hit("s9")])])
    created = []
    runtime = rt(
        tmp_path, script,
        create_job=lambda **kwargs: created.append(kwargs) or {"id": "new"},
        write_guard=lambda path: "denied",
        profile=lambda: "default",
    )
    out = body_of(service.index_watch(runtime, {
        "action": "save", "index": "other", "q": "hermes", "id_field": "sku",
    }))
    assert out["ok"] is False
    assert "was not written" in out["message"]
    assert service.MSG_INCOMPLETE not in out["message"]
    assert (tmp_path / "state.json").read_bytes() == before
    assert script.calls == []
    assert created == []
    runtime = rt(
        tmp_path, script,
        create_job=lambda **kwargs: created.append(kwargs) or {"id": "new"},
        profile=lambda: "default",
    )
    out = body_of(service.index_watch(runtime, {
        "action": "save", "index": "other", "q": "next", "id_field": "sku", "title_field": "name",
    }))
    assert "did not search" in out["message"]
    assert "Title field: name" in out["message"]
    assert "did not create a cron job" in out["message"]
    assert created == []
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["seen"] == [{"t": "s", "v": "s1"}]
    assert saved["baseline_pending"] is True
    assert saved["q"] == "next"
    assert saved["job_id"] == "seed"
    assert script.calls == []


def test_unschedule_does_not_delete_state(tmp_path):
    seed(tmp_path, scheduled=False, job_id="")
    before = (tmp_path / "state.json").read_bytes()
    removed = []
    runtime = rt(tmp_path, Script([]), remove_job=lambda job_id: removed.append(job_id) or True)
    out = body_of(service.index_watch(runtime, {"action": "unschedule"}))
    assert out["ok"] is False
    assert "does not create or remove" in out["message"]
    assert "hermes cron remove" in out["message"]
    assert removed == []
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
        assert searched["message"] == service.MSG_SEARCH_UID
        scheduled = body_of(service.index_watch(runtime, {
            "action": "save", "index": index, "q": "hermes", "id_field": "sku",
        }))
        assert scheduled["message"] == service.MSG_UID
    assert script.calls == []
    assert created == []
    assert not (tmp_path / "state.json").exists()


def test_save_does_not_call_session_origin_or_create_a_job(tmp_path):
    script = Script([])
    created = []
    origins = []
    runtime = rt(
        tmp_path, script,
        create_job=lambda **kwargs: created.append(kwargs) or {"id": "job"},
        session_origin=lambda schedule: origins.append(schedule) or {"platform": "desk", "chat_id": "1"},
        profile=lambda: "default",
    )
    out = body_of(service.index_watch(runtime, {
        "action": "save", "index": "probes", "q": "hermes", "id_field": "sku",
    }))
    assert out["ok"] is True
    assert created == []
    assert origins == []
    assert "hermes cron create" in out["message"]
    assert "--deliver local" in out["message"]
    assert "Title field: (none)" in out["message"]
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["deliver"] == ""
    assert saved["title_field"] == ""


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
    seed(tmp_path, seen=[{"t": "s", "v": "s1"}], job_id="seed")
    before = (tmp_path / "state.json").read_bytes()
    removed = []
    runtime = rt(
        tmp_path, Script([]),
        remove_job=lambda job_id: removed.append(job_id) or True,
        profile=lambda: "default",
    )
    out = body_of(service.index_watch(runtime, {"action": "unschedule"}))
    assert out["ok"] is False
    assert service.MSG_INCOMPLETE not in out["message"]
    assert "was removed" not in out["message"]
    assert "does not create or remove" in out["message"]
    assert removed == []
    assert (tmp_path / "state.json").read_bytes() == before
    created = []
    fresh = rt(
        tmp_path, Script([]),
        create_job=lambda **kwargs: created.append(kwargs) or {"id": "new-job"},
        remove_job=lambda job_id: removed.append(job_id) or True,
        write_guard=lambda path: "denied",
        profile=lambda: "default",
    )
    scheduled = body_of(service.index_watch(fresh, {
        "action": "save", "index": "probes", "q": "hermes", "id_field": "sku",
    }))
    assert scheduled["ok"] is False
    assert "was not written" in scheduled["message"]
    assert "search failed" not in scheduled["message"]
    assert service.MSG_INCOMPLETE not in scheduled["message"]
    assert created == []
    assert removed == []
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


def test_a_marked_query_is_saved_and_stays_out_of_the_cron_line(tmp_path):
    script = Script([])
    asked = []
    created = []
    runtime = rt(
        tmp_path, script,
        request_approval=lambda question: asked.append(question) or {"approved": True},
        create_job=lambda **kwargs: created.append(kwargs) or {"id": "job"},
        profile=lambda: "default",
    )
    payload = {"action": "save", "index": "probes", "q": "hermes", "id_field": "sku"}
    marked = body_of(service.index_watch(runtime, dict(payload, q="say _bold_ now")))
    assert marked["ok"] is True
    assert "Query: say _bold_ now" in marked["message"]
    command = marked["message"].split("hermes cron create", 1)[1]
    assert "_bold_" not in command
    assert asked == []
    assert created == []


def test_a_quoted_query_is_not_copied_into_the_cron_line(tmp_path):
    asked = []
    created = []
    runtime = rt(
        tmp_path, Script([]),
        request_approval=lambda question: asked.append(question) or {"approved": True},
        create_job=lambda **kwargs: created.append(kwargs) or {"id": "job"},
        profile=lambda: "default",
    )
    payload = {"action": "save", "index": "probes", "q": "hermes", "id_field": "sku"}
    ordinary = body_of(service.index_watch(runtime, dict(payload, q='say "hi"')))
    assert ordinary["ok"] is True
    assert 'Query: say "hi"' in ordinary["message"]
    command = ordinary["message"].split("hermes cron create", 1)[1]
    assert 'say "hi"' not in command
    assert asked == []
    assert created == []


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


def test_unschedule_does_not_remove_the_job(tmp_path):
    asked = []
    removed = []
    idle = rt(tmp_path, Script([]), request_approval=lambda question: asked.append(question) or {"approved": False})
    out = body_of(service.index_watch(idle, {"action": "unschedule"}))
    assert out["ok"] is False
    assert "does not create or remove" in out["message"]
    assert asked == []
    seed(tmp_path, job_id="job-9", scheduled=True)
    before = (tmp_path / "state.json").read_bytes()
    runtime = rt(
        tmp_path, Script([]),
        request_approval=lambda question: asked.append(question) or {"approved": True},
        get_job=lambda job_id: {"id": job_id},
        remove_job=lambda job_id: removed.append(job_id) or True,
        profile=lambda: "work",
    )
    out = body_of(service.index_watch(runtime, {"action": "unschedule"}))
    assert out["ok"] is False
    assert "was removed" not in out["message"]
    assert "does not create or remove" in out["message"]
    assert "hermes cron remove" in out["message"]
    assert asked == []
    assert removed == []
    assert (tmp_path / "state.json").read_bytes() == before
    created = []
    schedule = rt(
        tmp_path, Script([]),
        create_job=lambda **kwargs: created.append(kwargs) or {"id": "job-new"},
        profile=lambda: "default",
    )
    saved = body_of(service.index_watch(schedule, {
        "action": "save", "index": "probes", "q": "hermes", "id_field": "sku", "title_field": "title",
    }))
    assert saved["ok"] is True
    assert "Title field: title" in saved["message"]
    assert "hermes cron create" in saved["message"]
    assert created == []
    assert json.loads((tmp_path / "state.json").read_text())["job_id"] == "job-9"


def test_status_names_the_old_job_and_does_not_delete_it(tmp_path):
    seed(tmp_path, job_id="job-9", scheduled=True)
    before = json.loads((tmp_path / "state.json").read_text())["seen"]
    script = Script([page([hit("s9")])])
    removed = []
    runtime = rt(
        tmp_path, script,
        cron=lambda: "cron",
        get_job=lambda job_id: None,
        remove_job=lambda job_id: removed.append(job_id) or False,
        profile=lambda: "work",
    )
    status = body_of(service.index_watch(runtime, {"action": "status"}))
    assert "A watch query is saved." in status["message"]
    assert "Index: probes" in status["message"]
    assert "Title field: title" in status["message"]
    assert "job is gone" not in status["message"]
    assert "was removed" not in status["message"]
    assert "hermes -p work cron create" in status["message"]
    assert "This plugin did not remove cron job job-9." in status["message"]
    assert "hermes -p work cron remove job-9" in status["message"]
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["job_id"] == "job-9"
    assert saved["scheduled"] is True
    assert saved["seen"] == before
    checked = body_of(service.index_watch(runtime, {"action": "check"}))
    assert "s9" in checked["message"] or "Baseline" in checked["message"] or checked["ok"] is True or "did not write" in checked["message"]
    assert script.calls
    assert removed == []
    stopped = body_of(service.index_watch(runtime, {"action": "unschedule"}))
    assert "was removed" not in stopped["message"]
    assert "job is gone" not in stopped["message"]
    assert json.loads((tmp_path / "state.json").read_text())["job_id"] == "job-9"


def test_save_keeps_a_stored_job_id_the_user_must_remove(tmp_path):
    asked = []
    removed = []
    runtime = rt(
        tmp_path, Script([]),
        request_approval=lambda question: asked.append(question) or {"approved": True},
        create_job=lambda **kwargs: {"id": "job"},
        profile=lambda: "default",
    )
    marked = body_of(service.index_watch(runtime, {
        "action": "save", "index": "_a-b_", "q": "hermes", "id_field": "_bold_",
    }))
    assert marked["ok"] is True
    assert "Index: _a-b_" in marked["message"]
    assert "Id field: _bold_" in marked["message"]
    assert asked == []
    seed(tmp_path, job_id="bad<id", q="hermes", scheduled=True)
    watched = rt(
        tmp_path, Script([]),
        get_job=lambda job_id: {"id": job_id},
        remove_job=lambda job_id: removed.append(job_id) or True,
        profile=lambda: "default",
    )
    out = body_of(service.index_watch(watched, {
        "action": "save", "index": "probes", "q": "hermes", "id_field": "sku",
    }))
    assert "This plugin did not remove cron job bad<id." in out["message"]
    assert "hermes cron remove bad<id" in out["message"]
    assert "was removed" not in out["message"]
    assert removed == []
    assert json.loads((tmp_path / "state.json").read_text())["job_id"] == "bad<id"


def test_an_underscore_inside_a_word_is_stored_unchanged(tmp_path):
    """movies_en_us and created_at_id are saved as given."""
    asked = []
    created = []
    runtime = rt(
        tmp_path, Script([]),
        request_approval=lambda question: asked.append(question) or {"approved": True},
        create_job=lambda **kwargs: created.append(kwargs) or {"id": "job-9"},
        profile=lambda: "default",
    )
    out = body_of(service.index_watch(runtime, {
        "action": "save", "index": "movies_en_us", "q": "a_b_c",
        "id_field": "created_at_id",
    }))
    assert out["ok"] is True
    assert "Index: movies_en_us. Query: a_b_c. Id field: created_at_id." in out["message"]
    assert asked == []
    assert created == []
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["index"] == "movies_en_us"
    assert saved["id_field"] == "created_at_id"


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

    payload = {"action": "save", "index": "probes", "q": "hermes", "id_field": "sku", "title_field": "title"}
    runtime = rt(tmp_path, Script([]), profile=boom)
    out = body_of(service.index_watch(runtime, payload))
    assert "Title field: title" in out["message"]
    assert "profile unknown" in out["message"]
    assert "hermes cron" not in out["message"]
    blank = rt(tmp_path, Script([]), profile=lambda: None)
    out = body_of(service.index_watch(blank, payload))
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
    try:
        import agent.secret_scope as real_scope
    except ImportError:
        real_scope = None

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
    put("cron.execution_identity", current_cron_execution=lambda: None)
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
    # live_runtime reads MEILI_* through agent.secret_scope. The fake agent package
    # must still expose that module, or the helper probe raises ImportError.
    if real_scope is None:
        real_scope = types.ModuleType("agent.secret_scope")

        def get_secret(name, default=None):
            value = os.environ.get(name)
            return default if value is None else value

        real_scope.get_secret = get_secret
        real_scope.UnscopedSecretError = type("UnscopedSecretError", (RuntimeError,), {})
        real_scope.set_secret_scope = lambda mapping: None
        real_scope.reset_secret_scope = lambda token: None
        real_scope.set_multiplex_active = lambda active: None
    monkeypatch.setitem(sys.modules, "agent.secret_scope", real_scope)
    setattr(sys.modules["agent"], "secret_scope", real_scope)


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
    ("cron.execution_identity", "current_cron_execution", "cron"),
    ("agent.file_safety", "get_write_denied_error", "guard"),
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
        sys.modules["cron.execution_identity"].current_cron_execution = lambda: {"id": "cron"}
        seed(tmp_path, baseline_pending=True, seen=[], job_id="")
        out = body_of(service.index_watch(runtime, {"action": "check"}))
        assert out["message"] == service.MSG_PAGE_NOT_SAVED
        assert json.loads((tmp_path / "state.json").read_text())["seen"] == []
        return
    raise AssertionError(kind)


def test_a_full_page_with_no_saved_id_says_matches_may_have_been_missed(tmp_path):
    seed(tmp_path, seen=[{"t": "s", "v": "old"}])
    page_ids = [hit(f"n{i}") for i in range(20)]
    script = Script([page(page_ids, 40)])
    out = body_of(service.index_watch(rt(tmp_path, script, cron=lambda: "cron"), {"action": "check"}))
    assert "New ids" in out["message"]
    assert service.MSG_GAP.strip() in out["message"]
    assert out["notify"] is True


def test_unschedule_names_the_user_remove_command(tmp_path):
    seed(tmp_path, job_id="job-9")
    asked = []
    removed = []
    runtime = rt(
        tmp_path, Script([]),
        get_job=lambda job_id: {"id": job_id},
        remove_job=lambda job_id: removed.append(job_id) or True,
        request_approval=lambda question: asked.append(question) or {"approved": False},
        profile=lambda: "default",
    )
    out = body_of(service.index_watch(runtime, {"action": "unschedule"}))
    assert asked == []
    assert removed == []
    assert "does not create or remove" in out["message"]
    assert "hermes cron remove" in out["message"]
    assert out["ok"] is False
    assert json.loads((tmp_path / "state.json").read_text())["job_id"] == "job-9"


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


_SEARCH_ARGS = {"index": "probes", "q": "hermes", "id_field": "sku"}
_MEILI_ENV_READ = re.compile(
    r"""os\s*\.\s*(?:getenv\s*\(|environ\s*\.\s*get\s*\(|environ\s*\[\s*)\s*['\"](?:MEILI_HOST|MEILI_API_KEY)['\"]"""
)


def _search_handler():
    # A file named __init__.py is a package unless search locations are cleared.
    # The flat import is the same live_runtime() the package path calls, and it
    # uses the service module this test patches.
    spec = importlib.util.spec_from_file_location("index_search_scope_entry", ROOT / "__init__.py")
    spec.submodule_search_locations = None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    found = {}

    class Ctx:
        def register_tool(self, **kwargs):
            found[kwargs["name"]] = kwargs["handler"]

    mod.register(Ctx())
    return found["index_search"]


def _patch_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "plugin_data_directory", lambda: tmp_path)


def _release_scope(scope, token):
    if token is not None:
        try:
            scope.reset_secret_scope(token)
        except ValueError:
            pass
    scope.set_secret_scope(None)
    scope.set_multiplex_active(False)


def test_scope_value_is_used_when_environ_differs(monkeypatch, tmp_path):
    scope = pytest.importorskip("agent.secret_scope")
    monkeypatch.setenv("MEILI_HOST", "http://env.example:9")
    monkeypatch.setenv("MEILI_API_KEY", "env-key-value")
    calls = []

    def perform(method, url, headers, body, timeout=30, open_fn=None):
        calls.append({"url": url, "authorization": headers.get("Authorization")})
        return page([hit()])

    monkeypatch.setattr(client, "perform", perform)
    _patch_data_dir(monkeypatch, tmp_path)
    handler = _search_handler()
    token = scope.set_secret_scope({
        "MEILI_HOST": "http://scope.example:9",
        "MEILI_API_KEY": "scope-key-value",
    })
    try:
        out = body_of(handler(_SEARCH_ARGS))
        assert out["ok"] is True
        assert calls[0]["url"].startswith("http://scope.example:9/")
        assert calls[0]["authorization"] == "Bearer scope-key-value"
        scope.reset_secret_scope(token)
        token = scope.set_secret_scope({
            "MEILI_HOST": "http://scope-two.example:9",
            "MEILI_API_KEY": "scope-key-two",
        })
        body_of(handler(_SEARCH_ARGS))
        assert calls[1]["url"].startswith("http://scope-two.example:9/")
        assert calls[1]["authorization"] == "Bearer scope-key-two"
    finally:
        _release_scope(scope, token)


def test_multiplex_scope_miss_does_not_read_environ(monkeypatch, tmp_path):
    scope = pytest.importorskip("agent.secret_scope")
    monkeypatch.setenv("MEILI_HOST", "http://env.example:9")
    monkeypatch.setenv("MEILI_API_KEY", "env-key-value")
    calls = []

    def perform(*args, **kwargs):
        calls.append(args)
        raise AssertionError("upstream was called")

    monkeypatch.setattr(client, "perform", perform)
    _patch_data_dir(monkeypatch, tmp_path)
    handler = _search_handler()
    scope.set_multiplex_active(True)
    token = scope.set_secret_scope({"UNRELATED": "present"})
    try:
        raw = handler(_SEARCH_ARGS)
    finally:
        _release_scope(scope, token)
    out = body_of(raw)
    assert out["ok"] is False
    assert out["message"] == service.MSG_HOST_REQUIRED
    assert calls == []
    assert "env.example" not in raw
    assert "env-key-value" not in raw


def test_unscoped_multiplex_raises_and_does_not_call_upstream(monkeypatch, tmp_path):
    scope = pytest.importorskip("agent.secret_scope")
    monkeypatch.setenv("MEILI_HOST", "http://env.example:9")
    monkeypatch.setenv("MEILI_API_KEY", "env-key-value")
    calls = []

    def perform(*args, **kwargs):
        calls.append(args)
        raise AssertionError("upstream was called")

    monkeypatch.setattr(client, "perform", perform)
    _patch_data_dir(monkeypatch, tmp_path)
    handler = _search_handler()
    scope.set_multiplex_active(True)
    token = scope.set_secret_scope(None)
    try:
        with pytest.raises(scope.UnscopedSecretError):
            handler(_SEARCH_ARGS)
    finally:
        _release_scope(scope, token)
    assert calls == []


def test_plugin_code_does_not_read_meili_from_environ():
    blob = "\n".join(path.read_text(encoding="utf-8") for path in sorted(ROOT.glob("*.py")))
    assert _MEILI_ENV_READ.search(blob) is None
    assert "env=os.environ" not in (ROOT / "service.py").read_text(encoding="utf-8")
