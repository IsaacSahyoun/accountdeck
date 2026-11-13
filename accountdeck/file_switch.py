"""The switch of a client that keeps its whole login in one file and is closed while that file is replaced
(Codex, Muse). The frame and every check the two share are here; switch_codex and switch_muse add what only
their client needs."""
from contextlib import ExitStack
from pathlib import Path
import time

from . import recovery, waiting
from .core import DeckError, FileTransaction, is_frozen
from .diagnostics import Manifest
from .operations import Operation


def hold_checks(ctx, provider, client, stack):
    """Until `stack` closes, no usage check of `provider`'s accounts runs. A check can renew a login and save
    it, so a switch and a Recover hold every account while they read a saved login and write it."""
    for row in ctx.engine.store.accounts():
        if row["provider"] == provider:
            lock = ctx.scheduler.account_lock(row["id"])
            if not lock.acquire(timeout=ctx.timings.lock_wait):
                raise DeckError(f"A {client} usage check is still running.", "busy")
            stack.callback(lock.release)


class FileSwitch(Operation):
    """A subclass gives `client`, `adapter`, `login_file()` and the steps `wait`, `save` and `verify`; it may
    give `renew` and `afterwards`."""
    client = None           # the client's name as the user reads it: "Codex", "Muse"
    adapter = None          # its provider adapter: active_identity(), restore(profile, tx)

    def __init__(self, ctx, claimed, target):
        super().__init__("switch", provider=target["provider"], account=target["id"], label=target["label"],
                         consent=claimed.mode, token=claimed.token,
                         steps=(("wait", f"Waiting for {self.client} sessions"), ("save", "Saving the current sign-in"),
                                ("restore", "Restoring the selected sign-in"), ("verify", "Checking the sign-in")))
        self.ctx, self.claimed, self.target, self.tx = ctx, claimed, target, None

    def run(self):
        ctx, name = self.ctx, self.client
        manifest = Manifest.start(self.id, "switch", directory=Path(ctx.paths.data_dir) / "logs" / "manifests")
        self.report["manifest"] = self.id
        manifest.set("consent.mode", self.consent).set("consent.pair_count", len(self.token.pairs))
        outcome = "failed"
        ctx.scheduler.pause(self.provider)
        try:
            with ExitStack() as stack:
                try:
                    self._lock_accounts(stack)
                    with self.step("wait", phase="waiting_idle", message=f"Waiting for {name} sessions to close…"):
                        self.wait(stack)
                    with self.step("save", phase="saving", message=f"Saving the current {name} sign-in…", cancellable=False):
                        self.save()
                    with self.step("restore", phase="restoring", message="Restoring the selected sign-in…", cancellable=False):
                        profile = self._restore()
                    with self.step("verify", phase="verifying", message=f"Checking the selected {name} account…",
                                   cancellable=False):
                        verified = self.verify(profile)
                        self.tx.commit()
                        manifest.set("verification.result", verified)
                        outcome = "success"
                except BaseException:
                    # While the client runs again the record stays open: Recover puts it right once it is closed.
                    recovery.after_failure(self.tx, lambda tx: recovery.put_back(ctx, tx, self.login_file()))
                    raise
        finally:
            self.afterwards()
            ctx.scheduler.resume_provider(self.provider)
            manifest.set("outcome", outcome).finish()

    # ---- what a subclass gives ----
    def login_file(self) -> recovery.FileLogin:
        raise NotImplementedError

    def wait(self, stack):
        """The wait step: `sessions_closed`, then whatever else must stop before the file may change."""
        raise NotImplementedError

    def save(self):
        """The save step: the signed-in login goes to the vault under its own account."""
        raise NotImplementedError

    def renew(self, profile):
        """The saved login the client is handed: `profile`, or a copy renewed first."""
        return profile

    def verify(self, profile) -> str:
        """The verify step: "verified" or "unconfirmed" (see `confirmed`), or DeckError when the client or the
        provider did not take the login."""
        raise NotImplementedError

    def afterwards(self):
        """Runs once the switch is over, whatever its outcome."""

    # ---- the shared parts of the steps ----
    def _lock_accounts(self, stack):
        """No usage check of this client runs while `stack` lasts, and the account switched to is read again."""
        ctx = self.ctx
        hold_checks(ctx, self.provider, self.client, stack)
        self.target = ctx.engine.store.account(self.account)
        if self.target is None:
            raise DeckError("That account no longer exists.", "not_found")
        if is_frozen(self.target):
            raise DeckError("This account's sign-in belongs to another computer.", "read_only")

    def sessions_closed(self, look, replan):
        """Waits until the client has no session open (waiting.for_sessions), then checks again that the
        outgoing sign-in, the ownership of the logins and the confirmation still hold. Returns what `look()`
        saw last."""
        engine = self.ctx.engine
        seen = waiting.for_sessions(self, self.client, look, replan)
        engine.sense_active()
        if not engine.identity_known[self.provider] or engine.active_identity[self.provider] != self.claimed.entry["identity"]:
            raise DeckError("The outgoing sign-in changed. Review the switch again.", "identity")
        if engine.read_only:
            raise DeckError(f"{self.client} sign-ins are read-only.", "read_only")
        if time.time() >= self.token.expires:
            raise DeckError("The switch confirmation expired. Review it again.", "stale_plan")
        self.checkpoint()
        return seen

    def _restore(self):
        """The restore step: with the client still closed and still signed in as the outgoing account, the
        switch's record is saved and the selected login written. Returns that login."""
        ctx, engine, client = self.ctx, self.ctx.engine, self.login_file()
        if client.running():
            raise DeckError(f"{self.client} started again. Nothing was changed.", "busy")
        active = self.adapter.active_identity()
        if active.status == "unknown" or active.identity != self.claimed.entry["identity"]:
            raise DeckError("The outgoing sign-in changed.", "identity")
        profile = ctx.vault.get(self.account)
        if client.identity_of(profile) != self.target["identity"]:
            raise DeckError("The saved sign-in identity changed.", "identity")
        profile = self.renew(profile)
        if client.running() or engine.read_only:
            raise DeckError("The process set changed before the swap. Review it again.", "busy")
        self.tx = FileTransaction(ctx.vault, engine.roots())
        self.tx.doc.update(provider=self.provider, purpose="switch", outgoing=self.claimed.entry["identity"],
                           target=self.target["identity"])
        self.adapter.restore(profile, self.tx)
        return profile

    def confirmed(self, reading, unsure) -> str:
        """The verdict of a usage check made with the new login, written to the report: "verified" when the
        provider answered, else "unconfirmed" with `unsure` as the reason (the local sign-in does match)."""
        verified = "verified" if reading.error is None and reading.usage is not None else "unconfirmed"
        self.report.update(verified=verified, verified_at=self.ctx.clock() if verified == "verified" else None,
                           verify_reason=None if verified == "verified" else unsure,
                           summary=f"{self.client} is ready as {self.label}." +
                           (" The outgoing account was saved." if self.claimed.plan["saves_outgoing"] else ""))
        return verified
