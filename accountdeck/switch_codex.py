"""Consent-bound Codex switching; only auth.json is exchanged.

The consumers are what the process controller classifies (ChatGPT.app, the codex processes it bundles,
Codex CLI terminals); Codex Desktop is never signalled, and the Codex CLI is the one
`Codex.executable()` resolves."""
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
import tempfile
import uuid

from . import recovery
from .rules import plan_current, plan_target
from .core import DeckError, is_frozen, read_json
from .file_switch import FileSwitch, hold_checks
from .platform import facts_mac
from .procs import ProcessSet, unix_time
from .providers.codex import RPC, Codex, daemon_settings, freshness, identity_key, token_state
from .switching import DryRun, QuickOp, assemble, claim, context, note, register, submit


def _valid_uuid(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


def processes(ctx):
    rows = ctx.procs.snapshot()
    kinds = ctx.procs.classify(rows)
    consumers = ctx.procs.codex_consumers()
    daemons = kinds["codex_daemon"]
    return consumers, daemons, ProcessSet.of(consumers + daemons)


def build_plan(ctx, account, purpose="switch", *, ignore_op=None):
    if purpose not in ("switch", "capture", "add", "finish-login", "cancel-login", "recover"):
        raise DeckError("That Codex action is unavailable.", "invalid")
    engine = ctx.engine
    if purpose == "switch" and is_frozen(engine.store.account(account)):
        raise DeckError("This account's sign-in belongs to another computer.", "read_only")
    engine.sense_active()
    views = [a for a in engine.snapshot()["accounts"] if a["provider"] == "codex"]
    current = next((a for a in views if a["active"]), None)
    target = next((a for a in views if a["id"] == account), None) if purpose == "switch" else None
    if purpose == "switch" and (target is None or not target["switchable"]):
        raise DeckError("Choose a saved Codex account.", "not_found")
    if target and target["active"]:
        raise DeckError("This account is already active.", "invalid")
    consumers, daemons, found = processes(ctx)
    blockers = []
    if engine.read_only:
        blockers.append(note("read_only", "Another AccountDeck 2 is managing your sign-ins. Quit it to make changes here."))
    if not engine.identity_known["codex"] and purpose != "recover":
        blockers.append(note("identity", "The current Codex sign-in cannot be identified. Nothing will change."))
    if engine.recovery and purpose != "recover":
        blockers.append(note("recovery", "Finish the pending recovery first."))
    if purpose == "recover" and found.pairs:
        blockers.append(note("busy", "Close Codex and its daemon before recovering the previous sign-in."))
    if (engine.login and purpose not in ("finish-login", "cancel-login")) or engine.pending and engine.pending["id"] != ignore_op:
        blockers.append(note("busy", "Finish or cancel the current action first."))
    # ctime 0 means "start time unknown" (on macOS, e.g. another user's process): unverifiable, so it blocks.
    if len(daemons) > 1 or any(not r.ctime or r.family == "?" or r.kind == "unknown" for r in consumers + daemons):
        blockers.append(note("safety", "The Codex process set cannot be verified."))
    try:
        ctx.codex.validate_store()
        if target:
            profile = ctx.vault.get(account)
            if identity_key(profile) != engine.store.account(account)["identity"]:
                raise DeckError("The saved sign-in has a different identity.", "identity")
            ctx.codex.restore(profile, DryRun())
    except DeckError as exc:
        blockers.append(note(exc.code, str(exc)))
    open_now = [{"pid": r.pid, "kind": r.kind.removeprefix("codex_"), "started": unix_time(r.ctime), "parent": None}
                for r in consumers]
    saved = current is not None and not current["saved"] and purpose == "switch"
    result = assemble(ctx, provider="codex", purpose=purpose, account=account, target=plan_target(target) if target else None,
                      current=plan_current(current) if current else None, consumers=open_now,
                      pairs=found.pairs, blockers=blockers, warnings=[],
                      will=(["Wait for Codex sessions to close.", "Save the outgoing sign-in.",
                             "Replace only auth.json and verify the selected account."] if purpose == "switch" else
                            ["Use an isolated sign-in folder. The current Codex sign-in stays in place."]),
                      preserved=["Configuration, MCP servers, plugins and session history"], saves_outgoing=saved)
    result.entry["identity"] = engine.active_identity["codex"]
    return result


def login_file(ctx) -> recovery.FileLogin:
    """Codex's auth.json as recovery.put_back sees it."""
    path = ctx.codex.codex_home / "auth.json"

    def live():
        try:
            return read_json(path)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise DeckError("Codex's auth.json could not be read.", "login_needed") from None

    def keep(doc):
        if identity_key(doc) is not None:
            ctx.accounts.save("codex", doc, ctx.codex.identity(doc), source="recover", owner="v2")
    return recovery.FileLogin("codex", path, running=lambda: bool(processes(ctx)[2].pairs),
                              busy="Close Codex before recovering.", live=live, keep=keep, identity_of=identity_key)


def plan_codex_switch(ctx, account=None, purpose="switch", *, ignore_op=None):
    return register(ctx, build_plan(ctx, account, purpose, ignore_op=ignore_op))


class CodexSwitch(FileSwitch):
    client = "Codex"
    stopped = False             # the daemon was stopped for the switch; it is started again afterwards

    @property
    def adapter(self):
        return self.ctx.codex

    def login_file(self):
        return login_file(self.ctx)

    def wait(self, stack):
        ctx = self.ctx
        _, daemons, _ = self.sessions_closed(lambda: processes(ctx),
                                             lambda: plan_codex_switch(ctx, self.account, ignore_op=self.id))
        self.set_phase("stopping", "Stopping the Codex daemon…", cancellable=False)
        if daemons:
            self.codex_exe = ctx.codex.executable()
            result = ctx.procs.codex_daemon_stop(self.codex_exe, ctx.paths.codex_home, daemon_settings(ctx.paths.codex_home))
            if result == "timeout":
                raise DeckError("The Codex daemon is still running. Nothing was changed.", "busy")
            self.stopped = True
        if processes(ctx)[2].pairs:
            raise DeckError("Codex is still running. Nothing was changed.", "busy")

    def save(self):
        if self.ctx.engine.active_identity["codex"] is not None:
            self.ctx.accounts.capture("codex", owner="v2")

    def renew(self, profile):
        """A saved login with less than a day left is renewed first, and Codex is handed the renewed copy."""
        ctx = self.ctx
        remaining = token_state(profile, ctx.clock()).remaining
        if remaining is None or remaining >= 86400:
            return profile
        reading = ctx.codex.usage(profile, mode="refresh", owner=self.target["owner"],
                                  persist=lambda value: ctx.vault.put_guarded(self.account, value, "refresh", identity_key,
                                                                              freshness, expected=profile),
                                  quarantine=lambda raw: ctx.vault.quarantine(self.account, raw), account=self.account)
        if reading.error is not None:
            raise reading.error
        return ctx.vault.get(self.account)

    def verify(self, profile):
        ctx = self.ctx
        if ctx.codex.active_identity().identity != self.target["identity"]:
            raise DeckError("Codex did not retain the selected sign-in.", "identity")
        reading = ctx.codex.usage(profile, mode="read_only")
        if getattr(reading.error, "kind", None) in ("identity", "unauthorized", "refresh_rejected"):
            self.report.update(verified="failed", verify_reason="The provider rejected the selected sign-in.")
            raise DeckError("The selected Codex sign-in could not be verified.", "identity")
        return self.confirmed(reading, "The provider check is unavailable; the local identity matches.")

    def afterwards(self):
        if self.stopped:
            self.ctx.procs.codex_daemon_start(self.codex_exe, self.ctx.paths.codex_home)


def dispatch(engine, name, body):
    ctx = context(engine)
    account = body.get("id")
    if name == "switch-plan":
        return {"plan": plan_codex_switch(ctx, account, body.get("purpose", "switch"))}
    if name == "recover":
        claimed = claim(ctx, "recover", body, provider="codex", build=build_plan)
        def recover():
            if engine.read_only:
                raise DeckError("Codex sign-ins are read-only.", "read_only")
            client = login_file(ctx)
            with ExitStack() as stack:
                hold_checks(ctx, "codex", "Codex", stack)
                return recovery.summary(recovery.press(engine, "codex", lambda tx: recovery.put_back(ctx, tx, client)))
        return submit(ctx, claimed, QuickOp("recover", recover, provider="codex", message="Recovering Codex…",
                                           step=("recover", "Restoring the previous sign-in")))
    if name in ("add", "finish-login", "cancel-login"):
        claimed = claim(ctx, name, body, provider="codex", build=build_plan)
        def login_action():
            if name == "add":
                flow = Enrollment(ctx, device=body.get("device_code") is True or body.get("method") == "device_code")
                engine.login = {"provider": "codex", "started": ctx.clock(), "url": flow.url,
                                "user_code": flow.user_code, "_flow": flow}
                engine.closers.append(flow.close)
                return "Open the sign-in page, then choose Save sign-in. Your current account stays in place."
            flow = (engine.login or {}).get("_flow")
            if flow is None:
                raise DeckError("No Codex sign-in is waiting.", "invalid")
            if name == "finish-login":
                flow.save()
            flow.close()
            engine.login = None
            return "Codex account saved." if name == "finish-login" else "Sign-in cancelled."
        return submit(ctx, claimed, QuickOp(name, login_action, provider="codex", message="Updating the Codex sign-in…",
                                           step=("login", "Codex sign-in")))
    if name in ("switch", "capture"):
        claimed = claim(ctx, name, body, account=account if name == "switch" else None,
                        provider="codex", build=build_plan)
        if name == "switch":
            return submit(ctx, claimed, CodexSwitch(ctx, claimed, engine.store.account(account)))
        def capture():
            ctx.accounts.capture("codex", label=body.get("label"), owner="v2")
            return "The current Codex account was saved."
        return submit(ctx, claimed, QuickOp("capture", capture, provider="codex", message="Saving Codex…",
                                           step=("capture", "Saving the sign-in")))
    if name == "open":
        if engine.read_only or engine.pending or engine.recovery:
            raise DeckError("Finish the current action before opening Codex.", "busy")
        session = body.get("session")
        if session is not None and not _valid_uuid(session):
            raise DeckError("Choose a valid session.", "invalid")
        ctx.procs.launch_codex(ctx.codex.executable(), ctx.paths.codex_home, ctx.settings.get("cwd"), session)
        return {}
    if name == "stop-consumer":
        entry = engine.plans.get(body.get("plan_id"))
        if not entry or entry["provider"] != "codex" or engine.clock() >= entry["expires"]:
            raise DeckError("Review the Codex switch again.", "stale_plan")
        claimed = claim(ctx, entry["purpose"], {**body, "consent": "now"}, account=entry["account"],
                        provider="codex", build=build_plan)
        row = next((r for r in ctx.procs.codex_consumers() if r.pid == body.get("pid")), None)
        if row is None:
            raise DeckError("That Codex session has ended.", "not_found")
        def stop():
            ctx.procs.stop_consumer(claimed.token, row)
            return "The selected Codex session was stopped."
        return submit(ctx, claimed, QuickOp("stop-consumer", stop, provider="codex", message="Stopping the selected session…",
                                           step=("stop", "Stopping the selected session")))
    raise DeckError("Sign in using Codex, then save the signed-in account here.", "unsupported")


class Enrollment:
    """A fresh helper home prevents the official login flow from revoking the current account."""
    def __init__(self, ctx, *, device=False):
        self.ctx, self.rpc, self.closed = ctx, None, False
        work = Path(ctx.paths.data_dir) / "work"
        work.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="login-", dir=work))
        self.url, self.user_code = None, None
        self.adapter = Codex(replace(ctx.paths, codex_home=self.home), ctx.procs)
        ctx.codex.setup_home(self.home)
        if ctx.engine.mode == "demo":
            from .config import Paths
            from .providers.fake import build_fake_home
            fake = Paths.synthetic(self.home / "synthetic")
            build_fake_home(fake, codex_identity=str(uuid.uuid4()))
            self.adapter = Codex(fake, ctx.procs)
            return
        try:
            self.rpc = RPC(ctx.codex.command(), self.home, procs=ctx.procs, grace=ctx.timings.helper_refresh_grace)
            response = self.rpc.call("account/login/start", {"type": "chatgptDeviceCode" if device else "chatgpt"})
            self.url = response.get("authUrl") or response.get("verificationUrl")
            self.user_code = response.get("userCode")
        except BaseException:
            self.close()
            raise

    def save(self):
        profile = self.adapter.current()
        info = self.adapter.identity(profile)
        self.ctx.accounts.save("codex", profile, info, source="login", owner="v2")

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.rpc is not None:
            self.rpc.close()
        # Preserve any completed sign-in before deleting a helper home, even on cancel/shutdown.
        auth = self.adapter.codex_home / "auth.json"
        if auth.exists():
            try:
                profile = self.adapter.current()
                self.ctx.accounts.save("codex", profile, self.adapter.identity(profile), source="login", owner="v2")
            except Exception:
                self.ctx.vault.quarantine("codex-login", auth.read_bytes())
        facts_mac.remove_work_directory(self.home, Path(self.ctx.paths.data_dir) / "work")
