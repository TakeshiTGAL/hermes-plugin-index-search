"""HTTP for one origin. Search sends a bearer token and does not follow redirects."""

from __future__ import annotations

import http.client
import socket
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 30
MAX_BODY = 2_000_000


class Redirected(Exception):
    def __init__(self, code: int):
        self.code = int(code)
        super().__init__(f"redirect {code}")


class Incomplete(Exception):
    """The response ended before a complete search result was in hand."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop on every redirect. urllib would otherwise send Authorization to Location.

    http_error_* is what urllib calls, and it does not pass newurl.
    redirect_request is the follow-up that would build the next request.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise Redirected(int(code))

    def http_error_302(self, req, fp, code, msg, headers):  # noqa: ANN001
        raise Redirected(int(code))

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def opener() -> urllib.request.OpenerDirector:
    # ProxyHandler({}) replaces urllib's default, so HTTP_PROXY does not receive
    # Authorization. _NoRedirect replaces the default redirect handler.
    built = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    built.addheaders = []
    return built


def _declared_too_big(headers) -> bool:
    if not headers:
        return False
    declared = headers.get("content-length") or headers.get("Content-Length")
    return declared is not None and str(declared).isdigit() and int(declared) > MAX_BODY


def _read_capped(read) -> bytes:
    raw = read(MAX_BODY + 1)
    if not raw:
        return b""
    if len(raw) > MAX_BODY:
        raise Incomplete()
    return raw


def perform(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float = TIMEOUT_SECONDS,
    open_fn=None,
) -> tuple[int, dict[str, str], bytes]:
    """One attempt. Raises Redirected or Incomplete. Does not retry."""
    send = open_fn or _open
    try:
        status, hdrs, raw = send(method, url, headers, body, timeout)
    except Redirected:
        raise
    except Incomplete:
        raise
    except (TimeoutError, socket.timeout, ConnectionError, http.client.RemoteDisconnected, Incomplete):
        raise Incomplete() from None
    except urllib.error.URLError:
        raise Incomplete() from None
    except http.client.IncompleteRead:
        raise Incomplete() from None
    lowered = {k.lower(): v for k, v in hdrs.items()}
    if _declared_too_big(lowered) or len(raw) > MAX_BODY:
        raise Incomplete()
    declared = lowered.get("content-length")
    if declared is not None and str(declared).isdigit() and int(declared) > len(raw):
        raise Incomplete()
    return status, lowered, raw


def _open(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float):
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with opener().open(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            hdrs = {k.lower(): v for k, v in response.headers.items()}
            if _declared_too_big(hdrs):
                raise Incomplete()
            return status, hdrs, _read_capped(response.read)
    except Redirected:
        raise
    except urllib.error.HTTPError as exc:
        hdrs = {k.lower(): v for k, v in exc.headers.items()}
        if _declared_too_big(hdrs):
            raise Incomplete()
        return exc.code, hdrs, _read_capped(exc.read)
