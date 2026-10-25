"""First request, a health read with no approval, and one retry from the reply."""
from __future__ import annotations

import json
import re
from pathlib import Path

import service

ROOT = Path(__file__).resolve().parents[1]
SECRET = "test-key-value-not-a-real-secret"
PHRASES = ("サーバは生きてる", "猫を検索", "索引の様子", "新しい文書を見張って", "検索して")


class _Script:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(url)
        if url.endswith("/health"):
            raw = b'{"status":"available"}'
        else:
            raw = json.dumps({
                "hits": [{"sku": "s1", "title": "one", "id": "s1"}],
                "estimatedTotalHits": 1,
                "limit": 20,
            }).encode()
        return 200, {"content-type": "application/json"}, raw


def _runtime(tmp_path, script, env):
    return service.Runtime(
        env=env,
        data_dir=tmp_path,
        exchange=script,
        now=lambda: 1_700_000_000.0,
        cron=lambda: "not_cron",
        host=lambda: "",
        write_guard=lambda path: None,
    )


def test_skill_puts_each_japanese_request_on_an_english_search_line():
    text = (ROOT / "skills" / "meilisearch-index" / "SKILL.md").read_text(encoding="utf-8")
    assert "tool_search" in text and "English" in text
    for phrase in PHRASES:
        line = next(row for row in text.splitlines() if phrase in row)
        assert "tool_search" in line and "Meilisearch" in line


def test_readme_first_request_has_four_steps():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text.split("## First request", 1)[1].split("\n## ", 1)[0]
    assert len(re.findall(r"(?m)^\d+\. ", section)) == 4
    assert "index_health" in section and "no arguments" in section


def test_health_succeeds_without_an_approval_callback(tmp_path):
    script = _Script()
    runtime = _runtime(tmp_path, script, {"MEILI_HOST": "http://127.0.0.1:7700"})
    body = json.loads(service.index_health(runtime, {}))
    assert body["ok"] is True
    assert script.calls == ["http://127.0.0.1:7700/health"]


def test_a_partial_search_names_one_complete_call(tmp_path):
    script = _Script()
    runtime = _runtime(
        tmp_path, script,
        {"MEILI_HOST": "http://127.0.0.1:7700", "MEILI_API_KEY": SECRET},
    )
    missing = json.loads(service.index_search(runtime, {"q": "cats"}))
    assert missing["ok"] is False and script.calls == []
    step = missing["next_step"]
    assert "index probes" in step and "q hermes" in step and "id_field sku" in step
    assert step.index("index probes") < step.index("日本語")
    again = json.loads(service.index_search(runtime, {"index": "probes", "q": "hermes", "id_field": "sku"}))
    assert again["ok"] is True
    assert script.calls[-1].endswith("/indexes/probes/search")


def test_a_missing_host_names_health_and_a_bad_action_names_status(tmp_path):
    script = _Script()
    bare = _runtime(tmp_path, script, {"MEILI_API_KEY": SECRET})
    missing = json.loads(service.index_search(bare, {"index": "probes", "q": "hermes", "id_field": "sku"}))
    assert missing["ok"] is False and script.calls == []
    assert "MEILI_HOST" in missing["next_step"] and "http://127.0.0.1:7700" in missing["next_step"]
    hosted = _runtime(tmp_path, script, {"MEILI_HOST": "http://127.0.0.1:7700"})
    health = json.loads(service.index_health(hosted, {}))
    assert health["ok"] is True
    bad = json.loads(service.index_watch(hosted, {"action": "nope"}))
    assert bad["ok"] is False and "action status" in bad["message"]
    assert bad["message"].index("action status") < bad["message"].index("日本語")
    status = json.loads(service.index_watch(hosted, {"action": "status"}))
    assert status["ok"] is True and status["message"] == "No watch query is saved."
