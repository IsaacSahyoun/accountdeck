"""Terminal login swap (the credentials file, or on macOS Claude Code's keychain item). Never launches,
stops or signals a process."""
import copy

from . import cli_auto, diagnostics
from .cli_locks import config_lock, credential_locks
from .cli_policy import considered
from .rules import plan_current, plan_target
from .core import DeckError, FileTransaction, atomic_write, json_bytes
from .operations import Operation
from .providers.claude_cli import merge, object_file, profile, shared
from .switching import assemble, context, register


class CLITransaction(FileTransaction):
    """Credentials use byte ownership (the file) or value ownership (a keychain item, `keychain_changes`);
    oauthAccount uses field ownership on rollback."""
    def __init__(self, service, key=None):
        super().__init__(service.vault, [service.paths.claude_cli_home], key)
        self.service = service
        self.doc["provider"] = "claude_cli"

    def write_login(self, credentials):
        """Writes the live login through the machine's CredStore, its before-state recorded first."""
        self.service.adapter.store.write(self, credentials)

    @property
    def wrote(self):
        return bool(self.doc["changes"] or self.doc.get("keychain_changes"))

    def write_account(self, target):
        service = self.service
        with config_lock(service.paths, timeout=service.lock_timeout) as check:
            current = object_file(service.paths.claude_user_config, missing=True)
            self.doc["oauth_change"] = {"present": "oauthAccount" in current,
                                        "file_present": service.paths.claude_user_config.exists(),
                                        "before": current.get("oauthAccount"), "after": copy.deepcopy(target)}
            self.save()
            current["oauthAccount"] = target
            check()
            atomic_write(service.paths.claude_user_config, json_bytes(current))

    def rollback(self):
        service = self.service
        conflict = False
        change = self.doc.get("oauth_change")
        if change:
            with config_lock(service.paths, timeout=service.lock_timeout) as check:
                try:
                    current = object_file(service.paths.claude_user_config,
                                          missing=change.get("file_present") is False)
                except DeckError:
                    conflict = True
                else:
                    if current.get("oauthAccount") == change["after"]:
                        if change["present"]:
                            current["oauthAccount"] = change["before"]
                        else:
                            current.pop("oauthAccount", None)
                        check()
                        if not current and change.get("file_present") is False:
                            service.paths.claude_user_config.unlink(missing_ok=True)
                        else:
                            atomic_write(service.paths.claude_user_config, json_bytes(current))
                    elif current.get("oauthAccount") != change["before"]:
                        conflict = True
        conflict |= service.adapter.store.rollback(self)
        super().rollback()
        if conflict:
            self.doc["status"] = "conflict"
            self.save()
            raise DeckError("Claude Code changed its account after the switch. The encrypted backup was preserved.", "recovery")


def perform(service, key):
    """§6, shared by manual/automatic/Undo switches; returns the outgoing account id."""
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
                # Re-read under CC's locks, then persist outgoing rotations BEFORE writing.
                # Only the Mac store permits initial setup without an outgoing login.
                # missing=True accepts absence, never unreadable or malformed existing data.
                fresh_mac = store.kind == "keychain"
                absent = False
                try:
                    credentials = store.read()
                except DeckError as exc:
                    if not fresh_mac or exc.code != "login_needed":
                        raise
                    credentials, absent = {}, True
                config = object_file(service.paths.claude_user_config, missing=fresh_mac)
                account_uuid = (config.get("oauthAccount") or {}).get("accountUuid")
                if account_uuid == target["identity"]:
                    raise DeckError("Your terminals already use this account.", "invalid")
                if absent and account_uuid is None:
                    previous = None
                else:
                    try:
                        outgoing = service.crosscheck(profile(credentials, config.get("oauthAccount"), service.clock()))
                    except DeckError as exc:
                        known = service.store.find("claude_cli", account_uuid)
                        if exc.code != "login_needed" or known is None or known["flags"].get("health") != "needs_signin":
                            raise
                        previous = known["id"]  # blank revoked stub: retain the last encrypted generation
                    else:
                        previous = service.save(outgoing, source="sync-back")
                composed = merge(target["credentials"], credentials)
                expected_shared = shared(credentials)
                account = target.get("oauthAccount") or {"accountUuid": target["identity"],
                    "emailAddress": target["email"], "organizationUuid": target["organizationUuid"]}
                tx.doc["oauth_change"] = {"present": "oauthAccount" in config,
                    "file_present": service.paths.claude_user_config.exists(),
                    "before": config.get("oauthAccount"), "after": copy.deepcopy(account)}
                tx.save()     # encrypted account-field recovery exists before either live write
                check()
                tx.write_login(composed)
                tx.write_account(account)
                check()
            # Verify after releasing CC's locks as required. On failure reacquire before
            # conditional rollback, so recovery cannot interleave with CC's refresh.
            verified = service.adapter.current()
            if (verified["identity"] != target["identity"] or verified["fingerprint"] != target["fingerprint"]
                    or shared(store.read()) != expected_shared):
                raise DeckError("The terminal account could not be verified.", "identity")
            tx.commit()
            service.engine.active_identity["claude_cli"] = target["identity"]
            service.engine.identity_known["claude_cli"] = True
            service.engine.bump()
            return previous
        except BaseException:
            if tx.wrote:
                with credential_locks(service.paths, timeout=service.lock_timeout, storage_write=store.storage_lock):
                    tx.rollback()
            raise


class CLISwitch(Operation):
    def __init__(self, engine, key, *, kind="manual", reason=""):
        row = engine.cli.row(key)
        super().__init__("switch", provider="claude_cli", account=key, label=row["label"], consent="now",
                         steps=(("switch", "Switching the terminal login"),))
        self.engine, self.auto_kind, self.reason = engine, kind, reason

    def run(self):
        with self.step("switch", phase="restoring", message="Switching the terminal login…", cancellable=False):
            manifest = diagnostics.Manifest.start(self.id, "switch", directory=self.engine.paths.data_dir / "logs" / "manifests")
            manifest.set("provider", "claude_cli").set("account", diagnostics.account_ref(self.account))
            self.report["manifest"] = self.id
            try:
                previous = perform(self.engine.cli, self.account)
            except BaseException as exc:
                manifest.set("verification.result", "failed").set("error_type", type(exc).__name__).finish()
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
            will=["Restore only terminal login fields still owned by the interrupted switch. No terminal is closed."],
            preserved=["Later application changes"], account_bound=[], invisible=None, stop_tier="not_running")
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
        if service.enrollment or engine.runner.busy or engine.recovery:
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
        planned.plan["warnings"].append({"code": "rate_limited", "message": "This account is over your auto-switch threshold; auto-switch may fire again."})
    return register(context(engine), planned)


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
        with service.mutex, credential_locks(service.paths, timeout=service.lock_timeout,
                                             storage_write=service.adapter.store.storage_lock):
            for tx_key in engine.recovery:
                doc = service.vault.get(tx_key)
                if doc.get("provider") == "claude_cli":
                    tx = CLITransaction(service, tx_key)
                    tx.doc = doc
                    tx.rollback()
        engine.recovery = engine._scan_recovery()
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
