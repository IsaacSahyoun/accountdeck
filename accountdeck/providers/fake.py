"""Synthetic providers and machine for tests and demo mode (§11.4).

Nothing here reads or writes a real profile or Keychain item: build_fake_home refuses any Paths that is
not synthetic and every target outside its test root, the fake adapters never make a request, and
FakeMacCrypto stands in for the "Claude Safe Storage" Keychain password with a fixed synthetic one.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from ..config import SafetyViolation
from ..core import atomic_write, json_bytes
from .chromium_crypto import ChromiumCryptoMac, derive_mac_key, seal_cbc
from .claude import CHROMIUM_EPOCH, DEFAULT_SCOPE, Claude
from .codex import AUTH_CLAIMS, Codex, jwt_claims
from .errors import ProviderError
from .status import StatusSource

CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_SCOPES = "user:inference user:profile user:sessions:claude_code"
SAFE_STORAGE_PASSWORD = "synthetic-safe-storage-password"     # what FakeMacCrypto "reads from the Keychain"
REGISTRY_VERSION = "2.1.284"
REGISTRY_KEYS = ("pid", "status", "kind", "entrypoint", "name", "cwd", "updatedAt", "statusUpdatedAt", "procStart",
                 "version")
ROTATED_LIFETIME = 10 * 86400
COOKIE_SCHEMA = """
CREATE TABLE meta(key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR);
INSERT INTO meta VALUES('version', '24');
CREATE TABLE cookies(creation_utc INTEGER NOT NULL, host_key TEXT NOT NULL, top_frame_site_key TEXT NOT NULL,
    name TEXT NOT NULL, value TEXT NOT NULL, encrypted_value BLOB NOT NULL, path TEXT NOT NULL,
    expires_utc INTEGER NOT NULL, is_secure INTEGER NOT NULL, is_httponly INTEGER NOT NULL,
    last_access_utc INTEGER NOT NULL, has_expires INTEGER NOT NULL, is_persistent INTEGER NOT NULL,
    priority INTEGER NOT NULL, samesite INTEGER NOT NULL, source_scheme INTEGER NOT NULL,
    source_port INTEGER NOT NULL, last_update_utc INTEGER NOT NULL);
"""


def test_cipher(data, decrypt=False):
    """A reversible stand-in for the vault cipher in tests and demos; it protects nothing."""
    if decrypt:
        if not data.startswith(b"TEST-ENCRYPTED:"):
            raise ValueError("not a test-cipher blob")
        return base64.b64decode(data[15:])
    return b"TEST-ENCRYPTED:" + base64.b64encode(data)


def unsigned_jwt(claims):
    """A JWT with alg "none", as Codex's auth.json holds them for synthetic accounts."""
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    return part({"alg": "none", "typ": "JWT"}) + "." + part(claims) + "."


class FakeMacCrypto(ChromiumCryptoMac):
    """Claude Desktop's macOS encryption with a fixed password instead of the Keychain item."""

    def __init__(self, password=SAFE_STORAGE_PASSWORD):
        super().__init__()
        self._fixed = password

    def _password(self):
        return self._fixed


class Timeline:
    """A script [(t_offset, payload_or_exception)] against an injectable clock. current() returns
    (a copy of) the payload of the last entry whose offset has passed since construction, None
    before the first one, and raises an entry that is an exception."""

    def __init__(self, script, now=time.time):
        self.script = sorted(script or (), key=lambda entry: entry[0])
        self.now = now
        self.start = now()

    def current(self):
        elapsed = self.now() - self.start
        payload = None
        for offset, value in self.script:
            if offset > elapsed:
                break
            payload = value
        if isinstance(payload, BaseException) or (isinstance(payload, type) and issubclass(payload, BaseException)):
            raise payload
        return copy.deepcopy(payload)


class FakeClaude(Claude):
    """Claude with scripted usage instead of HTTP. `scripts` maps an account uuid to its script and
    `script` serves every other account; payloads are what /api/oauth/usage returns (the cookie
    route reads the same timeline). Credential tiers, health and restore are the real code, on
    macOS-format profiles under FakeMacCrypto unless `crypto` is given."""

    def __init__(self, paths, script=(), *, scripts=None, identities=None, now=time.time, scope=DEFAULT_SCOPE,
                 crypto=None):
        super().__init__(paths, crypto=crypto or FakeMacCrypto(), scope=scope, clock=now)
        self.default = Timeline(script, now)
        self.timelines = {key: Timeline(value, now) for key, value in (scripts or {}).items()}
        self.identities = identities or {}

    def identity(self, profile):
        ident = profile.get("identity")
        known = self.identities.get(ident, {})
        return {"identity": ident, "email": known.get("email", f"claude-{str(ident)[:8]}@example.test"),
                "plan": known.get("plan", "max"), "org": known.get("org", profile.get("org", ""))}

    def _read_oauth(self, profile, token, *, incident):
        return self._payload(profile)

    def _read_cookie(self, profile, *, incident):
        self.cookie_header(profile)
        return self._payload(profile)

    def _payload(self, profile):
        payload = self.timelines.get(profile.get("identity"), self.default).current()
        if payload is None:
            raise ProviderError("network", detail="no_script")
        return payload


class FakeCodex(Codex):
    """Codex with scripted probes instead of the app-server. Payloads are what
    account/rateLimits/read returns; a refresh-mode probe rotates the probe's auth.json like the
    real helper, so persistence and fingerprints run the real code."""

    def __init__(self, paths, script=(), *, scripts=None, now=time.time, timings=None, sleep=time.sleep):
        super().__init__(paths, None, timings=timings, clock=now, sleep=sleep)
        self.default = Timeline(script, now)
        self.timelines = {key: Timeline(value, now) for key, value in (scripts or {}).items()}

    def _probe(self, work, identity, refresh):
        payload = self.timelines.get(identity, self.default).current()
        if refresh:
            _rotate_codex_auth(Path(work) / "auth.json", self.clock())
        if payload is None:
            raise ProviderError("codex", detail="no_script")
        return payload, None


class FakeStatusSource(StatusSource):
    """A status page following a script. A payload is a level ("ok", "degraded", "outage"), a
    partial status dict, or an exception (a failed fetch); None before the first entry means ok."""
    SUMMARIES = {"ok": "All Systems Operational", "degraded": "Partially Degraded Service",
                 "outage": "Major Service Outage", "unknown": "Status unknown"}

    def __init__(self, script=(), *, now=time.time, url="https://status.example.test/api/v2/summary.json"):
        super().__init__(url, clock=now)
        self.timeline = Timeline(script, now)

    def _load(self):
        payload = self.timeline.current()
        payload = {"level": payload or "ok"} if payload is None or isinstance(payload, str) else payload
        level = payload.get("level", "ok")
        return {**self._status(level, self.SUMMARIES.get(level, level)), **payload}


def build_fake_home(paths, *, claude_identity, claude_org, codex_identity, cookies=True, menu_bar_enabled=True,
                    sessions=(), codex_exp_offset=9 * 86400, claude_exp_offset=8 * 3600, now=None,
                    password=SAFE_STORAGE_PASSWORD, meta_version=24):
    """Writes a complete synthetic Claude Desktop and Codex installation into a Paths.synthetic tree
    (§11.4) and returns {"paths", "claude", "codex", "codex_exe", "sessions"}.

    Claude (macOS format, docs/mac/PLAN.md §1.6): config.json with the token caches as base64("v10" +
    AES-128-CBC) under PBKDF2(`password`), a Chromium cookie database in the profile folder at
    `meta_version` (unless `cookies` is false), a Code session with its transcript,
    claude_desktop_config.json with two MCP servers, registry rows for `sessions` (dicts with at least
    "pid") and main.log. Codex: auth.json with unsigned JWTs, config.toml, session_index.jsonl, daemon
    settings and `paths.codex_exe` as a synthetic app-server script. The same `password` again keeps
    profiles captured in between restorable."""
    if getattr(paths, "mode", None) != "synthetic" or getattr(paths, "temp_root", None) is None:
        raise SafetyViolation("build_fake_home writes only into Paths.synthetic trees.")
    root = Path(paths.temp_root).resolve()
    targets = [paths.claude_profile, paths.claude_sessions, paths.claude_projects, paths.claude_main_log,
               paths.codex_home, paths.home] + ([paths.codex_exe] if paths.codex_exe else [])
    if not all(Path(target).resolve().is_relative_to(root) for target in targets):
        raise SafetyViolation("A synthetic path points outside its test root.")
    for value in (claude_identity, claude_org):
        uuid.UUID(value)
    now = time.time() if now is None else now
    ms = int(now * 1000)
    profile_dir = Path(paths.claude_profile)
    project = Path(paths.home) / "projects" / "synthetic"

    key = derive_mac_key(password)
    expires_at = now + claude_exp_offset
    cache = {f"acct:{claude_identity}|{CLAUDE_CLIENT_ID}:{claude_org}:https://api.anthropic.com:{CLAUDE_SCOPES}": {
        "token": "synthetic-access-" + secrets.token_hex(12), "refreshToken": "synthetic-refresh-" + secrets.token_hex(12),
        "expiresAt": int(expires_at * 1000)}}
    atomic_write(profile_dir / "config.json", json_bytes({
        "locale": "en-US", "userThemeMode": "system", "lastKnownAccountUuid": claude_identity,
        "oauth:tokenCache": encrypt_v10(key, b"{}"), "oauth:tokenCacheV2": encrypt_v10(key, json_bytes(cache))}))
    for name in DEFAULT_SCOPE.cookie_files:
        (profile_dir / name).unlink(missing_ok=True)
    if cookies:
        write_cookies(profile_dir / DEFAULT_SCOPE.cookie_files[0], key, now, [
            (".claude.ai", "sessionKey", "synthetic-session-" + secrets.token_hex(12)),
            (".claude.ai", "lastActiveOrg", claude_org),
            (".claude.ai", "anthropic-device-id", str(uuid.uuid4())),
            (".claude.ai", "cf_clearance", "synthetic-clearance-" + secrets.token_hex(6)),
            (".example.test", "sessionKey", "synthetic-foreign-cookie")], meta_version=meta_version)

    local, cli = uuid.uuid4(), uuid.uuid4()
    atomic_write(profile_dir / "claude-code-sessions" / claude_identity / claude_org / f"local_{local}.json", json_bytes({
        "sessionId": f"local_{local}", "cliSessionId": str(cli), "cwd": str(project), "originCwd": str(project),
        "createdAt": ms, "lastActivityAt": ms, "model": "claude-sonnet-4-5", "isArchived": False,
        "title": "Synthetic session", "permissionMode": "default"}))
    atomic_write(Path(paths.claude_projects) / "synthetic-project" / f"{cli}.jsonl",
                 json_bytes({"type": "summary", "summary": "Synthetic session"}) + b"\n")
    atomic_write(profile_dir / "claude_desktop_config.json", json_bytes({
        "mcpServers": {"filesystem": {"command": "synthetic-mcp-filesystem", "args": [str(project)]},
                       "github": {"command": "synthetic-mcp-github", "args": [], "env": {"GITHUB_TOKEN": "synthetic"}}},
        "preferences": {"menuBarEnabled": bool(menu_bar_enabled)}}))
    rows = []
    for session in sessions:
        pid = int(session["pid"])
        row = {"status": "idle", "kind": "interactive", "entrypoint": "claude-desktop", "name": f"Session {pid}",
               "cwd": str(project), "updatedAt": ms, "statusUpdatedAt": ms, "procStart": "0",
               "version": REGISTRY_VERSION, **session, "pid": pid}
        target = Path(paths.claude_sessions) / f"{pid}.json"
        atomic_write(target, json_bytes({name: row[name] for name in REGISTRY_KEYS}))
        rows.append(target)
    log = Path(paths.claude_main_log)
    if not log.exists():
        atomic_write(log, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now)).encode()
                     + b" [info] Synthetic Claude Desktop log\n")

    codex_home = Path(paths.codex_home)
    codex_email = f"codex-{codex_identity[:8]}@example.test"
    exp = int(now + codex_exp_offset)
    claims = {AUTH_CLAIMS: {"chatgpt_account_id": codex_identity, "chatgpt_plan_type": "plus"}}
    atomic_write(codex_home / "auth.json", json_bytes({
        "OPENAI_API_KEY": None,
        "tokens": {"id_token": unsigned_jwt({**claims, "email": codex_email, "exp": exp}),
                   "access_token": unsigned_jwt({**claims, "exp": exp}),
                   "refresh_token": "synthetic-refresh-" + secrets.token_hex(12), "account_id": codex_identity},
        "last_refresh": _iso(now)}))
    atomic_write(codex_home / "config.toml", b'cli_auth_credentials_store = "file"\n')
    atomic_write(codex_home / "session_index.jsonl", json_bytes(
        {"id": str(uuid.uuid4()), "thread_name": "Synthetic thread", "updated_at": _iso(now)}) + b"\n")
    atomic_write(codex_home / "app-server-daemon" / "settings.json", json_bytes({"shutdownGraceSeconds": 60}))
    codex_exe = _write_codex_stub(Path(paths.codex_exe), paths.python_exe) if paths.codex_exe else None

    return {"paths": paths, "codex_exe": codex_exe, "sessions": rows,
            "claude": {"identity": claude_identity, "org": claude_org, "email": f"claude-{claude_identity[:8]}@example.test",
                       "plan": "max", "profile_dir": profile_dir, "password": password, "key": key,
                       "expires_at": expires_at, "local_session": f"local_{local}", "cli_session": str(cli)},
            "codex": {"identity": codex_identity, "email": codex_email, "plan": "plus", "home": codex_home, "exp": exp}}


STUB_APP_SERVER = r'''"""Synthetic Codex app-server for AccountDeck tests and demos: JSON-RPC over stdio, no network."""
import base64
import json
import os
import secrets
import sys
import time
from pathlib import Path

AUTH = "https://api.openai.com/auth"


def encode(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def claims(token):
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def account_read(params):
    auth = json.loads(Path("auth.json").read_text(encoding="utf-8"))
    tokens = auth["tokens"]
    if params.get("refreshToken") and tokens.get("refresh_token"):
        access = claims(tokens["access_token"])
        access["exp"] = int(time.time()) + 10 * 86400
        tokens["access_token"] = encode({"alg": "none", "typ": "JWT"}) + "." + encode(access) + "."
        tokens["refresh_token"] = "synthetic-refresh-" + secrets.token_hex(8)
        auth["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        Path("auth.json.tmp").write_text(json.dumps(auth), encoding="utf-8")
        os.replace("auth.json.tmp", "auth.json")
    info = claims(tokens.get("id_token") or tokens["access_token"])
    plan = (info.get(AUTH) or {}).get("chatgpt_plan_type", "plus")
    return {"account": {"type": "chatgpt", "email": info.get("email", ""), "planType": plan},
            "requiresOpenaiAuth": True}


def rate_limits(params):
    now = int(time.time())
    return {"rateLimits": {"limitId": "codex",
                           "primary": {"usedPercent": 12.0, "windowDurationMins": 300, "resetsAt": now + 3600},
                           "secondary": {"usedPercent": 40.0, "windowDurationMins": 10080, "resetsAt": now + 259200}}}


METHODS = {"initialize": lambda params: {"userAgent": "codex-stub/0.0.0"}, "account/read": account_read,
           "account/rateLimits/read": rate_limits,
           "account/usage/read": lambda params: {"summary": {"totalTokens": 1234}}}


def serve():
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or "id" not in message:
            continue
        handler = METHODS.get(message.get("method"))
        if handler is None:
            reply = {"id": message["id"], "error": {"code": -32601, "message": "Method not found"}}
        else:
            try:
                reply = {"id": message["id"], "result": handler(message.get("params") or {})}
            except Exception as exc:
                reply = {"id": message["id"], "error": {"code": -32603, "message": type(exc).__name__}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    return 0


def main(argv):
    if argv[:2] == ["app-server", "daemon"]:
        return 0
    if argv[:1] == ["app-server"]:
        return serve()
    sys.stderr.write("codex stub: synthetic test binary\n")
    return 2


sys.exit(main(sys.argv[1:]))
'''


def _write_codex_stub(target, python):
    """`paths.codex_exe`: an executable running the synthetic app-server with `python` (isolated). The
    second line is both a shell command and a harmless Python string expression, so the interpreter's
    path may contain spaces (a #! line cannot)."""
    launcher = f'#!/bin/sh\n"exec" "{Path(python)}" "-I" "$0" "$@"\n'
    atomic_write(target, launcher.encode() + STUB_APP_SERVER.encode())
    os.chmod(target, 0o755)
    return target


def encrypt_v10(key, plaintext):
    """macOS Chromium's "v10" AES-128-CBC value as base64 text (how config.json holds the token caches);
    `key` is the 16-byte derive_mac_key(password)."""
    return base64.b64encode(seal_cbc(key, plaintext)).decode()


def write_cookies(path, key, now, cookies, *, meta_version=24):
    """A Chromium cookie database of (host, name, value) rows at `path`; values are macOS "v10"
    AES-128-CBC under the 16-byte `key`, with the sha256(host) prefix from `meta_version` 24, and expire
    30 days after `now`."""
    atomic_write(path, _cookie_database(now, cookies, lambda plaintext: seal_cbc(key, plaintext), meta_version))


def empty_cookie_db(meta_version=24):
    """The bytes of an empty Chromium cookie database at `meta_version`, standing in for the one Claude
    Desktop creates on its first launch (the schema donor of claude_portable.from_portable)."""
    return _cookie_database(0, (), None, meta_version)


def _cookie_database(now, cookies, seal, meta_version):
    stamp = int((now + CHROMIUM_EPOCH) * 1_000_000)
    expires = int((now + 30 * 86400 + CHROMIUM_EPOCH) * 1_000_000)
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(COOKIE_SCHEMA)
        if meta_version != 24:
            db.execute("UPDATE meta SET value = ? WHERE key = 'version'", (str(int(meta_version)),))
        for host, name, value in cookies:
            plaintext = value.encode()
            if meta_version >= 24:
                plaintext = hashlib.sha256(host.encode()).digest() + plaintext
            db.execute("INSERT INTO cookies VALUES (?, ?, '', ?, '', ?, '/', ?, 1, 1, ?, 1, 1, 1, 0, 2, 443, ?)",
                       (stamp, host, name, seal(plaintext), expires, stamp, stamp))
        db.commit()
        data = db.serialize()
    finally:
        db.close()
    return data


def _rotate_codex_auth(path, now):
    auth = json.loads(Path(path).read_bytes())
    tokens = auth["tokens"]
    tokens["access_token"] = unsigned_jwt({**jwt_claims(tokens["access_token"]), "exp": int(now) + ROTATED_LIFETIME})
    tokens["refresh_token"] = "synthetic-refresh-" + secrets.token_hex(8)
    auth["last_refresh"] = _iso(now)
    atomic_write(path, json_bytes(auth))


def _iso(when):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))
