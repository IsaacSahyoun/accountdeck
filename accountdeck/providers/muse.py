"""Muse Code adapter (Meta's terminal coding agent): who is signed in, usage through `muse usage --json`,
and auth.json swaps. Checked against Muse 1.4.13-R6996.1 on 2026-10-26.

Where Muse keeps its login. `~/.config/muse/auth.json` (`$XDG_CONFIG_HOME/muse`) names the account under
`providers.meta` (mechanism, user_email, user_full_name...). With `storage: "keychain"`, the macOS default,
the tokens live in the login Keychain item "ai.meta.dev.credentials" / "meta", which only Meta's signed
muse binary reads without a password prompt, and every config folder shares that one item. Started with
TBH_CREDENTIAL_BACKEND=file, Muse ignores the Keychain and keeps the credentials in auth.json itself:
1.4.13 writes an access token and an API key there, with no storage field, refresh token or expiry. In
file mode it will not replace a Keychain login (FM-008): `muse logout` must run in Keychain mode first.

AccountDeck never reads or writes that Keychain item. Every saved Muse account is a file-mode login:
Add Account runs `muse login` in a private folder with TBH_CREDENTIAL_BACKEND=file. A saved account's
usage is read by running `muse usage --json` in a throw-away folder holding its auth.json, also in file
mode; Muse renews the token itself when it must, and the renewed auth.json goes back to the vault. The
signed-in account is read in place, in the mode its auth.json names, so Muse uses the copy it owns.
Saving the signed-in account and switching need a file-mode login: a Keychain login cannot be copied, and
replacing its auth.json would leave the Keychain one behind.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time

from .. import diagnostics
from ..config import Timings
from ..core import DeckError, atomic_write, json_bytes, read_json
from ..platform import facts_mac
from ..procs import DEVNULL
from ..usage import muse_plan, muse_usage
from .base import Reading
from .codex import ActiveIdentity, stash
from .errors import ProviderError

USAGE_TIMEOUT = 45.0
IDENTITY_RETRIES, IDENTITY_RETRY_DELAY = 3, 0.05
KILL_WAIT = 3.0
MAX_OUTPUT = 256 * 1024
FILE_BACKEND = {"TBH_CREDENTIAL_BACKEND": "file"}
# Never inherited by a Muse helper: each one gets exactly the folders and credential mode chosen here, and no
# variable that could send its sign-in elsewhere (TBH_AUTH_BASE_URL, TBH_CA_CERTIFICATE, MUSE_CUSTOM_HEADERS...).
CLEARED_PREFIXES = ("TBH_", "MUSE_", "META_")
CLEARED_NAMES = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
WORK_PREFIXES = ("muse-probe-", "muse-login-")
TOKEN_KEYS = ("access_token", "refresh_token", "api_key")
NOT_INSTALLED_MESSAGE = "Install Muse Code and sign in once, then try again."
SIGN_IN_MESSAGE = "Sign in to Muse first, or add an account here."
KEYCHAIN_MESSAGE = ("Muse keeps this sign-in in the macOS Keychain, which AccountDeck does not touch, so it can show "
                    "its usage but not save or switch it. To manage Muse accounts here, run "
                    "`env -u TBH_CREDENTIAL_BACKEND muse logout` (Muse will not replace a Keychain sign-in in file "
                    "mode), add `export TBH_CREDENTIAL_BACKEND=file` to ~/.zshrc, open a new terminal, run "
                    "`muse login`, then choose Save Current Account.")
API_KEY_MESSAGE = "Muse is signed in with an API key, not a Meta account, so there is nothing to save here."
REJECTED_MESSAGE = "Muse no longer accepts this saved sign-in. Choose Sign In Again."
KEYCHAIN_READ_MESSAGE = "Muse could not read its sign-in from the macOS Keychain. Open Muse once, then refresh."
SUBSCRIPTION_MESSAGE = "This Meta account has no active Muse Code subscription or payment method."
TIMEOUT_MESSAGE = "Muse took too long to report usage. Your last reading is kept."
UNREADABLE_MESSAGE = "Muse could not report usage right now. Your last reading is kept."
# Muse 1.4.13's own words for a sign-in it no longer has or Meta refused. "run `muse login`" alone is no sign:
# Muse adds it to retryable failures too ("If the problem continues, run `muse login`").
REJECTED_HINTS = ("not logged in", "no meta credentials were found", "stored login is no longer valid",
                  "still unauthorized after a token refresh", "login is no longer saved", "no login to refresh")
SUBSCRIPTION_HINTS = ("payment method", "subscribe at", "finish onboarding")
NETWORK_HINTS = ("network", "connection", "connect", "timed out", "timeout", "dns", "unreachable", "offline")
CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b")
URL = re.compile(r"https://\S+")


def meta_block(doc):
    """`providers.meta` of an auth.json document, or None."""
    providers = doc.get("providers") if isinstance(doc, dict) else None
    meta = providers.get("meta") if isinstance(providers, dict) else None
    return meta if isinstance(meta, dict) else None


def identity_key(profile):
    """The account a Muse auth.json belongs to (its Meta email, case-folded), or None."""
    meta = meta_block(profile)
    email = meta.get("user_email") if meta else None
    return email.strip().casefold() if isinstance(email, str) and "@" in email and email.strip() else None


def expiry(profile):
    """When the access token in an auth.json runs out (expires_at as stored: seconds or milliseconds), or None."""
    meta = meta_block(profile) or {}
    value = meta.get("expires_at")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) \
        else None


def unordered(profile):
    """Vault.put_guarded's freshness for Muse: none. expires_at says when an access token runs out, not which
    copy is newer (an early renewal can shorten it), so the signed-in file wins over its saved copy unless its
    refresh token was already replaced (AccountService._put_muse), and a copy a usage read renewed wins only
    over the one it started from (put_guarded's `expected`)."""
    return None


def expires_in(profile, now):
    """Seconds until the saved access token expires (expires_at in seconds or milliseconds), or None."""
    value = expiry(profile)
    if value is None:
        return None
    return (value / 1000 if value > 100_000_000_000 else value) - now


def cleared_env():
    """The variables a Muse helper must not inherit, as this process's environment has them now."""
    return CLEARED_NAMES + tuple(name for name in os.environ if name.upper().startswith(CLEARED_PREFIXES))


def spent_mark(profile):
    """A short one-way mark of `profile`'s credentials (TOKEN_KEYS; the store never holds them), or None when
    it has none."""
    meta = meta_block(profile) or {}
    values = [meta[key] if isinstance(meta.get(key), str) else "" for key in TOKEN_KEYS]
    return hashlib.sha256("\0".join(values).encode()).hexdigest()[:24] if any(values) else None


def without_refresh(profile):
    """A copy of `profile` whose refresh token is blank: Muse can use its access token but cannot renew it."""
    copy = json.loads(json.dumps(profile))
    if "refresh_token" in copy["providers"]["meta"]:
        copy["providers"]["meta"]["refresh_token"] = ""
    return copy


def is_file_login(profile):
    """True for an auth.json that holds its own Meta-account credentials: not Keychain-referenced, and a
    token in the file (1.4.13 writes access_token and api_key and no storage field)."""
    meta = meta_block(profile)
    return bool(meta) and meta.get("mechanism") in (None, "oauth") and meta.get("storage") in (None, "file") and any(
        isinstance(meta.get(key), str) and meta[key] for key in TOKEN_KEYS)


class Muse:
    """Muse Code adapter. `procs` is the process controller (only helpers need it)."""
    provider = "muse"

    def __init__(self, paths, procs=None, *, timings=None, clock=time.time, sleep=time.sleep):
        self.paths, self.procs = paths, procs
        self.timings = timings or Timings()
        self.clock, self.sleep = clock, sleep

    @property
    def muse_config(self):
        return Path(self.paths.muse_config)

    @property
    def auth_path(self):
        return self.muse_config / "auth.json"

    @property
    def workspace(self):
        return Path(self.paths.data_dir) / "work"

    def executable(self):
        """The Muse binary to run, or None when Muse is not installed."""
        try:
            return facts_mac.muse_exe(self.paths)
        except DeckError:
            return None

    def command(self):
        found = self.executable()
        if found is None:
            raise DeckError(NOT_INSTALLED_MESSAGE, "not_installed")
        return [str(found)]

    # ---- who is signed in ----
    @staticmethod
    def identity(profile):
        ident = identity_key(profile)
        if ident is None:
            raise DeckError("This Muse sign-in has no Meta account email. Sign in again.", "login_needed")
        meta = meta_block(profile)
        return {"identity": ident, "email": meta["user_email"].strip(), "plan": "", "org": ""}

    def live_document(self):
        """The signed-in auth.json, or None when there is none."""
        try:
            doc = read_json(self.auth_path)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise DeckError("Muse's auth.json could not be read.", "login_needed") from None
        if not isinstance(doc, dict):
            raise DeckError("Muse's auth.json could not be read.", "login_needed")
        return doc

    def active_identity(self):
        """Who is signed in to Muse: absent (no auth.json), ok(identity), or unknown (a file that stays
        unreadable or partial over 3 retries 50 ms apart, or an API-key login)."""
        reason = "unreadable"
        for attempt in range(IDENTITY_RETRIES + 1):
            if attempt:
                self.sleep(IDENTITY_RETRY_DELAY)
            try:
                doc = json.loads(self.auth_path.read_bytes())
            except FileNotFoundError:
                return ActiveIdentity("absent")
            except (OSError, ValueError):
                reason = "unreadable"
                continue
            meta = meta_block(doc)
            if meta is None:
                reason = "partial"
                continue
            if meta.get("mechanism") not in (None, "oauth"):
                return ActiveIdentity("unknown", reason="api_key")
            ident = identity_key(doc)
            if ident is not None:
                return ActiveIdentity("ok", ident)
            reason = "partial"
        return ActiveIdentity("unknown", reason=reason)

    def validate_switchable(self):
        """Refuses a swap that would not take effect or would lose a login: a Keychain or API-key login."""
        doc = self.live_document()
        if doc is None:
            return
        meta = meta_block(doc)
        if meta is not None and meta.get("mechanism") not in (None, "oauth"):
            raise DeckError(API_KEY_MESSAGE, "unsupported")
        if meta is not None and meta.get("storage") not in (None, "file"):
            raise DeckError(KEYCHAIN_MESSAGE, "unsupported")

    def current(self):
        """The signed-in file-mode auth.json (with its tokens), for saving it."""
        doc = self.live_document()
        if doc is None:
            raise DeckError(SIGN_IN_MESSAGE, "login_needed")
        self.validate_switchable()
        if not is_file_login(doc):
            raise DeckError("Muse has not finished signing in. Run `muse login` again, then save.", "login_needed")
        self.identity(doc)
        return doc

    # ---- usage ----
    def usage(self, profile=None, *, live=False, persist=None, quarantine=None, account=None, read_only=False):
        """One `muse usage --json`. `live`: the signed-in login, in place and in the mode its auth.json
        names. Otherwise `profile` (a saved file-mode auth.json) in a throw-away folder; a renewed auth.json
        goes to `persist` (Vault.put_guarded), an unreadable one to `quarantine`, and the folder is deleted
        only after that. `read_only`: the copy goes without its refresh token, so Muse cannot renew it, and
        nothing is stored. Provider failures come back in the Reading; local ones raise DeckError."""
        now = self.clock()
        if live:
            doc = self.live_document()
            if doc is None:
                raise DeckError(SIGN_IN_MESSAGE, "login_needed")
            ident = identity_key(doc)
            ref = diagnostics.account_ref(account or ident)
            # Keychain mode only for a login marked so: file mode never touches the Keychain (at worst it fails)
            file_mode = (meta_block(doc) or {}).get("storage") in (None, "file")
            self.workspace.mkdir(parents=True, exist_ok=True)
            reading = self._read(self.muse_config.parent, file_mode=file_mode, isolated=False, now=now)
            diagnostics.log("muse.read", account=ref, mode="live", storage="file" if file_mode else "keychain",
                            outcome=reading.outcomes[0][1] if reading.outcomes else None)
            return reading
        if not is_file_login(profile):
            raise DeckError("This saved Muse sign-in is incomplete. Sign in again.", "login_needed")
        identity = self.identity(profile)["identity"]
        ref = diagnostics.account_ref(account or identity)
        self.workspace.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="muse-probe-", dir=self.workspace))
        keep, settled = False, {}
        try:
            self.setup_home(work, without_refresh(profile) if read_only else profile)
            try:
                reading = self._read(work, file_mode=True, isolated=True, now=now)
            finally:
                if not read_only:
                    try:
                        settled = self._settle(work, profile, identity, persist, quarantine, ref)
                    except BaseException:
                        keep = not stash(work / "muse" / "auth.json", profile, quarantine, "muse", ref)
                        raise
            if settled:
                reading = Reading(usage=reading.usage, source=reading.source, error=reading.error,
                                  outcomes=reading.outcomes, credential=reading.credential, checked=reading.checked,
                                  meta={**reading.meta, **settled})
        finally:
            if keep:
                diagnostics.log("muse.helper_home_kept", "warning", account=ref)
            else:
                facts_mac.remove_work_directory(work, self.workspace)
        diagnostics.log("muse.read", account=ref, mode="saved",
                        outcome=reading.outcomes[0][1] if reading.outcomes else None,
                        rotated=bool(settled.get("rotated")))
        return reading

    def setup_home(self, path, profile):
        """A private XDG_CONFIG_HOME holding `profile` as muse/auth.json."""
        atomic_write(Path(path) / "muse" / "auth.json", json_bytes(json.loads(json.dumps(profile))))

    def _env(self, config_home, *, file_mode, isolated):
        env = {"XDG_CONFIG_HOME": str(config_home), "HOME": str(self.paths.home)}
        if file_mode:
            env.update(FILE_BACKEND)
        if isolated:
            env.update(XDG_DATA_HOME=str(Path(config_home) / "data"), XDG_STATE_HOME=str(Path(config_home) / "state"),
                       XDG_CACHE_HOME=str(Path(config_home) / "cache"))
        return env

    def _read(self, config_home, *, file_mode, isolated, now):
        """The Reading of one `muse usage --json` run with `config_home` as XDG_CONFIG_HOME."""
        try:
            data = self._run_usage(config_home, file_mode=file_mode, isolated=isolated)
        except ProviderError as exc:
            credential = "reauth_required" if exc.kind == "refresh_rejected" else None
            return Reading(usage=None, source="helper", error=exc, outcomes=(("helper", exc.kind, exc.http_class),),
                           credential=credential, checked=now)
        allowance = data.get("allowance")
        if not isinstance(allowance, dict):
            exc = ProviderError("codex", detail="no_allowance", meta={"class": "other"},
                                message=UNREADABLE_MESSAGE)
            return Reading(usage=None, source="helper", error=exc, outcomes=(("helper", exc.kind, exc.http_class),),
                           checked=now)
        return Reading(usage=muse_usage(data, now=now), source="helper", outcomes=(("helper", "ok", "2xx"),),
                       checked=now, meta={"plan": muse_plan(data)})

    def _run_usage(self, config_home, *, file_mode, isolated):
        """The parsed JSON `muse usage --json` printed. Raises ProviderError for what Muse refused or
        could not do. Muse's own words are never logged or shown: they may name the account."""
        if self.procs is None:
            raise RuntimeError("Muse reads need a process controller.")
        # The working folder is AccountDeck's own: Muse never sees a project folder it might trust or index.
        # Output goes to unlinked files, not pipes: a child Muse leaves running would hold a pipe open, and the
        # read ends when Muse itself exits.
        with tempfile.TemporaryFile(dir=self.workspace) as stdout, tempfile.TemporaryFile(dir=self.workspace) as stderr:
            process = self.procs.spawn_helper(self.command() + ["usage", "--json"], cwd=str(self.workspace),
                                              env=self._env(config_home, file_mode=file_mode, isolated=isolated),
                                              remove_env=cleared_env(), stdin=DEVNULL, stdout=stdout, stderr=stderr)
            try:
                process.wait(timeout=USAGE_TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=KILL_WAIT)
                raise ProviderError("network", detail="timeout", meta={"class": "timeout"},
                                    message=TIMEOUT_MESSAGE) from None
            stdout.seek(0)
            out = stdout.read(MAX_OUTPUT)
            stderr.seek(0)
            err = stderr.read(MAX_OUTPUT)
        text = err.decode("utf-8", "replace").lower()
        if process.returncode != 0:
            raise classify_failure(process.returncode, text)
        try:
            data = json.loads(out)
        except ValueError:
            raise ProviderError("codex", detail="unreadable", meta={"class": "other"},
                                message=UNREADABLE_MESSAGE) from None
        if not isinstance(data, dict):
            raise ProviderError("codex", detail="unreadable", meta={"class": "other"}, message=UNREADABLE_MESSAGE)
        return data

    def _settle(self, work, before, identity, persist, quarantine, ref):
        """Hands a renewed auth.json to `persist`, or its raw bytes to `quarantine` when it cannot be read.
        Returns meta for the Reading. A copy for another account, or a "conflict" from `persist`, raises
        DeckError, and usage() hands the copy to `quarantine` (codex.stash)."""
        path = Path(work) / "muse" / "auth.json"
        try:
            latest = read_json(path)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            if quarantine is None:
                raise DeckError("Muse renewed this sign-in, but the new copy could not be read.", "vault") from None
            record = quarantine(path.read_bytes())
            diagnostics.log("muse.quarantined", "warning", account=ref)
            return {"needs_review": record}
        if not is_file_login(latest):       # Muse signed this copy out (a refused sign-in): the saved one stays
            return {}
        latest_identity = identity_key(latest)
        if latest_identity != identity:
            raise DeckError("Muse changed account while checking usage. The saved sign-in was kept.", "identity")
        if latest == before or persist is None:
            return {}
        stored = persist(latest)
        if stored == "conflict":
            diagnostics.log("muse.conflict", "warning", account=ref)
            raise DeckError("Muse renewed this sign-in, but the saved copy belongs to another account. "
                            "The new copy was kept for review.", "identity")
        if stored == "kept":
            # The vault kept its own copy, yet Muse changed this one (a token or key): never drop it.
            if quarantine is None:
                raise DeckError("Muse renewed this sign-in, but the new copy could not be stored.", "vault")
            record = quarantine(path.read_bytes())
            diagnostics.log("muse.kept_for_review", "warning", account=ref)
            return {"rotated": True, "stored": "kept", "needs_review": record}
        diagnostics.log("muse.renewed", account=ref, stored=stored if isinstance(stored, str) else None)
        return {"rotated": True, "stored": stored if isinstance(stored, str) else None}

    def sweep(self, quarantine):
        """Deletes the private folders an earlier run left behind (a crash during a usage read or a sign-in),
        handing any sign-in in them to `quarantine` (Vault.quarantine) first. Run only before any read starts."""
        try:
            folders = [entry for entry in self.workspace.iterdir() if entry.name.startswith(WORK_PREFIXES)]
        except FileNotFoundError:
            return 0
        for folder in folders:
            auth = folder / "muse" / "auth.json"
            if auth.is_file():
                quarantine("muse-leftover", auth.read_bytes())
            facts_mac.remove_work_directory(folder, self.workspace)
        if folders:
            diagnostics.log("muse.swept", "warning", count=len(folders))
        return len(folders)

    # ---- switching ----
    def restore(self, profile, tx):
        """Writes a saved file-mode auth.json through `tx`. Waiting for Muse sessions and verifying are the
        switch's own steps; this refuses a live Keychain login and an incomplete profile."""
        self.validate_switchable()
        if not is_file_login(profile):
            raise DeckError("This saved Muse sign-in is incomplete. Sign in again.", "login_needed")
        self.identity(profile)
        tx.write(self.auth_path, json_bytes(profile))


def classify_failure(code, text):
    """The ProviderError for a failed `muse usage` (exit `code`, lower-cased stderr `text`)."""
    if any(hint in text for hint in REJECTED_HINTS):
        return ProviderError("refresh_rejected", detail=f"exit_{code}", meta={"class": "4xx"}, message=REJECTED_MESSAGE)
    if any(hint in text for hint in SUBSCRIPTION_HINTS):
        return ProviderError("codex", detail=f"exit_{code}", meta={"class": "4xx"}, message=SUBSCRIPTION_MESSAGE)
    if "keychain" in text:
        return ProviderError("codex", detail=f"exit_{code}", meta={"class": "other"}, message=KEYCHAIN_READ_MESSAGE)
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return ProviderError("rate_limited", detail=f"exit_{code}", meta={"class": "429"})
    if any(hint in text for hint in NETWORK_HINTS):
        return ProviderError("network", detail=f"exit_{code}", meta={"class": "network"})
    return ProviderError("codex", detail=f"exit_{code}", meta={"class": "other"}, message=UNREADABLE_MESSAGE)


class LoginOutput:
    """Collects a `muse login` helper's output lines on a thread: the sign-in page and the code to confirm."""

    def __init__(self, stream):
        self.lines, self.url, self.code = [], None, None
        self.done = threading.Event()
        self._changed = threading.Condition()
        self.thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self.thread.start()

    def _read(self, stream):
        try:
            for line in stream:
                text = line.strip()
                with self._changed:
                    if len(self.lines) < 200:
                        self.lines.append(text)
                    found = URL.search(text)
                    if self.url is None and found:
                        self.url = found.group().rstrip(".,)")
                    code = CODE.search(text)
                    if self.code is None and code and not URL.search(text):
                        self.code = code.group()
                    self._changed.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            self.done.set()
            with self._changed:
                self._changed.notify_all()

    def wait_ready(self, timeout):
        """True once the page and the code are known; False when the output ended or the time ran out."""
        end = time.monotonic() + timeout
        with self._changed:
            while not (self.url and self.code):
                left = end - time.monotonic()
                if left <= 0 or self.done.is_set():
                    return bool(self.url and self.code)
                self._changed.wait(min(left, 0.25))
            return True
