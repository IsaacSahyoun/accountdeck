"""Terminal login swap (the credentials file, or on macOS Claude Code's keychain item). Never launches,
stops or signals a process.

Claude Code keeps the login token and the name of the account it belongs to in two places, and renews the
token whenever it runs. So a switch is judged by who owns the login that is live afterwards, never by
whether its bytes are the ones written, and the two parts are always written together."""
import copy

from . import cli_auto, diagnostics, recovery
from .cli_locks import config_lock, credential_locks
from .cli_policy import considered
from .rules import plan_current, plan_target
from .core import DeckError, FileTransaction, atomic_write, is_frozen, json_bytes
from .operations import Operation, OperationCancelled
from .providers.claude_cli import fingerprint, merge, oauth, object_file, profile, scoped
from .switching import assemble, context, register

DISAGREE = "Claude Code's account and login disagree. Wait for sign-in to finish."
UNCONFIRMED = "AccountDeck could not confirm which account Claude Code's login belongs to yet. Try again shortly."
NOTHING_TO_UNDO = "The interrupted switch had changed nothing."
CHANGED_SINCE = "Claude Code changed accounts before the automatic switch, so nothing was switched."


class CLITransaction(FileTransaction):
    """The record of one terminal switch: the login as it was before (the credentials file under `changes`,
    or the keychain item under `keychain_changes`) and `oauth_change`, the account name before and after."""
    def __init__(self, service, key=None):
        super().__init__(service.vault, [service.paths.claude_cli_home], key)
        self.service = service
        self.doc["provider"] = "claude_cli"

    @property
    def wrote(self):
        return bool(self.doc["changes"] or self.doc.get("keychain_changes"))

    def swap(self, saved, live_credentials, name):
        """Writes the saved profile's login and its account name, together; `name` is the account named
        until now. The caller holds Claude Code's credential locks. MCP logins and plugin secrets belong
        to the machine, so the live ones stay."""
        account = account_of(saved)
        self.doc["oauth_change"] = {"before": name, "after": copy.deepcopy(account)}
        self.service.adapter.store.write(self, merge(saved["credentials"], live_credentials))
        write_account(self.service, account)


def account_of(saved):
    """The `oauthAccount` entry of a saved profile."""
    return saved.get("oauthAccount") or {"accountUuid": saved["identity"], "emailAddress": saved["email"],
                                         "organizationUuid": saved["organizationUuid"]}


def write_account(service, account):
    """Names `account` in `.claude.json`, under Claude Code's own lock for that file."""
    with config_lock(service.paths, timeout=service.lock_timeout) as check:
        current = object_file(service.paths.claude_user_config, missing=True)
        current["oauthAccount"] = account
        check()
        atomic_write(service.paths.claude_user_config, json_bytes(current))


def perform(service, key, *, leaving=None):
    """Switches Claude Code to the saved account `key` (manual, automatic and Undo switches); returns the
    outgoing account's id. An automatic switch gives `leaving`, the identity of the account it decided to
    leave: when Claude Code names another account by the time its files are locked, nothing is switched."""
    service.guard()
    with service.mutex, service.key_lock(key):
        if service.enrollment:
            raise DeckError("Close the enrollment terminal and finish adding its account first.", "busy")
        row = service.row(key)
        service.require_local(key)
        target = service.vault.get(key)
        if target["identity"] != row["identity"] or (target.get("oauthAccount") or {}).get("accountUuid", target["identity"]) != row["identity"]:
            raise DeckError("The saved terminal login has a different identity.", "identity")
        if target["health"] != "ok":
            raise DeckError("Sign in again before switching to this account.", "reauth_required")
        live_identity = service.mirror()
        if live_identity == row["identity"]:
            raise DeckError("Your terminals already use this account.", "invalid")
        target = service.refresh(key, for_switch=True)
        # Claude Code renews whatever is swapped in, so only a login confirmed as this account's goes in.
        target = service.verified(key, target)
        store = service.adapter.store
        tx = CLITransaction(service)
        try:
            with credential_locks(service.paths, timeout=service.lock_timeout, storage_write=store.storage_lock) as check:
                service.guard()
                credentials, config = _read_live(service)
                named = (config.get("oauthAccount") or {}).get("accountUuid")
                if named == target["identity"]:
                    raise DeckError("Your terminals already use this account.", "invalid")
                if leaving is not None and named != leaving:
                    raise OperationCancelled(CHANGED_SINCE)
                previous = _file_outgoing(service, credentials, config.get("oauthAccount"))
                check()
                tx.swap(target, credentials, config.get("oauthAccount"))
                check()
            # Checked after Claude Code's locks are released; a failure takes them again to undo, so
            # the undo cannot interleave with a renewal by Claude Code.
            live = service.adapter.current()
            if live["identity"] != target["identity"] or service.whose(live["credentials"]) != target["identity"]:
                raise DeckError("The terminal account could not be verified.", "identity")
            tx.commit()
            service.engine.active_identity["claude_cli"] = target["identity"]
            service.engine.identity_known["claude_cli"] = True
            service.engine.bump()
            return previous
        except BaseException as exc:
            note = recovery.after_failure(tx, lambda _: _settle_locked(service, tx, final=False)) if tx.wrote else None
            if note and isinstance(exc, DeckError):
                raise DeckError(f"{exc} {note}", exc.code, exc.retry_after) from None
            raise


def _read_live(service):
    """(the live credentials, `.claude.json`), re-read under Claude Code's locks. Only the Mac keychain
    store may hold no login at all (a first setup); a login that exists but cannot be read is refused."""
    store = service.adapter.store
    fresh_mac = store.kind == "keychain"
    try:
        credentials = store.read()
    except DeckError as exc:
        if not fresh_mac or exc.code != "login_needed":
            raise
        credentials = {}
    return credentials, object_file(service.paths.claude_user_config, missing=fresh_mac)


def _file_outgoing(service, credentials, name):
    """Saves the login Claude Code is leaving, before it is replaced, under the account that owns it (the
    rule of `CLIAccounts.file_live`). Returns that account's id, or None when nobody was signed in."""
    if not credentials and name is None:
        return None
    try:
        outgoing = service.crosscheck(profile(credentials, name, service.clock()))
    except DeckError as exc:
        known = service.store.find("claude_cli", (name or {}).get("accountUuid"))
        if exc.code != "login_needed" or known is None or known["flags"].get("health") != "needs_signin":
            raise
        return known["id"]       # a signed-out stub: the account keeps its last saved login
    previous, found = service.file_live(outgoing, source="sync-back")
    if previous is None:
        raise DeckError(DISAGREE if found else UNCONFIRMED, "identity" if found else "network")
    return previous


def _settle_locked(service, tx, *, final):
    store = service.adapter.store
    with credential_locks(service.paths, timeout=service.lock_timeout, storage_write=store.storage_lock):
        return settle(service, tx, final=final)


def settle(service, tx, *, final):
    """Puts one unfinished terminal switch right and closes its record (the steps of recovery.py); returns
    a sentence for the user. The caller holds the service mutex and Claude Code's credential locks.

    The login that is live decides. If it is the switch's target, the outgoing account's saved login and
    name are written together. If Claude Code signed in to another account since, or out, that stays and
    the account name is made to match it: someone chose it in a running terminal. When nobody can say
    whose the live login is, `final` (the Recover press) puts the outgoing account back and keeps the
    displaced login in the vault; without `final` the record stays open."""
    if tx.doc.get("purpose") == "recover":      # an undo that never finished: settling its switch writes the login
        tx.close()
        return NOTHING_TO_UNDO
    change = tx.doc.get("oauth_change") or {}
    target = (change.get("after") or {}).get("accountUuid")
    credentials = service.adapter.store.read(missing=True)
    name = object_file(service.paths.claude_user_config, missing=True).get("oauthAccount")
    try:
        oauth(credentials)
    except DeckError:
        tx.close()
        return "Claude Code is signed out, so nothing was put back."
    owner = service.whose(credentials)
    if owner is None and not final:
        raise DeckError(UNCONFIRMED, "network")
    if owner is not None:
        _keep(service, credentials, owner)
    outgoing = _restorable(service, (change.get("before") or {}).get("accountUuid"))
    if owner in (None, target) and outgoing is not None:
        if owner is None:
            service.vault.quarantine("cli-recover", json_bytes(scoped(credentials)))
        fix = CLITransaction(service)
        fix.doc["purpose"] = "recover"
        fix.swap(outgoing, credentials, name)
        fix.commit()
        sentence = f"Terminals are back on {outgoing['label']}."
    elif owner is None:
        raise DeckError("Nobody can say whose login Claude Code holds, and the previous one is not saved.", "recovery")
    else:
        row = service.store.find("claude_cli", owner)
        if (name or {}).get("accountUuid") != owner:
            write_account(service, _account_for(service, credentials, row, owner))
        sentence = f"Claude Code is signed in as {row['label'] if row else 'another account'}, so that login stays."
    tx.close()
    diagnostics.log("cli.settled", back=owner in (None, target) and outgoing is not None, owner_known=owner is not None)
    return sentence


def _restorable(service, identity):
    """The saved profile of the terminal account `identity` when its login can be written back: readable,
    healthy, on this computer, and confirmed as that account's. Else None."""
    row = service.store.find("claude_cli", identity) if identity else None
    saved = service._record(row["id"]) if row and not is_frozen(row) else None
    if not saved or saved.get("health") != "ok" or not saved.get("credentials"):
        return None
    return saved if service.whose(saved["credentials"]) == identity else None


def _keep(service, credentials, owner):
    """Saves the live login under `owner`, its confirmed account, when that account's saved copy is older."""
    row = service.store.find("claude_cli", owner)
    saved = service._record(row["id"]) if row and not is_frozen(row) else None
    if row is None or is_frozen(row) or (saved and saved.get("fingerprint") == fingerprint(credentials)):
        return
    account = account_of(saved) if saved else {"accountUuid": owner, "emailAddress": row["email"],
                                               "organizationUuid": row["org"]}
    live = profile(credentials, account, service.clock())
    service.save({**live, "verified": live["fingerprint"]}, source="recover")


def _account_for(service, credentials, row, owner):
    """The `oauthAccount` entry that names `owner`: the saved account's own, else the provider's answer."""
    saved = service._record(row["id"]) if row else None
    if saved:
        return account_of(saved)
    try:
        return service.adapter.resolve_account(credentials, timeout=5)
    except DeckError:
        return {"accountUuid": owner}


class CLISwitch(Operation):
    """A terminal switch as an operation: manual, automatic or Undo. It changes only Claude Code's own
    files and closes nothing, so it may run while another client's switch waits for its sessions."""
    beside_waits = True

    def __init__(self, engine, key, *, kind="manual", reason="", leaving=None):
        row = engine.cli.row(key)
        super().__init__("switch", provider="claude_cli", account=key, label=row["label"], consent="now",
                         steps=(("switch", "Switching the terminal login"),))
        self.engine, self.auto_kind, self.reason, self.leaving = engine, kind, reason, leaving

    def run(self):
        with self.step("switch", phase="restoring", message="Switching the terminal login…", cancellable=False):
            manifest = diagnostics.Manifest.start(self.id, "switch", directory=self.engine.paths.data_dir / "logs" / "manifests")
            manifest.set("provider", "claude_cli").set("account", diagnostics.account_ref(self.account))
            self.report["manifest"] = self.id
            try:
                previous = perform(self.engine.cli, self.account, leaving=self.leaving)
            except BaseException as exc:
                manifest.set("verification.result", "failed").set("error_type", type(exc).__name__).finish()
                if self.beside and self.auto_kind == "manual" and isinstance(exc, DeckError):
                    # Beside a wait this operation has no report of its own, so the failure is said here.
                    self.engine.notice(f"Terminals were not switched to {self.label}. {exc}", "warning")
                raise
            now = self.engine.clock()
            with cli_auto.history(self.engine) as history:
                history.update(left=previous, left_at=now)
                if self.auto_kind == "manual":
                    history["manual_at"] = now
                    if history.get("last"):
                        history["last"]["undo_until"] = now
                else:
                    history["last"] = {"from": previous, "to": self.account, "at": now, "kind": self.auto_kind,
                        "reason": self.reason, "undo_until": now + 600}
                    if self.auto_kind == "early":
                        history["early_at"] = now
                    history["exhausted"] = False
            self.report.update(verified="verified", summary=f"Terminals now use {self.label}. Open terminals switch on their next message; the first reply may be slower.")
            self.engine.notice(self.report["summary"], "good")
            diagnostics.log("cli.switched", acct=diagnostics.account_ref(self.account), kind=self.auto_kind)
            manifest.set("verification.result", "verified").set("kind", self.auto_kind).finish()


def plan(engine, key, purpose="switch"):
    service = engine.cli
    if purpose == "recover":
        blockers = []
        try:
            service.guard()
            if service.enrollment or engine.runner.busy:
                raise DeckError("Finish the current terminal operation first.", "busy")
        except DeckError as exc:
            blockers.append({"code": exc.code, "message": str(exc)})
        planned = assemble(context(engine), provider="claude_cli", purpose="recover", account=None,
            target=None, current=None, headroom=None,
            running={"desktop": False, "sessions": [], "busy": 0, "unknown": 0, "foreign_claude": 0, "codex_consumers": []},
            pairs=(), blockers=blockers, warnings=[], tip=None,
            will=["Save the login Claude Code holds now under its own account.",
                  "Put back the account your terminals used before the interrupted switch, login and name "
                  "together. If you signed in to another account since, that one stays.",
                  "Set aside a record that cannot be read or undone. No terminal is closed."],
            preserved=["MCP logins, projects, settings and history"], account_bound=[], invisible=None,
            stop_tier="not_running")
        planned.plan.update(confirmation_required=True, remember_available=False)
        return register(context(engine), planned)
    row = service.row(key)
    service.require_local(key)
    views = engine.snapshot()["accounts"]
    target = next(a for a in views if a["id"] == key)
    current = next((a for a in views if a["provider"] == "claude_cli" and a["active"]), None)
    blockers = []
    try:
        service.guard()
        if service.enrollment or not engine.runner.free_for("claude_cli", beside_waits=True) or engine.recovery:
            raise DeckError("Finish the current operation or recovery first.", "busy")
        if row["flags"].get("health") == "needs_signin":
            raise DeckError("Sign in again before switching to this account.", "reauth_required")
        if target["active"]:
            raise DeckError("Your terminals already use this account.", "invalid")
    except DeckError as exc:
        blockers.append({"code": exc.code, "message": str(exc)})
    planned = assemble(context(engine), provider="claude_cli", purpose="switch", account=key,
        target=plan_target(target), current=plan_current(current) if current else None, headroom=None,
        running={"desktop": False, "sessions": [], "busy": 0, "unknown": 0, "foreign_claude": 0, "codex_consumers": []},
        pairs=(), blockers=blockers, warnings=[], tip=None,
        will=["Open terminals continue on the new account from their next message — nothing is closed.",
              "The first reply after switching is slower."], preserved=["MCP logins, projects, settings and history"],
        account_bound=[], invisible=None, stop_tier="not_running")
    planned.plan.update(confirmation_required=True, remember_available=False)
    if any(used is not None and used >= engine.prefs.get("cli_threshold") for _, used, _ in considered(target, engine.prefs.public())):
        planned.plan["warnings"].append({"code": "rate_limited", "message": "This account is over your auto-switch threshold. Auto-switch keeps your choice for 2 hours, unless the account runs out or needs sign-in."})
    return register(context(engine), planned)


def recover_pending(engine):
    """The Recover action for terminal records (and for a record nobody can read): one press settles each."""
    service = engine.cli

    def one(loaded):
        tx = CLITransaction(service, loaded.key)
        tx.doc = loaded.doc
        return settle(service, tx, final=True)
    with service.mutex, credential_locks(service.paths, timeout=service.lock_timeout,
                                         storage_write=service.adapter.store.storage_lock):
        return recovery.press(engine, "claude_cli", one)


def dispatch(engine, name, body):
    service = engine.cli
    key = body.get("id")
    if name == "switch-plan":
        return {"plan": plan(engine, key, body.get("purpose", "switch"))}
    service.guard()
    if name == "cli-exclude":
        service.set_excluded(key, body.get("excluded"))
        return {}
    if name == "add":
        service.begin_enrollment()
        return {}
    if name in ("finish-login", "cancel-login"):
        service.poll_enrollment(cancel=name == "cancel-login")
        return {}
    if name == "cli-undo":
        history = engine.store.setting("cli_history", {})
        last = history.get("last")
        if not last or last["undo_until"] < engine.clock():
            raise DeckError("Undo is no longer available.", "invalid")
        return {"plan": plan(engine, last["from"])}
    if name == "recover":
        for sentence in recover_pending(engine).sentences:
            engine.notice(sentence, "good")
        return {}
    if name == "switch":
        if engine.recovery or service.enrollment:
            raise DeckError("Finish the current operation or recovery first.", "busy")
        entry = engine.plans.get(body.get("plan_id"))
        if not entry or entry.get("provider") != "claude_cli" or entry.get("account") != key or entry["expires"] < engine.clock():
            raise DeckError("Confirm the terminal switch again.", "stale_plan")
        engine.plans.pop(body["plan_id"], None)
        return engine.submit(CLISwitch(engine, key))
    raise DeckError("That terminal action is not available.", "invalid")
