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
from unittest import mock
import uuid

from tests import support

from accountdeck import cli_auto, switch_claude, switch_cli, switching
from accountdeck.core import DeckError, FileTransaction, Vault, read_json, recover_transactions
from accountdeck.operations import Operation, OperationRunner
from accountdeck.providers import codex as codex_module
from accountdeck.providers.fake import test_cipher, unsigned_jwt
from accountdeck.providers.muse import identity_key as muse_identity


class Crash(BaseException):
    """Stands in for the app dying: nothing after the point it is raised at runs, not even the undo."""


class Waiting(Operation):
    """Another operation sitting in its wait for sessions to close, until the test releases it."""

    def __init__(self, provider, phase="waiting_idle"):
        super().__init__("switch", provider=provider, label="stand-in", consent="idle",
                         steps=(("wait", "Waiting for sessions to close"),))
        self.release, self.sits_in = threading.Event(), phase

    def run(self):
        with self.step("wait", phase=self.sits_in, message="Waiting for sessions to close…", cancellable=True):
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


class TerminalInterruptedSwitchTest(DeckCase):
    """Act on 1. The app dies at each step of a terminal switch, alone or while Claude Code changes its own
    login. After at most one press of Recover the account name and the login token belong to one account,
    AccountDeck reports that account, no saved login is filed under another account, and nothing waits."""

    STEPS = ("after the login is written", "after the account name is written", "after the check")

    def setUp(self):
        super().setUp()
        self.service = self.engine.cli
        self.outgoing, self.target = self.active("claude_cli"), self.spare("claude_cli")
        self.third = next(a for a in self.rows("claude_cli") if a["id"] not in (self.outgoing["id"], self.target["id"])
                          and a.get("health", "ok") == "ok" and not a.get("excluded"))

    def die(self, step):
        """Runs a switch to the target that dies at `step`: nothing after it runs, the undo included."""
        store = self.service.adapter.store

        def then_die(real):
            def call(*args, **kwargs):
                real(*args, **kwargs)
                raise Crash()
            return call
        seam = {self.STEPS[0]: (store, "write", then_die(store.write)),
                self.STEPS[1]: (switch_cli, "write_account", then_die(switch_cli.write_account)),
                self.STEPS[2]: (switch_cli.CLITransaction, "commit", lambda tx: (_ for _ in ()).throw(Crash()))}[step]
        with mock.patch.object(*seam), mock.patch.object(switch_cli, "settle", side_effect=Crash()):
            with self.assertRaises(Crash):
                switch_cli.perform(self.service, self.target["id"])

    def claude_code_signs_in_as(self, key):
        saved = self.vault.get(key)
        Path(self.paths.claude_cli_credentials).write_text(json.dumps(saved["credentials"]))
        config = json.loads(Path(self.paths.claude_user_config).read_text())
        config["oauthAccount"] = saved["oauthAccount"]
        Path(self.paths.claude_user_config).write_text(json.dumps(config))

    def press_recover_once(self):
        if self.pending_records():
            switching.dispatch(self.engine, "recover", {})

    def assert_settled(self, expected=None):
        self.assertEqual(self.pending_records(), [], "a record still waits after one press of Recover")
        self.assert_consistent("claude_cli", expected and self.identity(expected["id"]))
        for row in self.rows("claude_cli"):      # no saved login is filed under another account's name
            saved = self.vault.get(row["id"])["credentials"]
            if (saved or {}).get("claudeAiOauth", {}).get("accessToken"):
                self.assertEqual(self.service.adapter.resolve_account(saved)["accountUuid"], self.identity(row["id"]))
        other = next(a for a in self.rows("claude_cli") if not a["active"] and a.get("health", "ok") == "ok")
        self.assertEqual(switch_cli.plan(self.engine, other["id"])["blockers"], [])

    def test_the_app_dies_at_each_step(self):
        for step in self.STEPS:
            with self.subTest(step=step):
                self.setUp()
                self.die(step)
                self.press_recover_once()
                self.assert_settled(self.outgoing)

    def test_the_app_dies_at_each_step_and_claude_code_renews_its_login(self):
        for step in self.STEPS:
            with self.subTest(step=step):
                self.setUp()
                self.die(step)
                support.claude_code_renews(self.engine)
                renewed = json.loads(Path(self.paths.claude_cli_credentials).read_text())["claudeAiOauth"]["refreshToken"]
                self.press_recover_once()
                self.assert_settled(self.outgoing)
                # The renewed login is the only one that still works, so it is the one kept for the target.
                self.assertEqual(self.vault.get(self.target["id"])["credentials"]["claudeAiOauth"]["refreshToken"], renewed)

    def test_the_app_dies_at_each_step_and_someone_signs_in_to_another_account(self):
        for step in self.STEPS:
            with self.subTest(step=step):
                self.setUp()
                self.die(step)
                self.claude_code_signs_in_as(self.third["id"])
                self.press_recover_once()
                self.assert_settled(self.third)

    def test_a_record_the_shipped_app_left_is_settled_after_a_renewal(self):
        """The proven dead end, built from the 2.0.0 record format: the swap is on disk, the record is open,
        and Claude Code renewed the login before anyone pressed Recover."""
        target, live = self.vault.get(self.target["id"]), json.loads(Path(self.paths.claude_cli_credentials).read_text())
        config = json.loads(Path(self.paths.claude_user_config).read_text())
        tx = switch_cli.CLITransaction(self.service)
        tx.doc["oauth_change"] = {"present": True, "file_present": True, "before": config["oauthAccount"],
                                  "after": target["oauthAccount"]}
        self.service.adapter.store.write(tx, {**live, **target["credentials"]})
        config["oauthAccount"] = target["oauthAccount"]
        Path(self.paths.claude_user_config).write_text(json.dumps(config))
        support.claude_code_renews(self.engine)
        self.press_recover_once()
        self.assert_settled()

    def test_recover_works_while_the_provider_cannot_say_whose_login_is_live(self):
        self.die(self.STEPS[1])
        support.claude_code_renews(self.engine)
        displaced = json.loads(Path(self.paths.claude_cli_credentials).read_text())["claudeAiOauth"]["refreshToken"]
        answer = self.service.adapter.resolve_account
        self.service.adapter.resolve_account = mock.Mock(side_effect=DeckError("offline", "network"))
        self.press_recover_once()
        self.service.adapter.resolve_account = answer
        self.assert_settled(self.outgoing)
        kept = [self.vault.protect(path.read_bytes(), decrypt=True) for path in self.vault.root.glob("quarantine-cli-recover-*")]
        self.assertEqual(len(kept), 1)
        self.assertEqual(json.loads(kept[0])["claudeAiOauth"]["refreshToken"], displaced)

    def test_a_switch_that_fails_before_it_writes_leaves_no_record(self):
        store = self.service.adapter.store
        with mock.patch.object(store, "write", side_effect=DeckError("Claude Code's login lock changed. Try again.", "busy")):
            with self.assertRaises(DeckError):
                switch_cli.perform(self.service, self.target["id"])
        self.assert_settled(self.outgoing)

    def test_a_record_nobody_can_read_is_set_aside_by_one_press(self):
        key = "transaction-" + "0" * 32
        self.vault.path(key).write_bytes(b"not a vault record")
        self.assertEqual(self.pending_records(), [key])
        self.press_recover_once()
        self.assert_settled(self.outgoing)
        self.assertEqual(self.vault.path("set-aside-" + key).read_bytes(), b"not a vault record")
        self.assertIn("set the record aside", self.engine.snapshot()["notices"][-1]["message"])

    def test_a_record_that_cannot_be_applied_is_set_aside_by_one_press(self):
        self.die(self.STEPS[1])
        Path(self.paths.claude_cli_credentials).write_text("{not json")       # the live login cannot be read at all
        key = self.pending_records()[0]
        self.press_recover_once()
        self.assertEqual(self.pending_records(), [])
        self.assertTrue(self.vault.path("set-aside-" + key).exists())
        self.assertIn("set that switch's record aside", self.engine.snapshot()["notices"][-1]["message"])

    def test_a_login_that_belongs_to_someone_else_is_never_filed_under_the_outgoing_account(self):
        """Consider 9: the name still says the outgoing account while the token is already the third one's."""
        other = self.vault.get(self.third["id"])["credentials"]
        other["claudeAiOauth"]["refreshToken"] += "-renewed"          # a token the vault has not seen yet
        Path(self.paths.claude_cli_credentials).write_text(json.dumps(other))
        with self.assertRaises(DeckError) as raised:
            switch_cli.perform(self.service, self.target["id"])
        self.assertEqual(raised.exception.code, "identity")
        saved = self.vault.get(self.outgoing["id"])["credentials"]
        self.assertEqual(self.service.adapter.resolve_account(saved)["accountUuid"], self.identity(self.outgoing["id"]))
        self.assertEqual(self.pending_records(), [])


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

    def start(self, operation):
        self.addCleanup(operation.release.set)
        self.engine.submit(operation)
        self.wait_for_phase(operation.sits_in)
        return operation

    def tick_until_switched(self, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            cli_auto.tick(self.engine)
            time.sleep(0.1)
            if support.terminal_on_disk(self.engine)["name"] != self.identity(self.active_row["id"]):
                return True
        return False

    def test_it_acts_while_each_other_client_waits_for_its_sessions(self):
        """Act on 4, for a Desktop, a Codex and a Muse wait."""
        for provider in ("claude", "codex", "muse"):
            with self.subTest(waiting=provider):
                self.setUp()
                waiting = self.start(Waiting(provider))
                self.assertTrue(self.tick_until_switched(), "the terminal stayed on the account at 97%")
                self.assertTrue(self.engine.runner.wait(0.5) is False, "the other client's wait ended")
                pending = self.engine.snapshot()["pending"]
                self.assertEqual((pending["id"], pending["phase"]), (waiting.id, "waiting_idle"))
                self.assert_consistent("claude_cli")
                waiting.release.set()
                self.assertTrue(waiting.wait(10))
                self.assertEqual(self.engine.snapshot()["report"]["op"], waiting.id)

    def test_it_stands_down_while_another_client_writes(self):
        for phase in ("saving", "restoring", "verifying"):
            with self.subTest(phase=phase):
                self.setUp()
                self.start(Waiting("codex", phase))
                self.assertFalse(self.tick_until_switched(0.6))

    def test_the_switch_it_made_during_a_wait_can_be_undone_during_that_wait(self):
        """The reverse of the state above: Undo is a manual terminal switch, and it must not wait for hours."""
        self.start(Waiting("codex"))
        self.assertTrue(self.tick_until_switched())
        deadline = time.monotonic() + 5
        while not self.engine.store.setting("cli_history", {}).get("last") and time.monotonic() < deadline:
            time.sleep(0.05)
        plan = self.engine.action("cli-undo", {"provider": "claude_cli"})["plan"]
        self.assertEqual(plan["blockers"], [])
        self.engine.action("switch", {"id": self.active_row["id"], "provider": "claude_cli", "plan_id": plan["plan_id"]})
        deadline = time.monotonic() + 5
        while support.terminal_on_disk(self.engine)["name"] != self.identity(self.active_row["id"]) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assert_consistent("claude_cli", self.identity(self.active_row["id"]))
        self.assertEqual(self.engine.snapshot()["pending"]["phase"], "waiting_idle")

    def test_it_acts_during_a_real_codex_wait_and_that_switch_still_finishes(self):
        side = self.add_codex_account()
        sessions = self.procs.codex_consumers()
        self.act("codex", "switch", side, consent="idle", wait=False)
        self.wait_for_phase("waiting_idle")
        self.assertTrue(self.tick_until_switched())
        self.assertEqual(self.engine.snapshot()["pending"]["phase"], "waiting_idle")
        for row in sessions:
            self.procs.exit_process(row.pid)
        self.assertTrue(self.engine.runner.wait(60))
        self.assertEqual(self.engine.snapshot()["report"]["outcome"], "success")
        self.assert_consistent("codex", self.identity(side))

    def test_it_stands_down_while_a_record_waits_for_recovery(self):
        self.engine.recovery = ["transaction-" + "0" * 32]
        cli_auto.tick(self.engine)
        self.assertFalse(self.engine.runner.busy)
        self.assertEqual(self.active("claude_cli")["id"], self.active_row["id"])


class RunnerLanesTest(unittest.TestCase):
    """The operation runner: one operation at a time, plus one beside another client's wait."""

    class Beside(Waiting):
        beside_waits = True

    def setUp(self):
        self.runner = OperationRunner()
        self.addCleanup(self.runner.stop, 5)

    def start(self, operation):
        self.addCleanup(operation.release.set)
        self.runner.submit(operation)
        deadline = time.monotonic() + 5
        while operation.phase != operation.sits_in and time.monotonic() < deadline:
            time.sleep(0.01)
        return operation

    def refused(self, operation):
        with self.assertRaises(DeckError) as raised:
            self.runner.submit(operation)
        return raised.exception.code

    def test_only_an_operation_that_may_run_beside_a_wait_does(self):
        self.start(Waiting("codex"))
        self.assertEqual(self.refused(Waiting("muse")), "busy")
        beside = self.start(self.Beside("claude_cli"))
        self.assertEqual(self.runner.pending()["provider"], "codex", "the waiting operation is the one shown")
        self.assertEqual(self.refused(self.Beside("claude_cli")), "busy", "one beside at a time")
        beside.release.set()
        self.assertTrue(beside.wait(5))
        self.assertIsNone(self.runner.report(), "the waiting operation's report slot is its own")

    def test_nothing_runs_beside_a_client_of_its_own_or_a_write(self):
        self.start(Waiting("claude_cli"))
        self.assertEqual(self.refused(self.Beside("claude_cli")), "busy")
        self.runner.current.release.set()
        self.assertTrue(self.runner.wait(5))
        self.start(Waiting("codex", "restoring"))
        self.assertEqual(self.refused(self.Beside("claude_cli")), "busy")

    def test_the_runner_stays_busy_until_the_operation_beside_ends(self):
        waiting = self.start(Waiting("codex"))
        beside = self.start(self.Beside("claude_cli"))
        waiting.release.set()
        self.assertTrue(waiting.wait(5))
        self.assertTrue(self.runner.busy)
        self.assertEqual(self.refused(Waiting("muse")), "busy")
        beside.release.set()
        self.assertTrue(self.runner.wait(5))
        self.assertFalse(self.runner.busy)


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
        self.engine.accounts.capture("codex", owner="v2")        # the switch's own "save the current sign-in" step
        tx = FileTransaction(self.vault, self.engine.roots())
        tx.doc.update(provider="codex", purpose="switch")
        self.engine.providers["codex"].restore(self.vault.get(self.side), tx)
        self.assertEqual(len(self.pending_records()), 1)
        report = self.act("codex", "recover")
        self.assertEqual(report["outcome"], "success", report)
        self.assertEqual(self.pending_records(), [])
        self.assertEqual(read_json(self.auth), before)
        self.assert_consistent("codex", self.identity(self.main["id"]))


class CodexInterruptedSwitchTest(DeckCase):
    """Act on 1 for Codex. A switch that stopped after it wrote auth.json is settled by one press of Recover,
    whatever Codex did to its login in between."""

    def setUp(self):
        super().setUp()
        self.side = self.add_codex_account()
        self.main = self.active("codex")
        self.auth = Path(self.paths.codex_home) / "auth.json"
        self.close_codex()

    def die_after_the_login_is_written(self):
        self.engine.accounts.capture("codex", owner="v2")        # the switch's own "save the current sign-in" step
        tx = FileTransaction(self.vault, self.engine.roots())
        tx.doc.update(provider="codex", purpose="switch")
        self.engine.providers["codex"].restore(self.vault.get(self.side), tx)
        return tx

    def codex_renews(self):
        live = read_json(self.auth)
        live["tokens"]["refresh_token"] += "-renewed"
        self.auth.write_text(json.dumps(live, indent=2))
        return live["tokens"]["refresh_token"]

    def assert_settled(self, expected):
        self.assertEqual(self.pending_records(), [], "a record still waits after one press of Recover")
        self.assert_consistent("codex", self.identity(expected))
        self.assertEqual(self.plan("codex", "capture")["blockers"], [])

    def test_recover_after_codex_renewed_the_new_login(self):
        self.die_after_the_login_is_written()
        renewed = self.codex_renews()
        self.pending_records()
        report = self.act("codex", "recover")
        self.assertEqual(report["outcome"], "success", report)
        self.assert_settled(self.main["id"])
        self.assertEqual(self.vault.get(self.side)["tokens"]["refresh_token"], renewed,
                         "the renewed login is the only one that still works, so it is the one kept")

    def test_recover_after_someone_signed_in_to_another_account(self):
        self.die_after_the_login_is_written()
        other = codex_login("codex-third@example.test", now=self.engine.clock(), refresh="synthetic-refresh-third")
        self.auth.write_text(json.dumps(other))
        self.pending_records()
        report = self.act("codex", "recover")
        self.assertEqual(report["outcome"], "success", report)
        self.assert_settled(self.main["id"])
        third = self.engine.store.find("codex", codex_module.identity_key(other))
        self.assertIsNotNone(third, "the login that was signed in was not saved")
        self.assertEqual(self.vault.get(third["id"])["tokens"]["refresh_token"], "synthetic-refresh-third")

    def test_a_failed_check_after_codex_rewrote_its_login_leaves_nothing_to_recover(self):
        codex = self.engine.providers["codex"]
        real = codex.usage

        def rejected(profile, **kwargs):
            if codex_module.identity_key(profile) != self.identity(self.side):
                return real(profile, **kwargs)
            self.codex_renews()
            return dataclasses.replace(real(profile, **kwargs), usage=None, error=codex_module.ProviderError("unauthorized"))
        codex.usage = rejected
        report = self.act("codex", "switch", self.side)
        self.assertEqual(report["outcome"], "failed")
        self.assert_settled(self.main["id"])

    def test_recover_waits_for_codex_to_close(self):
        self.die_after_the_login_is_written()
        session = self.procs.add_codex("codex_tui")
        self.pending_records()
        self.assertEqual([b["code"] for b in self.plan("codex", "recover")["blockers"]], ["busy"])
        self.procs.exit_process(session.pid)
        self.assertEqual(self.act("codex", "recover")["outcome"], "success")
        self.assert_settled(self.main["id"])

    def test_a_record_that_cannot_be_applied_is_set_aside_by_one_press(self):
        tx = self.die_after_the_login_is_written()
        tx.doc["changes"][0]["before"] = "not base64"
        tx.save()
        self.engine.accounts.remove(self.main["id"])            # and the outgoing account is no longer saved
        self.pending_records()
        self.act("codex", "recover")
        self.assertEqual(self.pending_records(), [])
        self.assertTrue(self.vault.path("set-aside-" + tx.key).exists())
        self.assertIn("could not undo an interrupted Codex switch", self.engine.snapshot()["notices"][-1]["message"])
        self.assertEqual(self.plan("codex", "capture")["blockers"], [])


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


class DesktopInterruptedSwitchTest(DeckCase):
    """Act on 1 for Claude Desktop: one press of Recover after the app died at each step of a switch."""

    def setUp(self):
        super().setUp()
        self.outgoing, self.target = self.active("claude"), self.spare("claude")
        self.ctx = switching.context(self.engine)
        self.close_claude()

    def die_after_the_login_is_written(self, status="prepared"):
        tx = FileTransaction(self.vault, self.engine.roots())
        tx.doc.update(provider="claude", purpose="switch", outgoing_id=self.outgoing["id"], target_id=self.target["id"])
        tx.save()
        switch_claude.restore_profile(self.ctx, self.target["id"], tx)
        tx.close(status)
        return tx

    def assert_settled(self, expected):
        self.assertEqual(self.pending_records(), [], "a record still waits after one press of Recover")
        self.assert_consistent("claude", self.identity(expected["id"]))
        self.assertEqual(self.plan("claude", "switch", self.spare("claude")["id"])["blockers"], [])

    def test_the_app_dies_at_each_step(self):
        for status in ("prepared", "verifying", "conflict"):       # written; Claude reopened; a failed undo
            with self.subTest(status=status):
                self.setUp()
                self.die_after_the_login_is_written(status)
                self.pending_records()
                self.assertEqual(self.act("claude", "recover")["outcome"], "success")
                self.assert_settled(self.outgoing)

    def test_a_recovery_that_died_is_not_replayed_over_the_right_account(self):
        """Consider 10: the first Recover wrote the old login back and died before it closed its records."""
        tx = self.die_after_the_login_is_written()
        first = FileTransaction(self.vault, self.engine.roots())
        first.doc.update(provider="claude", purpose="recover", outgoing_id=None)
        first.save()
        switch_claude.restore_profile(self.ctx, self.outgoing["id"], first)
        for order in ([tx.key, first.key], [first.key, tx.key]):
            with self.subTest(switch_record_first=order[0] == tx.key):
                tx.close("prepared")
                first.close("prepared")
                self.engine.recovery = order
                self.assertEqual(self.act("claude", "recover")["outcome"], "success")
                self.assert_settled(self.outgoing)

    def test_a_record_that_cannot_be_applied_is_set_aside_by_one_press(self):
        tx = self.die_after_the_login_is_written()
        tx.doc["outgoing_id"] = None                   # nobody saved to go back to, and the files changed since
        tx.save()
        config = Path(self.paths.claude_profile) / "config.json"
        config.write_text(json.dumps({**json.loads(config.read_text()), "userThemeMode": "dark"}))
        self.pending_records()
        self.act("claude", "recover")
        self.assertEqual(self.pending_records(), [])
        self.assertTrue(self.vault.path("set-aside-" + tx.key).exists())
        self.assertIn("could not undo an interrupted Claude switch", self.engine.snapshot()["notices"][-1]["message"])

    def test_a_switch_stops_when_claude_starts_again_before_the_write(self):
        """Consider 1."""
        saved = switch_claude.ClaudeJob._save_current

        def then_claude_starts(job, **kwargs):
            profile = saved(job, **kwargs)
            self.procs.add_claude_desktop()
            return profile
        with mock.patch.object(switch_claude.ClaudeJob, "_save_current", then_claude_starts):
            report = self.act("claude", "switch", self.target["id"])
        self.assertEqual(report["outcome"], "failed")
        self.assert_settled(self.outgoing)


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


class MuseInterruptedSwitchTest(DeckCase):
    """Act on 1 for Muse, which already recovered this way; the cases the shared function adds."""

    def setUp(self):
        super().setUp()
        self.outgoing, self.target = self.active("muse"), self.spare("muse")
        self.auth = Path(self.paths.muse_config) / "auth.json"

    def die_after_the_login_is_written(self):
        tx = FileTransaction(self.vault, self.engine.roots())
        tx.doc.update(provider="muse", purpose="switch", outgoing=self.identity(self.outgoing["id"]),
                      target=self.identity(self.target["id"]))
        self.engine.providers["muse"].restore(self.vault.get(self.target["id"]), tx)
        self.pending_records()
        return tx

    def test_recover_when_muses_login_file_cannot_be_opened(self):
        """Noted 7."""
        self.die_after_the_login_is_written()
        self.auth.chmod(0)
        self.addCleanup(lambda: self.auth.exists() and self.auth.chmod(0o600))
        report = self.act("muse", "recover")
        self.assertEqual(report["outcome"], "success", report)
        self.assertEqual(self.pending_records(), [])
        self.assert_consistent("muse", self.identity(self.outgoing["id"]))

    def test_a_record_that_cannot_be_applied_is_set_aside_by_one_press(self):
        tx = self.die_after_the_login_is_written()
        tx.doc["changes"][0]["before"] = "not base64"
        tx.doc.pop("outgoing")
        tx.save()
        self.act("muse", "recover")
        self.assertEqual(self.pending_records(), [])
        self.assertTrue(self.vault.path("set-aside-" + tx.key).exists())
        self.assertIn("could not undo an interrupted Muse switch", self.engine.snapshot()["notices"][-1]["message"])
        self.assertEqual(self.plan("muse", "capture")["blockers"], [])


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
