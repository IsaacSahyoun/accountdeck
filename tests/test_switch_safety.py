"""Everything that acts on a login, for all four clients: switching, waiting, recovery and the stored copies.
Each test runs a whole engine on the demo's fake data in safe mode; the real apps, their logins and the
Keychain are never touched."""
import dataclasses
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
import uuid

from tests import support

from accountdeck import cli_auto, switch_cli, switching
from accountdeck.core import DeckError, FileTransaction, Vault, read_json, recover_transactions
from accountdeck.operations import Operation
from accountdeck.providers import codex as codex_module
from accountdeck.providers.fake import test_cipher, unsigned_jwt
from accountdeck.providers.muse import identity_key as muse_identity


class Crash(BaseException):
    """Stands in for the app dying: nothing after the point it is raised at runs, not even the undo."""


class Waiting(Operation):
    """Another operation sitting in its wait for sessions to close, until the test releases it."""

    def __init__(self, provider):
        super().__init__("switch", provider=provider, label="stand-in", consent="idle",
                         steps=(("wait", "Waiting for sessions to close"),))
        self.release = threading.Event()

    def run(self):
        with self.step("wait", phase="waiting_idle", message="Waiting for sessions to close…", cancellable=True):
            while not self.release.wait(0.05):
                self.checkpoint()


def codex_login(email, *, now, refresh="synthetic-refresh-side"):
    """A Codex auth.json for a new synthetic account."""
    identity = str(uuid.uuid4())
    claims = {codex_module.AUTH_CLAIMS: {"chatgpt_account_id": identity, "chatgpt_plan_type": "plus"}}
    exp = int(now + 9 * 86400)
    return {"OPENAI_API_KEY": None, "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "tokens": {"id_token": unsigned_jwt({**claims, "email": email, "exp": exp}),
                       "access_token": unsigned_jwt({**claims, "exp": exp}), "refresh_token": refresh,
                       "account_id": identity}}


class DeckCase(unittest.TestCase):
    """The `terminal` demo (accounts of all four clients) with its tick thread off."""

    def setUp(self):
        self.engine = support.demo_engine(self, "terminal")
        self.paths, self.procs, self.vault = self.engine.paths, self.engine.procs, self.engine.vault
        # The Desktop stop ladder waits `idle_hold` seconds on real clocks; tests need the steps, not the wait.
        self.engine.timings = dataclasses.replace(self.engine.timings, idle_hold=0.2, idle_poll=0.05)

    # ---- accounts ----
    def rows(self, provider):
        return [a for a in self.engine.snapshot()["accounts"] if a["provider"] == provider]

    def active(self, provider):
        return next((a for a in self.rows(provider) if a["active"]), None)

    def spare(self, provider):
        """A saved account of `provider` that is not signed in and can be switched to."""
        return next(a for a in self.rows(provider)
                    if not a["active"] and a["switchable"] and a.get("health", "ok") == "ok" and not a.get("excluded"))

    def add_codex_account(self, email="codex-side@example.test"):
        codex = self.engine.providers["codex"]
        login = codex_login(email, now=self.engine.clock())
        return self.engine.accounts.save("codex", login, codex.identity(login), source="login", owner="v2")

    def close_codex(self):
        for row in self.procs.snapshot():
            if row.kind.startswith("codex"):
                self.procs.exit_process(row.pid)

    def close_claude(self):
        for row in self.procs.claude_desktop():
            self.procs.exit_process(row.pid)

    # ---- who is signed in, read from each client's own files ----
    def signed_in(self, provider):
        if provider == "claude_cli":
            disk = support.terminal_on_disk(self.engine)
            self.assertEqual(disk["name"], disk["token"], "the account name and the login token belong to different accounts")
            return disk["name"]
        if provider == "codex":
            return codex_module.identity_key(read_json(Path(self.paths.codex_home) / "auth.json"))
        if provider == "muse":
            return muse_identity(read_json(Path(self.paths.muse_config) / "auth.json"))
        return read_json(Path(self.paths.claude_profile) / "config.json").get("lastKnownAccountUuid")

    def identity(self, key):
        return self.engine.store.account(key)["identity"]

    # ---- actions ----
    def plan(self, provider, purpose="switch", account=None):
        body = {"purpose": purpose, "provider": provider}
        if account:
            body["id"] = account
        return self.engine.action("switch-plan", body)["plan"]

    def act(self, provider, name, account=None, *, consent="now", wait=True):
        """Confirms the plan for `name` and runs it; returns the finished report (None when not waited for)."""
        plan = self.plan(provider, name, account)
        self.assertEqual(plan["blockers"], [], plan["blockers"])
        body = {"provider": provider, "plan_id": plan["plan_id"], "consent": consent}
        if account:
            body["id"] = account
        self.engine.action(name, body)
        if not wait:
            return None
        self.assertTrue(self.engine.runner.wait(120), "the operation did not finish")
        return self.engine.snapshot()["report"]

    def wait_for_phase(self, phase, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pending = self.engine.snapshot()["pending"]
            if pending and pending["phase"] == phase:
                return pending
            time.sleep(0.02)
        self.fail(f"the operation never reached {phase}")

    def pending_records(self):
        self.engine.recovery = self.engine._scan_recovery()
        return self.engine.recovery

    def assert_consistent(self, provider, expected_identity=None):
        """One account everywhere: in the client's files and in what AccountDeck reports."""
        found = self.signed_in(provider)
        if expected_identity is not None:
            self.assertEqual(found, expected_identity)
        self.engine.sense_active()
        active = self.active(provider)
        self.assertIsNotNone(active, "AccountDeck reports nobody signed in")
        self.assertEqual(self.identity(active["id"]), found, "AccountDeck reports a different account")


class TerminalSwitchTest(DeckCase):
    def setUp(self):
        super().setUp()
        self.service = self.engine.cli
        self.outgoing, self.target = self.active("claude_cli"), self.spare("claude_cli")

    def live_credentials(self):
        return json.loads(Path(self.paths.claude_cli_credentials).read_text())

    def test_a_switch_swaps_the_login_and_the_account_name_together(self):
        shared_before = {k: v for k, v in self.live_credentials().items() if k in ("mcpOAuth", "pluginSecrets")}
        self.assertTrue(shared_before)
        previous = switch_cli.perform(self.service, self.target["id"])
        self.assertEqual(previous, self.outgoing["id"])
        self.assert_consistent("claude_cli", self.identity(self.target["id"]))
        live = self.live_credentials()
        self.assertEqual({k: v for k, v in live.items() if k in shared_before}, shared_before,
                         "MCP logins and plugin secrets belong to the machine, not the account")
        self.assertEqual(live["claudeAiOauth"]["refreshToken"],
                         self.vault.get(self.target["id"])["credentials"]["claudeAiOauth"]["refreshToken"])
        self.assertEqual(self.pending_records(), [])

    def test_a_switch_saves_the_login_claude_code_was_using(self):
        support.claude_code_renews(self.engine)        # the outgoing login is newer on disk than in the vault
        renewed = self.live_credentials()["claudeAiOauth"]["refreshToken"]
        switch_cli.perform(self.service, self.target["id"])
        self.assertEqual(self.vault.get(self.outgoing["id"])["credentials"]["claudeAiOauth"]["refreshToken"], renewed)

    def test_a_refused_switch_changes_nothing(self):
        needs_sign_in = next(a for a in self.rows("claude_cli") if a.get("health") == "needs_signin")
        before = (Path(self.paths.claude_cli_credentials).read_bytes(), Path(self.paths.claude_user_config).read_bytes())
        with self.assertRaises(DeckError) as raised:
            switch_cli.perform(self.service, needs_sign_in["id"])
        self.assertEqual(raised.exception.code, "reauth_required")
        self.assertEqual((Path(self.paths.claude_cli_credentials).read_bytes(),
                          Path(self.paths.claude_user_config).read_bytes()), before)
        self.assert_consistent("claude_cli", self.identity(self.outgoing["id"]))

    def test_a_confirmed_switch_runs_as_an_operation_and_notes_the_manual_choice(self):
        report = self.act("claude_cli", "switch", self.target["id"])
        self.assertEqual(report["outcome"], "success", report)
        self.assert_consistent("claude_cli", self.identity(self.target["id"]))
        history = self.engine.store.setting("cli_history", {})
        self.assertEqual(history["left"], self.outgoing["id"])
        self.assertEqual(history["manual_at"], history["left_at"])

    def test_a_switch_that_fails_its_own_check_puts_the_old_login_back(self):
        real = self.service.adapter.current
        calls = []

        def current():           # the mirror before the swap reads normally; the check after it fails
            calls.append(1)
            if len(calls) > 2:
                raise DeckError("Claude Code's login is changing. Try again shortly.", "busy")
            return real()
        self.service.adapter.current = current
        with self.assertRaises(DeckError):
            switch_cli.perform(self.service, self.target["id"])
        self.service.adapter.current = real
        self.assert_consistent("claude_cli", self.identity(self.outgoing["id"]))
        self.assertEqual(self.pending_records(), [])

    def test_one_press_of_recover_settles_a_switch_the_app_died_in(self):
        real = switch_cli.CLITransaction.commit
        switch_cli.CLITransaction.commit = lambda tx: (_ for _ in ()).throw(Crash())
        undo = switch_cli.CLITransaction.rollback
        switch_cli.CLITransaction.rollback = lambda tx: None       # a dead app undoes nothing
        self.addCleanup(setattr, switch_cli.CLITransaction, "commit", real)
        self.addCleanup(setattr, switch_cli.CLITransaction, "rollback", undo)
        with self.assertRaises(Crash):
            switch_cli.perform(self.service, self.target["id"])
        switch_cli.CLITransaction.commit, switch_cli.CLITransaction.rollback = real, undo
        self.assertEqual(len(self.pending_records()), 1)
        switching.dispatch(self.engine, "recover", {})
        self.assertEqual(self.pending_records(), [])
        self.assert_consistent("claude_cli")
        self.assertEqual(switch_cli.plan(self.engine, self.spare("claude_cli")["id"])["blockers"], [])


class TerminalAutoSwitchTest(DeckCase):
    def setUp(self):
        super().setUp()
        self.engine.sense_active()
        self.active_row = self.active("claude_cli")
        support.with_reading(self.engine, self.active_row["id"], session=97)
        for row in self.engine.store.accounts():
            if row["provider"] == "claude_cli" and row["id"] != self.active_row["id"] and row["last_good_usage"]:
                support.with_reading(self.engine, row["id"], session=20, weekly=20)

    def test_it_switches_away_from_an_account_over_the_threshold(self):
        self.assertEqual(cli_auto.decision(self.engine)["action"], "switch")
        cli_auto.tick(self.engine)
        self.assertTrue(self.engine.runner.wait(30))
        self.assertNotEqual(self.active("claude_cli")["id"], self.active_row["id"])

    def test_it_stands_down_while_a_terminal_operation_runs(self):
        running = Waiting("claude_cli")
        self.addCleanup(running.release.set)
        self.engine.submit(running)
        self.wait_for_phase("waiting_idle")
        cli_auto.tick(self.engine)
        time.sleep(0.2)
        self.assertEqual(self.active("claude_cli")["id"], self.active_row["id"])
        running.release.set()
        self.assertTrue(running.wait(10))

    def test_it_stands_down_while_a_record_waits_for_recovery(self):
        self.engine.recovery = ["transaction-" + "0" * 32]
        cli_auto.tick(self.engine)
        self.assertFalse(self.engine.runner.busy)
        self.assertEqual(self.active("claude_cli")["id"], self.active_row["id"])


class CodexSwitchTest(DeckCase):
    def setUp(self):
        super().setUp()
        self.side = self.add_codex_account()
        self.main = self.active("codex")
        self.auth = Path(self.paths.codex_home) / "auth.json"

    def test_a_switch_saves_the_outgoing_login_and_writes_the_selected_one(self):
        self.close_codex()
        outgoing_refresh = read_json(self.auth)["tokens"]["refresh_token"]
        report = self.act("codex", "switch", self.side)
        self.assertEqual(report["outcome"], "success", report)
        self.assert_consistent("codex", self.identity(self.side))
        self.assertEqual(self.vault.get(self.main["id"])["tokens"]["refresh_token"], outgoing_refresh)
        self.assertEqual(self.pending_records(), [])

    def test_a_switch_now_is_refused_while_a_codex_session_is_open(self):
        report = self.act("codex", "switch", self.side)
        self.assertEqual(report["outcome"], "failed")
        self.assert_consistent("codex", self.identity(self.main["id"]))
        self.assertEqual(self.pending_records(), [])

    def test_a_switch_waits_for_open_sessions_when_asked_to(self):
        sessions = self.procs.codex_consumers()
        self.assertTrue(sessions)
        self.act("codex", "switch", self.side, consent="idle", wait=False)
        self.wait_for_phase("waiting_idle")
        self.assertEqual(self.signed_in("codex"), self.identity(self.main["id"]))
        for row in sessions:
            self.procs.exit_process(row.pid)
        self.assertTrue(self.engine.runner.wait(60))
        self.assertEqual(self.engine.snapshot()["report"]["outcome"], "success")
        self.assert_consistent("codex", self.identity(self.side))

    def test_a_sign_in_the_provider_rejects_puts_the_old_login_back(self):
        self.close_codex()
        codex = self.engine.providers["codex"]
        real = codex.usage

        def rejected(profile, **kwargs):
            reading = real(profile, **kwargs)
            if codex_module.identity_key(profile) != self.identity(self.side):
                return reading
            return dataclasses.replace(reading, usage=None, error=codex_module.ProviderError("unauthorized"))
        codex.usage = rejected
        report = self.act("codex", "switch", self.side)
        self.assertEqual(report["outcome"], "failed")
        self.assert_consistent("codex", self.identity(self.main["id"]))
        self.assertEqual(self.pending_records(), [])

    def test_one_press_of_recover_settles_a_switch_the_app_died_in(self):
        self.close_codex()
        before = read_json(self.auth)
        tx = FileTransaction(self.vault, self.engine.roots())
        tx.doc.update(provider="codex", purpose="switch")
        self.engine.providers["codex"].restore(self.vault.get(self.side), tx)
        self.assertEqual(len(self.pending_records()), 1)
        report = self.act("codex", "recover")
        self.assertEqual(report["outcome"], "success", report)
        self.assertEqual(self.pending_records(), [])
        self.assertEqual(read_json(self.auth), before)
        self.assert_consistent("codex", self.identity(self.main["id"]))


class DesktopSwitchTest(DeckCase):
    def setUp(self):
        super().setUp()
        self.outgoing, self.target = self.active("claude"), self.spare("claude")
        self.procs.relaunch_identity = self.identity(self.target["id"])

    def test_a_switch_closes_claude_writes_the_selected_login_and_reopens_it(self):
        report = self.act("claude", "switch", self.target["id"])
        self.assertEqual(report["outcome"], "success", report)
        self.assert_consistent("claude", self.identity(self.target["id"]))
        self.assertTrue(self.procs.claude_desktop(), "Claude was not reopened")
        self.assertEqual(self.pending_records(), [])

    def test_a_switch_waits_for_busy_sessions_when_asked_to(self):
        busy = [s for s in self.engine.snapshot()["providers"]["claude"]["running"]["sessions"] if s["status"] == "busy"]
        self.assertTrue(busy)
        self.act("claude", "switch", self.target["id"], consent="idle", wait=False)
        self.wait_for_phase("waiting_idle")
        self.assertEqual(self.signed_in("claude"), self.identity(self.outgoing["id"]))
        for session in busy:
            self.procs.set_session_status(session["pid"], "idle", age_s=60)
        self.assertTrue(self.engine.runner.wait(120))
        self.assertEqual(self.engine.snapshot()["report"]["outcome"], "success")
        self.assert_consistent("claude", self.identity(self.target["id"]))

    def test_one_press_of_recover_settles_a_switch_the_app_died_in(self):
        from accountdeck import switch_claude
        self.close_claude()
        tx = FileTransaction(self.vault, self.engine.roots())
        tx.doc.update(provider="claude", purpose="switch", outgoing_id=self.outgoing["id"], target_id=self.target["id"])
        tx.save()
        switch_claude.restore_profile(switching.context(self.engine), self.target["id"], tx)
        self.assertEqual(self.signed_in("claude"), self.identity(self.target["id"]))
        self.assertEqual(len(self.pending_records()), 1)
        report = self.act("claude", "recover")
        self.assertEqual(report["outcome"], "success", report)
        self.assertEqual(self.pending_records(), [])
        self.assert_consistent("claude", self.identity(self.outgoing["id"]))


class MuseSwitchTest(DeckCase):
    def setUp(self):
        super().setUp()
        self.outgoing, self.target = self.active("muse"), self.spare("muse")

    def test_a_switch_saves_the_outgoing_login_and_writes_the_selected_one(self):
        report = self.act("muse", "switch", self.target["id"])
        self.assertEqual(report["outcome"], "success", report)
        self.assert_consistent("muse", self.identity(self.target["id"]))
        self.assertEqual(self.pending_records(), [])

    def test_one_press_of_recover_settles_a_switch_the_app_died_in(self):
        tx = FileTransaction(self.vault, self.engine.roots())
        tx.doc.update(provider="muse", purpose="switch", outgoing=self.identity(self.outgoing["id"]),
                      target=self.identity(self.target["id"]))
        self.engine.providers["muse"].restore(self.vault.get(self.target["id"]), tx)
        self.assertEqual(len(self.pending_records()), 1)
        report = self.act("muse", "recover")
        self.assertEqual(report["outcome"], "success", report)
        self.assertEqual(self.pending_records(), [])
        self.assert_consistent("muse", self.identity(self.outgoing["id"]))


class StoredCopiesTest(unittest.TestCase):
    """The vault rules a renewed login is stored under, and which records count as waiting."""

    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="ad-switch-safety-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.vault = Vault(root / "vault", protect=test_cipher)

    @staticmethod
    def copy(token, expires, owner="a"):
        return {"owner": owner, "token": token, "expires": expires}

    def put(self, incoming, source="probe", **kwargs):
        return self.vault.put_guarded("acct", incoming, source, lambda d: d.get("owner"), lambda d: d.get("expires"),
                                      **kwargs)

    def test_a_fresher_copy_replaces_the_stored_one(self):
        self.put(self.copy("one", 100))
        self.assertEqual(self.put(self.copy("two", 200)), "accepted")
        self.assertEqual(self.vault.get("acct")["token"], "two")

    def test_an_older_copy_never_replaces_the_stored_one(self):
        self.put(self.copy("two", 200))
        self.assertEqual(self.put(self.copy("one", 100)), "kept")
        self.assertEqual(self.vault.get("acct")["token"], "two")

    def test_a_copy_for_another_account_is_a_conflict(self):
        self.put(self.copy("one", 100))
        self.assertEqual(self.put(self.copy("x", 500, owner="b")), "conflict")
        self.assertEqual(self.vault.get("acct")["token"], "one")

    def test_a_renewal_of_the_stored_copy_is_stored_whatever_its_expiry(self):
        stored = self.copy("one", 100)
        self.put(stored)
        self.assertEqual(self.put(self.copy("two", 100), expected=stored), "accepted")
        self.assertEqual(self.vault.get("acct")["token"], "two")

    def test_only_unfinished_records_wait_for_recovery(self):
        waiting = []
        for status in ("prepared", "verifying", "conflict", "complete", "rolled_back"):
            tx = FileTransaction(self.vault, [self.vault.root])
            tx.doc["status"] = status
            tx.save()
            if status in ("prepared", "verifying", "conflict"):
                waiting.append(tx.key)
        self.assertEqual(sorted(recover_transactions(self.vault)), sorted(waiting))

    def test_finished_records_are_dropped_after_thirty_days(self):
        tx = FileTransaction(self.vault, [self.vault.root])
        tx.doc.update(status="complete", at=time.time() - 31 * 86400)
        tx.save()
        self.assertEqual(recover_transactions(self.vault), [])
        self.assertFalse(self.vault.path(tx.key).exists())


if __name__ == "__main__":
    unittest.main()
