"""Search, health, and one saved watch. Seen ids move only on a proven cron check."""

from __future__ import annotations

import importlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

if __package__:
    from . import client, store
else:
    import client
    import store

TOOLSET = "index_search"
PLUGIN_NAME = "index-search"
SEEN_CAP = store.SEEN_CAP
DAY = 86400
# A daily tick can land a few seconds early. The margin stays inside one 15-minute slot.
CADENCE_EARLY = 90
UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SAFE_CODE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

MSG_QUERY = "The query must be a non-empty string of 400 characters or fewer."
MSG_SEARCH_ARGS = (
    "index_search takes only index, q, id_field, and an optional title_field, "
    "for example index probes, q hermes, and id_field sku."
)
MSG_SEARCH_QUERY = MSG_QUERY + " Use a query such as hermes."
MSG_SEARCH_UID = (
    "The index uid must be 1 to 64 letters, digits, hyphens, or underscores, such as probes."
)
MSG_SEARCH_ID = (
    "The id field must be a non-empty string of 64 characters or fewer, "
    "without a dot, a slash, or a control character, such as sku."
)
MSG_SEARCH_TITLE = (
    "The title field must be a non-empty string of 64 characters or fewer, "
    "without a dot, a slash, or a control character, such as title."
)
MSG_UID = "The index uid must be 1 to 64 letters, digits, hyphens, or underscores."
MSG_HOST_REQUIRED = "MEILI_HOST is required."
MSG_HOST_BAD = "MEILI_HOST is not a usable origin."
MSG_KEY_REQUIRED = "MEILI_API_KEY is required for search."
MSG_REDIRECT = "The server redirected the request. It was not followed."
MSG_INCOMPLETE = "The search did not complete, and seen ids were not advanced."
MSG_PAGE_NOT_SAVED = "The page was read, but the watch file was not written. Seen ids were not advanced."
MSG_NO_ID = "A hit has no usable id field, so seen ids were not advanced."
MSG_DUP = "This page has duplicate ids, so seen ids were not advanced."
MSG_TOO_MANY = "The page has more than 20 hits, so seen ids were not advanced."
MSG_ZERO = "This page has 0 hits. Seen ids were kept."
MSG_BASELINE = "Baseline saved. These ids are not reported as new."
MSG_NOT_CRON_BASELINE = "This check is not a proven cron run, so the baseline was not saved. Seen ids were not advanced."
MSG_CANNOT = "This form cannot monitor, so seen ids were not advanced."
MSG_GAP = (
    " This page shares no saved id, so matches between the saved ids and this page may have been missed."
)
MSG_MANUAL_SEEN = " This call did not write seen ids."
MSG_HEALTH_ARGS = "index_health takes no arguments, so call it with an empty object."
MSG_BAD_PAGE = "The search returned an unusable page, so seen ids were not advanced."
MSG_ACTION = (
    "index_watch action must be save, status, or check. "
    "Call index_watch with action status to see the watch. Status does not ask for approval. " "日本語: 見張りを見るだけなら action を status にして index_watch をもう一度呼ぶ。"
)
MSG_KEY_IN_URL = "The API key is part of the request URL, so nothing was sent."
MSG_LIMIT = (
    " Only the first 20 hits are watched. If estimatedTotalHits is over 20, "
    "matches past that page are never reported."
)

JOB_PROMPT = (
    "Call the index_watch tool with action check and no other arguments. "
    "If notify is true, relay the tool message and nothing else. "
    "If notify is false, reply with exactly [SILENT]."
)


class Runtime:
    def __init__(self, **kwargs):
        self.env = kwargs.get("env") or {}
        self.data_dir = Path(kwargs["data_dir"])
        self.now = kwargs.get("now") or time.time
        self.cron = kwargs.get("cron") or (lambda: "not_cron")
        self.host = kwargs.get("host") or (lambda: None)
        self.exchange = kwargs.get("exchange") or client.perform
        self.gates = kwargs.get("gates") or _open_gates
        self.request_approval = kwargs.get("request_approval") or (lambda question: {"approved": False})
        self.public_approval = bool(kwargs.get("public_approval"))
        self.approval_payload = kwargs.get("approval_payload")
        self.approval_ids = kwargs.get("approval_ids") or ("", "")
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


def clean_profile(name: object) -> str | None:
    if not isinstance(name, str) or not _PROFILE_NAME.fullmatch(name.strip()):
        return None
    return name.strip() or None


def plugin_data_directory() -> Path:
    try:
        module = importlib.import_module("plugins.plugin_storage")
    except Exception as exc:
        raise RuntimeError("data dir unavailable") from exc
    fn = getattr(module, "plugin_data_dir", None)
    if not callable(fn):
        raise RuntimeError("data dir unavailable")
    try:
        path = fn(PLUGIN_NAME)
    except Exception as exc:
        raise RuntimeError("data dir unavailable") from exc
    return path if isinstance(path, Path) else Path(path)


def _cron_mark() -> str:
    try:
        module = importlib.import_module("cron.execution_identity")
        fn = getattr(module, "current_cron_execution", None)
        if not callable(fn):
            return "unknown"
        return "cron" if fn() is not None else "not_cron"
    except Exception:
        return "unknown"


def _guard_or_none():
    try:
        module = importlib.import_module("agent.file_safety")
        fn = getattr(module, "get_write_denied_error", None)
    except Exception:
        return None
    if not callable(fn):
        return None

    def guard(path: str):
        return fn(path)

    return guard


def live_runtime(profile: str | None = None) -> Runtime:
    from agent.secret_scope import get_secret

    env = {k: get_secret(k) for k in ("MEILI_HOST", "MEILI_API_KEY")}
    data_dir = plugin_data_directory()

    def host():
        if "HERMES_PLUGIN_HOST_PROCESS" not in os.environ:
            return None
        return os.environ.get("HERMES_PLUGIN_HOST_PROCESS")

    def gates():
        # yolo, approvals off, cron, single-query, and unattended are Hermes's gate.
        return {
            "cron": False,
            "yolo": False,
            "approvals": "manual",
            "single_query": False,
            "unattended": False,
            "approval_loaded": True,
        }

    return Runtime(
        env=env,
        data_dir=data_dir,
        cron=_cron_mark,
        host=host,
        gates=gates,
        request_approval=lambda question: {"approved": False},
        profile=lambda: profile,
        write_guard=_guard_or_none(),
        public_approval=False,
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


_FW_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def _fold_digits(value):
    if isinstance(value, str):
        return value.strip().translate(_FW_DIGITS)
    return value


_SEARCH_AGAIN = (
    "Call index_search once with index probes, q hermes, and id_field sku. " "日本語: 例の index と q と id_field で index_search をもう一度呼ぶ。"
)
_HOST_STEP = (
    "Set MEILI_HOST to an origin such as http://127.0.0.1:7700, then call index_health with no arguments. " "日本語: ホストを設定して、引数なしの index_health を呼ぶ。"
)


def _search_arg(secret: str, message: str) -> str:
    step = message if "id_field" in message else f"{message} {_SEARCH_AGAIN}"
    return reply(
        message, ok=False, notify=False, secret=secret, retryable=False,
        error_type="bad_args", next_step=step,
    )


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
        return reply(MSG_HEALTH_ARGS, ok=False, notify=False, retryable=False, error_type="bad_args")
    origin, err = _origin(runtime.env.get("MEILI_HOST"))
    if err:
        extra = {"next_step": _HOST_STEP} if err == MSG_HOST_REQUIRED else {}
        return reply(err, ok=False, notify=False, retryable=False, **extra)
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
        return _search_arg(secret, MSG_SEARCH_ARGS)
    extra = set(data) - {"index", "q", "id_field", "title_field"}
    if extra:
        return _search_arg(secret, MSG_SEARCH_ARGS)
    data = {key: _fold_digits(value) for key, value in data.items()}
    if not _uid_ok(data.get("index")):
        return _search_arg(secret, MSG_SEARCH_UID)
    if not _query_ok(data.get("q")):
        return _search_arg(secret, MSG_SEARCH_QUERY)
    if not _field_ok(data.get("id_field")):
        return _search_arg(secret, MSG_SEARCH_ID)
    title_field = ""
    if "title_field" in data and data.get("title_field") not in (None, ""):
        if not _field_ok(data.get("title_field")):
            return _search_arg(secret, MSG_SEARCH_TITLE)
        title_field = data["title_field"]
    origin, err = _origin(runtime.env.get("MEILI_HOST"))
    if err:
        extra = {"next_step": _HOST_STEP} if err == MSG_HOST_REQUIRED else {}
        return reply(err, ok=False, notify=False, secret=secret, retryable=False, **extra)
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
        return reply(
            "No watch query is saved, so seen ids were not advanced.",
            ok=False, notify=False, secret=secret,
        )
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


def _profile_name(runtime: Runtime) -> str | None:
    try:
        name = runtime.profile()
    except Exception:
        return None
    return clean_profile(name)


def _hermes_prefix(profile: str) -> str:
    if profile == "default":
        return "hermes"
    return f"hermes -p {profile}"


def _watch_commands(profile: str | None, job_id: str) -> str:
    """The lines the user runs. This plugin does not create or remove the job."""
    if profile is None:
        return "profile unknown."
    prefix = _hermes_prefix(profile)
    create = (
        f'{prefix} cron create "every 15 minutes" "{JOB_PROMPT}" '
        "--name index-search --deliver local"
    )
    rows = [create]
    if job_id:
        rows.append(
            f"This plugin did not remove cron job {job_id}. "
            f"Remove it with: {prefix} cron remove {job_id}"
        )
    return "\n".join(rows)


def _save(runtime: Runtime, data: dict) -> str:
    secret = _secret(runtime)
    allowed = {"action", "index", "q", "id_field", "title_field"}
    if set(data) - allowed:
        return reply(
            "Delivery is not a tool argument. Put --deliver on hermes cron create. "
            "index_watch save takes only index, q, id_field, and an optional title_field.",
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
    title_given = "title_field" in data and data.get("title_field") is not None
    title_field = ""
    if title_given and data.get("title_field") != "":
        if not _field_ok(data.get("title_field")):
            return reply(
                "The title field must be a non-empty string of 64 characters or fewer, without a dot, a slash, or a control character.",
                ok=False, notify=True, secret=secret, retryable=False,
            )
        title_field = data["title_field"]
    kind, current = _watch_state(runtime)
    if kind == "corrupt":
        return reply(
            "The saved watch file is unreadable, so it was not overwritten.",
            ok=False, notify=True, secret=secret, retryable=False,
        )
    previous = store.normalize(current) if current else None
    same_identity = bool(previous) and (
        previous.get("index"), previous.get("q"), previous.get("id_field"),
    ) == (data["index"], data["q"], data["id_field"])
    if previous and not title_given and same_identity:
        title_field = previous.get("title_field") or ""
    if previous and same_identity:
        seen = list(previous.get("seen") or [])
        baseline_pending = bool(previous.get("baseline_pending"))
        last_empty = bool(previous.get("last_empty"))
        fail = previous.get("fail")
        over = previous.get("over")
        zero_told = bool(previous.get("zero_told"))
    elif previous:
        seen = list(previous.get("seen") or [])
        baseline_pending = True
        last_empty = False
        fail = None
        over = None
        zero_told = False
    else:
        seen = []
        baseline_pending = True
        last_empty = False
        fail = None
        over = None
        zero_told = False
    job_id = (previous or {}).get("job_id") or ""
    state = {
        "index": data["index"],
        "q": data["q"],
        "id_field": data["id_field"],
        "title_field": title_field,
        "deliver": "",
        "job_id": job_id,
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
        return reply(
            "The watch file was not written. This call did not search and did not create a cron job. Seen ids were not changed.",
            ok=False, notify=True, secret=secret, retryable=False,
        )
    shown_title = title_field or "(none)"
    lead = (
        f"Watch query saved. Index: {data['index']}. Query: {data['q']}. "
        f"Id field: {data['id_field']}. Title field: {shown_title}. "
        "This call did not search and did not create a cron job."
    )
    if previous and not same_identity:
        lead += " The next check saves a new baseline. Seen ids were kept."
    elif previous and same_identity and (previous.get("title_field") or "") != title_field:
        lead += " The title field was updated. Seen ids were kept."
    elif previous and same_identity:
        lead += " Seen ids were kept."
    lead += MSG_LIMIT
    lines_out = _watch_commands(_profile_name(runtime), job_id)
    if lines_out == "profile unknown.":
        text = lead + " profile unknown."
    else:
        text = lead + "\n" + lines_out
    return reply(text, ok=True, notify=True, secret=secret)


def _retired(runtime: Runtime, _data: dict) -> str:
    secret = _secret(runtime)
    return reply(
        "This action does not create or remove a cron job. "
        "Save the query with action save. "
        "You create the watch with hermes cron create and remove it with hermes cron remove. "
        "Seen ids were not changed.",
        ok=False, notify=True, secret=secret, retryable=False,
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
        return reply("No watch query is saved.", ok=True, notify=False, secret=secret, scheduled=False)
    state = current
    title = state.get("title_field") or "(none)"
    lead = (
        f"A watch query is saved. Index: {state.get('index')}. Query: {state.get('q')}. "
        f"Id field: {state.get('id_field')}. Title field: {title}."
    )
    lines_out = _watch_commands(_profile_name(runtime), state.get("job_id") or "")
    if lines_out == "profile unknown.":
        text = lead + " profile unknown."
    else:
        text = lead + "\n" + lines_out
    return reply(
        text,
        ok=True, notify=False, secret=secret, scheduled=True,
        index=state.get("index"), q=state.get("q"), id_field=state.get("id_field"),
        title_field=state.get("title_field") or "",
    )


def index_watch(runtime: Runtime, args, **kwargs) -> str:
    del kwargs
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
    if action == "save":
        return _save(runtime, data)
    if action in {"schedule", "unschedule"}:
        return _retired(runtime, data)
    if action == "status":
        return _status(runtime, data)
    return reply(MSG_ACTION, ok=False, notify=True, secret=secret, retryable=False)
