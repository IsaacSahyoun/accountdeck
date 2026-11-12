"""Codex adapter (§5.11, A8, A9): token state, read-only and refresh probes through a private
app-server helper, the tri-state active identity, and auth.json swaps.

The helper runs in a throw-away CODEX_HOME under the data folder and is spawned only through the
process controller (§4.1). A read-only probe gets a blank refresh token, so it can never rotate the
refresh token the official client holds; refresh mode is reserved for accounts AccountDeck 2 owns.

Only the file store, ~/.codex/auth.json, is swapped (docs/mac/PLAN.md §1.8, §8). Config leaves
`codex_exe` unset live, so the CLI is found at run time (facts_mac.codex_exe: PATH, the ~/.local/bin
launcher resolved to its package binary, the package, then the CLI inside ChatGPT.app). A config.toml
that keeps the sign-in in the Keychain ("keyring" or "auto") fails closed with a message saying how to
switch instead.

How the app-server reports a sign-in it cannot use (checked against Codex 0.161 with a stub token
endpoint): when its refresh token is refused for good (expired, already used, revoked) Codex drops the
sign-in and `account/read` answers `{"account": null, "requiresOpenaiAuth": true}` instead of an error.
That is a dead sign-in, not a different account, so it is classified as "unauthorized" (a refresh probe
then reports refresh_rejected and the account needs signing in again). Codex also tries a refresh at
start-up whenever the access token has expired, with the blank refresh token in read-only mode, which
can rotate nothing.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, replace
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import queue
import random
import tempfile
import threading
import time
import tomllib
import uuid

from .. import __version__, diagnostics
from ..config import Timings
from ..core import DeckError, atomic_write, json_bytes, read_json
from ..platform import facts_mac
from ..procs import DEVNULL, PIPE   # the stdio sentinels passed through procs.spawn_helper
from ..usage import codex_usage
from .base import Reading
from .errors import RATE_LIMIT_MARGIN, ProviderError

HOUR, DAY = 3600, 86400
REFRESH_WINDOW = 48 * HOUR
STALE_REFRESH = 5 * DAY
MIN_ATTEMPT_GAP = 6 * HOUR
BACKOFF_BASE, BACKOFF_CAP = 300, 6 * HOUR
ACCESS_EXPIRY_MARGIN = 600      # a 401 this close to expiry is the access token lapsing, not a lost sign-in
IDENTITY_RETRIES, IDENTITY_RETRY_DELAY = 3, 0.05
KILL_WAIT = 3
MODES = ("read_only", "refresh")
AUTH_CLAIMS = "https://api.openai.com/auth"
REFRESH_REJECTED = ("refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated", "invalid_grant")
PROBE_CONFIG = (b'cli_auth_credentials_store = "file"\n[features]\nplugins = false\napps = false\n'
                b'remote_plugin = false\nhooks = false\nskill_search = false\n[analytics]\nenabled = false\n')
NOT_INSTALLED_MESSAGE = "Install Codex CLI and sign in once, then try again."
KEYCHAIN_STORES = ("keyring", "auto")    # on macOS both keep the sign-in in the Keychain, not in auth.json
MAC_KEYCHAIN_MESSAGE = ('Codex keeps its sign-in in the macOS Keychain (cli_auth_credentials_store = "{mode}" in '
                        '{config}), so switching here would not take effect. Set it to "file" there, or switch '
                        'accounts in Codex yourself.')
MAC_STORE_MESSAGE = ('Codex is set to cli_auth_credentials_store = "{mode}" in {config}, so switching here would not '
                     'take effect. Set it to "file" there, or switch accounts in Codex yourself.')


def jwt_claims(token):
    """The payload of a JWT. Never signature-checked: only expiry and account claims are read."""
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, IndexError, AttributeError, TypeError):
        claims = None
    if not isinstance(claims, dict):
        raise DeckError("The saved sign-in is incomplete. Sign in again.", "login_needed")
    return claims


@dataclass(frozen=True)
class TokenState:
    exp: float | None             # access-token expiry (unix seconds)
    last_refresh: float | None
    remaining: float | None       # exp - now
    readable: bool


def token_state(auth, now):
    """TokenState of a Codex auth.json dict, from the access-token JWT `exp` and `last_refresh`."""
    auth = auth if isinstance(auth, dict) else {}
    exp = None
    try:
        value = jwt_claims((auth.get("tokens") or {}).get("access_token")).get("exp")
    except (DeckError, AttributeError):
        value = None
    if _number(value) is not None:
        exp = float(value)
    return TokenState(exp=exp, last_refresh=_timestamp(auth.get("last_refresh")),
                      remaining=None if exp is None else exp - now, readable=exp is not None)


def backoff(failures, rng=random.random):
    """5 min x 2^failures, capped at 6 h, with ±10 % jitter."""
    return min(BACKOFF_CAP, BACKOFF_BASE * 2 ** max(0, int(failures))) * (0.9 + 0.2 * rng())


def refresh_due(ts, *, owner, last_attempt, now, failures=0, rng=random.random):
    """Whether AccountDeck 2 should renew a saved Codex sign-in now (A8): only accounts it owns,
    only when the access token has ≤ 48 h left (or cannot be read and the last refresh is ≥ 5 days
    old), and never sooner than max(6 h, backoff) after the previous attempt."""
    if owner != "v2":
        return False
    if ts.readable:
        needed = ts.remaining <= REFRESH_WINDOW
    else:
        needed = ts.last_refresh is not None and now - ts.last_refresh >= STALE_REFRESH
    if not needed:
        return False
    return last_attempt is None or now - last_attempt >= max(MIN_ATTEMPT_GAP, backoff(failures, rng))


def identity_key(profile):
    """The account id a saved auth.json belongs to (v1's key), or None when it cannot be read."""
    try:
        return Codex.identity(profile)["identity"]
    except DeckError:
        return None


def freshness(profile):
    """Comparable freshness of a saved auth.json for Vault.put_guarded: the access-token expiry."""
    return token_state(profile, 0).exp


def stash(path, before, quarantine, provider, ref) -> bool:
    """After a renewed login could not be saved: True once the helper's file `path` holds nothing worth
    keeping (no file, the unchanged login `before`, or one now handed to `quarantine`), so its folder can be
    deleted rather than left unencrypted. Shared by Codex and Muse."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        if json.loads(raw) == before:
            return True
    except ValueError:
        pass
    if quarantine is None:
        return False
    try:
        quarantine(raw)
    except Exception:
        return False
    diagnostics.log(provider + ".kept_for_review", "warning", account=ref)
    return True


def fingerprint(profile):
    """sha256[:8] of a profile's refresh token, safe to log across a rotation; None when blank."""
    token = ((profile or {}).get("tokens") or {}).get("refresh_token") if isinstance(profile, dict) else None
    return hashlib.sha256(token.encode()).hexdigest()[:8] if isinstance(token, str) and token else None


def daemon_settings(codex_home, default=Timings.daemon_grace_default):
    """shutdownGraceSeconds from <codex_home>\\app-server-daemon\\settings.json, clamped to 0-300."""
    try:
        value = read_json(Path(codex_home) / "app-server-daemon" / "settings.json").get("shutdownGraceSeconds")
    except (OSError, ValueError, AttributeError):
        value = None
    return float(default) if _number(value) is None else float(min(300, max(0, value)))


@dataclass(frozen=True)
class ActiveIdentity:
    status: str                   # "absent" | "unknown" | "ok"
    identity: str | None = None
    reason: str | None = None     # for "unknown": "store" | "unreadable" | "partial" | "api_key"


def classify_rpc_error(error):
    """A ProviderError for a JSON-RPC error object {code, message, data} from the app-server."""
    error = error if isinstance(error, dict) else {}
    code, data = error.get("code"), error.get("data")
    info = data.get("codexErrorInfo") if isinstance(data, dict) else None
    if isinstance(info, dict):
        info = next(iter(info), None)
    info = info if isinstance(info, str) else None
    text = (str(error.get("message", "")) + " " + json.dumps(data, default=str)[:2000]).lower()
    retry = None
    if any(name in text for name in REFRESH_REJECTED):
        kind = "refresh_rejected"
    elif info == "unauthorized":
        kind = "unauthorized"
    elif info == "httpConnectionFailed":
        kind = "network"
    elif info is None and code == -32603 and "401" in text:
        kind = "needs_attention"
    elif info is None and any(hint in text for hint in ("401", "unauthorized", "refresh token", "not logged",
                                                        "authentication")):
        kind = "unauthorized"
    elif "429" in text or "rate limit" in text:
        kind, retry = "rate_limited", RATE_LIMIT_MARGIN
    else:
        kind = "codex"
    detail = info or (str(code) if isinstance(code, int) else None)
    klass = {"network": "network", "rate_limited": "429", "codex": "other"}.get(kind, "4xx")
    return ProviderError(kind, retry_after=retry, detail=detail, meta={"class": klass, "rpc_code": code})


class RPC:
    """One Codex app-server over stdio in `home`, spawned through `procs.spawn_helper` (§4.1).

    close() closes stdin and waits `grace` seconds for the helper to exit on its own (30 s in
    refresh mode, so a rotation in flight can finish writing auth.json; 3 s read-only) before it
    terminates the helper. `monotonic`/`sleep` drive only that wait."""

    def __init__(self, command, home, *, procs, grace, monotonic=time.monotonic, sleep=time.sleep):
        self.grace, self.monotonic, self.sleep = grace, monotonic, sleep
        self.seq = 0
        self.messages = queue.Queue()
        self.notifications = []
        self.process = procs.spawn_helper(list(command) + ["app-server", "--stdio"], home=home, cwd=str(home),
                                          env={"CODEX_HOME": str(home)}, stdin=PIPE, stdout=PIPE,
                                          stderr=DEVNULL, text=True, encoding="utf-8")
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        try:
            self.call("initialize", {"clientInfo": {"name": "accountdeck", "version": __version__},
                                     "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
        except (OSError, ValueError):
            pass
        finally:
            self.messages.put({"closed": True})

    def send(self, message):
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise ProviderError("codex", message="Codex helper closed unexpectedly.") from None

    def call(self, method, params=None, timeout=25):
        self.seq += 1
        request_id = self.seq
        self.send({"id": request_id, "method": method, "params": params or {}})
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                message = self.messages.get(timeout=max(0.01, end - time.monotonic()))
            except queue.Empty:
                break
            if not isinstance(message, dict):
                continue
            if message.get("closed"):
                self.messages.put(message)
                raise ProviderError("codex", message="Codex helper exited. Try refreshing again.")
            if message.get("id") == request_id and "method" not in message:
                if message.get("error"):
                    raise classify_rpc_error(message["error"])
                return message.get("result") or {}
            if "method" in message and "id" in message:
                # This monitor never approves anything the helper asks for.
                self.send({"id": message["id"], "error": {"code": -32601, "message": "This monitor does not approve actions."}})
            else:
                self.notifications.append(message)
        raise ProviderError("network", detail="timeout", meta={"class": "timeout"},
                            message="Codex took too long to respond. Your last reading is kept.")

    def close(self):
        process = self.process
        if process.poll() is None:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            if not self._wait(self.grace):
                diagnostics.log("codex.helper_terminated", "warning", grace=self.grace)
                process.terminate()
                if not self._wait(KILL_WAIT):
                    process.kill()
                    self._wait(KILL_WAIT)
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _wait(self, seconds):
        end = self.monotonic() + seconds
        while self.process.poll() is None:
            if self.monotonic() >= end:
                return False
            self.sleep(0.05)
        return True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


class Codex:
    """Codex CLI adapter. `procs` is the process controller (only probes need it). `sleep` and
    `monotonic` pace the identity retries and the helper's close grace."""
    provider = "codex"

    def __init__(self, paths, procs=None, *, timings=None, clock=time.time, sleep=time.sleep,
                 monotonic=time.monotonic):
        self.paths, self.procs = paths, procs
        self.timings = timings or Timings()
        self.clock, self.sleep, self.monotonic = clock, sleep, monotonic

    @property
    def codex_home(self):
        return Path(self.paths.codex_home)

    @property
    def workspace(self):
        return Path(self.paths.data_dir) / "work"

    def executable(self):
        """The Codex CLI to run, or None when it is not installed: `paths.codex_exe` when configured
        (synthetic paths), else the native binary facts_mac.codex_exe finds now (PLAN §1.8)."""
        found = self.paths.codex_exe
        if found is None:
            try:
                found = facts_mac.codex_exe(self.paths)
            except DeckError:
                found = None
        return found

    def command(self):
        found = self.executable()
        if found is None:
            raise DeckError(NOT_INSTALLED_MESSAGE, "not_installed")
        return [str(found)]

    @staticmethod
    def identity(profile):
        tokens = profile.get("tokens") if isinstance(profile, dict) else None
        tokens = tokens if isinstance(tokens, dict) else {}
        claim = jwt_claims(tokens.get("id_token") or tokens.get("access_token"))
        access = jwt_claims(tokens.get("access_token"))
        auth = claim.get(AUTH_CLAIMS) or access.get(AUTH_CLAIMS) or {}
        auth = auth if isinstance(auth, dict) else {}
        ident = tokens.get("account_id") or auth.get("chatgpt_account_id")
        if not ident or not isinstance(ident, str):
            raise DeckError("Codex subscription identity is missing. Sign in with ChatGPT.", "login_needed")
        return {"identity": ident, "email": claim.get("email") or access.get("email", ""),
                "plan": auth.get("chatgpt_plan_type", "unknown"), "org": ""}

    def validate_store(self):
        # Pinned Codex config/src/loader: the user's config and both system layers can choose the auth
        # store. Refuse any non-file policy even if another layer overrides it. MDM-managed preferences
        # are out of scope; they are not represented by these TOML files.
        for config in (self.codex_home / "config.toml", *self.paths.codex_managed_configs):
            try:
                mode = tomllib.loads(config.read_text(encoding="utf-8")).get("cli_auth_credentials_store", "file")
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                message = (f"Codex's {config.name} could not be read. File switching is disabled.")
                raise DeckError(message, "unsupported") from None
            if mode != "file":
                raise DeckError(_store_message(mode, config), "unsupported")

    def current(self):
        self.validate_store()
        try:
            result = read_json(self.codex_home / "auth.json")
        except (OSError, ValueError):
            raise DeckError("Sign in to Codex CLI first, or add a new account here.", "login_needed") from None
        self.identity(result)
        return result

    def active_identity(self):
        """Who is signed in to Codex: absent (no auth.json), ok(identity), or unknown (a file that
        stays unreadable or partial over 3 retries 50 ms apart, API-key mode, or a non-file store)."""
        try:
            self.validate_store()
        except DeckError:
            return ActiveIdentity("unknown", reason="store")
        path = self.codex_home / "auth.json"
        reason = "unreadable"
        for attempt in range(IDENTITY_RETRIES + 1):
            if attempt:
                self.sleep(IDENTITY_RETRY_DELAY)
            try:
                auth = json.loads(path.read_bytes())
            except FileNotFoundError:
                return ActiveIdentity("absent")
            except (OSError, ValueError):
                reason = "unreadable"
                continue
            if not isinstance(auth, dict):
                reason = "unreadable"
                continue
            if str(auth.get("auth_mode", "")).lower() in ("apikey", "api_key") or (
                    auth.get("OPENAI_API_KEY") and not auth.get("tokens")):
                return ActiveIdentity("unknown", reason="api_key")
            ident = identity_key(auth)
            if ident is not None:
                return ActiveIdentity("ok", ident)
            reason = "partial"
        return ActiveIdentity("unknown", reason=reason)

    def setup_home(self, path, profile=None, refresh=False):
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        atomic_write(path / "config.toml", PROBE_CONFIG)
        if profile:
            value = json.loads(json.dumps(profile))
            if not refresh and isinstance(value.get("tokens"), dict):
                # Active official clients own refresh rotation. This disposable probe gets no usable
                # refresh token, so it cannot invalidate their copy.
                value["tokens"]["refresh_token"] = ""
            atomic_write(path / "auth.json", json_bytes(value))

    def usage(self, profile, *, mode="read_only", owner="v2", persist=None, quarantine=None, account=None):
        """One probe of a saved auth.json through a private app-server (§5.11). Provider failures
        come back in the Reading; local ones (not installed, a refused refresh, a rotation that
        could not be stored) raise DeckError.

        read_only (always for the active account): blank refresh token, account/read{refreshToken:
        false}. refresh (owner "v2" only): the intact profile and {refreshToken: true}; a rotated
        auth.json goes to `persist` (Vault.put_guarded with `expected=profile`, the copy the renewal
        started from). One that cannot be read or that `persist` did not store goes to `quarantine`
        (Vault.quarantine), and the helper folder is deleted only after one of the two holds it."""
        if mode not in MODES:
            raise ValueError(f"unknown Codex probe mode: {mode}")
        refresh = mode == "refresh"
        identity = self.identity(profile)["identity"]
        ref = diagnostics.account_ref(account or identity)
        if refresh and owner != "v2":
            diagnostics.log("codex.refresh_refused", "warning", account=ref, owner=owner)
            raise DeckError("AccountDeck does not manage this Codex sign-in here, so it only reads it.",
                            "read_only")
        if refresh and persist is None:
            raise ValueError("refresh mode needs persist")
        now = self.clock()
        tokens = token_state(profile, now)
        self.workspace.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="probe-", dir=self.workspace))
        keep = False
        try:
            self.setup_home(work, profile, refresh)
            try:
                rates, used = self._probe(work, identity, refresh)
                reading = Reading(usage=codex_usage(rates, used, now=now), source="helper",
                                  outcomes=(("helper", "ok", "2xx"),), checked=now, meta={"mode": mode})
            except ProviderError as exc:
                reading = self._failed(exc, refresh, tokens, profile, now)
            finally:
                if refresh:
                    try:
                        settled = self._settle(work, profile, identity, persist, quarantine, ref)
                    except BaseException:
                        keep = not stash(work / "auth.json", profile, quarantine, "codex", ref)
                        raise
            if refresh:
                reading = replace(reading, meta={**reading.meta, **settled})
        finally:
            if keep:
                diagnostics.log("codex.helper_home_kept", "warning", account=ref)
            else:
                facts_mac.remove_work_directory(work, self.workspace)
        diagnostics.log("codex.read", account=ref, mode=mode, remaining=None if tokens.remaining is None
                        else round(tokens.remaining), outcome=reading.outcomes[0][1] if reading.outcomes else None,
                        credential=reading.credential)
        return reading

    def _probe(self, work, identity, refresh):
        """(account/rateLimits/read result, account/usage/read result or None) from a helper in `work`."""
        if self.procs is None:
            raise RuntimeError("Codex probes need a process controller.")
        grace = self.timings.helper_refresh_grace if refresh else self.timings.helper_readonly_grace
        with RPC(self.command(), work, procs=self.procs, grace=grace, monotonic=self.monotonic,
                 sleep=self.sleep) as rpc:
            found = (rpc.call("account/read", {"refreshToken": refresh}) or {}).get("account")
            if found is None:        # Codex dropped a sign-in it could not renew (see the module docstring)
                raise ProviderError("unauthorized", detail="no_account", meta={"class": "4xx"})
            if not isinstance(found, dict) or found.get("type") != "chatgpt":
                raise ProviderError("identity", message="This is not a Codex subscription login.")
            rates = rpc.call("account/rateLimits/read")
            if rates.get("accountId") and rates["accountId"] != identity:
                raise ProviderError("identity", message="Codex returned a different account. No switch was made.")
            try:
                used = rpc.call("account/usage/read", timeout=12)
            except ProviderError:
                used = None
            return rates, used

    def _failed(self, exc, refresh, tokens, profile, now):
        """The Reading for a failed probe, applying the §5.11 rules for 401-class answers."""
        auth_error = exc.kind in ("unauthorized", "needs_attention", "refresh_rejected")
        meta = {"mode": "refresh" if refresh else "read_only"}
        if not refresh and auth_error and (tokens.remaining is None or tokens.remaining < ACCESS_EXPIRY_MARGIN):
            return Reading(usage=None, source="helper", outcomes=(("helper", "access_expired", exc.http_class),),
                           credential="access_expired", checked=now, meta=meta)
        intact = bool(((profile.get("tokens") or {}).get("refresh_token")))
        if refresh and intact and exc.kind == "unauthorized":
            exc = ProviderError("refresh_rejected", detail=exc.detail, meta=exc.meta)
        elif not refresh and exc.kind == "refresh_rejected":
            exc = ProviderError("unauthorized", detail=exc.detail, meta=exc.meta)
        return Reading(usage=None, source="helper", error=exc, outcomes=(("helper", exc.kind, exc.http_class),),
                       credential="reauth_required" if exc.kind == "refresh_rejected" else None,
                       checked=now, meta=meta)

    def _settle(self, work, before, identity, persist, quarantine, ref):
        """Hands a refresh probe's auth.json to `persist` when the helper rotated it, or its raw
        bytes to `quarantine` when it cannot be read or `persist` kept its own copy. Returns meta for the
        Reading. A copy for another account, or a "conflict" from `persist`, raises DeckError, and usage()
        hands the copy to `quarantine` (stash)."""
        path = Path(work) / "auth.json"
        try:
            latest = read_json(path)
            latest_identity = self.identity(latest)["identity"]
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, DeckError):
            if quarantine is None:
                raise DeckError("Codex renewed this sign-in, but the new copy could not be read.", "vault") from None
            record = quarantine(self.quarantine(path))
            diagnostics.log("codex.quarantined", "warning", account=ref)
            return {"needs_review": record}
        if latest_identity != identity:
            raise DeckError("Codex changed identity while refreshing. Saved profile was preserved.", "identity")
        if latest == before:
            return {}
        fp_before, fp_after = fingerprint(before), fingerprint(latest)
        stored = persist(latest)
        if stored == "conflict":
            diagnostics.log("codex.conflict", "warning", account=ref)
            raise DeckError("Codex renewed this sign-in, but the saved copy belongs to another account. "
                            "The new copy was kept for review.", "identity")
        stored = stored if isinstance(stored, str) else None      # put_guarded's "accepted" / "kept"
        diagnostics.log("codex.rotation", account=ref, fp_before=fp_before, fp_after=fp_after, stored=stored)
        meta = {"rotated": True, "fp_before": fp_before, "fp_after": fp_after, "stored": stored}
        if stored == "kept":
            # The vault holds another copy than the one this renewal started from, yet Codex issued this
            # one: an expiry does not say which is newer, so it is never dropped.
            if quarantine is None:
                raise DeckError("Codex renewed this sign-in, but the new copy could not be stored.", "vault")
            meta["needs_review"] = quarantine(self.quarantine(path))
        return meta

    @staticmethod
    def quarantine(path):
        """The raw bytes of an unreadable credential file, for Vault.quarantine (A9)."""
        return Path(path).read_bytes()

    def restore(self, profile, tx):
        """Writes a saved auth.json through `tx`. Waiting for consumers, the daemon and verification
        are the engine's steps (§5.5); this refuses non-file stores and incomplete profiles."""
        self.validate_store()
        self.identity(profile)
        tx.write(self.codex_home / "auth.json", json_bytes(profile))

    def sessions(self):
        index = self.codex_home / "session_index.jsonl"
        if not index.exists():
            return []
        with index.open("rb") as stream:
            stream.seek(max(0, index.stat().st_size - 128 * 1024))
            raw = stream.read()
        found = {}
        for line in raw.splitlines():
            try:
                row = json.loads(line)
                sid = str(uuid.UUID(row.get("id", "")))
                found[sid] = {"id": sid, "title": str(row.get("thread_name") or "Conversation")[:160],
                              "updated": row.get("updated_at")}
            except (ValueError, TypeError, AttributeError):
                pass
        return list(found.values())[-20:][::-1]


def _store_message(mode, config):
    """Why a non-file Codex credential store blocks switching, and what to do instead."""
    shown = mode if isinstance(mode, str) and mode.isprintable() and len(mode) <= 40 else "?"
    template = MAC_KEYCHAIN_MESSAGE if shown.lower() in KEYCHAIN_STORES else MAC_STORE_MESSAGE
    return template.format(mode=shown, config=config)


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _timestamp(value):
    if _number(value) is not None:
        return value / 1000 if value > 100_000_000_000 else float(value)
    if isinstance(value, str):
        try:
            when = dt.datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return (when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)).timestamp()
    return None
