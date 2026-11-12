"""Consent-bound Muse Code switching: only ~/.config/muse/auth.json is exchanged, and only between file-mode
logins (providers/muse.py explains why). Muse sessions are never signalled: a switch waits until they close.

Muse rewrites its auth.json whenever it runs (a usage read included) and may renew the tokens in it, so a
switch holds the Muse read lock while it changes the file, and undoing a switch never compares bytes: it saves
whatever Muse has signed in at that moment, then writes the outgoing account's newest saved copy (`undo`).

Add Account runs `muse login` in a private folder with TBH_CREDENTIAL_BACKEND=file, so the new sign-in lands
in a file AccountDeck can save, and the signed-in account and Muse's Keychain item stay as they are."""
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
import secrets
import tempfile
import time
import urllib.parse

from . import readings, recovery
from .contract import UNSAVED_IDS
from .core import DeckError, atomic_write, is_frozen, json_bytes
from .engine import muse_consumers
from .file_switch import FileSwitch
from .platform import facts_mac
from .procs import DEVNULL, PIPE, ProcessSet
from .providers.muse import FILE_BACKEND, LoginOutput, Muse, cleared_env, identity_key, is_file_login
from .rules import plan_current, plan_target
from .switching import DryRun, QuickOp, assemble, claim, context, note, register, submit

LOGIN_START_TIMEOUT = 30.0
LOGIN_EXIT_WAIT = 5.0
KILL_WAIT = 3.0
LOGIN_HOSTS = ("auth.meta.com", "www.meta.com", "meta.com")
NOT_APPROVED = "Approve the sign-in on Meta's page first, then choose Save sign-in."
LOGIN_ENDED = "The Muse sign-in ended without signing in (the code may have expired). Cancel, then use Add Account again."
BUSY_READ = "A Muse usage check is still running. Try again in a moment."
FILE_LOGIN_NOTE = ("Muse is signed out. The selected account is a file login, so start Muse with "
                   "TBH_CREDENTIAL_BACKEND=file (for example in ~/.zshrc) to use it.")
INCOMPLETE_NOTE = "The current Muse sign-in has no tokens left, so there is nothing of it to save."
WILL = {
    "switch": ["Wait for Muse sessions to close.", "Save the outgoing sign-in.",
               "Replace only Muse's auth.json and check the selected account."],
    "capture": ["Save the account Muse is signed in to. Nothing in Muse changes."],
    "add": ["Sign in through a private folder. The current Muse sign-in stays in place."],
    "finish-login": ["Save the new account. The current Muse sign-in stays in place."],
    "cancel-login": ["Stop the sign-in. A sign-in that already finished is still saved."],
    "recover": ["Save the account Muse is signed in to now.",
                "Put back the account Muse used before the interrupted switch."],
}


def processes(ctx):
    consumers = ctx.procs.muse_consumers()
    return consumers, ProcessSet.of(consumers)


def build_plan(ctx, account, purpose="switch", *, ignore_op=None):
    if purpose not in WILL:
        raise DeckError("That Muse action is unavailable.", "invalid")
    engine = ctx.engine
    if purpose == "switch" and is_frozen(engine.store.account(account)):
        raise DeckError("This account's sign-in belongs to another computer.", "read_only")
    engine.sense_active()
    views = [a for a in engine.snapshot()["accounts"] if a["provider"] == "muse"]
    current = next((a for a in views if a["active"]), None)
    target = next((a for a in views if a["id"] == account), None) if purpose == "switch" else None
    if purpose == "switch" and (target is None or not target["switchable"]):
        raise DeckError("Choose a saved Muse account.", "not_found")
    if target and target["active"]:
        raise DeckError("This account is already active.", "invalid")
    consumers, found = processes(ctx)
    blockers, warnings = [], []
    if engine.read_only:
        blockers.append(note("read_only", "Another AccountDeck 2 is managing your sign-ins. Quit it to make changes here."))
    if not engine.identity_known["muse"] and purpose in ("switch", "capture"):
        blockers.append(note("identity", "The current Muse sign-in cannot be identified. Nothing will change."))
    if engine.recovery and purpose != "recover":
        blockers.append(note("recovery", "Finish the pending recovery first."))
    if purpose == "recover" and found.pairs:
        blockers.append(note("busy", "Close Muse before recovering the previous sign-in."))
    if (engine.login and purpose not in ("finish-login", "cancel-login")) or engine.pending and engine.pending["id"] != ignore_op:
        blockers.append(note("busy", "Finish or cancel the current action first."))
    # ctime 0 means "start time unknown" (on macOS, e.g. another user's process): unverifiable, so it blocks.
    if any(not r.ctime for r in consumers):
        blockers.append(note("safety", "The Muse process set cannot be verified."))
    try:
        if purpose in ("add", "switch"):
            ctx.muse.command()                 # the switch checks the new sign-in with Muse itself
        if purpose in ("switch", "capture"):
            ctx.muse.validate_switchable()
        if target:
            profile = ctx.vault.get(account)
            if identity_key(profile) != engine.store.account(account)["identity"]:
                raise DeckError("The saved sign-in has a different identity.", "identity")
            ctx.muse.restore(profile, DryRun())
        if purpose == "switch":
            live = ctx.muse.live_document()
            if live is None:
                warnings.append(note("file_login", FILE_LOGIN_NOTE))
            elif not is_file_login(live):
                warnings.append(note("incomplete", INCOMPLETE_NOTE))
    except DeckError as exc:
        blockers.append(note(exc.code, str(exc)))
    running = {"desktop": False, "sessions": [], "busy": 0, "unknown": 0, "foreign_claude": 0,
               "codex_consumers": muse_consumers(consumers, {})}
    saved = current is not None and not current["saved"] and purpose == "switch"
    result = assemble(ctx, provider="muse", purpose=purpose, account=account, target=plan_target(target) if target else None,
                      current=plan_current(current) if current else None, headroom=None, running=running,
                      pairs=found.pairs, blockers=blockers, warnings=warnings, tip=None, will=list(WILL[purpose]),
                      preserved=["Muse settings, trusted folders and session history"], account_bound=[],
                      invisible=None, stop_tier="not_running", saves_outgoing=saved)
    result.entry["identity"] = engine.active_identity["muse"]
    return result


def plan_muse_switch(ctx, account=None, purpose="switch", *, ignore_op=None):
    return register(ctx, build_plan(ctx, account, purpose, ignore_op=ignore_op))


def hold_reads(ctx, stack):
    """Until `stack` closes, no Muse usage read runs: reads are paused, and one already running (it may be
    rewriting the live auth.json) is waited for. Every Muse account's lock comes first and the scheduler's Muse
    read lock last, the order a read takes them, so a save inside never waits on a read that waits on us."""
    ctx.scheduler.pause("muse")
    stack.callback(ctx.scheduler.resume_provider, "muse")
    accounts = [row["id"] for row in ctx.store.accounts() if row["provider"] == "muse"] + [UNSAVED_IDS["muse"]]
    for lock in [ctx.scheduler.account_lock(key) for key in accounts] + [ctx.scheduler.helper_lock("muse")]:
        if not lock.acquire(timeout=ctx.timings.lock_wait):
            raise DeckError(BUSY_READ, "busy")
        stack.callback(lock.release)


@contextmanager
def reads_held(ctx):
    with ExitStack() as stack:
        hold_reads(ctx, stack)
        yield


def login_file(ctx) -> recovery.FileLogin:
    """Muse's auth.json as recovery.put_back sees it."""
    muse = ctx.muse

    def keep(doc):
        if is_file_login(doc) and identity_key(doc):
            ctx.accounts.save("muse", doc, muse.identity(doc), source="recover")
    return recovery.FileLogin("muse", muse.auth_path, running=lambda: bool(processes(ctx)[1].pairs),
                              busy="Close your Muse sessions, then try again.", live=muse.live_document, keep=keep,
                              identity_of=identity_key)


class MuseSwitch(FileSwitch):
    client = "Muse"

    @property
    def adapter(self):
        return self.ctx.muse

    def login_file(self):
        return login_file(self.ctx)

    def wait(self, stack):
        ctx = self.ctx
        self.sessions_closed(lambda: processes(ctx), lambda: plan_muse_switch(ctx, self.account, ignore_op=self.id))
        # A usage read that started before the pause may still be rewriting the live auth.json.
        self.set_phase("saving", "Waiting for a Muse usage check to finish…", cancellable=False)
        hold_reads(ctx, stack)

    def save(self):
        ctx = self.ctx
        ctx.muse.validate_switchable()
        live = ctx.muse.live_document()
        if ctx.engine.active_identity["muse"] is not None and is_file_login(live):
            ctx.accounts.capture("muse", owner="v2")
        elif live is not None:
            self.note(INCOMPLETE_NOTE, status="skipped")

    def verify(self, profile):
        ctx = self.ctx
        if ctx.muse.active_identity().identity != self.target["identity"]:
            raise DeckError("Muse did not keep the selected sign-in.", "identity")
        reading = ctx.muse.usage(live=True, account=self.account)
        if getattr(reading.error, "kind", None) in ("identity", "refresh_rejected"):
            self.report.update(verified="failed", verify_reason="Meta rejected the selected sign-in.")
            raise DeckError("The selected Muse sign-in could not be verified. Sign in again with Add Account.",
                            "reauth_required")
        ctx.accounts.keep_live_muse(self.account, self.target["identity"])
        verified = self.confirmed(reading, "Muse could not report usage right now; the local sign-in matches.")
        if reading.usage is not None:
            readings.record(ctx.store, self.account, reading.usage)
        return verified


def recover_pending(ctx):
    """Settles every unfinished Muse switch (the Recover action). No Muse usage read runs meanwhile: a read
    rewrites the live auth.json."""
    engine = ctx.engine
    if engine.read_only:
        raise DeckError("Muse sign-ins are read-only.", "read_only")
    client = login_file(ctx)
    with reads_held(ctx):
        return recovery.summary(recovery.press(engine, "muse", lambda tx: recovery.put_back(ctx, tx, client)))


def dispatch(engine, name, body):
    ctx = context(engine)
    account = body.get("id")
    if name == "switch-plan":
        return {"plan": plan_muse_switch(ctx, account, body.get("purpose", "switch"))}
    if name == "recover":
        claimed = claim(ctx, "recover", body, provider="muse", build=build_plan)
        return submit(ctx, claimed, QuickOp("recover", lambda: recover_pending(ctx), provider="muse",
                                           message="Recovering Muse…", step=("recover", "Restoring the previous sign-in")))
    if name in ("add", "finish-login", "cancel-login"):
        claimed = claim(ctx, name, body, provider="muse", build=build_plan)
        def login_action():
            if name == "add":
                flow = Enrollment(ctx)
                engine.login = {"provider": "muse", "started": ctx.clock(), "url": flow.url,
                                "user_code": flow.user_code, "_flow": flow}
                engine.closers.append(flow.close)
                return "Open Meta's sign-in page, approve the code, then choose Save sign-in. Your current account stays in place."
            flow = (engine.login or {}).get("_flow")
            if flow is None or (engine.login or {}).get("provider") != "muse":
                raise DeckError("No Muse sign-in is waiting.", "invalid")
            if name == "finish-login":
                flow.save()
            flow.close()
            engine.login = None
            return "Muse account saved." if name == "finish-login" else "Sign-in cancelled."
        return submit(ctx, claimed, QuickOp(name, login_action, provider="muse", message="Updating the Muse sign-in…",
                                           step=("login", "Muse sign-in")))
    if name in ("switch", "capture"):
        claimed = claim(ctx, name, body, account=account if name == "switch" else None,
                        provider="muse", build=build_plan)
        if name == "switch":
            return submit(ctx, claimed, MuseSwitch(ctx, claimed, engine.store.account(account)))
        def capture():
            with reads_held(ctx):
                ctx.accounts.capture("muse", label=body.get("label"), owner="v2")
            return "The current Muse account was saved."
        return submit(ctx, claimed, QuickOp("capture", capture, provider="muse", message="Saving Muse…",
                                           step=("capture", "Saving the sign-in")))
    raise DeckError("Sign in using Muse, then save the signed-in account here.", "unsupported")


class Enrollment:
    """`muse login` in a private config folder with file storage. The new sign-in lands in that folder's
    auth.json; the signed-in account, its auth.json and Muse's Keychain item are never touched."""

    def __init__(self, ctx):
        self.ctx, self.process, self.output, self.closed = ctx, None, None, False
        self.work = Path(ctx.paths.data_dir) / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="muse-login-", dir=self.work))
        self.adapter = Muse(replace(ctx.paths, muse_config=self.home / "muse"), ctx.procs)
        self.url, self.user_code = None, None
        if ctx.engine.mode == "demo":
            from .providers.fake import muse_auth
            self.url, self.user_code = "https://auth.meta.com/oauth/device/", "DEMO-CODE"
            email = f"muse-{secrets.token_hex(3)}@example.test"
            atomic_write(self.home / "muse" / "auth.json", json_bytes(muse_auth(email, storage=None, now=ctx.clock())))
            return
        try:
            env = {"XDG_CONFIG_HOME": str(self.home), "HOME": str(ctx.paths.home), **FILE_BACKEND,
                   "XDG_DATA_HOME": str(self.home / "data"), "XDG_STATE_HOME": str(self.home / "state"),
                   "XDG_CACHE_HOME": str(self.home / "cache")}
            self.process = ctx.procs.spawn_helper(ctx.muse.command() + ["login"], home=self.home, cwd=str(self.home), env=env,
                                                  remove_env=cleared_env(), stdin=DEVNULL, stdout=PIPE, stderr=DEVNULL,
                                                  text=True, encoding="utf-8")
            self.output = LoginOutput(self.process.stdout)
            if not self.output.wait_ready(LOGIN_START_TIMEOUT):
                raise DeckError("Muse could not start a sign-in. Check your connection, then try again.", "network")
            if urllib.parse.urlsplit(self.output.url).hostname not in LOGIN_HOSTS:
                raise DeckError("Muse offered an unexpected sign-in page. Nothing was opened.", "safety")
            self.url, self.user_code = self.output.url, self.output.code
        except BaseException:
            self.close()
            raise

    def save(self):
        if self.process is not None and self.process.poll() is None:
            # `muse login` exits once Meta approved the code; give it a moment to finish writing.
            end = time.monotonic() + LOGIN_EXIT_WAIT
            while self.process.poll() is None and time.monotonic() < end:
                time.sleep(0.1)
            if self.process.poll() is None:
                raise DeckError(NOT_APPROVED, "login_needed")
        if not self.adapter.auth_path.exists():
            raise DeckError(NOT_APPROVED if self.process is None else LOGIN_ENDED, "login_needed")
        profile = self.adapter.current()
        info = self.adapter.identity(profile)
        self.ctx.accounts.save("muse", profile, info, source="login", owner="v2")

    def close(self):
        if self.closed:
            return
        self.closed = True
        process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(KILL_WAIT)
            except Exception:
                process.kill()
                process.wait(KILL_WAIT)
        if process is not None and process.stdout is not None:
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass
        # Preserve a completed sign-in before deleting the private folder, even on cancel or shutdown.
        auth = self.adapter.auth_path
        if auth.exists():
            try:
                profile = self.adapter.current()
                self.ctx.accounts.save("muse", profile, self.adapter.identity(profile), source="login", owner="v2")
            except Exception:
                self.ctx.vault.quarantine("muse-login", auth.read_bytes())
        facts_mac.remove_work_directory(self.home, self.work)
