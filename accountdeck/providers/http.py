"""Provider HTTP (A1, §5.10): one request with classification, an honest user agent and the safe-mode host gate.

In safe mode (tests, demos) a request may only reach an in-process loopback server registered with
`register_test_host`; everything else raises SafetyViolation before a socket is opened.
"""
from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import diagnostics
from ..config import DEFAULT_PORT, V1_PORT, SafetyViolation, safe_mode
from .base import OAUTH_BETA, OAUTH_HOST, OAUTH_PATH, USER_AGENT
from .errors import ProviderError, classify_http, error_type, header, rate_limit_wait, retry_after_seconds

DEFAULT_TIMEOUT = 18
MAX_BODY = 4 * 1024 * 1024
HEAD_BYTES = 2048          # only this much of an error body is read, and only to classify it
_LOOPBACK = ("127.0.0.1", "::1")
_CONTROLLED = frozenset({"user-agent", "authorization", "cookie", "anthropic-beta"})

_test_hosts: set[str] = set()
_test_hosts_lock = threading.Lock()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None        # never forward a credential request; the 3xx surfaces as an HTTPError


def register_test_host(host_port):
    """Lets requests reach an in-process test server at "127.0.0.1:<port>" (safe mode only)."""
    host, port = _split(host_port)
    if not safe_mode():
        raise SafetyViolation("Test servers can only be registered in safe mode.")
    if host not in _LOOPBACK or port in (V1_PORT, DEFAULT_PORT):
        raise SafetyViolation("Only a loopback test server on a free test port can be registered.")
    with _test_hosts_lock:
        _test_hosts.add(f"{host}:{port}")


def unregister_test_host(host_port):
    host, port = _split(host_port)
    with _test_hosts_lock:
        _test_hosts.discard(f"{host}:{port}")


def request_headers(url, *, headers=None, token=None, cookie=None):
    """The headers sent to `url`: the caller's, except that the user agent, credentials and the
    OAuth beta flag (api.anthropic.com OAuth endpoints only) are always decided here."""
    parts = urllib.parse.urlsplit(url)
    result = {"Accept": "application/json"}
    result.update({key: value for key, value in (headers or {}).items() if key.lower() not in _CONTROLLED})
    result["User-Agent"] = USER_AGENT
    if (parts.hostname or "").lower() == OAUTH_HOST and parts.path.startswith(OAUTH_PATH):
        result["anthropic-beta"] = OAUTH_BETA
    if token:
        result["Authorization"] = "Bearer " + token
    if cookie:
        result["Cookie"] = cookie
    return result


def request(method, url, *, headers=None, timeout=DEFAULT_TIMEOUT, cookie=None, token=None,
            token_was_expired=False, incident=False, json_body=None):
    """Sends one request. Returns (parsed JSON or None, meta) for a 2xx answer and raises a
    classified ProviderError otherwise. meta = {"status", "class", "ms", "cf_mitigated", "server",
    "error_type", "retry_after"}; bodies, tokens and cookies never reach meta or the log."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    test_server = _gate(parts.scheme, host, parts.port or (443 if parts.scheme == "https" else 80))
    handlers = [_NoRedirect()]
    if test_server:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    sent_headers = request_headers(url, headers=headers, token=token, cookie=cookie)
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        sent_headers["Content-Type"] = "application/json"
    outgoing = urllib.request.Request(url, method=method, data=data, headers=sent_headers)
    meta = {"status": None, "class": "network", "ms": 0, "cf_mitigated": None, "server": None,
            "error_type": None, "retry_after": None}
    started = time.monotonic()
    try:
        with opener.open(outgoing, timeout=timeout) as response:
            meta.update(status=response.status, **_header_meta(response.headers))
            raw = response.read(MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        error = _http_error(url, exc, meta, token_was_expired, incident)
    except (OSError, http.client.HTTPException) as exc:
        timed_out = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
        meta["class"] = "timeout" if timed_out else "network"
        error = ProviderError(classify_http(url, None, incident=incident), detail=meta["class"], meta=meta)
    else:
        meta["class"] = diagnostics.http_class(meta["status"])
        error = None
        if len(raw) > MAX_BODY:
            error = ProviderError("network", http=meta["status"], detail="oversize", meta=meta,
                                  message="The provider's answer was too large. Your last reading is kept.")
    meta["ms"] = round((time.monotonic() - started) * 1000)
    diagnostics.log("http.request", "info" if error else "debug", host=host, path=parts.path,
                    status=meta["status"], cls=meta["class"], ms=meta["ms"], cf_mitigated=meta["cf_mitigated"],
                    error_type=meta["error_type"], kind=error.kind if error else None)
    if error:
        raise error
    try:
        return json.loads(raw), meta
    except ValueError:
        return None, meta


def _gate(scheme, host, port):
    """Refuses what safe mode does not allow, before any socket exists; True for a registered test server."""
    if safe_mode():
        with _test_hosts_lock:
            registered = f"{host}:{port}" in _test_hosts
        if not registered:
            raise SafetyViolation(f"Network access to {host or 'that address'} is blocked in safe mode.")
        return True
    if scheme != "https":
        raise ValueError("Provider requests must use HTTPS.")
    return False


def _http_error(url, exc, meta, token_was_expired, incident):
    try:
        head = exc.read(HEAD_BYTES)
    except (OSError, http.client.HTTPException):
        head = b""
    finally:
        exc.close()
    headers = exc.headers or {}
    meta.update(status=exc.code, **_header_meta(headers))
    meta["class"] = diagnostics.http_class(exc.code)
    meta["error_type"] = error_type(headers, head)
    if url == "https://platform.claude.com/v1/oauth/token":
        try:
            verdict = json.loads(head).get("error")
        except (ValueError, AttributeError):
            verdict = None
        if verdict in ("invalid_grant", "invalid_client"):
            meta["error_type"] = verdict
    wait = None
    if exc.code == 429:
        meta["retry_after"] = retry_after_seconds(header(headers, "retry-after"))
        wait = rate_limit_wait(meta["retry_after"])
    kind = classify_http(url, exc.code, headers, head, token_was_expired, incident)
    return ProviderError(kind, http=exc.code, retry_after=wait, detail=meta["error_type"], meta=meta)


def _header_meta(headers):
    return {"cf_mitigated": _clean(header(headers, "cf-mitigated"), 20), "server": _clean(header(headers, "server"), 40)}


def _clean(value, limit):
    if not value:
        return None
    text = "".join(ch for ch in str(value) if ch.isprintable())[:limit].strip()
    return text or None


def _split(host_port):
    parts = urllib.parse.urlsplit("//" + str(host_port))
    try:
        port = parts.port
    except ValueError:
        port = None
    if not parts.hostname or not port:
        raise ValueError("A test host is written as 127.0.0.1:<port>.")
    return parts.hostname.lower(), port
