"""Schedule and unschedule go through Hermes's real pre_tool_call hook. No network."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import service
from service import Runtime, index_watch, on_pre_tool_call
from test_acceptance import body_of, seed

ROOT = Path(__file__).resolve().parents[1]
HERMES_SRC = Path(os.environ.get("HERMES_SRC") or ROOT.parent / "hermes-agent-ref")
SCHEDULE = {"action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku", "deliver": "local"}


def _runtime(tmp_path):
    created = []

    def create_job(**kwargs):
        created.append(kwargs)
        return {"id": "job-new"}

    removed = []
    runtime = Runtime(
        data_dir=tmp_path,
        public_approval=True,
        create_job=create_job,
        remove_job=lambda job_id: removed.append(job_id) or True,
        get_job=lambda job_id: {"id": job_id},
        host=lambda: None,
        cron=lambda: "not_cron",
        profile=lambda: "default",
        known_platform=lambda name: True,
    )
    runtime.created = created
    runtime.removed = removed
    return runtime


@pytest.fixture
def hermes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.delenv("HERMES_YOLO", raising=False)
    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
    if not (HERMES_SRC / "hermes_cli" / "plugins.py").is_file():
        pytest.skip("set HERMES_SRC to a Hermes source tree")
    src = str(HERMES_SRC)
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        import hermes_cli.plugins as hp
    except Exception as exc:
        pytest.skip("Hermes source needs a newer Python: " + str(exc))
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(service, "live_runtime", lambda: runtime)
    mgr = hp.PluginManager(scope_key=str(tmp_path))
    mgr._discovered = True
    mgr._hooks["pre_tool_call"] = [on_pre_tool_call]
    monkeypatch.setattr(hp, "get_plugin_manager", lambda: mgr)
    monkeypatch.setattr(hp, "_plugin_manager", mgr)
    from tools.approval import clear_session
    from tools.approval_context import get_current_session_key

    clear_session(get_current_session_key())
    service._STASH.clear()
    yield hp, runtime
    service._STASH.clear()
    clear_session(get_current_session_key())


def _assert_blocked_skips_handler(monkeypatch, tool_name, args):
    import agent.tool_executor as executor
    import model_tools

    called = []
    monkeypatch.setattr(model_tools, "_execute_tool", lambda *_a, **_k: called.append("handler") or "{}")
    blocked = model_tools.handle_function_call(tool_name, args, session_id="s1", task_id="t1")
    assert called == []
    assert "BLOCKED" in str(blocked)

    ref = executor._ToolCallRef(tool_name, dict(args), "t1", "c1", [])
    state = executor._ManagedToolResult(
        result=None, args=dict(args), middleware_trace=[], blocked=False, dispatched=False,
    )

    class _Agent:
        session_id = "s1"

    executor._dispatch_authorized_once(
        _Agent(),
        state,
        ref,
        execute=lambda next_args: called.append(next_args) or "ran",
        scope_block=None,
        display_index=None,
        begin_execution=None,
        authorization_gate=None,
    )
    assert called == []
    assert state.blocked is True


def test_other_room_preview_stays(hermes):
    assert on_pre_tool_call("index_watch", SCHEDULE, session_id="room-a", task_id="t1")["action"] == "approve"
    assert any(key[2:] == ("room-a", "t1") for key in service._STASH)
    other = dict(SCHEDULE, q="other")
    assert on_pre_tool_call("index_watch", other, session_id="room-b", task_id="t2")["action"] == "approve"
    assert any(key[2:] == ("room-a", "t1") for key in service._STASH)
    assert any(key[2:] == ("room-b", "t2") for key in service._STASH)


def test_dispatcher_refusal_does_not_call_the_handler(hermes, monkeypatch):
    _assert_blocked_skips_handler(monkeypatch, "index_watch", SCHEDULE)


def test_write_returns_approve(hermes):
    hp, _runtime_obj = hermes
    details = hp._get_pre_tool_call_directive_details("index_watch", SCHEDULE)
    assert details.action == "approve"
    assert details.message
    assert "\n" not in details.message
    assert "probes" in details.message
    assert "hermes" in details.message
    assert "every 15 minutes" in details.message
    assert str(details.rule_key).startswith("index_watch:")
    assert len(str(details.rule_key)) == len("index_watch:") + 16


def test_same_plan_reuses_rule_key_and_session_does_not_cover_another(hermes):
    hp, _runtime_obj = hermes
    first = hp._get_pre_tool_call_directive_details("index_watch", SCHEDULE)
    again = hp._get_pre_tool_call_directive_details("index_watch", dict(SCHEDULE))
    other = hp._get_pre_tool_call_directive_details("index_watch", dict(SCHEDULE, q="other"))
    titled = hp._get_pre_tool_call_directive_details("index_watch", dict(SCHEDULE, title_field="name"))
    assert first.rule_key == again.rule_key
    assert first.rule_key != other.rule_key
    assert first.rule_key != titled.rule_key
    from tools.approval import approve_session
    from tools.approval_context import get_current_session_key

    approve_session(get_current_session_key(), "plugin_rule:" + first.rule_key)
    allowed, _modified = hp._dispatch_pre_tool_call_hooks("index_watch", SCHEDULE)
    blocked, _modified = hp._dispatch_pre_tool_call_hooks("index_watch", dict(SCHEDULE, q="other"))
    assert allowed is None
    assert blocked and "BLOCKED" in blocked


def test_read_is_silent(hermes):
    hp, _runtime_obj = hermes
    assert hp._get_pre_tool_call_directive_details("index_search", SCHEDULE).action is None
    assert hp._get_pre_tool_call_directive_details("index_health", {}).action is None
    assert hp._get_pre_tool_call_directive_details("index_watch", {"action": "status"}).action is None
    assert hp._get_pre_tool_call_directive_details("index_watch", {"action": "check"}).action is None


def test_denied_gate_creates_nothing(hermes):
    hp, runtime = hermes
    body = body_of(index_watch(runtime, SCHEDULE))
    assert body["ok"] is False
    assert body.get("isError") is True
    assert runtime.created == []
    block, _modified = hp._dispatch_pre_tool_call_hooks("index_watch", SCHEDULE)
    assert block
    assert "BLOCKED" in block
    assert runtime.created == []


def test_mismatch_after_preview_removes_nothing(hermes, tmp_path):
    hp, runtime = hermes
    seed(tmp_path, job_id="job-9")
    assert hp._get_pre_tool_call_directive_details("index_watch", {"action": "unschedule"}).action == "approve"
    seed(tmp_path, job_id="job-other")
    body = body_of(index_watch(runtime, {"action": "unschedule"}))
    assert body.get("isError") is True
    assert "changed" in body["message"]
    assert runtime.removed == []


def test_no_private_imports():
    pattern = "|".join(
        [
            "tools\\." + "approval",
            "_" + "yolo" + "_active",
            "approval" + "_context",
            "approval" + "_prompt",
            "request_elicitation_consent",
            "prompt_dangerous_approval",
        ]
    )
    proc = subprocess.run(
        ["grep", "-rnE", pattern, "--include=*.py", "."],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    leaked = [
        line
        for line in proc.stdout.splitlines()
        if "/tests/" not in line and not line.startswith("tests/")
    ]
    assert leaked == [], leaked


def test_preview_for_one_query_does_not_cover_another(hermes):
    _hp, runtime = hermes
    on_pre_tool_call("index_watch", SCHEDULE)
    other = dict(SCHEDULE, q="other")
    body = body_of(index_watch(runtime, other))
    assert runtime.created == []
    assert body["ok"] is False


def test_preview_from_another_session_is_not_used(hermes):
    _hp, runtime = hermes
    on_pre_tool_call("index_watch", SCHEDULE, session_id="other", task_id="t1")
    body = body_of(index_watch(runtime, SCHEDULE))
    assert runtime.created == []
    assert body["ok"] is False


def test_preview_from_another_task_is_not_used(hermes):
    _hp, runtime = hermes
    on_pre_tool_call("index_watch", SCHEDULE, session_id="s1", task_id="other")
    assert body_of(index_watch(runtime, SCHEDULE))["ok"] is False
    assert runtime.created == []


def test_silent_call_rewritten_to_a_write_finds_no_preview(hermes):
    _hp, runtime = hermes
    on_pre_tool_call("index_watch", SCHEDULE)
    assert on_pre_tool_call("index_watch", dict(SCHEDULE, extra=1)) is None
    assert body_of(index_watch(runtime, SCHEDULE))["ok"] is False
    assert runtime.created == []


def test_old_preview_is_refused(hermes, monkeypatch):
    import time

    _hp, runtime = hermes
    on_pre_tool_call("index_watch", SCHEDULE)
    real = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: real + 601)
    body = body_of(index_watch(runtime, SCHEDULE))
    assert "too old" in body["message"]
    assert runtime.created == []


def test_matching_preview_creates_one_job(hermes):
    _hp, runtime = hermes
    assert on_pre_tool_call("index_watch", SCHEDULE)["action"] == "approve"
    body = body_of(index_watch(runtime, SCHEDULE))
    assert body["ok"] is True
    assert runtime.created
    assert body_of(index_watch(runtime, SCHEDULE))["ok"] is False
    assert len(runtime.created) == 1
