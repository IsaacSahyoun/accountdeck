"""Saved accounts: saving the signed-in account, reading usage, and the signed-in account that is not
saved yet.

These are the Codex and Muse accounts; the Claude Code accounts are in cli_accounts. A capture of a known
account goes through `Vault.put_guarded`, so an older refresh token never replaces a newer one. Usage
reads never write a provider file, and the active account is read with the token its official app
holds, which a read cannot rotate. (Muse renews the active login itself, in its own file, as it would
for any `muse` command.)
"""
from __future__ import annotations

import contextlib
import threading

from . import diagnostics, intel, readings
from .contract import ERROR_CODES, PROVIDER_FAILURES, UNSAVED_IDS
from .core import DeckError, FrozenAccountError, is_frozen
from .providers import Reading
from .providers import muse as muse_auth
from .providers.codex import freshness, identity_key, refresh_due, token_state

READ_FAILED = "Could not refresh this account. Your last reading is kept."
BUSY_ACCOUNT = "This account is being refreshed. Try again in a moment."
FRESH_MARGIN = 180
SPENT_KEPT = 20            # replaced Muse refresh tokens remembered per account (_put_muse)
ACTIVE_TOUCH = 3600.0       # how often an active account's `last_active_at` is renewed
# The flags a usage read owns. It merges only these into the stored flags, so a save or a rename
# made while the read ran is never overwritten by the read's older copy.
READ_FLAGS = ("checked", "error_since", "refresh_attempt", "refresh_failures")


PROVIDER_NOUNS = {"codex": "Codex account", "muse": "Muse account"}
# The identity and freshness of a rotating credential (Vault.put_guarded), per file-swapped provider.
GUARDS = {"codex": (identity_key, freshness), "muse": (muse_auth.identity_key, muse_auth.unordered)}


def default_label(email, provider):
    local = (email or "").partition("@")[0].strip()
    return local or PROVIDER_NOUNS.get(provider, "Codex account")


def restorable(provider, profile) -> bool:
    """Whether a saved profile can be switched to: a Muse login has to be a file login."""
    return muse_auth.is_file_login(profile) if provider == "muse" else provider == "codex"


def error_code(exc) -> str:
    kind = getattr(exc, "kind", None)
    if kind in PROVIDER_FAILURES:
        return kind
    code = getattr(exc, "code", None)
    return code if code in ERROR_CODES else "network"


def _touch_due(account, now) -> bool:
    last = account["flags"].get("last_active_at")
    return last is None or now - last >= ACTIVE_TOUCH


class AccountService:
    """`observed` is the ObservedStatus per provider. `lock_for(account_id)` is the account's lock (the
    scheduler's): saving or removing an account waits up to `lock_wait` seconds for a read of it to finish.
    `on_change()` is called after the saved accounts changed (the engine moves its revision)."""

    def __init__(self, store, vault, providers, *, clock, observed, lock_for, lock_wait, on_change):
        self.store, self.vault, self.providers = store, vault, providers
        self.clock, self.observed = clock, observed
        self.lock_for, self.lock_wait, self.on_change = lock_for, lock_wait, on_change
        self.unsaved = {"codex": None, "muse": None}
        self._series = {}
        self._series_lock = threading.Lock()

    @contextlib.contextmanager
    def _locked(self, account_id):
        lock = self.lock_for(account_id)
        if not lock.acquire(timeout=self.lock_wait):
            raise DeckError(BUSY_ACCOUNT, "busy")
        try:
            yield
        finally:
            lock.release()

    # ---- saving ----
    def capture(self, provider, *, label=None, owner="v2") -> str:
        """Saves the account signed in right now; returns its id."""
        if provider not in GUARDS:
            raise DeckError("Choose Codex or Muse.", "invalid")
        adapter = self.providers[provider]
        profile = adapter.current()
        return self.save(provider, profile, adapter.identity(profile), label=label, source="capture", owner=owner)

    def save(self, provider, profile, info, *, label=None, source="capture", owner="v2") -> str:
        """Stores a captured profile under its identity (a new account or the saved one). An account whose
        sign-in belongs to another computer is never saved over (FrozenAccountError, kept as an event)."""
        existing = self.store.find(provider, info["identity"])
        if provider == "muse" and existing is not None and not info.get("plan"):
            info = {**info, "plan": existing["plan"]}     # Muse names the plan only in its usage answer
        if is_frozen(existing):
            diagnostics.log("capture.refused", lvl="warning", provider=provider, reason=existing["owner"],
                            acct=diagnostics.account_ref(existing["id"]))
            self.store.event("frozen", f"Did not save {existing['label']}: its sign-in belongs to another computer.")
            raise FrozenAccountError(existing)
        if existing is None:
            key = self.store.save_account(provider, info["identity"], label or default_label(info.get("email"), provider),
                                          info.get("email", ""), info.get("plan", ""), info.get("org", ""), owner=owner)
            try:
                digest = self.vault.put(key, profile)
            except BaseException:
                self.store.remove(key)
                raise
            self.store.record_generation(key, digest, source=source)
            self.store.update(key, flags={"restorable": restorable(provider, profile)})
            self._carry_over(key, provider, info["identity"])
        else:
            key = existing["id"]
            with self._locked(key):
                self._save_over(existing, provider, profile, info, source)
        with self._series_lock:
            self._series.pop(key, None)
        if self.unsaved.get(provider) and self.unsaved[provider]["identity"] == info["identity"]:
            self.unsaved[provider] = None
        diagnostics.log("capture.saved", provider=provider, acct=diagnostics.account_ref(key),
                        complete=restorable(provider, profile), new=existing is None)
        self.on_change()
        return key

    def _carry_over(self, key, provider, identity):
        """A newly saved account keeps the reading its unsaved row showed, so it never goes blank."""
        row = self.unsaved.get(provider)
        if row and row["identity"] == identity and row["usage"]:
            readings.record(self.store, key, row["usage"])
            self.store.update(key, last_outcome=row["last_outcome"] or "2xx")

    def _save_over(self, existing, provider, profile, info, source):
        """Replaces a saved account's profile with a newer capture of the same identity (under its lock)."""
        key = existing["id"]
        if is_frozen(self.store.account(key)):
            raise FrozenAccountError(existing)
        self.store.save_account(provider, info["identity"], existing["label"], info.get("email", ""),
                                info.get("plan", ""), info.get("org", "") or existing["org"] or "")
        stored = self._put_muse(key, profile, "live") if provider == "muse" else \
            self.vault.put_guarded(key, profile, "live", *GUARDS[provider])
        if stored == "conflict":
            name = "Muse" if provider == "muse" else "Codex"
            raise DeckError(f"The saved {name} sign-in belongs to another account. Nothing was changed.",
                            "identity")
        flags = dict((self.store.account(key) or existing)["flags"])
        flags["restorable"] = restorable(provider, profile)
        self.store.update(key, flags=flags)

    # ---- removing and renaming ----
    def rename(self, account_id, label):
        label = label.strip() if isinstance(label, str) else ""
        if not 1 <= len(label) <= 80:
            raise DeckError("Use a name of 1 to 80 characters.", "invalid")
        if self.store.account(account_id) is None:
            raise DeckError("That account no longer exists.", "not_found")
        self.store.update(account_id, label=label)
        self.on_change()

    def remove(self, account_id):
        account = self.store.account(account_id)
        if account is None:
            raise DeckError("That account no longer exists.", "not_found")
        with self._locked(account_id):
            self.vault.remove(account_id)
            self.store.remove(account_id)
        with self._series_lock:
            self._series.pop(account_id, None)
        self.store.event("remove", f"Removed {account['label']}.")
        diagnostics.log("account.removed", acct=diagnostics.account_ref(account_id), provider=account["provider"])
        self.on_change()

    # ---- reading ----
    def read(self, account_id, *, active_identity, read_only=False) -> str:
        with self._locked(account_id):
            return self._read(account_id, active_identity=active_identity, read_only=read_only)

    def _read(self, account_id, *, active_identity, read_only=False) -> str:
        """Reads one account (a saved id or an unsaved row id) and records the outcome; returns
        "ok", "rate_limited", "outage" or another failure kind."""
        if account_id in UNSAVED_IDS.values():
            return self.read_unsaved(account_id.removeprefix("unsaved-"))
        account = self.store.account(account_id)
        if account is None:
            return "not_found"
        if is_frozen(account):       # another computer's sign-in: never read or renewed here
            return "skipped"
        now = self.clock()
        flags = dict(account["flags"])
        flags["checked"] = now
        active = active_identity.get(account["provider"]) == account["identity"]
        try:
            profile = self.vault.get(account_id)
            if account["provider"] == "muse":
                reading, credential = self._read_muse(account, profile, flags, active=active, now=now,
                                                      read_only=read_only)
            else:
                reading, credential = self._read_codex(account, profile, flags, active=active, now=now,
                                                       read_only=read_only)
        except DeckError as exc:
            self._failed(account, flags, exc, now)
            return error_code(exc)
        expires_in = flags.pop("expires_in", None)
        fields = {"credential_state": credential, "expires_at": None if expires_in is None else now + expires_in}
        plan = reading.meta.get("plan")
        if plan and account["provider"] == "muse" and plan != account["plan"]:
            fields["plan"] = plan
        if reading.usage is not None:
            readings.record(self.store, account_id, reading.usage)
            flags.pop("error_since", None)
            self.store.update(account_id, flags=self._flags(account_id, flags), last_outcome="2xx",
                              paused_until=None, **fields)
            return "ok"
        if reading.error is None and reading.meta.get("hollow"):   # answered without new numbers
            flags.pop("error_since", None)
            self.store.update(account_id, flags=self._flags(account_id, flags), last_outcome="2xx", error=None,
                              error_code=None, paused_until=None, **fields)
            return "ok"
        self.store.update(account_id, flags=self._flags(account_id, flags), **fields)
        if reading.error is None:        # no request was made (a sign-in to renew, a route skipped)
            return "skipped"
        return self._failed(self.store.account(account_id), flags, reading.error, now,
                            http=reading.outcomes[-1][2] if reading.outcomes else None)

    def _flags(self, account_id, flags):
        """The stored flags with this read's own keys (READ_FLAGS) laid over them."""
        current = dict((self.store.account(account_id) or {"flags": {}})["flags"])
        for key in READ_FLAGS:
            if key in flags:
                current[key] = flags[key]
            else:
                current.pop(key, None)
        return current

    def _failed(self, account, flags, exc, now, http=None):
        code = error_code(exc)
        message = str(exc) if isinstance(exc, DeckError) and str(exc) else READ_FAILED
        if account["error_code"] != code or "error_since" not in flags:
            flags["error_since"] = now
        fields = {"error": message[:300], "error_code": code, "flags": self._flags(account["id"], flags)}
        if http:
            fields["last_outcome"] = http
        if code == "rate_limited":
            fields["paused_until"] = now + float(getattr(exc, "retry_after", None) or 900)
        self.store.update(account["id"], **fields)
        diagnostics.log("refresh.failed", lvl="info", acct=diagnostics.account_ref(account["id"]),
                        provider=account["provider"], code=code)
        return code

    def _record(self, provider, account_id, outcomes, now):
        watcher = self.observed.get(provider)
        if watcher is not None:
            for _route, _kind, klass in outcomes:
                watcher.record(account_id, klass, at=now)

    def _read_codex(self, account, profile, flags, *, active, now, read_only):
        codex = self.providers["codex"]
        mode = "read_only"
        if active:
            try:
                live = codex.current()
            except DeckError:
                live = None
            if live is not None and identity_key(live) == account["identity"]:
                profile = live
        elif not read_only and refresh_due(
                token_state(profile, now), owner=account["owner"], last_attempt=flags.get("refresh_attempt"),
                now=now, failures=int(flags.get("refresh_failures") or 0)):
            mode = "refresh"
            flags["refresh_attempt"] = now
        key = account["id"]
        reading = codex.usage(profile, mode=mode, owner=account["owner"],
                              persist=lambda auth: self.vault.put_guarded(key, auth, "probe", identity_key, freshness,
                                                                          expected=profile),
                              quarantine=lambda raw: self.vault.quarantine(key, raw), account=key)
        if mode == "refresh":
            flags["refresh_failures"] = 0 if reading.meta.get("rotated") or reading.usage is not None \
                else int(flags.get("refresh_failures") or 0) + 1
        self._record("codex", key, reading.outcomes, now)
        if active:
            credential = "live"
        elif reading.credential:
            credential = reading.credential
        else:
            remaining = token_state(profile, now).remaining
            flags["expires_in"] = None if remaining is None else round(remaining)
            credential = "fresh" if remaining is not None and remaining > FRESH_MARGIN else "expired"
        if active:
            remaining = token_state(profile, now).remaining
            flags["expires_in"] = None if remaining is None else round(remaining)
        return reading, credential

    def _read_muse(self, account, profile, flags, *, active, now, read_only=False):
        """The active account is read in place (Muse renews its own file, and that newer copy goes back to the
        vault); a saved one in a private folder, where a renewed sign-in goes back to the vault through
        put_guarded. `read_only` (another AccountDeck 2 owns the sign-ins): saved copies go to Muse without their
        refresh token and nothing is stored; the signed-in login is still read in place, like any `muse usage`
        run, and Muse may renew it there."""
        muse = self.providers["muse"]
        key = account["id"]
        live = muse.active_identity()
        signed_in = live.status == "ok" and live.identity == account["identity"]
        if active and not signed_in:
            # Muse's file cannot be read right now. The saved copy may hold the refresh token Muse itself
            # uses, so it is not renewed here; the next read tries again.
            return Reading(usage=None, checked=now), "live"
        active = signed_in
        remaining = muse_auth.expires_in(profile, now)
        if active:
            reading = muse.usage(live=True, account=key)
            if not read_only:
                self.keep_live_muse(key, account["identity"])
        elif read_only and remaining is not None and remaining < FRESH_MARGIN:
            return Reading(usage=None, checked=now), "expired"     # only a renewal could read it
        elif read_only:
            reading = muse.usage(profile, read_only=True, account=key)
            if reading.credential == "reauth_required":
                # Muse wanted to renew, and this copy carries no refresh token: the sign-in itself may be fine
                return Reading(usage=None, checked=now), "expired"
        else:
            reading = muse.usage(profile, persist=lambda auth: self._put_muse(key, auth, "probe", expected=profile),
                                 quarantine=lambda raw: self.vault.quarantine(key, raw), account=key)
            if reading.meta.get("rotated"):
                try:
                    profile = self.vault.get(key)
                except DeckError:
                    pass
        self._record("muse", key, reading.outcomes, now)
        remaining = muse_auth.expires_in(profile, now)
        flags["expires_in"] = None if remaining is None or active else round(remaining)
        if active:
            return reading, "live"
        if reading.credential:
            return reading, reading.credential
        # Muse renews an expired access token with the refresh token during the read, so only a refused
        # sign-in (reauth_required, above) needs the user. A login with no expiry (1.4.13) never runs out.
        fresh = reading.usage is not None or remaining is None or remaining > FRESH_MARGIN
        return reading, "fresh" if fresh else "expired"

    def keep_live_muse(self, key, identity):
        """Stores the signed-in Muse login over saved account `key` when it is a complete file login of the same
        account: Muse renews it in place, and the saved copy must not fall behind. Returns put_guarded's answer,
        or None when there was nothing to store."""
        muse = self.providers["muse"]
        try:
            doc = muse.live_document()
        except DeckError:
            return None
        if not muse_auth.is_file_login(doc) or muse_auth.identity_key(doc) != identity:
            return None
        return self._put_muse(key, doc, "live")

    def _put_muse(self, key, doc, source, expected=None):
        """Vault.put_guarded for a Muse copy, plus the order expiry cannot give: a refresh token the vault has
        replaced is never stored again (an older auth.json can reappear in Muse's folder: a restored backup,
        synced dotfiles). Every caller holds account `key`'s lock."""
        spent = (self.store.account(key) or {"flags": {}})["flags"].get("muse_spent", [])
        if muse_auth.spent_mark(doc) in spent:
            return "kept"
        try:
            held = muse_auth.spent_mark(self.vault.get(key))
        except DeckError:
            held = None
        stored = self.vault.put_guarded(key, doc, source, *GUARDS["muse"], expected=expected)
        if stored == "accepted" and held and held != muse_auth.spent_mark(doc):
            flags = dict(self.store.account(key)["flags"])
            flags["muse_spent"] = [held] + spent[:SPENT_KEPT - 1]
            self.store.update(key, flags=flags)
        return stored

    # ---- what the snapshot shows about a saved account ----
    def series(self, account, now):
        """({window: readings in its current reset epoch}, trend points) of a saved account, worked out
        once per new reading (the stored readings never change between two readings)."""
        usage = account["last_good_usage"]
        if not usage:
            return {}, []
        with self._series_lock:
            cached = self._series.get(account["id"])
            if cached is not None and cached[0] == account["last_good_at"]:
                return cached[1], cached[2]
        samples, readings = {}, {}
        for window in map(intel.normalize_window, usage.get("windows") or ()):
            if window is None:
                continue
            since = window["reset"] - window["minutes"] * 60 if window["reset"] and window["minutes"] else None
            readings[window["id"]] = self.store.readings(account["id"], window["id"], since=since)
            samples[window["id"]] = intel.epoch_samples(readings[window["id"]], window["reset"], window["minutes"])
        followed = intel.trend_window(usage)
        trend = [] if followed is None else intel.trend(readings.get(followed["id"], []), followed["reset"],
                                                        followed["minutes"], now)
        with self._series_lock:
            self._series[account["id"]] = (account["last_good_at"], samples, trend)
        return samples, trend

    def touch_active(self, provider, identity, now):
        """Notes that a saved account is the signed-in one (`flags.last_active_at`, renewed hourly):
        the dashboard's "unused for N days" hint reads it. A read writes the account's flags under its
        lock, so the flags are rewritten under that lock too, from the row as it is then. The tick never
        waits for a read: when one holds the lock, the next tick tries again."""
        account = self.store.find(provider, identity)
        if account is None or is_frozen(account) or not _touch_due(account, now):
            return
        lock = self.lock_for(account["id"])
        if not lock.acquire(blocking=False):
            return
        try:
            account = self.store.account(account["id"])
            if account is not None and _touch_due(account, now):
                self.store.update(account["id"], flags={**account["flags"], "last_active_at": now})
        finally:
            lock.release()

    # ---- the unsaved signed-in account ----
    def sense_unsaved(self, provider, identity):
        """Tracks the account signed in to `provider` when it is not saved (None clears it)."""
        if identity is None or self.store.find(provider, identity) is not None:
            self.unsaved[provider] = None
            return None
        current = self.unsaved.get(provider)
        if current is None or current["identity"] != identity:
            current = self.unsaved[provider] = {"identity": identity, "label": None, "email": None, "plan": None,
                                                "usage": None, "last_good_at": None, "checked": None, "error": None,
                                                "expires_in": None, "last_outcome": None}
        return current

    def read_unsaved(self, provider) -> str:
        row = self.unsaved.get(provider)
        if row is None:
            return "not_found"
        if is_frozen(self.store.find(provider, row["identity"])):
            return "skipped"
        now = self.clock()
        row["checked"] = now
        try:
            if provider == "muse":
                muse = self.providers["muse"]
                live = muse.active_identity()
                if live.status != "ok" or live.identity != row["identity"]:
                    return "identity"
                if row["label"] is None:
                    doc = muse.live_document() or {}
                    try:
                        info = muse.identity(doc)
                        row.update(label=default_label(info["email"], "muse"), email=info["email"] or None)
                    except DeckError:
                        pass
                reading = muse.usage(live=True, account=UNSAVED_IDS["muse"])
                if reading.meta.get("plan"):
                    row["plan"] = reading.meta["plan"]
                row["expires_in"] = None
            else:
                codex = self.providers["codex"]
                profile = codex.current()
                if identity_key(profile) != row["identity"]:
                    return "identity"
                if row["label"] is None:
                    info = codex.identity(profile)
                    row.update(label=default_label(info["email"], "codex"), email=info["email"] or None,
                               plan=info["plan"] or None)
                reading = codex.usage(profile, mode="read_only", account=UNSAVED_IDS["codex"])
                remaining = token_state(profile, now).remaining
                row["expires_in"] = None if remaining is None else round(remaining)
        except DeckError as exc:
            row["error"] = {"code": error_code(exc), "message": str(exc)[:300] or READ_FAILED,
                            "since": (row["error"] or {}).get("since", now)}
            return error_code(exc)
        self._record(provider, UNSAVED_IDS[provider], reading.outcomes, now)
        if reading.usage is not None:
            row.update(usage=reading.usage, last_good_at=reading.usage["checked"], error=None, last_outcome="2xx")
            return "ok"
        if reading.error is None:
            if reading.meta.get("hollow"):
                row.update(error=None, last_outcome="2xx")
                return "ok"
            return "skipped"
        code = error_code(reading.error)
        row["error"] = {"code": code, "message": str(reading.error)[:300] or READ_FAILED,
                        "since": (row["error"] or {}).get("since", now)}
        return code
