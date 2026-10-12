"""Search, health, and one saved watch. Seen ids move only on a proven cron check."""

from __future__ import annotations

import html
import importlib
import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

if __package__:
    from . import client, store
else:
    import client
    import store

TOOLSET = "index_search"
PLUGIN_NAME = "index-search"
# Internal schedule only. Public text says "every 15 minutes" and never this expression.
SCHEDULE_EXPR = "*/15 * * * *"
REASON_BUDGET = 300
# An HTML approval card measures the reason after html.escape and cuts past this.
ESCAPED_REASON_BUDGET = 500
SEEN_CAP = store.SEEN_CAP
DAY = 86400
# A daily tick can land a few seconds early. The margin stays inside one 15-minute slot.
CADENCE_EARLY = 90
UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SAFE_CODE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
REWRITE_CHARS = set("<>*\\`~[]|@")
# Markdown emphasis with an underscore needs a word edge on both ends, so movies_en_us and
# created_at_id are shown unchanged and only a run like _bold_ is a rewrite.
EMPHASIS_RE = re.compile(r"(?<![0-9A-Za-z_])_(?!\s)[^\n]*?_(?![0-9A-Za-z])")
SCHEDULE_TOKENS = {"every", "in", "at", "weekdays", "monday", "9am", "5m"}
REFUSED_DELIVER = {"cli", "cron", "api_server", "bot-chat"}
PLAIN_DELIVER = {"origin", "local", "all"}

MSG_QUERY = "The query must be a non-empty string of 400 characters or fewer."
MSG_SEARCH_ARGS = "index_search takes only index, q, id_field, and an optional title_field."
MSG_UID = "The index uid must be 1 to 64 letters, digits, hyphens, or underscores."
MSG_HOST_REQUIRED = "MEILI_HOST is required."
MSG_HOST_BAD = "MEILI_HOST is not a usable origin."
MSG_KEY_REQUIRED = "MEILI_API_KEY is required for search."
MSG_REDIRECT = "The server redirected the request. It was not followed."
MSG_INCOMPLETE = "The search did not complete, and seen ids were not advanced."
MSG_SCHEDULE_NOT_WRITTEN = "The new cron job was removed, and the watch file was not written. Seen ids were not changed."
MSG_UNSCHEDULE_NOT_WRITTEN = "The cron job {job_id} was removed, but the watch file was not updated. The file still says a watch is scheduled. Seen ids were not changed."
MSG_PAGE_NOT_SAVED = "The page was read, but the watch file was not written. Seen ids were not advanced."
MSG_ORIGIN_UNREADABLE = "The session origin could not be read, so no job was created. Seen ids were not changed."
MSG_NO_ID = "A hit has no usable id field, so seen ids were not advanced."
MSG_DUP = "This page has duplicate ids, so seen ids were not advanced."
MSG_TOO_MANY = "The page has more than 20 hits, so seen ids were not advanced."
MSG_ZERO = "This page has 0 hits. Seen ids were kept."
MSG_BASELINE = "Baseline saved. These ids are not reported as new."
MSG_NOT_CRON_BASELINE = "This check is not a proven cron run, so the baseline was not saved. Seen ids were not advanced."
REWRITE_LABELS = {
    "index": "index uid",
    "q": "query",
    "id_field": "id field",
    "deliver": "delivery target",
    "job_id": "cron job id",
}
MSG_CANNOT = "This form cannot monitor, so seen ids were not advanced."
MSG_GAP = (
    " This page shares no saved id, so matches between the saved ids and this page may have been missed."
)
MSG_MANUAL_SEEN = " This call did not write seen ids."
MSG_HEALTH_ARGS = "index_health takes no arguments."
MSG_APPROVAL_FIT = "The approval text does not fit in one message. Shorten the query and try again."
MSG_BAD_PAGE = "The search returned an unusable page, so seen ids were not advanced."
MSG_ACTION = "index_watch action must be schedule, unschedule, status, or check."
MSG_KEY_IN_URL = "The API key is part of the request URL, so nothing was sent."
MSG_NOTHING_UNSCHEDULE = "There is nothing to unschedule. Seen ids were kept."
MSG_JOB_GONE = "The cron job is gone. Seen ids were not advanced."
MSG_JOB_GONE_KEPT = "The cron job is gone. Seen ids were kept."
MSG_JOB_UNKNOWN = "The cron job could not be confirmed, so seen ids were not advanced."
MSG_LIMIT = (
    " Only the first 20 hits are watched. If estimatedTotalHits is over 20, "
    "matches past that page are never reported."
)

JOB_PROMPT = (
    "Call the index_watch tool with action check and no other arguments. "
    "If notify is true, relay the tool message and nothing else. "
    "If notify is false, reply with exactly [SILENT]. "
    "Do not call index_search. Calling index_search does not advance seen ids. "
    "Do not schedule or unschedule."
)


def _load(module: str, name: str):
    try:
        mod = importlib.import_module(module)
    except Exception:
        return "failed", None
    if not hasattr(mod, name):
        return "missing", None
    try:
        return "ok", getattr(mod, name)
    except Exception:
        return "failed", None


class Runtime:
    def __init__(self, **kwargs):
        self.env = kwargs.get("env") or {}
        self.data_dir = Path(kwargs["data_dir"])
        self.now = kwargs.get("now") or __import__("time").time
        self.cron = kwargs.get("cron") or (lambda: "not_cron")
        self.host = kwargs.get("host") or (lambda: None)
        self.exchange = kwargs.get("exchange") or client.perform
        self.gates = kwargs.get("gates") or _open_gates
        self.request_approval = kwargs.get("request_approval") or (lambda question: {"approved": False})
        self.create_job = kwargs.get("create_job") or (lambda **kw: (_ for _ in ()).throw(RuntimeError("no cron")))
        self.remove_job = kwargs.get("remove_job") or (lambda job_id: False)
        self.get_job = kwargs.get("get_job") or (lambda job_id: {"id": job_id})
        self.profile = kwargs.get("profile") or (lambda: "default")
        self.session_origin = kwargs.get("session_origin") or (lambda schedule: None)
        if "write_guard" in kwargs:
            self.write_guard = kwargs["write_guard"]
        else:
            self.write_guard = lambda path: None
        self.known_platform = kwargs.get("known_platform") or (lambda name: False)


def _open_gates():
    return {
        "cron": False,
        "yolo": False,
        "approvals": "manual",
        "single_query": False,
        "unattended": False,
        "approval_loaded": True,
    }


def live_runtime() -> Runtime:
    from agent.secret_scope import get_secret

    env = {k: get_secret(k) for k in ("MEILI_HOST", "MEILI_API_KEY")}
    status, data_dir_fn = _load("plugins.plugin_storage", "plugin_data_dir")
    if status != "ok":
        raise RuntimeError("data dir unavailable")
    try:
        data_dir = data_dir_fn(PLUGIN_NAME)
    except Exception as exc:
        raise RuntimeError("data dir unavailable") from exc

    def cron():
        status, fn = _load("tools.approval_context", "_is_cron_approval_context")
        if status != "ok":
            return "unknown"
        try:
            return "cron" if fn() else "not_cron"
        except Exception:
            return "unknown"

    def host():
        if "HERMES_PLUGIN_HOST_PROCESS" not in os.environ:
            return None
        return os.environ.get("HERMES_PLUGIN_HOST_PROCESS")

    def guard(path: str):
        status, fn = _load("agent.file_safety", "get_write_denied_error")
        if status != "ok":
            raise RuntimeError("write guard missing")
        return fn(path)

    def gates():
        out = {}
        status, fn = _load("tools.approval_context", "_is_cron_approval_context")
        if status != "ok":
            return {"unreadable": True}
        try:
            out["cron"] = bool(fn())
        except Exception:
            return {"unreadable": True}
        status, fn = _load("tools.approval", "_yolo_active")
        if status != "ok":
            return {"unreadable": True}
        try:
            out["yolo"] = bool(fn())
        except Exception:
            return {"unreadable": True}
        status, fn = _load("tools.approval_context", "_get_approval_mode")
        if status != "ok":
            return {"unreadable": True}
        try:
            out["approvals"] = fn()
        except Exception:
            return {"unreadable": True}
        status, fn = _load("tools.approval_context", "_is_single_query_approval_context")
        if status != "ok":
            return {"unreadable": True}
        try:
            out["single_query"] = bool(fn())
        except Exception:
            return {"unreadable": True}
        status, fn = _load("tools.approval_context", "_is_unattended_platform_approval_context")
        if status != "ok":
            return {"unreadable": True}
        try:
            out["unattended"] = bool(fn())
        except Exception:
            return {"unreadable": True}
        status, _fn = _load("tools.approval", "request_tool_approval")
        out["approval_loaded"] = status == "ok"
        if status != "ok":
            return {"unreadable": True}
        return out

    def request_approval(question: str):
        _status, fn = _load("tools.approval", "request_tool_approval")
        return fn("index_watch", question, rule_key=make_rule_key())

    def create_job(**kwargs):
        status, fn = _load("cron.jobs", "create_job")
        if status != "ok":
            raise RuntimeError("create_job unavailable")
        call = {
            "prompt": kwargs["prompt"],
            "schedule": kwargs["schedule"],
            "name": kwargs["name"],
            "deliver": kwargs["deliver"],
            "enabled_toolsets": kwargs["enabled_toolsets"],
            "no_agent": False,
        }
        # Only the shared origin argument. Main-only kwargs are not passed.
        if kwargs.get("origin") is not None:
            call["origin"] = kwargs["origin"]
        return fn(**call)

    def session_origin(schedule):
        status, fn = _load("tools.cronjob_job_args", "_origin_from_env")
        if status != "ok":
            raise RuntimeError("origin helper missing")
        found = fn(schedule)
        if found is None:
            return None
        if not isinstance(found, dict):
            raise RuntimeError("origin helper returned a non-dict")
        return found

    def remove_job(job_id: str):
        status, fn = _load("cron.jobs", "remove_job")
        if status != "ok":
            raise RuntimeError("remove_job unavailable")
        return fn(job_id)

    def get_job(job_id: str):
        status, fn = _load("cron.jobs", "get_job")
        if status != "ok":
            raise RuntimeError("get_job unavailable")
        return fn(job_id)

    def profile():
        status, fn = _load("hermes_cli.profiles", "get_active_profile_name")
        if status != "ok":
            return None
        try:
            name = fn()
        except Exception:
            return None
        if not isinstance(name, str) or not name.strip():
            return None
        return name

    def known_platform(name: str):
        status, fn = _load("cron.scheduler_delivery", "_is_known_delivery_platform")
        if status != "ok":
            return None
        try:
            return bool(fn(name))
        except Exception:
            return None

    guard_fn = guard
    # A missing guard must not look like an allowed write. Probe the import once.
    status, _fn = _load("agent.file_safety", "get_write_denied_error")
    if status != "ok":
        guard_fn = None

    return Runtime(
        env=env,
        data_dir=data_dir,
        cron=cron,
        host=host,
        gates=gates,
        request_approval=request_approval,
        create_job=create_job,
        remove_job=remove_job,
        get_job=get_job,
        profile=profile,
        known_platform=known_platform,
        write_guard=guard_fn,
        session_origin=session_origin,
    )


def _json_escaped(secret: str) -> str:
    encoded = json.dumps(secret, ensure_ascii=False)
    if len(encoded) >= 2 and encoded[0] == '"' and encoded[-1] == '"':
        return encoded[1:-1]
    return ""


def _scrub_text(text: str, secret: str) -> str:
    if not secret or not isinstance(text, str) or not text:
        return text
    needles = [secret]
    escaped = _json_escaped(secret)
    if escaped and escaped != secret:
        needles.append(escaped)
    for needle in sorted(set(needles), key=len, reverse=True):
        text = text.replace(needle, "[redacted]")
    return text


def _scrub_value(value, secret: str):
    if isinstance(value, str):
        return _scrub_text(value, secret)
    if isinstance(value, list):
        return [_scrub_value(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_value(item, secret) for key, item in value.items()}
    return value


def reply(message: str, *, ok: bool, notify: bool, secret: str = "", retryable: bool | None = None, **extra) -> str:
    body = {"ok": ok, "notify": bool(notify), "message": message}
    if retryable is not None:
        body["retryable"] = retryable
    for key, value in extra.items():
        body[key] = value
    body = _scrub_value(body, secret)
    text = json.dumps(body, ensure_ascii=False)
    return _scrub_text(text, secret)


def _form(runtime: Runtime) -> str:
    try:
        host = runtime.host()
    except Exception:
        return "cannot"
    if host not in (None, "", "0"):
        return "cannot"
    try:
        mark = runtime.cron()
    except Exception:
        return "cannot"
    if mark == "cron":
        return "cron"
    if mark == "not_cron":
        return "manual"
    return "cannot"


def _origin(value: str | None) -> tuple[str | None, str | None]:
    if value is None or value == "":
        return None, MSG_HOST_REQUIRED
    if any(ord(ch) < 32 or ord(ch) == 127 or ch in " \\" for ch in value):
        return None, MSG_HOST_BAD
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None, MSG_HOST_BAD
    if parts.username or parts.password or parts.query or parts.fragment:
        return None, MSG_HOST_BAD
    if parts.path not in {"", "/"}:
        return None, MSG_HOST_BAD
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    origin = f"{parts.scheme}://{host}"
    if parts.port:
        origin += f":{parts.port}"
    return origin, None


def _uid_ok(value) -> bool:
    return isinstance(value, str) and UID_RE.fullmatch(value) is not None


def _field_ok(value) -> bool:
    if not isinstance(value, str) or value == "" or len(value) > 64:
        return False
    if "." in value or "/" in value:
        return False
    return not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _query_ok(value) -> bool:
    return isinstance(value, str) and value.strip() != "" and len(value) <= 400


def _usable_id(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return ("i", value)
    if isinstance(value, str) and value != "":
        return ("s", value)
    return None


def _usable_title(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float)):
        text = str(value)
    else:
        return None
    if text == "":
        return None
    return text[:200]


def _fmt_id(item) -> str:
    if item[0] == "s":
        return json.dumps(item[1], ensure_ascii=False)
    return str(item[1])


def _as_dict(args) -> dict | None:
    if args is None:
        return {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None
    if not isinstance(args, dict):
        return None
    return args


def _secret(runtime: Runtime) -> str:
    return str(runtime.env.get("MEILI_API_KEY") or "")


def _search_url(origin: str, uid: str) -> str:
    return origin.rstrip("/") + "/indexes/" + quote(uid, safe="") + "/search"


def _health_url(origin: str) -> str:
    return origin.rstrip("/") + "/health"


def _safe_code(value) -> str:
    if isinstance(value, str) and SAFE_CODE.fullmatch(value):
        return value
    return ""


def _fail_message(status: int, code: str) -> str:
    if code:
        return f"The search failed (HTTP {status}, {code}). Seen ids were not advanced."
    return f"The search failed (HTTP {status}). Seen ids were not advanced."


def _parse_page(status: int, raw: bytes):
    if status in {301, 302, 303, 307, 308}:
        return ("redirect", MSG_REDIRECT, "redirect")
    if status >= 500 or status < 200:
        return ("incomplete", MSG_INCOMPLETE, f"http_{status}")
    if status != 200:
        code = ""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeError, json.JSONDecodeError):
            body = None
        if isinstance(body, dict):
            code = _safe_code(body.get("code"))
        return ("http", _fail_message(status, code), f"http_{status}_{code}" if code else f"http_{status}")
    if not raw or not raw.strip():
        return ("incomplete", MSG_INCOMPLETE, "incomplete")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return ("bad", MSG_BAD_PAGE, "bad_page")
    if not isinstance(body, dict) or "hits" not in body:
        return ("bad", MSG_BAD_PAGE, "bad_page")
    hits = body.get("hits")
    total = body.get("estimatedTotalHits")
    if not isinstance(hits, list) or isinstance(total, bool) or not isinstance(total, int):
        return ("bad", MSG_BAD_PAGE, "bad_page")
    if len(hits) > 20:
        return ("bad", MSG_TOO_MANY, "too_many")
    if total == 0 and hits:
        return ("bad", MSG_BAD_PAGE, "bad_page")
    if total < len(hits):
        return ("bad", MSG_BAD_PAGE, "bad_page")
    if not hits and total != 0:
        return ("bad", MSG_BAD_PAGE, "bad_page")
    if total == 0 and not hits:
        return ("zero", MSG_ZERO, "zero")
    parsed = []
    for hit in hits:
        if not isinstance(hit, dict):
            return ("bad", MSG_NO_ID, "no_id")
        parsed.append(hit)
    return ("hits", parsed, total)


def _ids_from_hits(hits: list, id_field: str, title_field: str):
    found = []
    seen = set()
    for hit in hits:
        item = _usable_id(hit.get(id_field)) if id_field in hit else None
        if item is None:
            return None, MSG_NO_ID, "no_id"
        if item in seen:
            return None, MSG_DUP, "duplicate"
        seen.add(item)
        title = _usable_title(hit.get(title_field)) if title_field and title_field in hit else None
        public = {"id": item[1]}
        if title is not None:
            public["title"] = title
        found.append((item, public))
    return found, "", ""


def _exchange(runtime: Runtime, method: str, url: str, headers: dict, body: bytes | None):
    return runtime.exchange(method, url, headers, body, client.TIMEOUT_SECONDS)


def _call_search(runtime: Runtime, origin: str, uid: str, query: str, secret: str):
    url = _search_url(origin, uid)
    if secret and secret in url:
        return ("key", MSG_KEY_IN_URL, "key_in_url")
    headers = {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = json.dumps({"q": query, "limit": 20}, ensure_ascii=False).encode("utf-8")
    try:
        status, _hdrs, raw = _exchange(runtime, "POST", url, headers, payload)
    except client.Redirected:
        return ("redirect", MSG_REDIRECT, "redirect")
    except client.Incomplete:
        return ("incomplete", MSG_INCOMPLETE, "incomplete")
    return _parse_page(status, raw)


def _cadence(state: dict, cause: str, now: float) -> bool:
    prev = state.get("fail")
    if not isinstance(prev, dict) or prev.get("cause") != cause:
        notify = True
    else:
        try:
            notify = (now - float(prev.get("at"))) >= (DAY - CADENCE_EARLY)
        except (TypeError, ValueError):
            notify = True
    if notify:
        state["fail"] = {"cause": cause, "at": now}
    return notify


def _over_cadence(state: dict, over: bool, now: float) -> bool:
    """Tell the user once that the page is full, then once a day while it stays full.

    A baseline is otherwise quiet, so without this the warning never leaves cron.
    """
    prev = state.get("over")
    if not over:
        state["over"] = None
        return False
    if not isinstance(prev, dict):
        state["over"] = {"at": now}
        return True
    try:
        due = (now - float(prev.get("at"))) >= (DAY - CADENCE_EARLY)
    except (TypeError, ValueError):
        due = True
    if due:
        state["over"] = {"at": now}
    return due


def _finish_watch(runtime: Runtime, state: dict, message: str, *, ok: bool, notify: bool, secret: str, **extra) -> str:
    # A manual check can read the page and must not write seen ids or the failure clock.
    if _form(runtime) != "cron":
        if "did not write seen ids" not in message and "were not advanced" not in message and "was not saved" not in message:
            message = message + MSG_MANUAL_SEEN
        return reply(message, ok=ok, notify=False, secret=secret, **extra)
    written = store.save(runtime.data_dir, state, runtime.write_guard)
    if written != "ok":
        return reply(MSG_PAGE_NOT_SAVED, ok=False, notify=True, secret=secret, retryable=False)
    return reply(message, ok=ok, notify=notify, secret=secret, **extra)


def _watch_state(runtime: Runtime):
    kind, data = store.load(runtime.data_dir)
    if kind == "corrupt":
        return "corrupt", None
    if kind == "missing" or not data:
        return "missing", None
    state = store.normalize(data)
    if not state.get("scheduled"):
        return "idle", state
    if not (state.get("index") and state.get("q") and state.get("id_field")):
        return "idle", state
    return "ready", state


def index_health(runtime: Runtime, args) -> str:
    data = _as_dict(args)
    if data is None or data:
        return reply(MSG_HEALTH_ARGS, ok=False, notify=False, retryable=False)
    origin, err = _origin(runtime.env.get("MEILI_HOST"))
    if err:
        return reply(err, ok=False, notify=False, retryable=False)
    secret = _secret(runtime)
    url = _health_url(origin)
    try:
        status, _hdrs, raw = _exchange(runtime, "GET", url, {"Accept": "application/json"}, None)
    except client.Redirected:
        return reply(MSG_REDIRECT, ok=False, notify=False, secret=secret, healthy=False)
    except client.Incomplete:
        return reply("The health check did not complete.", ok=False, notify=False, secret=secret, healthy=False)
    healthy = False
    if status == 200 and raw:
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            body = None
        healthy = isinstance(body, dict) and body.get("status") == "available"
    if healthy:
        return reply("The server status is available.", ok=True, notify=False, secret=secret, healthy=True)
    return reply("The server is not healthy.", ok=False, notify=False, secret=secret, healthy=False)


def index_search(runtime: Runtime, args) -> str:
    data = _as_dict(args)
    secret = _secret(runtime)
    if data is None:
        return reply(MSG_SEARCH_ARGS, ok=False, notify=False, secret=secret, retryable=False)
    extra = set(data) - {"index", "q", "id_field", "title_field"}
    if extra:
        return reply(MSG_SEARCH_ARGS, ok=False, notify=False, secret=secret, retryable=False)
    if not _uid_ok(data.get("index")):
        return reply(MSG_UID, ok=False, notify=False, secret=secret, retryable=False)
    if not _query_ok(data.get("q")):
        return reply(MSG_QUERY, ok=False, notify=False, secret=secret, retryable=False)
    if not _field_ok(data.get("id_field")):
        return reply(
            "The id field must be a non-empty string of 64 characters or fewer, without a dot, a slash, or a control character.",
            ok=False, notify=False, secret=secret, retryable=False,
        )
    title_field = ""
    if "title_field" in data and data.get("title_field") not in (None, ""):
        if not _field_ok(data.get("title_field")):
            return reply(
                "The title field must be a non-empty string of 64 characters or fewer, without a dot, a slash, or a control character.",
                ok=False, notify=False, secret=secret, retryable=False,
            )
        title_field = data["title_field"]
    origin, err = _origin(runtime.env.get("MEILI_HOST"))
    if err:
        return reply(err, ok=False, notify=False, secret=secret, retryable=False)
    if not secret:
        return reply(MSG_KEY_REQUIRED, ok=False, notify=False, secret="", retryable=False)
    page = _call_search(runtime, origin, data["index"], data["q"], secret)
    return _render_search(page, data["id_field"], title_field, secret, watch=False, state=None, runtime=runtime)


def _total_suffix(total: int) -> str:
    text = f" estimatedTotalHits is {total}."
    if total > 20:
        text += " Matches past the first 20 are never reported."
    return text


def _render_search(page, id_field: str, title_field: str, secret: str, *, watch: bool, state, runtime: Runtime) -> str:
    kind = page[0]
    if kind == "zero":
        if not watch:
            return reply("This page has 0 hits." + _total_suffix(0), ok=True, notify=False, secret=secret, hits=[])
        return _watch_zero(runtime, state, secret)
    if kind == "hits":
        found, message, cause = _ids_from_hits(page[1], id_field, title_field)
        total = page[2]
        if found is None:
            if not watch:
                return reply(message, ok=False, notify=False, secret=secret, retryable=False)
            notify = _cadence(state, cause, float(runtime.now()))
            return _finish_watch(runtime, state, message, ok=False, notify=notify, secret=secret, retryable=False)
        if not watch:
            parts = []
            for item, public in found:
                bit = _fmt_id(item)
                if "title" in public:
                    bit += f" ({public['title']})"
                parts.append(bit)
            message = ("Hits: " + ", ".join(parts) + ".") if parts else "This page has 0 hits."
            message += _total_suffix(total)
            return reply(message, ok=True, notify=False, secret=secret, hits=[public for _item, public in found])
        return _watch_hits(runtime, state, found, secret, total)
    message, cause = page[1], page[2]
    if not watch:
        return reply(message, ok=False, notify=False, secret=secret, retryable=False)
    notify = _cadence(state, cause, float(runtime.now()))
    return _finish_watch(runtime, state, message, ok=False, notify=notify, secret=secret, retryable=False)


def _new_message(found) -> str:
    parts = []
    for item, public in found:
        bit = _fmt_id(item)
        if "title" in public:
            bit += f" ({public['title']})"
        parts.append(bit)
    return "New ids: " + ", ".join(parts) + "."


def _watch_zero(runtime: Runtime, state: dict, secret: str) -> str:
    was_empty = bool(state.get("last_empty"))
    recovering = state.get("fail") is not None
    _over_cadence(state, False, float(runtime.now()))
    if state.get("baseline_pending") and _form(runtime) != "cron":
        return reply(MSG_NOT_CRON_BASELINE, ok=False, notify=False, secret=secret, advanced=False)
    if state.get("baseline_pending"):
        state["baseline_pending"] = False
        state["seen"] = []
        state["last_empty"] = True
        state["fail"] = None
        state["zero_told"] = False
        notify = recovering
        return _finish_watch(
            runtime, state, MSG_BASELINE + _total_suffix(0), ok=True, notify=notify, secret=secret,
            advanced=_form(runtime) == "cron",
        )
    state["fail"] = None
    state["last_empty"] = True
    if recovering:
        notify = True
        state["zero_told"] = True
    elif was_empty:
        notify = False
        state["zero_told"] = True
    else:
        notify = True
        state["zero_told"] = True
    return _finish_watch(runtime, state, MSG_ZERO + _total_suffix(0), ok=True, notify=notify, secret=secret, advanced=False)


def _trim(seen: list) -> list:
    if len(seen) <= SEEN_CAP:
        return seen
    return seen[-SEEN_CAP:]


def _watch_hits(runtime: Runtime, state: dict, found, secret: str, total: int) -> str:
    recovering = state.get("fail") is not None
    over_notify = _over_cadence(state, total > 20, float(runtime.now()))
    state["fail"] = None
    state["last_empty"] = False
    state["zero_told"] = False
    items = [item for item, _public in found]
    if state.get("baseline_pending") and _form(runtime) != "cron":
        return reply(MSG_NOT_CRON_BASELINE, ok=False, notify=False, secret=secret, advanced=False)
    if state.get("baseline_pending"):
        state["baseline_pending"] = False
        state["seen"] = _trim([{"t": item[0], "v": item[1]} for item in items])
        notify = bool(recovering) or over_notify
        return _finish_watch(
            runtime, state, MSG_BASELINE + _total_suffix(total), ok=True, notify=notify, secret=secret,
            advanced=_form(runtime) == "cron", hits=[public for _item, public in found],
        )
    known = {(item["t"], item["v"]) for item in state.get("seen") or []}
    fresh = [(item, public) for item, public in found if item not in known]
    gap = _gap_note(state, found, total)
    state["seen"] = _trim(list(state.get("seen") or []) + [{"t": item[0], "v": item[1]} for item, _public in fresh])
    if fresh:
        message = _new_message(fresh) + _total_suffix(total) + gap
        if recovering:
            message = "The search is working again. " + message
        return _finish_watch(
            runtime, state, message, ok=True, notify=True, secret=secret,
            advanced=_form(runtime) == "cron", hits=[public for _item, public in fresh],
        )
    message = "No unseen id is on this page. Seen ids were kept." + _total_suffix(total)
    if recovering:
        message = "The search is working again. " + message
    return _finish_watch(
        runtime, state, message, ok=True, notify=bool(recovering) or over_notify, secret=secret, advanced=False,
    )


def _gap_note(state: dict, found, total: int) -> str:
    """A full page that shares no saved id is not proof those ids are the only new ones."""
    if total <= 20 or state.get("baseline_pending"):
        return ""
    seen = {(item.get("t"), item.get("v")) for item in state.get("seen") or [] if isinstance(item, dict)}
    if not seen:
        return ""
    page_ids = {item for item, _public in found}
    if seen.isdisjoint(page_ids):
        return MSG_GAP
    return ""


def _saved_query_ok(state: dict) -> str | None:
    if not _uid_ok(state.get("index")):
        return MSG_UID
    if not _query_ok(state.get("q")):
        return MSG_QUERY
    if not _field_ok(state.get("id_field")):
        return "The id field must be a non-empty string of 64 characters or fewer, without a dot, a slash, or a control character."
    if state.get("title_field") and not _field_ok(state.get("title_field")):
        return "The title field must be a non-empty string of 64 characters or fewer, without a dot, a slash, or a control character."
    return None


def _cannot_monitor(runtime: Runtime, secret: str) -> str:
    """This form cannot tell a cron run from a hand-typed check, so it writes nothing.

    A manual check must not move the failure clock. If it did, running one by hand
    would silence the first cron report to the delivery target for 24 hours, and the
    delivery target would read that silence as "nothing new". The clock moves only
    inside a proven cron run, so this sentence is not thinned out and repeats on
    every check until the form is fixed.
    """
    return reply(MSG_CANNOT, ok=False, notify=True, secret=secret, retryable=False)


def _mark_job_gone(runtime: Runtime, state: dict, secret: str, message: str, *, notify: bool, ok: bool, retryable: bool | None = None) -> str:
    """Drop a job id that Hermes no longer has. Later calls still say it is gone."""
    state = dict(state)
    state["scheduled"] = False
    state["job_id"] = ""
    state["job_gone"] = True
    written = store.save(runtime.data_dir, state, runtime.write_guard)
    if written != "ok":
        return reply(
            message + " The watch file still names that job.",
            ok=False, notify=True, secret=secret, retryable=False,
        )
    return reply(message, ok=ok, notify=notify, secret=secret, retryable=retryable, scheduled=False)


def _job_presence(runtime: Runtime, job_id: str) -> str:
    if not job_id:
        return "skip"
    try:
        found = runtime.get_job(job_id)
    except Exception:
        return "unreadable"
    if found:
        return "present"
    return "gone"


def index_watch_check(runtime: Runtime) -> str:
    secret = _secret(runtime)
    if _form(runtime) == "cannot":
        return _cannot_monitor(runtime, secret)
    kind, state = _watch_state(runtime)
    if kind == "corrupt":
        return reply(
            "The saved watch file is unreadable, so seen ids were not advanced.",
            ok=False, notify=True, secret=secret, retryable=False,
        )
    if kind != "ready":
        if state and state.get("job_gone"):
            return reply(MSG_JOB_GONE, ok=False, notify=True, secret=secret, retryable=False)
        return reply("No watch is scheduled, so seen ids were not advanced.", ok=False, notify=False, secret=secret)
    presence = _job_presence(runtime, state.get("job_id") or "")
    if presence == "gone":
        return _mark_job_gone(runtime, state, secret, MSG_JOB_GONE, notify=True, ok=False, retryable=False)
    if presence == "unreadable":
        return reply(MSG_JOB_UNKNOWN, ok=False, notify=True, secret=secret, retryable=False)
    bad = _saved_query_ok(state)
    if bad:
        return reply(bad, ok=False, notify=True, secret=secret, retryable=False)
    origin, err = _origin(runtime.env.get("MEILI_HOST"))
    if err:
        notify = _cadence(state, "host", float(runtime.now()))
        return _finish_watch(runtime, state, err, ok=False, notify=notify, secret=secret, retryable=False)
    if not secret:
        notify = _cadence(state, "key", float(runtime.now()))
        return _finish_watch(runtime, state, MSG_KEY_REQUIRED, ok=False, notify=notify, secret="", retryable=False)
    page = _call_search(runtime, origin, state["index"], state["q"], secret)
    return _render_search(page, state["id_field"], state.get("title_field") or "", secret, watch=True, state=state, runtime=runtime)


def _rewrite_risk(value: str) -> bool:
    if any(ch in REWRITE_CHARS or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return True
    return bool(EMPHASIS_RE.search(value))


def rewrite_message(field: str, action: str) -> str:
    return (
        f"The {REWRITE_LABELS[field]} cannot be shown unchanged in the approval text, "
        f"so {action} was not asked. Seen ids were not changed."
    )


def _approval_question(kind: str, index: str, query: str, id_field: str, deliver: str, job_id: str = "") -> str:
    if kind == "unschedule":
        return (
            f"Approve unschedule of an index watch. "
            f"Index: {index}. Query: {query}. Id field: {id_field}. Delivery: {deliver}. "
            f"This removes cron job {job_id}. Seen ids are kept."
        )
    return (
        "Approve schedule of an index watch. "
        f"Index: {index}. Query: {query}. Id field: {id_field}. Delivery: {deliver}. "
        "This creates a cron job every 15 minutes that calls the model."
    )


def _fits(text: str) -> bool:
    if len(text) > REASON_BUDGET:
        return False
    amp = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if len(amp) > REASON_BUDGET:
        return False
    return len(html.escape(text, quote=True)) <= ESCAPED_REASON_BUDGET


def _schedule_block(runtime: Runtime, action: str) -> str | None:
    if _form(runtime) == "cannot":
        return MSG_CANNOT
    try:
        gates = runtime.gates()
    except Exception:
        return MSG_CANNOT
    if not isinstance(gates, dict) or gates.get("unreadable"):
        return MSG_CANNOT
    if gates.get("cron") or _form(runtime) == "cron":
        return "Schedule and unschedule are refused in a cron session. Seen ids were not changed."
    if gates.get("yolo"):
        return f"Hermes yolo is on, so {action} was not asked. Seen ids were not changed."
    if gates.get("approvals") == "off":
        return f"Hermes approvals are off, so {action} was not asked. Seen ids were not changed."
    if gates.get("single_query"):
        return f"A single-query session cannot {action} a watch. Seen ids were not changed."
    if gates.get("unattended"):
        return f"An unattended session cannot {action} a watch. Seen ids were not changed."
    if not gates.get("approval_loaded", True):
        return MSG_CANNOT
    return None


def _deliver_error(runtime: Runtime, value) -> str | None:
    if not isinstance(value, str) or value.strip() == "":
        return "Delivery is empty, so it was not treated as local."
    raw = value.strip()
    low = raw.lower()
    if low in REFUSED_DELIVER or low.startswith("bot-chat:"):
        return "That delivery target is refused."
    if any(ch in raw for ch in "*@"):
        return "Delivery must be a target, not a schedule."
    if re.fullmatch(r"\d+", raw) or ISO_RE.match(raw):
        return "Delivery must be a target, not a schedule."
    tokens = re.split(r"[\s,]+", low)
    if any(token in SCHEDULE_TOKENS or re.fullmatch(r"\d+m", token) for token in tokens):
        return "Delivery must be a target, not a schedule."
    if "," in raw:
        return "Delivery must be one target."
    if low in PLAIN_DELIVER:
        return None
    if ":" in raw:
        platform, chat = raw.split(":", 1)
        if not platform or not chat or platform.lower() in REFUSED_DELIVER:
            return "That delivery target is refused."
        known = runtime.known_platform(platform)
        if known is True:
            return None
        if known is None:
            return "Delivery could not be checked, so no job was created."
        return "That delivery platform is not known to this Hermes."
    known = runtime.known_platform(raw)
    if known is True:
        return None
    if known is None:
        return "Delivery could not be checked, so no job was created."
    return "That delivery platform is not known to this Hermes."


def _canonical_deliver(value: str) -> str:
    raw = value.strip()
    if raw.lower() in PLAIN_DELIVER:
        return raw.lower()
    return raw


def _origin_plan(runtime: Runtime, deliver: str) -> tuple[dict | None, str, str | None]:
    """Origin dict for create_job, a note for the success text, or an error.

    origin, local, and all are already lowercase. A platform name is not.
    """
    if deliver != "origin":
        return None, "", None
    try:
        found = runtime.session_origin(SCHEDULE_EXPR)
    except Exception:
        return None, "", MSG_ORIGIN_UNREADABLE
    platform = found.get("platform") if isinstance(found, dict) else None
    chat_id = found.get("chat_id") if isinstance(found, dict) else None
    if isinstance(platform, str) and platform and isinstance(chat_id, str) and chat_id:
        return found, f" Delivery returns to this session on {platform}.", None
    return None, (
        " Delivery origin was not captured. The job uses the home channel if one is set, "
        "and nowhere if it is not. It does not return to this session."
    ), None


def make_rule_key() -> str:
    return f"index_watch:{uuid.uuid4().hex}"


def _profile_name(runtime: Runtime) -> str | None:
    try:
        name = runtime.profile()
    except Exception:
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    return name


def _lines(profile: str | None, job_id: str) -> str:
    """Commands that still work after the plugin is removed. An empty job id drops the
    remove line, because unschedule already removed that job."""
    if profile is None:
        return "profile unknown"
    prefix = "hermes" if profile == "default" else f"hermes -p {profile}"
    rows = [
        f"{prefix} cron list",
        f"{prefix} cron status",
    ]
    if job_id:
        rows.append(f"{prefix} cron remove {job_id}")
    return "\n".join(rows)


def _ask(runtime: Runtime, action: str, question: str, shown) -> str | None:
    for field, value in shown:
        if _rewrite_risk(value if isinstance(value, str) else ""):
            return reply(rewrite_message(field, action), ok=False, notify=True, retryable=False)
    if not _fits(question):
        return reply(MSG_APPROVAL_FIT, ok=False, notify=True, retryable=False)
    try:
        result = runtime.request_approval(question)
    except Exception:
        return reply(MSG_CANNOT, ok=False, notify=True, retryable=False)
    if not isinstance(result, dict) or result.get("approved") is not True:
        return reply("The watch change was not approved. Seen ids were not changed.", ok=False, notify=True, retryable=False)
    return None


def _schedule(runtime: Runtime, data: dict) -> str:
    secret = _secret(runtime)
    allowed = {"action", "index", "q", "id_field", "title_field", "deliver"}
    if set(data) - allowed:
        return reply(
            "index_watch schedule takes only index, q, id_field, deliver, and an optional title_field.",
            ok=False, notify=True, secret=secret, retryable=False,
        )
    if not _uid_ok(data.get("index")):
        return reply(MSG_UID, ok=False, notify=True, secret=secret, retryable=False)
    if not _query_ok(data.get("q")):
        return reply(MSG_QUERY, ok=False, notify=True, secret=secret, retryable=False)
    if not _field_ok(data.get("id_field")):
        return reply(
            "The id field must be a non-empty string of 64 characters or fewer, without a dot, a slash, or a control character.",
            ok=False, notify=True, secret=secret, retryable=False,
        )
    # An omitted title_field keeps the saved one. An empty string clears it.
    title_given = "title_field" in data and data.get("title_field") is not None
    title_field = ""
    if title_given and data.get("title_field") != "":
        if not _field_ok(data.get("title_field")):
            return reply(
                "The title field must be a non-empty string of 64 characters or fewer, without a dot, a slash, or a control character.",
                ok=False, notify=True, secret=secret, retryable=False,
            )
        title_field = data["title_field"]
    deliver_error = _deliver_error(runtime, data.get("deliver"))
    if deliver_error:
        return reply(deliver_error, ok=False, notify=True, secret=secret, retryable=False)
    deliver = _canonical_deliver(data["deliver"])
    blocked = _schedule_block(runtime, "schedule")
    if blocked:
        return reply(blocked, ok=False, notify=True, secret=secret, retryable=False)
    origin_for_job, origin_note, origin_error = _origin_plan(runtime, deliver)
    if origin_error:
        return reply(origin_error, ok=False, notify=True, secret=secret, retryable=False)
    question = _approval_question("schedule", data["index"], data["q"], data["id_field"], deliver)
    denied = _ask(runtime, "schedule", question, [
        ("index", data["index"]), ("q", data["q"]), ("id_field", data["id_field"]), ("deliver", deliver),
    ])
    if denied:
        return denied
    kind, current = _watch_state(runtime)
    if kind == "corrupt":
        return reply(
            "The saved watch file is unreadable, so it was not overwritten.",
            ok=False, notify=True, secret=secret, retryable=False,
        )
    previous = store.normalize(current) if current else None
    job_kwargs = {
        "prompt": JOB_PROMPT,
        "schedule": SCHEDULE_EXPR,
        "name": "index-search",
        "deliver": deliver,
        "enabled_toolsets": [TOOLSET],
        "no_agent": False,
    }
    if origin_for_job is not None:
        job_kwargs["origin"] = origin_for_job
    try:
        created = runtime.create_job(**job_kwargs)
    except Exception:
        return reply(
            "The cron job was not created. Seen ids were not changed.",
            ok=False, notify=True, secret=secret, retryable=False,
        )
    if not isinstance(created, dict) or not created.get("id"):
        return reply(
            "The cron job was not created. Seen ids were not changed.",
            ok=False, notify=True, secret=secret, retryable=False,
        )
    new_id = str(created["id"])
    if previous:
        seen = list(previous.get("seen") or [])
        same_identity = (previous.get("index"), previous.get("q"), previous.get("id_field")) == (
            data["index"], data["q"], data["id_field"],
        )
        if not title_given and same_identity:
            title_field = previous.get("title_field") or ""
        baseline_pending = False if same_identity else True
        if not same_identity:
            baseline_pending = True
        last_empty = bool(previous.get("last_empty")) if same_identity else False
        fail = previous.get("fail") if same_identity else None
        over = previous.get("over") if same_identity else None
        zero_told = bool(previous.get("zero_told")) if same_identity else False
        if previous.get("baseline_pending"):
            baseline_pending = True
    else:
        seen = []
        baseline_pending = True
        last_empty = False
        fail = None
        over = None
        zero_told = False
        same_identity = False
    state = {
        "index": data["index"],
        "q": data["q"],
        "id_field": data["id_field"],
        "title_field": title_field,
        "deliver": deliver,
        "job_id": new_id,
        "scheduled": True,
        "baseline_pending": baseline_pending,
        "seen": seen,
        "last_empty": last_empty,
        "fail": fail,
        "over": over,
        "zero_told": zero_told,
        "job_gone": False,
    }
    written = store.save(runtime.data_dir, state, runtime.write_guard)
    if written != "ok":
        try:
            removed = runtime.remove_job(new_id)
        except Exception:
            removed = None
        if removed is not True:
            return reply(
                f"The cron job {new_id} was created, but the watch file was not written. Seen ids were not changed.",
                ok=False, notify=True, secret=secret, retryable=False,
            )
        return reply(MSG_SCHEDULE_NOT_WRITTEN, ok=False, notify=True, secret=secret, retryable=False)
    old_id = (previous or {}).get("job_id") or ""
    removed_note = ""
    if old_id and old_id != new_id:
        try:
            removed_old = runtime.remove_job(old_id)
        except Exception:
            removed_old = None
        if removed_old is not True:
            removed_note = f" The previous job {old_id} was not confirmed removed."
    lines = _lines(_profile_name(runtime), new_id)
    previous_deliver = (previous or {}).get("deliver") or ""
    if previous and same_identity and previous_deliver and previous_deliver != deliver and (previous.get("title_field") or "") == title_field:
        lead = f"The previous delivery was {previous_deliver}. Seen ids were kept."
    elif previous and not same_identity:
        lead = "The next check saves a new baseline. This call did not search. Seen ids were kept."
    elif previous and (previous.get("title_field") or "") != title_field and same_identity and previous_deliver == deliver:
        lead = "The title field was updated. Seen ids were kept. This call did not search."
    else:
        lead = "Watch saved. This call did not search."
    return reply(lead + MSG_LIMIT + origin_note + removed_note + "\n" + lines, ok=True, notify=True, secret=secret, job_id=new_id)


def _unschedule(runtime: Runtime, data: dict) -> str:
    secret = _secret(runtime)
    if set(data) - {"action"}:
        return reply("index_watch unschedule takes no arguments.", ok=False, notify=True, secret=secret, retryable=False)
    kind, current = _watch_state(runtime)
    if kind == "corrupt":
        return reply(
            "The saved watch file is unreadable, so it was not overwritten.",
            ok=False, notify=True, secret=secret, retryable=False,
        )
    state = store.normalize(current) if current else None
    if state is None or not state.get("scheduled") or not (state.get("job_id") or ""):
        if state and state.get("job_gone"):
            return reply(MSG_JOB_GONE_KEPT, ok=False, notify=True, secret=secret, retryable=False)
        return reply(MSG_NOTHING_UNSCHEDULE, ok=True, notify=False, secret=secret)
    job_id = state.get("job_id") or ""
    # A cron session still refuses a live job before approval. A job that is already
    # gone is reported first, so the result does not claim a watch is scheduled.
    presence = _job_presence(runtime, job_id)
    if presence == "gone":
        return _mark_job_gone(runtime, state, secret, MSG_JOB_GONE_KEPT, notify=True, ok=False, retryable=False)
    if presence == "unreadable":
        return reply(MSG_JOB_UNKNOWN, ok=False, notify=True, secret=secret, retryable=False)
    if _form(runtime) == "cannot":
        lines = _lines(_profile_name(runtime), job_id)
        return reply(
            "This form cannot unschedule. The cron job is still there.\n"
            + lines
            + "\nSeen ids were not changed.",
            ok=False, notify=True, secret=secret, retryable=False,
        )
    blocked = _schedule_block(runtime, "unschedule")
    if blocked:
        return reply(blocked, ok=False, notify=True, secret=secret, retryable=False)
    index = state.get("index") or ""
    query = state.get("q") or ""
    id_field = state.get("id_field") or ""
    deliver = state.get("deliver") or "local"
    question = _approval_question("unschedule", index or "(none)", query or "(none)", id_field or "(none)", deliver, job_id)
    denied = _ask(runtime, "unschedule", question, [
        ("index", index), ("q", query), ("id_field", id_field), ("deliver", deliver), ("job_id", job_id),
    ])
    if denied:
        return denied
    try:
        removed = runtime.remove_job(job_id)
    except Exception:
        return reply(
            "The cron job was not removed. Seen ids were not changed.",
            ok=False, notify=True, secret=secret, retryable=False,
        )
    if removed is not True:
        return reply(
            "The cron job was not confirmed removed. Seen ids were kept.",
            ok=False, notify=True, secret=secret, retryable=False,
        )
    state["scheduled"] = False
    state["job_id"] = ""
    written = store.save(runtime.data_dir, state, runtime.write_guard)
    if written != "ok":
        return reply(
            MSG_UNSCHEDULE_NOT_WRITTEN.format(job_id=job_id),
            ok=False, notify=True, secret=secret, retryable=False,
        )
    lines = _lines(_profile_name(runtime), "")
    return reply(
        f"The cron job {job_id} was removed. Seen ids were kept.\n" + lines,
        ok=True, notify=True, secret=secret,
    )


def _status(runtime: Runtime, data: dict) -> str:
    secret = _secret(runtime)
    if set(data) - {"action"}:
        return reply("index_watch status takes no arguments.", ok=False, notify=False, secret=secret, retryable=False)
    kind, current = _watch_state(runtime)
    if kind == "corrupt":
        return reply(
            "The saved watch file is unreadable, so seen ids were not advanced.",
            ok=False, notify=True, secret=secret,
        )
    if kind != "ready":
        if current and current.get("job_gone"):
            return reply("The cron job is gone.", ok=True, notify=False, secret=secret, scheduled=False)
        return reply("No watch is scheduled.", ok=True, notify=False, secret=secret, scheduled=False)
    state = current
    presence = _job_presence(runtime, state.get("job_id") or "")
    if presence == "gone":
        return _mark_job_gone(runtime, state, secret, "The cron job is gone.", notify=False, ok=True)
    if presence == "unreadable":
        return reply(MSG_JOB_UNKNOWN, ok=False, notify=True, secret=secret, scheduled=False)
    return reply(
        "A watch is scheduled.",
        ok=True, notify=False, secret=secret, scheduled=True,
        index=state.get("index"), q=state.get("q"), id_field=state.get("id_field"),
        title_field=state.get("title_field") or "", deliver=state.get("deliver") or "",
        seen=len(state.get("seen") or []),
    )


def index_watch(runtime: Runtime, args) -> str:
    data = _as_dict(args)
    secret = _secret(runtime)
    if data is None:
        return reply(MSG_ACTION, ok=False, notify=True, secret=secret, retryable=False)
    # The cron probe calls this tool with no arguments. That is check only when this
    # process is a proven cron run, or when cron cannot be told from a manual call.
    if data == {}:
        if _form(runtime) in {"cron", "cannot"}:
            return index_watch_check(runtime)
        return reply(MSG_ACTION, ok=False, notify=True, secret=secret, retryable=False)
    if "action" not in data or data.get("action") in (None, ""):
        return reply(MSG_ACTION, ok=False, notify=True, secret=secret, retryable=False)
    action = data.get("action")
    if action == "check":
        if set(data) - {"action"}:
            return reply("index_watch check takes no arguments.", ok=False, notify=True, secret=secret, retryable=False)
        return index_watch_check(runtime)
    if action == "schedule":
        return _schedule(runtime, data)
    if action == "unschedule":
        return _unschedule(runtime, data)
    if action == "status":
        return _status(runtime, data)
    return reply(MSG_ACTION, ok=False, notify=True, secret=secret, retryable=False)
