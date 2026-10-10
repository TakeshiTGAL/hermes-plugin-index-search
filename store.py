"""Watch file under plugin-data/index-search. Corrupt bytes are never replaced."""

from __future__ import annotations

import json
import os
from pathlib import Path

SEEN_CAP = 500
FILE_NAME = "state.json"


def state_path(directory: Path) -> Path:
    return directory / FILE_NAME


def _seen_ok(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    kind = item.get("t")
    value = item.get("v")
    if kind == "s":
        return isinstance(value, str) and value != ""
    if kind == "i":
        return isinstance(value, int) and not isinstance(value, bool)
    return False


def _fail_ok(fail: object) -> bool:
    if fail is None:
        return True
    if not isinstance(fail, dict):
        return False
    at = fail.get("at")
    return isinstance(fail.get("cause"), str) and isinstance(at, (int, float)) and not isinstance(at, bool)


def _over_ok(over: object) -> bool:
    if over is None:
        return True
    if not isinstance(over, dict):
        return False
    at = over.get("at")
    return isinstance(at, (int, float)) and not isinstance(at, bool)


def load(directory: Path) -> tuple[str, dict | None]:
    path = state_path(directory)
    if not path.exists():
        return "missing", None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "corrupt", None
    if not isinstance(data, dict):
        return "corrupt", None
    seen = data.get("seen", [])
    if not isinstance(seen, list) or not all(_seen_ok(item) for item in seen):
        return "corrupt", None
    if not _fail_ok(data.get("fail", None)):
        return "corrupt", None
    if not _over_ok(data.get("over", None)):
        return "corrupt", None
    for key in ("index", "q", "id_field", "title_field", "deliver", "job_id"):
        if key in data and data[key] is not None and not isinstance(data[key], str):
            return "corrupt", None
    for key in ("baseline_pending", "last_empty", "scheduled", "job_gone"):
        if key in data and not isinstance(data[key], bool):
            return "corrupt", None
    return "ok", data


def normalize(data: dict) -> dict:
    out = dict(data)
    out["seen"] = list(data.get("seen") or [])
    out["baseline_pending"] = bool(data["baseline_pending"]) if "baseline_pending" in data else True
    out["last_empty"] = bool(data["last_empty"]) if "last_empty" in data else False
    out["fail"] = data.get("fail", None)
    out["over"] = data.get("over", None)
    out["title_field"] = data.get("title_field") or ""
    out["deliver"] = data.get("deliver") or ""
    out["job_id"] = data.get("job_id") or ""
    out["job_gone"] = bool(data.get("job_gone"))
    if "scheduled" in data:
        out["scheduled"] = bool(data["scheduled"])
    else:
        out["scheduled"] = bool(out.get("index") and out.get("q") and out.get("id_field"))
    return out


def save(directory: Path, state: dict, guard) -> str:
    """Write state. guard is get_write_denied_error, called with the path only.

    Returns ok, no_guard, or denied. A failed write leaves the previous file in place.
    """
    if guard is None:
        return "no_guard"
    path = state_path(directory)
    try:
        denied = guard(str(path))
    except Exception:
        return "denied"
    if denied:
        return "denied"
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    tmp = directory / f".{FILE_NAME}.{os.getpid()}.tmp"
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return "denied"
    return "ok"
