"""The plugin does not register a pre_tool_call hook and does not create cron jobs."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import service
from test_acceptance import body_of, seed

ROOT = Path(__file__).resolve().parents[1]


def test_register_adds_no_hook():
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    assert "register_hook" not in source
    assert "pre_tool_call" not in source
    assert not hasattr(service, "on_pre_tool_call")
    name = "index_search_no_hook"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
        hooks = []

        class Ctx:
            profile_name = "work"

            def register_tool(self, **kwargs):
                return None

            def register_hook(self, *args, **kwargs):
                hooks.append(args)

        mod.register(Ctx())
        assert hooks == []
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)


def test_save_schedule_and_unschedule_do_not_touch_cron_jobs(tmp_path):
    created = []
    removed = []
    runtime = service.Runtime(
        data_dir=tmp_path,
        create_job=lambda **kwargs: created.append(kwargs) or {"id": "job-new"},
        remove_job=lambda job_id: removed.append(job_id) or True,
        get_job=lambda job_id: {"id": job_id},
        profile=lambda: "work",
        write_guard=lambda path: None,
    )
    seed(tmp_path, job_id="job-9")
    saved = body_of(service.index_watch(runtime, {
        "action": "save", "index": "probes", "q": "hermes", "id_field": "sku", "title_field": "name",
    }))
    assert saved["ok"] is True
    assert "Title field: name" in saved["message"]
    assert "hermes -p work cron create" in saved["message"]
    assert "This plugin did not remove cron job job-9." in saved["message"]
    assert "hermes -p work cron remove job-9" in saved["message"]
    assert "--deliver local" in saved["message"]
    scheduled = body_of(service.index_watch(runtime, {
        "action": "schedule", "index": "probes", "q": "hermes", "id_field": "sku",
    }))
    stopped = body_of(service.index_watch(runtime, {"action": "unschedule"}))
    assert scheduled["ok"] is False and stopped["ok"] is False
    assert "does not create or remove" in scheduled["message"]
    assert "does not create or remove" in stopped["message"]
    assert created == []
    assert removed == []


def test_public_text_does_not_say_the_plugin_asks():
    files = [
        ROOT / "__init__.py",
        ROOT / "plugin.yaml",
        ROOT / "README.md",
        ROOT / "service.py",
        ROOT / "skills" / "meilisearch-index" / "SKILL.md",
    ]
    blob = "\n".join(path.read_text(encoding="utf-8") for path in files)
    assert "ask for approval every time" not in blob
    assert "asks once" not in blob
    assert "ask once" not in blob
    assert "does not ask" in (ROOT / "README.md").read_text(encoding="utf-8")


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
