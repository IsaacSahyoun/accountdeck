"""Terminal vault ownership, passive capture, consume gate and isolated enrollment."""
import copy
from contextlib import contextmanager, nullcontext
from pathlib import Path
import threading
import time
import uuid

from . import diagnostics, readings
from .cli_locks import credential_locks
from .core import DeckError, OwnerLock, is_frozen
from .providers.claude_cli import expires, fingerprint, object_file, oauth, profile
from .platform.facts_mac import remove_work_directory


REFRESH_TIMEOUT = 5.0         # the token POST runs while Claude Code's credential locks are held
RENEWAL_WAIT = 1800           # after a renewal that did not work, a read asks Claude for the next one this much later
RENEWAL_WAITS = "cli_renewal_wait"       # the setting that holds {account id: until when no read of it can be taken}
REAUTH = "Sign in again to repair this terminal account."
UNVERIFIABLE = "Sign in again so AccountDeck can confirm which account this saved login belongs to."
MISFILED = "This saved login belongs to a different Claude account. Sign in again with the right account."


def _waits(error, asked):
    """`error` with the time until when no read of the account can be taken: Claude was asked to renew its login
    at `asked`, the login was not renewed, and a read does not ask again for `RENEWAL_WAIT`."""
    error.renewal_wait = asked + RENEWAL_WAIT
    return error


class CLIAccounts:
    """Locks: `mutex` covers cross-account work (the live files, capture, the cross-check, enrollment and
    a switch); `key_lock(key)` and the per-account file lock (`gate`) cover one record's vault/store
    changes and its token consumption. Order: mutex, then one key lock; nothing holding a key lock takes
    the mutex. No network call runs under the mutex except a switch's own and a conflict check."""

    def __init__(self, engine, adapter):
        self.engine, self.adapter = engine, adapter
        self.paths, self.store, self.vault = engine.paths, engine.store, engine.vault
        self.clock = engine.clock
        self.mutex = threading.RLock()
        self._keys, self._keys_lock = {}, threading.Lock()
        self.enrollment = None
        self.phase = {"phase": "idle", "message": ""}
        self.lock_timeout = 9.0
        # Which account each token really belongs to (fingerprint -> accountUuid), and when an
        # unanswered question may be asked again. `.claude.json` names the account separately from
        # the token and can lag it, so the provider is asked once per new token.
        self.owners, self.owner_retry = {}, {}
        self._deferred = None        # (fingerprint, reason) of the capture deferral last logged

    def key_lock(self, key):
        with self._keys_lock:
            return self._keys.setdefault(key, threading.RLock())

    def owner(self, credentials, *, ask_again=False):
        """The accountUuid this token belongs to, or None when the provider cannot say right now. A failed
        lookup is not repeated for a minute, unless `ask_again`. An automatic switch empties `owner_retry`
        when it starts, so its own lookups ask (`CLISwitch.run`)."""
        fp = fingerprint(credentials)
        if fp in self.owners:
            return self.owners[fp]
        if not ask_again and self.clock() < self.owner_retry.get(fp, 0):
            return None
        try:
            found = self.adapter.resolve_account(credentials, timeout=5).get("accountUuid")
        except DeckError:
            found = None
        if not isinstance(found, str) or not found:
            self.owner_retry[fp] = self.clock() + 60
            return None
        if len(self.owners) >= 64:
            self.owners.clear()
        self.owners[fp] = found
        self.owner_retry.pop(fp, None)
        return found

    def whose(self, credentials, *, ask_again=False):
        """The accountUuid a login belongs to, or None when nobody can say. A saved account's confirmed
        token answers without the network (so it still answers after a restart); else `owner` asks."""
        fp = fingerprint(credentials)
        for row in self.store.accounts():
            saved = self._record(row["id"]) if row["provider"] == "claude_cli" else None
            if saved and saved.get("fingerprint") == fp and saved.get("verified") == fp:
                return saved["identity"]
        return self.owner(credentials, ask_again=ask_again)

    def confirm(self, key, saved, *, active):
        """False when the login filed under this account belongs to another one. An inactive account
        then loses that login (`_drop_foreign`), so its readings and switches never use it."""
        if saved.get("verified") == saved["fingerprint"]:
            return True
        found = self.owner(saved["credentials"])
        if found is None:            # the provider could not answer; the read-only usage GET decides
            return True
        if found == saved["identity"]:
            if not active and self.engine.owns_login:
                with self.gate(key):
                    self._mark_verified(key, saved["fingerprint"])
            return True
        diagnostics.log("cli.identity_mismatch", lvl="warning", acct=diagnostics.account_ref(key), active=active)
        if not active and self.engine.owns_login:
            with self.gate(key):
                self._drop_foreign(key, saved["fingerprint"])
        return False

    def verified(self, key, saved):
        """`saved` once the provider confirmed it is this account's login; raises otherwise. A switch
        swaps in only a confirmed login (Claude Code would otherwise renew someone else's)."""
        with self.gate(key):
            return self._ensure_verified(key, saved, self.clock())

    def _ensure_verified(self, key, saved, now):
        """Caller holds `gate(key)`. Confirms ownership before a token is consumed or swapped in."""
        fp = saved["fingerprint"]
        if saved.get("verified") == fp:
            return saved
        if expires(saved["credentials"]) > now + 5:       # the profile endpoint still accepts it
            found = self.owner(saved["credentials"])
            if found == saved["identity"]:
                self._mark_verified(key, fp)
                return {**saved, "verified": fp}
            if found is not None:
                diagnostics.log("cli.identity_mismatch", lvl="warning", acct=diagnostics.account_ref(key),
                                active=False, via="consume")
                self._drop_foreign(key, fp)
                raise DeckError(MISFILED, "identity")
            diagnostics.log("cli.refresh", acct=diagnostics.account_ref(key), outcome="unverified")
            raise DeckError("AccountDeck could not confirm this terminal login yet. Its last reading is kept.", "network")
        # Expired and never confirmed: renewing could rotate another account's token family (even
        # the one Claude Code is using), and there is no way to ask whose it is. Fail closed.
        diagnostics.log("cli.refresh", lvl="warning", acct=diagnostics.account_ref(key), outcome="unverifiable")
        current = self.vault.get(key)
        if current.get("fingerprint") == fp:
            current["health"] = "needs_signin"
            self.vault.put(key, current)
            row = self.row(key)
            self.store.update(key, flags=dict(row["flags"], health="needs_signin", restorable=False, checked=now),
                              credential_state="reauth_required", error_code="reauth_required", error=UNVERIFIABLE)
            self.engine.bump()
        raise DeckError(UNVERIFIABLE, "reauth_required")

    def _mark_verified(self, key, fp):
        current = self.vault.get(key)
        if current.get("fingerprint") == fp and current.get("verified") != fp:
            current["verified"] = fp
            self.vault.put(key, current, generation=False)

    def _drop_foreign(self, key, fp):
        """Caller holds `gate(key)`. Blanks another account's login in this record's current copy (identity,
        label and settings stay), so it is never renewed or swapped in; the account needs sign-in again.
        The blank copy is written in place, so this write pushes nothing into an older generation. Older
        generations saved before it are left in the vault and may still hold that login; no terminal
        code reads them back."""
        current = self.vault.get(key)
        if current.get("fingerprint") != fp:      # replaced since it was checked; the new one is unchecked
            return
        current.update(credentials=None, fingerprint=None, health="needs_signin")
        current.pop("verified", None)
        self.vault.put(key, current, generation=False)
        self.vault.remove(self._stash_key(key))
        row = self.row(key)
        self.store.update(key, flags=dict(row["flags"], health="needs_signin", restorable=False, checked=self.clock()),
                          credential_state="reauth_required", error_code="identity", error=MISFILED, expires_at=None)
        diagnostics.log("cli.login_dropped", lvl="warning", acct=diagnostics.account_ref(key))
        self.engine.bump()

    def guard(self):
        self.adapter.guard()
        if not self.engine.owns_login:
            raise DeckError("Another AccountDeck 2 is managing your sign-ins.", "read_only")

    def row(self, key):
        row = self.store.account(key)
        if not row or row["provider"] != "claude_cli":
            raise DeckError("That terminal account no longer exists.", "not_found")
        return row

    def require_local(self, key):
        if is_frozen(self.row(key)):
            raise DeckError("This account's sign-in belongs to another computer, so it is not used here.", "read_only")

    @contextmanager
    def gate(self, key):
        self.guard()
        self.row(key)
        with self.key_lock(key):
            self.require_local(key)
            lock = OwnerLock(self.paths.data_dir / "cli-locks" / (key + ".lock"))
            if not lock.acquire():
                raise DeckError("This terminal login is being renewed. Try again shortly.", "busy")
            try:
                yield
            finally:
                lock.release()

    def _record(self, key):
        """The vault record, or None when it is missing or unreadable."""
        try:
            return self.vault.get(key)
        except DeckError:
            return None

    def crosscheck(self, live):
        """Refuses a live login whose token is saved under another account. Records without a usable
        login (needs sign-in, dropped or unreadable) never block. When an unconfirmed record holds the
        live token, the provider decides: if the token is the live account's, that record loses it."""
        with self.mutex:
            for row in self.store.accounts():
                if is_frozen(row) or row["provider"] != "claude_cli" or row["identity"] == live["identity"]:
                    continue
                saved = self._record(row["id"])
                if not saved or saved.get("health", "ok") != "ok" or not saved.get("credentials"):
                    continue
                if saved.get("fingerprint") != live["fingerprint"]:
                    continue
                if (saved.get("verified") != saved["fingerprint"] and self.engine.owns_login
                        and self.owner(live["credentials"]) == live["identity"]):
                    diagnostics.log("cli.identity_mismatch", lvl="warning", acct=diagnostics.account_ref(row["id"]),
                                    active=False, via="crosscheck")
                    with self.gate(row["id"]):
                        self._drop_foreign(row["id"], saved["fingerprint"])
                    continue
                raise DeckError("Claude Code's account and login disagree. Wait for sign-in to finish.", "identity")
        return live

    def save(self, incoming, *, source="capture"):
        self.guard()
        with self.mutex:
            old = self.store.find("claude_cli", incoming["identity"])
            if is_frozen(old):
                self.engine.notice("This terminal account's sign-in belongs to another computer, so it was not saved here.", "warning")
                self.require_local(old["id"])
            incoming = self.crosscheck(copy.deepcopy(incoming))
            with self.key_lock(old["id"]) if old else nullcontext():
                if old:
                    self.require_local(old["id"])
                previous = self._record(old["id"]) if old else None
                if old:
                    incoming["label"] = old["label"]
                    if previous:
                        incoming["excluded"] = previous.get("excluded", False)
                        if incoming["fingerprint"] == previous.get("fingerprint"):
                            incoming["refreshed"] = previous.get("refreshed", incoming["refreshed"])
                            if previous.get("verified") == incoming["fingerprint"]:
                                incoming.setdefault("verified", incoming["fingerprint"])
                else:
                    incoming["label"] = incoming["email"].partition("@")[0] or "Terminal account"
                key = old["id"] if old else self.store.save_account("claude_cli", incoming["identity"], incoming["label"],
                          incoming["email"], incoming["plan"], incoming["organizationUuid"])
                try:
                    digest = self.vault.put(key, incoming)
                except BaseException:
                    if not old:
                        self.store.remove(key)
                    raise
                self.store.save_account("claude_cli", incoming["identity"], incoming["label"], incoming["email"],
                                        incoming["plan"], incoming["organizationUuid"])
                flags = dict(self.row(key)["flags"])
                flags.update(restorable=True, health=incoming["health"], excluded=incoming["excluded"], live_expired=False)
                self.store.update(key, flags=flags, credential_state="fresh", error=None, error_code=None,
                                  expires_at=expires(incoming["credentials"]))
                self.store.record_generation(key, digest, source=source, health=incoming["health"])
            self.engine.bump()
            return key

    def file_live(self, live, *, source):
        """Saves a live login under the account `.claude.json` names once that account is known to own its
        token: the token is the one already saved there, or the provider confirms it (Claude Code writes
        the token and the account name to different files at different times). Returns (the account id, or
        None when it was not saved; the owner the provider named, or None)."""
        old = self.store.find("claude_cli", live["identity"])
        previous = self._record(old["id"]) if old else None
        unchanged = bool(previous) and previous.get("fingerprint") == live["fingerprint"]
        found = None if unchanged else self.owner(live["credentials"])
        if not unchanged and found != live["identity"]:
            return None, found
        if found == live["identity"]:
            live["verified"] = live["fingerprint"]
        return self.save(live, source=source), found

    def _prefetch_owner(self):
        """Asks the provider about a new live token before `mirror` takes the mutex, so that network
        call never blocks the terminal reads; the locked pass then answers from `owners`."""
        if not self.engine.owns_login:
            return
        try:
            live = self.adapter.current()
        except DeckError:
            return
        fp = live["fingerprint"]
        if fp in self.owners or self.clock() < self.owner_retry.get(fp, 0):
            return
        old = self.store.find("claude_cli", live["identity"])
        if is_frozen(old):
            return
        previous = self._record(old["id"]) if old else None
        if previous and previous.get("fingerprint") == fp:
            return
        self.owner(live["credentials"])

    def mirror(self):
        self.adapter.guard()
        self._prefetch_owner()
        with self.mutex:
            try:
                live = self.adapter.current()
                if is_frozen(self.store.find("claude_cli", live["identity"])):
                    return live["identity"]
                live = self.crosscheck(live)
            except DeckError as exc:
                if exc.code != "login_needed":
                    raise
                account = object_file(self.paths.claude_user_config, missing=True).get("oauthAccount") or {}
                known = self.store.find("claude_cli", account.get("accountUuid"))
                if known and not is_frozen(known) and self.engine.owns_login:
                    with self.key_lock(known["id"]):
                        saved = self._record(known["id"])
                        if saved is not None and saved.get("health") != "needs_signin":
                            saved["health"] = "needs_signin"
                            self.vault.put(known["id"], saved)
                    flags = dict(known["flags"], health="needs_signin", restorable=False, live_expired=False)
                    if flags != known["flags"] or known["credential_state"] != "reauth_required":
                        self.store.update(known["id"], flags=flags, credential_state="reauth_required")
                return known["identity"] if known else None
            old = self.store.find("claude_cli", live["identity"])
            if is_frozen(old):
                return live["identity"]
            if self.engine.owns_login:
                previous = self._record(old["id"]) if old else None
                if not previous or any(previous.get(k) != live.get(k) for k in
                                       ("fingerprint", "credentials", "oauthAccount")):
                    unchanged = bool(previous) and previous.get("fingerprint") == live["fingerprint"]
                    key, found = self.file_live(live, source="mirror")
                    if key is not None:
                        self._deferred = None
                        diagnostics.log("cli.mirror_saved", acct=diagnostics.account_ref(key),
                                        reason="new" if not old else "updated" if unchanged else "rotated")
                        if not old:
                            self.engine.notice(f"Saved your current terminal login as {live['email']}.", "good")
                    else:
                        reason = "owner_unknown" if found is None else "mismatch"
                        if self._deferred != (live["fingerprint"], reason):
                            self._deferred = (live["fingerprint"], reason)
                            diagnostics.log("cli.capture_deferred", lvl="warning" if found else "info", reason=reason,
                                            acct=diagnostics.account_ref(old["id"]) if old else None)
                row = self.store.find("claude_cli", live["identity"])
                if row is None:
                    return live["identity"]
                expired = expires(live["credentials"]) <= self.clock() + 60
                if row["flags"].get("live_expired") != expired:
                    self.store.update(row["id"], flags=dict(row["flags"], live_expired=expired))
                    self.engine.bump()
            return live["identity"]

    def _stash_key(self, key):
        return "cli-stash-" + key

    def adopt_stashes(self):
        self.guard()
        with self.mutex:
            for row in self.store.accounts():
                if row["provider"] != "claude_cli" or is_frozen(row):
                    continue
                with self.gate(row["id"]):
                    self._adopt(row["id"])

    def _adopt(self, key):
        self.require_local(key)
        name = self._stash_key(key)
        if not self.vault.path(name).exists():
            return
        stash = self.vault.get(name)
        saved = self.vault.get(key)
        if saved.get("fingerprint") == stash["before"]:
            if stash.get("result"):
                if stash["result"]["identity"] != saved["identity"]:
                    raise DeckError("A terminal recovery record has a different identity. It was preserved.", "recovery")
                saved = stash["result"]
            else:
                # The process may have died after consuming but before saving the answer.
                # Never replay an uncertain single-use token.
                saved["health"] = "needs_signin"
            self.vault.put(key, saved)
            flags = dict(self.row(key)["flags"], health=saved["health"], restorable=saved["health"] == "ok")
            self.store.update(key, flags=flags, credential_state="fresh" if saved["health"] == "ok" else "reauth_required")
        self.vault.remove(name)

    def refresh(self, key, *, for_switch=False):
        return self._refresh(key, for_switch=for_switch)[0]

    def _refresh(self, key, *, for_switch=False):
        """(saved, renewed now). A token is consumed only once its owner is confirmed (`verified`):
        directly, or because it was renewed from a confirmed one (the same token family)."""
        with self.gate(key):
            self._adopt(key)
            saved = self.vault.get(key)
            if saved["identity"] != self.row(key)["identity"]:
                raise DeckError("The saved terminal login has a different identity.", "identity")
            if saved["health"] != "ok" or not saved.get("credentials"):
                raise DeckError(REAUTH, "reauth_required")
            now = self.clock()
            due = expires(saved["credentials"]) <= now + (600 if for_switch else 60)
            keepalive = now - saved.get("refreshed", 0) >= 7 * 86400
            if not due and not keepalive:
                return saved, False
            until = None if for_switch else self._too_soon(key, saved, now)
            if until is not None:
                error = DeckError("This terminal login was checked recently. Its last reading is kept.", "busy")
                error.renewal_wait = until
                raise error
            if self.enrollment:
                raise DeckError("Finish adding the terminal account first.", "busy")
            saved = self._ensure_verified(key, saved, now)
            # Also hold CC's locks across the live comparison and consumption. This
            # closes the race with a concurrent CLI refresh or another file swap.
            with credential_locks(self.paths, timeout=self.lock_timeout) as check:
                live_credentials = self.adapter.store.read(missing=True)
                live_account = object_file(self.paths.claude_user_config, missing=True).get("oauthAccount") or {}
                try:
                    live_fingerprint = fingerprint(live_credentials)
                except DeckError:
                    live_fingerprint = None
                if (live_account.get("accountUuid") == saved["identity"] or
                        live_fingerprint == saved["fingerprint"]):
                    raise DeckError("Claude Code owns this active login; AccountDeck will not renew it.", "busy")
                check()
                saved["refresh_attempt"] = now
                self.vault.put(key, saved)
                stash = {"before": saved["fingerprint"], "result": None}
                self.vault.put(self._stash_key(key), stash, generation=False)
                started = time.perf_counter()
                try:
                    credentials = self.adapter.refresh(saved["credentials"], timeout=REFRESH_TIMEOUT)
                except DeckError as exc:
                    http, cls = getattr(exc, "http", None), getattr(exc, "http_class", None)
                    ms = round((time.perf_counter() - started) * 1000)
                    if http in (400, 401) and getattr(exc, "detail", None) == "invalid_grant":
                        diagnostics.log("cli.refresh", lvl="warning", acct=diagnostics.account_ref(key),
                                        outcome="invalid_grant", http=cls, ms=ms, for_switch=for_switch)
                        saved["health"] = "needs_signin"
                        self.vault.put(self._stash_key(key), {**stash, "result": saved}, generation=False)
                        self._adopt(key)
                        raise DeckError(REAUTH, "reauth_required") from None
                    # A definite rejection did not consume the token. Ambiguous network
                    # failures retain the intent and are never automatically replayed.
                    definite = http in (400, 401, 403, 429)
                    diagnostics.log("cli.refresh", lvl="warning", acct=diagnostics.account_ref(key),
                                    outcome="rejected" if definite else "ambiguous", http=cls, ms=ms,
                                    for_switch=for_switch, code=exc.code)
                    if definite:
                        self.vault.remove(self._stash_key(key))
                    error = DeckError("The terminal login could not be renewed. Its last reading is kept.",
                                      exc.code, exc.retry_after)
                    error.http_class = cls        # a provider answer (or its absence) counts as observed
                    raise _waits(error, now) from None
                new = fingerprint(credentials)
                saved.update(credentials=credentials, fingerprint=new, refreshed=now, verified=new)
                self.vault.put(self._stash_key(key), {**stash, "result": saved}, generation=False)
                self.vault.put(key, saved)       # must succeed before a caller sees the access token
                self.vault.remove(self._stash_key(key))
                diagnostics.log("cli.refresh", acct=diagnostics.account_ref(key), outcome="ok", http="2xx",
                                ms=round((time.perf_counter() - started) * 1000), for_switch=for_switch)
                return saved, True

    def _too_soon(self, key, saved, now):
        """Until when a read does not ask Claude to renew the login of `key`, or None when it may ask now. That
        is 30 minutes after the last renewal that was asked for, or the wait a read noted for the account
        when that is later (`_blocked`): Claude can refuse a renewal and say to wait up to an hour. The pause
        on the row keeps reads off for that hour. The app dying after the wait was noted and before the pause
        was left no pause, and 30 minutes later a read asked Claude again, inside the hour."""
        noted = self.renewal_waits().get(key)
        until = max(saved.get("refresh_attempt", -1e20) + RENEWAL_WAIT, noted if isinstance(noted, (int, float)) else -1e20)
        return until if now < until else None

    def _settle_read(self, key, flags, **fields):
        """Writes a read's result onto the row as it is now (another thread may have changed its flags)."""
        with self.mutex:
            row = self.store.account(key)
            if row is None:
                return
            self.store.update(key, flags=dict(row["flags"], **flags), **fields)

    def renewal_waits(self):
        """{account id: until when no read of it can be taken}, as `_blocked` noted them."""
        waits = self.store.setting(RENEWAL_WAITS, {})
        return waits if isinstance(waits, dict) else {}

    def rows_and_waits(self):
        """(every saved account's row, `renewal_waits`) as they are at one moment, for the auto-switch rule
        (`cli_auto.inputs`). A read that ends at a refused renewal writes the wait and then the pause on the
        row (`_read`), so at every moment such a pause has its wait. Two looks at two moments do not show
        that: the waits from before that read and the row from after it are a pause with no wait, and the rule
        took the account for one whose usage checks Claude paused. The store's lock keeps every write out
        between the two looks."""
        with self.store.lock:
            waits = self.renewal_waits()
            return self.store.accounts(), waits

    def _blocked(self, key, until):
        """Notes until when no read of the account can be taken (None: a read of it worked), for the auto-switch
        rule (`cli_auto.inputs`). It has a setting of its own, which a read alone writes, under one lock. A
        row's `flags` are no place for it: every writer of those writes the whole set back, Leave Out under
        another lock than a read, so a fact kept there can be lost to an older copy. The wait of an account
        that is gone is dropped here. One that has ended is not: the pause on the row is not taken off for
        having ended either, and with the Mac's clock set ahead and put right both run again. Dropped then,
        the wait left a pause with no wait, and the rule took the account for a paused one."""
        with self.mutex:
            waits, saved = self.renewal_waits(), {row["id"] for row in self.store.accounts()}
            kept = {account: at for account, at in waits.items() if account != key and account in saved}
            if until is not None:
                kept[key] = until
            if kept != waits:
                self.store.set_setting(RENEWAL_WAITS, kept)

    def read(self, key):
        """Reads one terminal account under its lock."""
        with self.engine.scheduler.account_lock(key):
            if is_frozen(self.store.account(key)):
                return "skipped"
            return self._read(key)

    def _read(self, key):
        """Reads one terminal account's usage. Network calls (profile, renewal, usage) run outside the
        mutex, so the tick's `mirror` never waits for them."""
        started = time.perf_counter()
        row = self.row(key)
        now = self.clock()
        note = {"active": None, "refreshed": False, "http": None, "outcome": "error"}
        try:
            try:
                self.adapter.guard()
                try:
                    live = self.crosscheck(self.adapter.current())
                except DeckError as exc:
                    if exc.code != "login_needed":
                        raise
                    live = None
                active = (live["identity"] if live else self.engine.active_identity.get("claude_cli")) == row["identity"]
                note["active"] = active
                if active and live is None:
                    note["outcome"] = "skipped"
                    return "skipped"
                saved = live if active else self.vault.get(key)
                if not active and not saved.get("credentials"):
                    raise DeckError(REAUTH, "reauth_required")
                if not active and self.engine.owns_login:
                    # Check before renewing so a wrongly filed login is never consumed.
                    if expires(saved["credentials"]) > now + 60 and not self.confirm(key, saved, active=False):
                        note["outcome"] = "identity"
                        return "identity"
                    saved, note["refreshed"] = self._refresh(key)
                credentials = saved["credentials"]
                expired = expires(credentials) <= now + 60
                flags = {"checked": now, "live_expired": active and expired, "health": saved["health"]}
                if expired:
                    self._settle_read(key, flags, credential_state="access_expired" if active else "expired")
                    note["outcome"] = "skipped"
                    return "skipped"
                if not self.confirm(key, saved, active=active):
                    note["outcome"] = "skipped" if active else "identity"
                    return note["outcome"]
                data = self.adapter.usage(credentials)
                note["http"] = "2xx"
                with self.mutex:
                    readings.record(self.store, key, data)
                    self._settle_read(key, flags, credential_state="live" if active else "fresh",
                                      last_outcome="2xx", paused_until=None, expires_at=expires(credentials))
                    self._blocked(key, None)
                self.engine.observed["claude_cli"].record(key, "2xx", at=now)
                note["outcome"] = "ok"
                return "ok"
            except DeckError as exc:
                # Only static messages escape this boundary; HTTP bodies and secrets do not. Local
                # refusals (sign in again, checked recently) are not provider failures, so only an
                # answer from the provider, or its absence, feeds the observed outage.
                cls = getattr(exc, "http_class", None)
                note.update(http=cls, outcome=exc.code)
                current = self.store.account(key)
                paused = current["paused_until"] if current else row["paused_until"]
                if exc.code == "rate_limited":
                    paused = now + (exc.retry_after or 900)
                # A read that ended at the renewal of the login says until when no read of the account can be
                # taken (`_waits`). The auto-switch rule passes the account over until then (`cli_policy._row`).
                # Claude can answer a renewal with HTTP 429 and say to wait longer than that. The pause below
                # keeps every read off until then, so the login is not renewed before it ends either.
                # The wait is written before the pause. A check that looks between the two writes, or the app
                # dying there, finds a wait with no pause, and the rule passes that account over all the same.
                # The other way round it found a pause with no wait, and took the account for a paused one.
                if getattr(exc, "renewal_wait", None):
                    self._blocked(key, max(exc.renewal_wait, paused or 0))
                self._settle_read(key, {"checked": now}, error="Terminal usage is unavailable. Your last reading is kept.",
                                  error_code=exc.code, paused_until=paused)
                if cls is not None:
                    self.engine.observed["claude_cli"].record(key, cls, at=now)
                return exc.code
        finally:
            self.engine.bump()
            diagnostics.log("cli.read", acct=diagnostics.account_ref(key), active=note["active"],
                            outcome=note["outcome"], http=note["http"], refreshed=note["refreshed"],
                            ms=round((time.perf_counter() - started) * 1000))

    def set_excluded(self, key, excluded):
        if type(excluded) is not bool:
            raise DeckError("Choose whether to leave this account out.", "invalid")
        with self.gate(key):
            saved = self.vault.get(key)
            saved["excluded"] = excluded
            self.vault.put(key, saved)
            self.store.update(key, flags=dict(self.row(key)["flags"], excluded=excluded))

    def rename(self, key, label):
        with self.gate(key):
            self.engine.accounts.rename(key, label)
            saved = self.vault.get(key)
            saved["label"] = self.row(key)["label"]
            self.vault.put(key, saved)

    def begin_enrollment(self):
        self.guard()
        with self.mutex:
            if self.enrollment or self.engine.runner.busy or getattr(self.engine, "recovery", []):
                raise DeckError("Finish the current terminal operation first.", "busy")
            executable = self.engine.procs.find_claude_cli()
            if executable is None:
                raise DeckError("Install Claude Code first", "not_installed")
            folder = self.paths.cli_enroll / uuid.uuid4().hex
            folder.mkdir(parents=True)
            try:
                process = self.engine.procs.launch_claude_cli(executable, folder, self.paths.home)
            except Exception:
                folder.rmdir()
                raise DeckError("The enrollment terminal could not be opened.", "system") from None
            self.enrollment = {"folder": folder, "process": process, "started": self.clock(), "cancel": False}
            self.vault.put("cli-enrollment", {"folder": str(folder), "process": self.engine.procs.cli_enrollment_token(process),
                           "started": self.clock(), "cancel": False}, generation=False)
            self.phase = {"phase": "signing_in", "message": "Sign in with the account you want to add in the new terminal window. If the browser picks the wrong account, use a private window."}
            self.engine.bump()

    def restore_enrollment(self):
        self.guard()
        if not self.vault.path("cli-enrollment").exists():
            return
        saved = self.vault.get("cli-enrollment")
        folder = Path(saved["folder"])
        if folder.resolve().parent != self.paths.cli_enroll.resolve():
            raise DeckError("The saved enrollment folder is outside the private data folder.", "safety")
        self.enrollment = {**saved, "folder": folder}
        self.phase = {"phase": "waiting_exit", "message": "Close the enrollment terminal to finish saving its login."}

    def poll_enrollment(self, *, cancel=False):
        with self.mutex:
            session = self.enrollment
            if not session:
                return
            self.guard()
            session["cancel"] |= cancel or self.clock() - session["started"] >= 1200
            if cancel:
                saved = self.vault.get("cli-enrollment")
                saved["cancel"] = True
                self.vault.put("cli-enrollment", saved, generation=False)
            folder = session["folder"]
            # Where the enrollment's login lands: the folder's credentials file, or on macOS the keychain
            # item Claude Code scopes to that CLAUDE_CONFIG_DIR (platform/credstore.py).
            store = self.adapter.store
            exited = self.engine.procs.cli_enrollment_exited(session["process"])
            found = None
            try:
                credentials = store.read_enrollment(folder)
                oauth(credentials)
                account = object_file(folder / ".claude.json", missing=True).get("oauthAccount")
                if not account:
                    account = self.adapter.resolve_account(credentials, timeout=5)
                found = profile(credentials, account, self.clock())
            except DeckError:
                pass
            if not exited:
                if session["cancel"]:
                    self.phase = {"phase": "waiting_exit", "message": "Close the enrollment terminal to finish cancelling."}
                elif found:
                    self.phase = {"phase": "waiting_exit", "email": found["email"],
                                  "message": f"Signed in as {found['email']} — close that terminal window to finish"}
                self.engine.bump()
                return
            # This read happened AFTER observed exit. A valid login (including a repaired
            # duplicate on cancel) is saved before any helper files can be discarded.
            if found:
                duplicate = self.store.find("claude_cli", found["identity"]) is not None
                key = self.save(found, source="enrollment")
                message = "Already saved — its login was refreshed" if duplicate else f"Saved {found['email']}."
                self.engine.scheduler.request(key, self.clock())
                self.phase = {"phase": "saved", "email": found["email"], "message": message}
            elif not session["cancel"]:
                self.phase = {"phase": "failed", "message": "The terminal closed before a complete login was saved. Try adding the account again."}
            else:
                self.phase = {"phase": "cancelled", "message": "Enrollment cancelled."}
            if not found and store.enrollment_pending(folder):
                self.phase = {"phase": "failed", "message": "The enrollment login could not be saved yet. Its private folder is preserved; AccountDeck will retry."}
                self.engine.bump()
                return
            # Refuse recursive cleanup if a reparse point moved the directory out of scope.
            if folder.resolve().parent != self.paths.cli_enroll.resolve() or folder.is_symlink():
                raise DeckError("The enrollment folder moved; it was preserved.", "safety")
            # The login is saved (or there is none): its isolated keychain item goes first, so a failure
            # here keeps the folder that names it for the next poll.
            store.discard_enrollment(folder)
            # Claude Code clones plugin marketplaces with read-only git pack files; a plain rmtree
            # raises PermissionError on those and the enrollment never finishes.
            remove_work_directory(folder, self.paths.cli_enroll)
            self.vault.remove("cli-enrollment")
            self.enrollment = None
            self.engine.bump()
