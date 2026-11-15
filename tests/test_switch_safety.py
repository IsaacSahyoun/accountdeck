"""Everything that acts on a login, for all three clients: switching, waiting, recovery and the stored copies.
Each test runs a whole engine on the demo's fake data in safe mode; the real apps, their logins and the
Keychain are never touched."""
import copy
import dataclasses
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

from tests import support

from accountdeck import cli_auto, cli_policy, move_bundle, move_import, switch_cli, switching
from accountdeck.config import Paths
from accountdeck.core import DeckError, FileTransaction, Vault, read_json, recover_transactions
from accountdeck.engine import Engine
from accountdeck.operations import Operation, OperationRunner
from accountdeck.procs import FakeProcessController, helper_running
from accountdeck.providers import codex as codex_module, fake as fake_module
from accountdeck.providers.fake import build_fake_home, muse_auth, test_cipher, unsigned_jwt
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


def codex_login(email, *, now, refresh="synthetic-refresh-side", lifetime=9 * 86400):
    """A Codex auth.json for a new synthetic account, whose access token lasts `lifetime` seconds from `now`."""
    identity = str(uuid.uuid4())
    claims = {codex_module.AUTH_CLAIMS: {"chatgpt_account_id": identity, "chatgpt_plan_type": "plus"}}
    exp = int(now) + lifetime
    return {"OPENAI_API_KEY": None, "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "tokens": {"id_token": unsigned_jwt({**claims, "email": email, "exp": exp}),
                       "access_token": unsigned_jwt({**claims, "exp": exp}), "refresh_token": refresh,
                       "account_id": identity}}


class DeckCase(unittest.TestCase):
    """The `terminal` demo (accounts of all three clients) with its tick thread off."""

    def setUp(self):
        self.engine = support.demo_engine(self, "terminal")
        self.paths, self.procs, self.vault = self.engine.paths, self.engine.procs, self.engine.vault

    # ---- accounts ----
    def rows(self, provider):
        return [a for a in self.engine.snapshot()["accounts"] if a["provider"] == provider]

    def active(self, provider):
        return next((a for a in self.rows(provider) if a["active"]), None)

    def spare(self, provider):
        """A saved account of `provider` that is not signed in and can be switched to."""
        return next(a for a in self.rows(provider)
                    if not a["active"] and a["switchable"] and a.get("health", "ok") == "ok" and not a.get("excluded"))

    def add_codex_account(self, email="codex-side@example.test", **login):
        codex = self.engine.providers["codex"]
        login = codex_login(email, **{"now": self.engine.clock(), **login})
        return self.engine.accounts.save("codex", login, codex.identity(login), source="login", owner="v2")

    def close_codex(self):
        for row in self.procs.snapshot():
            if row.kind.startswith("codex"):
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
        raise AssertionError(f"AccountDeck manages no client named {provider}")

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

    def test_a_record_stays_while_the_live_login_cannot_be_read(self):
        """Second review: nobody can tell whether the name and the token match, so the record is not set
        aside. A new sign-in in Claude Code makes the login readable, and one press then settles it."""
        self.die(self.STEPS[1])
        Path(self.paths.claude_cli_credentials).write_text("{not json")       # the live login cannot be read at all
        key = self.pending_records()[0]
        with self.assertRaises(DeckError) as raised:
            self.press_recover_once()
        self.assertIn("press Recover again", str(raised.exception))
        self.assertEqual(self.pending_records(), [key])
        self.claude_code_signs_in_as(self.third["id"])
        self.press_recover_once()
        self.assert_settled(self.third)

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


class TerminalSoonestWeekTest(DeckCase):
    """The early switch through the real engine: the terminals end up signed in to the account whose week ends
    soonest, however far off that end is. The rule's own answers are in `tests.test_readings`
    (`SoonestWeekFirstTest` and the decision table)."""
    DAY = 86400

    def setUp(self):
        super().setUp()
        self.engine.sense_active()
        # The demo's terminals have just left an account at the threshold, and the early switch waits 2 hours
        # after any automatic switch. These tests start after that.
        self.engine.clock.advance(2 * 3600 + 60)
        self.first = self.active("claude_cli")
        usable = [row for row in self.rows("claude_cli") if not row["active"] and row["switchable"]
                  and row.get("health", "ok") == "ok" and not row.get("excluded")]
        self.later, self.soonest = usable[:2]
        self.assertNotIn(self.engine.store.setting("cli_history", {}).get("left"), (None, self.soonest["id"]))
        for row in usable[2:]:
            self.read(row, weekly=30, ends=7 * self.DAY)          # weeks that end after every other
        self.read(self.first, weekly=20, ends=6 * self.DAY)
        self.read(self.later, weekly=30, ends=4 * self.DAY)
        self.read(self.soonest, weekly=30, ends=2 * self.DAY)

    def read(self, row, **reading):
        support.with_reading(self.engine, row["id"], **reading)

    def started(self):
        """What one check of auto-switch starts, without running it."""
        submitted, submit = [], self.engine.submit
        self.engine.submit = lambda op: submitted.append((op.kind, op.provider))
        try:
            cli_auto.tick(self.engine)
        finally:
            self.engine.submit = submit
        return submitted

    def switched(self):
        before = self.engine.store.setting("cli_history", {}).get("last")
        cli_auto.tick(self.engine)
        self.assertTrue(self.engine.runner.wait(30))
        last = self.engine.store.setting("cli_history", {}).get("last")
        self.assertNotEqual(last, before, "auto-switch did not switch")
        return last

    def test_the_terminals_move_to_the_account_whose_week_ends_soonest(self):
        last = self.switched()
        self.assertEqual((last["kind"], last["from"], last["to"]), ("early", self.first["id"], self.soonest["id"]))
        self.assertEqual(last["reason"], f"Using {self.soonest['label']}'s weekly allowance before it resets in 2d 0h (70% left).")
        self.assert_consistent("claude_cli", self.identity(self.soonest["id"]))
        # It stays there. The next check starts nothing, and neither does one after the 2 hours between early switches.
        self.assertEqual(self.started(), [])
        self.engine.clock.advance(2 * 3600 + 60)
        for row, ends in ((self.first, 6), (self.later, 4), (self.soonest, 2)):
            self.read(row, weekly=30, ends=ends * self.DAY - 2 * 3600 - 60)
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["hold"]), ("none", None))
        self.assertEqual(self.started(), [])

    def test_an_undo_takes_the_terminals_back_and_is_held_for_two_hours(self):
        """The reverse. An Undo is the user's own choice, so the rule does not move the terminals again: first
        because an automatic switch just happened, and after those 2 hours because the choice is still held. Two
        hours after the Undo the hold is over, like any manual choice's, and the week that ends soonest is
        used first again."""
        self.switched()
        self.engine.clock.advance(300)
        plan = self.engine.action("cli-undo", {"provider": "claude_cli"})["plan"]
        self.assertEqual(plan["blockers"], [])
        self.engine.action("switch", {"id": self.first["id"], "provider": "claude_cli", "plan_id": plan["plan_id"]})
        self.assertTrue(self.engine.runner.wait(30))
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["hold"]), ("none", "The early switch waits 2 hours after an automatic switch"))
        self.assertEqual(self.started(), [])
        self.engine.clock.advance(2 * 3600 - 300 + 10)          # 2 hours after the early switch, not yet after the Undo
        for row, ends in ((self.first, 6), (self.later, 4), (self.soonest, 2)):       # new readings: the move is there to hold back
            self.read(row, weekly=30, ends=ends * self.DAY - 2 * 3600 - 10)
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["hold"]), ("none", "Your manual choice is held for 2 hours"))
        self.assertEqual(self.started(), [])
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))
        self.engine.clock.advance(300)                           # 2 hours after the Undo
        for row, ends in ((self.first, 6), (self.later, 4), (self.soonest, 2)):
            self.read(row, weekly=30, ends=ends * self.DAY - 2 * 3600 - 310)
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["kind"], decision["target"]), ("switch", "early", self.soonest["id"]))

    def test_after_a_switch_at_the_threshold_the_early_switch_waits_two_hours(self):
        """The order is "most room first", so at the threshold the terminals go to the account with the most
        room. The week of another account ends sooner. The early switch moves there 2 hours later, not at the
        next check: that would be two changes of login in a row, and the Undo of the first would be gone."""
        self.engine.prefs.update({"cli_order": "most_room"})
        self.read(self.first, weekly=96, ends=6 * self.DAY)
        self.read(self.later, weekly=10, ends=4 * self.DAY)
        last = self.switched()
        self.assertEqual((last["kind"], last["from"], last["to"]), ("threshold", self.first["id"], self.later["id"]))
        self.assert_consistent("claude_cli", self.identity(self.later["id"]))
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["hold"]), ("none", "The early switch waits 2 hours after an automatic switch"))
        self.assertEqual(self.started(), [])
        self.assertEqual(self.engine.store.setting("cli_history", {})["last"], last)       # the Undo is still that switch's
        self.engine.clock.advance(2 * 3600 + 5)
        self.read(self.later, weekly=10, ends=4 * self.DAY - 2 * 3600 - 5)
        self.read(self.soonest, weekly=30, ends=2 * self.DAY - 2 * 3600 - 5)
        last = self.switched()
        self.assertEqual((last["kind"], last["from"], last["to"]), ("early", self.later["id"], self.soonest["id"]))
        self.assert_consistent("claude_cli", self.identity(self.soonest["id"]))

    def test_an_early_switch_that_does_not_finish_is_not_tried_again_at_every_check(self):
        """A switch that fails before it writes leaves nothing behind, so the next check, 5 seconds later, would
        decide the same and start it again: a call to renew the login and an alert each time. The early switch
        is not urgent. It is tried again 2 hours later, like the next early switch after one that worked."""
        def cannot_renew(key, **how):
            raise DeckError("The terminal login could not be renewed. Its last reading is kept.", "rate_limited")
        self.engine.cli.refresh = cannot_renew
        submit, tried = self.engine.submit, []
        self.engine.submit = lambda op: tried.append((op.kind, op.provider)) or submit(op)
        for _ in range(3):
            cli_auto.tick(self.engine)
            self.assertTrue(self.engine.runner.wait(30))
        self.assertEqual(tried, [("switch", "claude_cli")])
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["hold"]),
                         ("none", "The early switch waits 2 hours after an automatic switch"))
        # 2 hours later it is tried again, and once the login can be renewed the terminals move.
        self.engine.clock.advance(2 * 3600 + 5)
        for row, ends in ((self.first, 6), (self.later, 4), (self.soonest, 2)):
            self.read(row, weekly=30, ends=ends * self.DAY - 2 * 3600 - 5)
        del self.engine.cli.refresh
        last = self.switched()
        self.assertEqual((last["kind"], last["to"]), ("early", self.soonest["id"]))
        self.assertEqual(len(tried), 2)

    def test_an_account_that_ran_out_is_left_for_a_ready_one_while_the_soonest_week_cannot_be_read(self):
        """A switch is made on a reading at most 3 minutes old, and an older one is read again first. The reading
        of the week that ends soonest is 4 minutes old and Claude refuses a new one (HTTP 429, which pauses its
        reads for 15 minutes or more). That account is first in the order, and the account in use has run out. The
        terminals go to the next account, which is ready: waiting for the paused one kept them on an account
        that works for no model."""
        def refused(credentials):
            raise DeckError("Too many requests.", "rate_limited", 900)
        self.engine.clock.advance(240)
        self.engine.cli.adapter.usage = refused
        self.assertEqual(self.engine.cli.read(self.soonest["id"]), "rate_limited")
        del self.engine.cli.adapter.usage
        self.assertGreater(self.engine.store.account(self.soonest["id"])["paused_until"], self.engine.clock())
        self.read(self.first, weekly=100, ends=6 * self.DAY - 240)
        self.read(self.later, weekly=10, ends=4 * self.DAY - 240)
        last = self.switched()
        self.assertEqual((last["kind"], last["from"], last["to"]), ("out", self.first["id"], self.later["id"]))
        self.assert_consistent("claude_cli", self.identity(self.later["id"]))

    def test_a_login_save_does_not_hide_that_the_reads_of_an_account_are_paused(self):
        """Every switch files the login of the account the terminals leave, and that save clears the row's
        `error` while the reads of the account stay paused. So the rule does not go by `error`. It goes by the
        pause itself (`paused_until`), which no save of a login changes. Here Claude refuses a read of the
        account in use, the user switches away from it, and the account they switched to runs out. The paused
        account is first in the order, and the terminals go past it to one that is ready."""
        def refused(credentials):
            raise DeckError("Too many requests.", "rate_limited", 900)
        self.act("claude_cli", "switch", account=self.soonest["id"])
        self.engine.clock.advance(200)
        self.engine.cli.adapter.usage = refused
        self.assertEqual(self.engine.cli.read(self.soonest["id"]), "rate_limited")
        del self.engine.cli.adapter.usage
        self.assertIsNotNone(self.engine.store.account(self.soonest["id"])["error"])
        self.act("claude_cli", "switch", account=self.first["id"])
        paused = self.engine.store.account(self.soonest["id"])
        self.assertIsNone(paused["error"], "the save of its login no longer clears the error: this test proves nothing")
        self.assertGreater(paused["paused_until"], self.engine.clock())
        self.engine.clock.advance(40)
        self.read(self.first, weekly=100, ends=6 * self.DAY - 240)
        self.read(self.later, weekly=10, ends=4 * self.DAY - 240)
        last = self.switched()
        self.assertEqual((last["kind"], last["from"], last["to"]), ("out", self.first["id"], self.later["id"]))
        self.assert_consistent("claude_cli", self.identity(self.later["id"]))

    def test_a_paused_account_is_read_and_used_again_once_its_pause_is_over(self):
        """The reverse, the way the app gets there. Claude refuses a read of the week that ends soonest, and the
        account is passed over while its reads are paused. The app's own scheduler starts no read of it in that
        time, also not the one the rule asks for. Once the pause is over the scheduler reads it, the pause is
        gone, and the account that ran out is left for it."""
        def refused(credentials):
            raise DeckError("Too many requests.", "rate_limited", 900)
        self.engine.clock.advance(240)
        self.engine.cli.adapter.usage = refused
        self.assertEqual(self.engine.cli.read(self.soonest["id"]), "rate_limited")
        del self.engine.cli.adapter.usage
        paused = self.soonest["id"]

        def row():
            return self.engine.store.account(paused)
        until, tried, reading = row()["paused_until"], row()["flags"]["checked"], row()["last_good_at"]
        self.read(self.first, weekly=100, ends=6 * self.DAY - 240)
        self.read(self.later, weekly=10, ends=4 * self.DAY - 240)
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["target"]), ("switch", self.later["id"]))
        self.assertEqual([entry["skip"] for entry in decision["next_up"] if entry["id"] == paused], ["Claude paused its checks"])
        # During the pause: a read is asked for, the scheduler runs, and no try at the account is made.
        self.engine.scheduler.request(paused, self.engine.clock())
        self.engine._schedule(self.engine.clock())
        self.engine.clock.advance(until - self.engine.clock() - 5)
        self.engine.scheduler.request(paused, self.engine.clock())
        self.engine._schedule(self.engine.clock())
        self.assertEqual((row()["flags"]["checked"], row()["last_good_at"], row()["paused_until"]), (tried, reading, until))
        # After it: the scheduler reads the account. It starts two reads of accounts not in use at a time.
        self.engine.clock.advance(400)
        self.engine.scheduler.request(paused, self.engine.clock())
        for _ in range(4):
            self.engine._schedule(self.engine.clock())
        self.assertGreater(row()["last_good_at"], until)
        self.assertIsNone(row()["paused_until"])
        self.read(self.soonest, weekly=30, ends=self.DAY)
        self.read(self.first, weekly=100, ends=5 * self.DAY)
        self.read(self.later, weekly=10, ends=3 * self.DAY)
        last = self.switched()
        self.assertEqual((last["kind"], last["from"], last["to"]), ("out", self.first["id"], paused))
        self.assert_consistent("claude_cli", self.identity(paused))

    def test_nothing_reads_an_account_while_its_reads_are_paused(self):
        """What the rule stands on: while an account's reads are paused no read of it starts. The Refresh button
        of the account is refused, and a refresh of every account leaves it out. The scheduler's side is in the
        test above and in `tests.test_readings.SchedulerTest`."""
        def refused(credentials):
            raise DeckError("Too many requests.", "rate_limited", 900)
        self.engine.clock.advance(240)
        self.engine.cli.adapter.usage = refused
        self.assertEqual(self.engine.cli.read(self.soonest["id"]), "rate_limited")
        del self.engine.cli.adapter.usage
        paused = self.soonest["id"]
        with self.assertRaises(DeckError) as failure:
            self.engine.action("refresh", {"id": paused})
        self.assertEqual(failure.exception.code, "rate_limited")
        asked, request_all = [], self.engine.scheduler.request_all
        self.engine.scheduler.request_all = lambda ids, now: asked.extend(ids) or request_all(ids, now)
        try:
            self.engine.action("refresh", {})
        finally:
            del self.engine.scheduler.request_all
        self.assertIn(self.later["id"], asked)
        self.assertNotIn(paused, asked)

    def test_a_writer_of_flags_cannot_hide_that_the_reads_of_an_account_are_paused(self):
        """Every writer of a row's `flags` writes the whole set back, and Leave Out does so under another lock
        than a read. So a fact a read keeps in `flags` can be lost to an older copy. The time of the last try
        was such a fact, and the rule then waited on the paused account. The pause is a column of its own."""
        def refused(credentials):
            raise DeckError("Too many requests.", "rate_limited", 900)
        self.engine.clock.advance(240)
        before = self.engine.store.account(self.soonest["id"])["flags"]                     # what Leave Out read
        self.engine.cli.adapter.usage = refused
        self.assertEqual(self.engine.cli.read(self.soonest["id"]), "rate_limited")
        del self.engine.cli.adapter.usage
        self.assertNotEqual(self.engine.store.account(self.soonest["id"])["flags"], before, "the read wrote no flag: this test proves nothing")
        self.engine.store.update(self.soonest["id"], flags=dict(before, excluded=False))     # what it then wrote
        self.read(self.first, weekly=100, ends=6 * self.DAY - 240)
        self.read(self.later, weekly=10, ends=4 * self.DAY - 240)
        last = self.switched()
        self.assertEqual((last["kind"], last["from"], last["to"]), ("out", self.first["id"], self.later["id"]))

    def test_a_read_that_works_is_not_taken_for_one_that_failed_when_the_clock_is_set_back(self):
        """A read is dated when it starts, and its reading when the answer is there, by two reads of the clock.
        With the clock set back between the two the try is dated later than its own reading, and a rule that
        compared those times took a read that worked for one that failed. The rule goes by the pause, and a
        read that works leaves none: the account is still the one the terminals go to."""
        usage = self.engine.cli.adapter.usage

        def after_the_clock_is_set_back(credentials):
            self.engine.clock.advance(-10)
            return usage(credentials)
        self.engine.cli.adapter.usage = after_the_clock_is_set_back
        self.assertEqual(self.engine.cli.read(self.soonest["id"]), "ok")
        del self.engine.cli.adapter.usage
        self.read(self.soonest, weekly=30, ends=2 * self.DAY)
        row = self.engine.store.account(self.soonest["id"])
        self.assertGreater(row["flags"]["checked"], row["last_good_at"], "the try is not dated later than its reading: this test proves nothing")
        self.engine.clock.advance(200)
        self.read(self.first, weekly=100, ends=6 * self.DAY)
        self.read(self.later, weekly=10, ends=4 * self.DAY)
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["target"], decision["kind"]), ("switch", self.soonest["id"], "out"))

    def test_with_the_setting_off_the_terminals_stay(self):
        self.engine.prefs.update({"cli_early_switch": False})
        self.assertEqual(cli_auto.decision(self.engine)["action"], "none")
        self.assertEqual(self.started(), [])
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))

    def test_a_week_that_ends_soonest_is_left_at_the_threshold_for_the_next_soonest(self):
        """After the early switch the account is used like any other: at 95% the terminals move on, to the week
        that ends next."""
        self.switched()
        self.read(self.soonest, weekly=96, ends=2 * self.DAY)
        last = self.switched()
        self.assertEqual((last["kind"], last["to"]), ("threshold", self.later["id"]))
        self.assert_consistent("claude_cli", self.identity(self.later["id"]))


class TerminalPausedAccountTest(DeckCase):
    """A paused account through the real engine. Claude refuses a usage read of `wanted`, the account with room,
    which pauses its reads. `second` is the only other account that can be used, and every account behind it has
    run out. Until this change the terminals stayed on an account that had run out for the whole pause, and at
    99% they waited there with `second` ready to be used up. The installed build (2.2.0, dadbf00) does both.
    The rule's own answers are in `tests.test_readings` (`PausedAccountTest`). The demo's clock runs with real
    time, so each wait here ends well before or well after a limit, never on it."""
    DAY = 86400
    PAUSED = "Claude paused the usage checks of {}"

    def setUp(self):
        super().setUp()
        self.engine.sense_active()
        self.engine.clock.advance(2 * 3600 + 60)        # past the demo's own switch, as in TerminalSoonestWeekTest
        self.first = self.active("claude_cli")
        usable = [row for row in self.rows("claude_cli") if not row["active"] and row["switchable"]
                  and row.get("health", "ok") == "ok" and not row.get("excluded")]
        self.second, self.wanted = usable[:2]
        self.rest = usable[2:]
        self.started, submit = [], self.engine.submit
        self.engine.submit = lambda op: self.started.append(op.account) or submit(op)

    def read(self, first, second, wanted=None):
        """New readings of the account in use and of `second`, and of `wanted` when given: nothing else reads
        that one here. Every other account has run out."""
        for row in self.rest:
            support.with_reading(self.engine, row["id"], weekly=100, ends=7 * self.DAY)
        support.with_reading(self.engine, self.first["id"], weekly=first, ends=6 * self.DAY)
        support.with_reading(self.engine, self.second["id"], weekly=second, ends=4 * self.DAY)
        if wanted is not None:
            support.with_reading(self.engine, self.wanted["id"], weekly=wanted, ends=2 * self.DAY)

    def pause(self, account=None):
        """Claude refuses a usage read of `wanted`, or of `account` (HTTP 429), which pauses its reads for an
        hour. Returns the account's row as the refusal left it."""
        def refused(credentials):
            raise DeckError("Too many requests.", "rate_limited", 3600)
        key = (account or self.wanted)["id"]
        self.engine.cli.adapter.usage = refused
        self.assertEqual(self.engine.cli.read(key), "rate_limited")
        del self.engine.cli.adapter.usage
        row = self.engine.store.account(key)
        self.assertGreater(row["paused_until"], self.engine.clock() + 3000)
        return row

    def check(self, times=1):
        """`times` checks of auto-switch, each run to its end."""
        for _ in range(times):
            cli_auto.tick(self.engine)
            self.assertTrue(self.engine.runner.wait(30))

    def last(self):
        return self.engine.store.setting("cli_history", {}).get("last") or {}

    def hold(self):
        decision = cli_auto.decision(self.engine)
        return (decision["action"], decision["exhausted"], decision["hold"])

    def ran_out_beside_the_paused_account(self, age):
        """`wanted` was read `age` seconds ago and is paused since. The account in use has run out, and so has
        every other account."""
        self.read(first=30, second=100, wanted=30)
        self.engine.clock.advance(age)
        paused = self.pause()
        self.read(first=100, second=100)
        return paused

    def moved_to_the_paused_account(self, age, words):
        """The check moves the terminals to `wanted`, and asks for no reading of it before the switch starts.
        `self.asked` is every reading asked for, with the switches started by then."""
        paused = self.ran_out_beside_the_paused_account(age)
        self.asked, request = [], self.engine.scheduler.request
        self.engine.scheduler.request = lambda key, now: self.asked.append((key, list(self.started))) or request(key, now)
        try:
            self.check()
        finally:
            del self.engine.scheduler.request
        self.assertEqual([key for key, started in self.asked if not started], [], "a reading was asked for before the switch")
        last = self.last()
        self.assertEqual((last.get("kind"), last.get("from"), last.get("to")), ("out", self.first["id"], self.wanted["id"]),
                         "the terminals did not move to the paused account")
        self.assertEqual(last["reason"], f"{self.first['label']} reached its weekly limit; {self.wanted['label']} had room at its "
                         f"last reading. Claude has paused {self.wanted['label']}'s usage checks, so its reading from {words} ago is used.")
        self.assert_consistent("claude_cli", self.identity(self.wanted["id"]))
        return paused

    def test_an_account_that_ran_out_is_left_for_the_paused_account_on_its_last_reading(self):
        """The switch is the one every automatic switch makes: it renews the login it moves to and confirms
        whose it is (`switch_cli.perform`). The check asks for no reading of the paused account before it
        starts the switch (`moved_to_the_paused_account`), and the terminals stay there."""
        paused = self.moved_to_the_paused_account(270, "5 min")
        row = self.engine.store.account(self.wanted["id"])
        self.assertEqual((row["last_good_at"], row["paused_until"]), (paused["last_good_at"], paused["paused_until"]))
        self.check(2)
        self.assertEqual(self.started, [self.wanted["id"]])
        self.assertEqual(self.hold()[:2], ("none", False))

    def test_the_same_with_a_reading_over_15_minutes_old(self):
        self.moved_to_the_paused_account(1230, "21 min")

    def used_up_and_refilled_since(self):
        """`wanted` was used up when it was last read, 2 minutes before its week ended. That was 270 seconds ago.
        The account in use has run out since, and so has every other account."""
        self.read(first=30, second=100)
        support.with_reading(self.engine, self.wanted["id"], weekly=100, ends=120)
        self.engine.clock.advance(270)

    def test_a_paused_account_whose_week_refilled_since_its_last_reading_is_moved_to(self):
        """Found by the first review of this change, and the installed build (2.2.0, dadbf00) does the same. By
        the number of its last reading `wanted` was used up, so the rule said every account was low and the
        terminals stayed on an account that had run out until the pause ended. Its week has ended since."""
        self.used_up_and_refilled_since()
        paused = self.pause()
        self.read(first=100, second=100)
        self.check()
        last = self.last()
        self.assertEqual((last.get("kind"), last.get("from"), last.get("to")), ("out", self.first["id"], self.wanted["id"]),
                         "the terminals did not move to the account whose week has refilled")
        self.assertEqual(last["reason"], f"{self.first['label']} reached its weekly limit; {self.wanted['label']}'s weekly limit has refilled "
                         f"since its last reading. Claude has paused {self.wanted['label']}'s usage checks, so it is used without a new reading.")
        self.assert_consistent("claude_cli", self.identity(self.wanted["id"]))
        row = self.engine.store.account(self.wanted["id"])
        self.assertEqual((row["last_good_at"], row["paused_until"]), (paused["last_good_at"], paused["paused_until"]))
        # In use, its week is known to have ended, so its old number moves nothing and its new reading is waited for.
        self.check(2)
        self.assertEqual(self.started, [self.wanted["id"]])
        self.assertEqual(self.hold(), ("none", False, f"{self.wanted['label']}'s weekly limit has refilled. Waiting for its new reading."))

    def test_one_that_can_be_read_is_read_again_and_judged_on_what_claude_says_then(self):
        """Without a pause the reading is seconds away. The check asks for it and starts no switch. Here Claude
        then says the new week is used up too, and nothing moves. With room in it, the terminals move."""
        self.used_up_and_refilled_since()
        self.read(first=100, second=100)
        asked, request = [], self.engine.scheduler.request
        self.engine.scheduler.request = lambda key, now: asked.append(key) or request(key, now)
        try:
            self.check()
        finally:
            del self.engine.scheduler.request
        self.assertEqual((self.started, asked), ([], [self.wanted["id"]]))
        self.assertEqual(self.hold(), ("none", False, f"Checking {self.wanted['label']} before switching"))
        support.with_reading(self.engine, self.wanted["id"], weekly=100, ends=7 * self.DAY)
        self.check(2)
        self.assertEqual(self.started, [])
        self.assertEqual(self.hold()[:2], ("none", True))
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))
        support.with_reading(self.engine, self.wanted["id"], weekly=5, ends=7 * self.DAY)
        self.check()
        self.assertEqual(self.started, [self.wanted["id"]])
        self.assert_consistent("claude_cli", self.identity(self.wanted["id"]))

    def test_a_switch_on_a_last_reading_that_fails_is_tried_once_more_and_then_waits_15_minutes(self):
        """The rule for a switch that does not happen holds here too (`TerminalFailedSwitchTest`). Without it this
        switch was started again at every check, 5 seconds apart, for as long as the pause lasted."""
        def cannot_renew(key, **how):
            raise DeckError("The terminal login could not be renewed. Its last reading is kept.", "rate_limited")
        self.ran_out_beside_the_paused_account(270)
        self.engine.cli.refresh = cannot_renew
        self.check(3)
        self.assertEqual(self.started, [self.wanted["id"]])
        self.assertEqual(self.hold(), ("none", False, f"A switch to {self.wanted['label']} was tried a moment ago and is tried once more shortly"))
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))
        self.engine.clock.advance(100)
        self.read(first=100, second=100)
        self.check(3)
        self.assertEqual(self.started, [self.wanted["id"]] * 2)
        self.assertEqual(self.hold(), ("none", False, f"A switch to {self.wanted['label']} was tried and is tried again in 15 min at the earliest"))
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))
        # 15 minutes later the pause still lasts. The login can be renewed again, and the terminals move.
        del self.engine.cli.refresh
        self.engine.clock.advance(905)
        self.read(first=100, second=100)
        self.assertGreater(self.engine.store.account(self.wanted["id"])["paused_until"], self.engine.clock())
        self.check()
        self.assertEqual(self.started, [self.wanted["id"]] * 3)
        self.assert_consistent("claude_cli", self.identity(self.wanted["id"]))

    def test_once_the_pause_is_over_the_account_is_read_and_judged_on_its_new_reading(self):
        """The reverse. The terminals are on the account whose reads were paused. When the pause ends the app's
        own scheduler reads it, and the rule goes by that reading: here it says the account has run out too,
        and with nowhere to go the rule says so and moves nothing."""
        paused = self.moved_to_the_paused_account(270, "5 min")
        key, until = self.wanted["id"], paused["paused_until"]

        def ran_out(credentials):
            """Claude's answer to every read from here on: the week of that account is used up. The scheduler
            reads the other accounts too, and nothing else gives any account a number."""
            answer = copy.deepcopy(paused["last_good_usage"])
            answer["checked"] = self.engine.clock()
            next(w for w in answer["windows"] if w.get("kind") == "weekly").update(used=100.0, reset=self.engine.clock() + self.DAY)
            return answer
        self.engine.cli.adapter.usage = ran_out
        self.engine.clock.advance(until - self.engine.clock() - 60)
        self.engine.scheduler.request(key, self.engine.clock())
        self.engine._schedule(self.engine.clock())
        row = self.engine.store.account(key)
        self.assertEqual((row["last_good_at"], row["paused_until"]), (paused["last_good_at"], until), "read during its pause")
        self.assertEqual([w["used"] for w in row["last_good_usage"]["windows"] if w.get("kind") == "weekly"], [30.0])
        self.engine.clock.advance(120)
        self.engine.scheduler.request(key, self.engine.clock())
        for _ in range(4):
            self.engine._schedule(self.engine.clock())
        del self.engine.cli.adapter.usage
        row = self.engine.store.account(key)
        self.assertGreater(row["last_good_at"], until)
        self.assertIsNone(row["paused_until"])
        self.assertEqual([w["used"] for w in row["last_good_usage"]["windows"] if w.get("kind") == "weekly"], [100.0])
        self.check(2)
        self.assertEqual(self.started, [key])
        self.assertEqual(self.hold()[:2], ("none", True))
        self.assert_consistent("claude_cli", self.identity(key))

    def test_an_account_that_can_be_read_is_still_read_again_before_the_switch(self):
        """What the switch on a last reading leaves as it was. `wanted` is not paused and its reading is over 3
        minutes old: the check starts no switch and asks the scheduler for a new reading (`cli_policy.READY`).
        With that reading the terminals move."""
        self.read(first=30, second=100, wanted=30)
        self.engine.clock.advance(270)
        self.read(first=100, second=100)
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["target"], decision["last_reading"]), ("switch", self.wanted["id"], False))
        asked, request = [], self.engine.scheduler.request
        self.engine.scheduler.request = lambda key, now: asked.append(key) or request(key, now)
        try:
            self.check()
        finally:
            del self.engine.scheduler.request
        self.assertEqual((self.started, asked), ([], [self.wanted["id"]]))
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))
        support.with_reading(self.engine, self.wanted["id"], weekly=30, ends=2 * self.DAY)
        self.check()
        self.assertEqual(self.started, [self.wanted["id"]])
        self.assert_consistent("claude_cli", self.identity(self.wanted["id"]))

    def test_a_forecast_does_not_send_the_terminals_back_and_forth_while_the_account_with_room_is_paused(self):
        """Found by the review of this change. The account in use and `second` both went from 82% to 96% in 4
        minutes, so the forecast of whichever is in use says 99%, and by its reading each can still be used up.
        With the wait for the paused account ended on that forecast, 7 of 8 checks moved the terminals from one
        to the other. The wait ends at 99% by the reading, and an account left there is not gone back to."""
        self.read(first=82, second=82, wanted=30)
        self.engine.clock.advance(240)
        self.pause()
        self.read(first=96, second=96)
        forecast = next(row["forecast"] for row in cli_auto.inputs(self.engine) if row["active"])
        self.assertGreaterEqual(max(forecast.values(), default=0), 99, "the forecast of the account in use is under 99%: this test proves nothing")
        for _ in range(8):
            self.check()
            self.engine.clock.advance(5)
        self.assertEqual(self.started, [])
        self.assertEqual(self.hold(), ("none", False, self.PAUSED.format(self.wanted["label"])))
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))

    def forecast(self):
        """The highest forecast of the account in use, as the rule is handed it."""
        return max(next(row["forecast"] for row in cli_auto.inputs(self.engine) if row["active"]).values(), default=0)

    def test_an_account_in_use_whose_own_checks_are_paused_is_left_at_99_percent_by_its_forecast(self):
        """Found by the second review of this change. Claude pauses the reads of the account in use too. Its
        reading stays where it was then, so it never says 99%, and the terminals waited on it for the paused
        account with room while `second` was ready to be used up. Once that reading is too old to act on, the
        account counts as at the ceiling. The account left is not gone back to while it cannot be read."""
        self.read(first=82, second=70, wanted=30)
        self.engine.clock.advance(240)
        self.pause()
        self.read(first=96, second=96)
        self.pause(self.first)
        self.assertGreaterEqual(self.forecast(), 99, "the forecast of the account in use is under 99%: this test proves nothing")
        self.check(2)                   # its reading is new: it could be gone back to on it, so the reading decides
        self.assertEqual(self.started, [])
        self.assertEqual(self.hold(), ("none", False, self.PAUSED.format(self.wanted["label"])))
        self.engine.clock.advance(200)
        # The reset time of its first reading, so that all three readings are of one week and its fill rate
        # counts from the first (`cli_policy.same_window`).
        support.with_reading(self.engine, self.second["id"], weekly=97, ends=4 * self.DAY - 440)
        self.check()
        last = self.last()
        self.assertEqual((last.get("kind"), last.get("from"), last.get("to")), ("threshold", self.first["id"], self.second["id"]),
                         "the terminals did not move to the account that was ready")
        self.assertEqual(last["reason"], f"{self.first['label']} will pass 99% before the next check; Claude paused the usage checks of "
                         f"{self.wanted['label']}, so {self.second['label']} is used up to 99%.")
        self.assert_consistent("claude_cli", self.identity(self.second["id"]))
        # `second` fills as fast and can be read: its reading decides, and the terminals stay. Its forecast is
        # made from its readings since the terminals came to it, so it gets two of those first.
        self.assertEqual(self.forecast(), 0, "an account the terminals just came to has a forecast")
        support.with_reading(self.engine, self.second["id"], weekly=97, ends=4 * self.DAY - 440)
        self.engine.clock.advance(150)
        support.with_reading(self.engine, self.second["id"], weekly=98.6, ends=4 * self.DAY - 590)
        self.assertGreaterEqual(self.forecast(), 99, "the forecast of `second` is under 99%: the rest proves nothing")
        for _ in range(8):
            self.check()
            self.engine.clock.advance(5)
        self.assertEqual(self.started, [self.second["id"]])
        self.assertEqual(self.hold(), ("none", False, self.PAUSED.format(self.wanted["label"])))
        # Claude pauses its reads too. The terminals came to it under 15 minutes ago, so that pause does not
        # count yet (`cli_policy.STAY`), and the paused account with room is waited for.
        self.pause(self.second)
        self.engine.clock.advance(200)
        self.assertGreaterEqual(self.forecast(), 99)
        for _ in range(8):
            self.check()
            self.engine.clock.advance(5)
        self.assertEqual(self.started, [self.second["id"]])
        self.assertEqual(self.hold(), ("none", False, self.PAUSED.format(self.wanted["label"])))
        # After those 15 minutes it counts. The account that was left still has its old reading, so nothing moves.
        self.engine.clock.advance(700)
        self.check(2)
        self.assertEqual(self.started, [self.second["id"]])
        self.assertEqual(self.hold(), ("none", False, self.PAUSED.format(f"{self.wanted['label']}, {self.first['label']}")))
        self.assert_consistent("claude_cli", self.identity(self.second["id"]))

    def test_the_same_when_its_readings_did_not_rise_so_that_it_has_no_forecast(self):
        """Found by the third review of this change. A forecast needs two readings of the last 30 minutes that
        rise. An account that sat still before Claude paused its reads has none, and neither has one the
        terminals just came to. It was waited on all the same, and nothing saw it run out."""
        self.read(first=96, second=92, wanted=30)
        self.engine.clock.advance(240)
        self.pause()
        self.read(first=96, second=92)
        self.pause(self.first)
        self.assertEqual(self.forecast(), 0, "the account in use has a forecast: this test proves nothing the one before does not")
        self.check(2)                   # its reading is new, so the reading decides
        self.assertEqual(self.started, [])
        self.assertEqual(self.hold(), ("none", False, self.PAUSED.format(self.wanted["label"])))
        self.engine.clock.advance(200)
        support.with_reading(self.engine, self.second["id"], weekly=92, ends=4 * self.DAY)
        self.check()
        last = self.last()
        self.assertEqual((last.get("kind"), last.get("from"), last.get("to")), ("threshold", self.first["id"], self.second["id"]),
                         "the terminals did not move to the account that was ready")
        self.assertEqual(last["reason"], f"{self.first['label']} reached 95% of its weekly limit and its usage checks are paused; "
                         f"Claude paused the usage checks of {self.wanted['label']}, so {self.second['label']} is used up to 99%.")
        self.assert_consistent("claude_cli", self.identity(self.second["id"]))
        for _ in range(8):
            self.check()
            self.engine.clock.advance(5)
        self.assertEqual(self.started, [self.second["id"]])

    def test_an_account_moved_to_on_its_last_reading_is_not_left_at_once_for_the_next_one_that_is_read(self):
        """Found by the fourth review. The account in use ran out with `second` and `wanted` both paused past
        the threshold, so the terminals moved to one of them on its last reading. Its reads are paused and
        that reading is old, so it counted as one that cannot be read from its first check, and the terminals
        left it seconds later, when the pause of the other ended and it was read. Each move renews a login
        and takes the Undo of the one before. The account they came to is used for 15 minutes first."""
        engine = self.engine
        self.read(first=30, second=96, wanted=96)
        engine.clock.advance(1200)
        self.pause(self.second)
        self.pause(self.wanted)
        support.with_reading(engine, self.first["id"], weekly=100, ends=6 * self.DAY - 1200)
        self.check()
        moved = next((row for row in (self.second, self.wanted) if row["id"] == self.last().get("to")), None)
        self.assertIsNotNone(moved, "the terminals did not move to a paused account on its last reading")
        other = self.wanted if moved is self.second else self.second

        def read_again():
            """The pause of `other` is over and Claude answers its read with the numbers it had."""
            answer = copy.deepcopy(engine.store.account(other["id"])["last_good_usage"])
            answer["checked"] = engine.clock()
            engine.cli.adapter.usage = lambda credentials: answer
            try:
                self.assertEqual(engine.cli.read(other["id"]), "ok")
            finally:
                del engine.cli.adapter.usage
            self.assertIsNone(engine.store.account(other["id"])["paused_until"])
        read_again()
        self.check(2)
        self.assertEqual(self.started, [moved["id"]], "the terminals left the account they had just come to")
        self.assertEqual(self.hold(), ("none", False, self.PAUSED.format(moved["label"])))
        self.assert_consistent("claude_cli", self.identity(moved["id"]))
        # 16 minutes after the switch its pause counts, and the account that can be read is used up.
        engine.clock.advance(cli_policy.STAY + 60)
        read_again()
        self.check()
        last = self.last()
        self.assertEqual((last.get("kind"), last.get("from"), last.get("to")), ("threshold", moved["id"], other["id"]))
        self.assertEqual(last["reason"], f"{moved['label']} reached 95% of its weekly limit and its usage checks are paused; every "
                         f"account is past 95%, so {other['label']} is used up to 99%.")
        self.assert_consistent("claude_cli", self.identity(other["id"]))

    def test_an_account_the_terminals_just_came_to_is_not_left_when_claude_refuses_its_first_read(self):
        """Found by the fourth review, and by the fifth that no test ran it through the engine. The account in
        use reaches 99% with `second` and `wanted` past the threshold, so the terminals move to one of them to
        use it up. After a switch the engine asks for a reading of the account they came to
        (`Engine._finished`), and here Claude refuses it. That account then had a reading over 3 minutes old
        with its reads paused, so it counted as one that cannot be read, and the terminals went on to the other
        account. It is used for 15 minutes first. Claude answers every other read with the numbers the
        account had.
        The scheduler's own next read of both accounts is put 2 hours off first. The sixth review found that it
        was overdue here, so the refused read went out with or without the engine asking for it."""
        engine, reading = self.engine, []
        self.read(first=99, second=96, wanted=96)
        for row in (self.second, self.wanted):
            engine.scheduler.defer(row["id"], engine.clock() + 7200)
        self.check()
        moved = next((row for row in (self.second, self.wanted) if row["id"] == self.last().get("to")), None)
        self.assertIsNotNone(moved, "the terminals did not move to an account that can be used up")
        other, read = self.wanted if moved is self.second else self.second, engine.cli.read
        self.assertLessEqual(engine.scheduler.planned(moved["id"]), engine.clock(), "no reading was asked for after the switch")
        self.assertGreater(engine.scheduler.planned(other["id"]), engine.clock() + 7000)

        def step():
            """A check, the reads it asked for, and the check after them. The scheduler starts the reads of two
            idle accounts at a time, so three turns of it reach every account, whatever their order."""
            self.check()
            for _ in range(3):
                engine._schedule(engine.clock())
            self.check()

        def read_one(account):
            reading.append(account)
            try:
                return read(account)
            finally:
                reading.pop()

        def answers(credentials):
            if reading[-1] == moved["id"]:
                raise DeckError("Too many requests.", "rate_limited", 3600)
            answer = copy.deepcopy(engine.store.account(reading[-1])["last_good_usage"])
            answer["checked"] = engine.clock()
            return answer
        engine.cli.read, engine.cli.adapter.usage = read_one, answers
        try:
            engine.clock.advance(cli_policy.READY + 60)
            engine._schedule(engine.clock())        # the read the engine asked for goes out, and Claude refuses it
            self.assertGreater(engine.store.account(moved["id"])["paused_until"] or 0, engine.clock() + 3000)
            step()
            self.assertEqual(self.started, [moved["id"]], "the terminals left the account they had just come to")
            self.assertEqual(self.hold(), ("none", False, self.PAUSED.format(moved["label"])))
            engine.clock.advance(cli_policy.STAY - cli_policy.READY)       # 16 minutes after the switch
            step()
        finally:
            del engine.cli.read, engine.cli.adapter.usage
        self.assertEqual(self.started, [moved["id"], other["id"]])
        self.assert_consistent("claude_cli", self.identity(other["id"]))

    def test_after_a_wake_an_old_reading_of_the_account_in_use_moves_nothing(self):
        """The reverse. After a start or a wake every reading is over 15 minutes old for some seconds. Nothing
        says the account in use cannot be read, so a reading of `second` that lands first moves nothing, and
        the rule asks for none. The first build of the third review's fix went by the age alone and moved.
        The account in use has no forecast here, and the clock alone stands in for the sleep. What a forecast
        does across a sleep is in `TerminalForecastTest`."""
        self.read(first=96, second=92, wanted=100)
        self.engine.clock.advance(1200)
        asked, request = [], self.engine.scheduler.request
        self.engine.scheduler.request = lambda key, now: asked.append(key) or request(key, now)
        try:
            self.check()
            support.with_reading(self.engine, self.second["id"], weekly=92, ends=4 * self.DAY - 1200)
            self.check(2)
        finally:
            del self.engine.scheduler.request
        self.assertEqual((self.started, asked), ([], []))
        self.assertEqual(self.hold(), ("none", False, f"Every account is past 95% — using {self.first['label']} up to 99%"))
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))

    def test_after_its_reset_a_used_up_account_is_read_at_the_schedulers_own_times(self):
        """Found by the second review, and by the third that no test ran the rule and the scheduler together.
        `wanted` was used up when it was last read, and its week ends 300 seconds later. The scheduler reads an
        account 45 and 300 seconds after a reset. The rule asked for a reading at its first check after the
        reset, the governor then put the scheduler's own reading off to 3 minutes after that one, and here
        Claude still answers with the old week in those first seconds. Every fake read answers with the
        numbers the account already has, so only the reads themselves change anything.
        The demo's clock runs with real time, so a read is noted by the step it came in, not by the clock's
        own number: the fourth review found that rounding that number failed the test on a busy Mac. Each
        step is 5 seconds or more past a time the scheduler acts at, or 15 or more short of it."""
        key, engine, step = self.wanted["id"], self.engine, ["before the reset"]
        self.read(first=100, second=100)
        support.with_reading(engine, key, weekly=100, ends=300)
        reset = engine.clock() + 300
        reading, started, asked = [], [], []
        read, tick, request = engine.cli.read, engine.scheduler.tick, engine.scheduler.request

        def read_one(account):
            reading.append(account)
            try:
                return read(account)
            finally:
                reading.pop()

        def the_same(credentials):
            answer = copy.deepcopy(engine.store.account(reading[-1])["last_good_usage"])
            answer["checked"] = engine.clock()
            return answer

        def watched(targets, now, clock):
            ids = tick(targets, now, clock)
            started.extend(step[0] for account in ids if account == key)
            return ids
        engine.cli.read, engine.cli.adapter.usage, engine.scheduler.tick = read_one, the_same, watched
        engine.scheduler.request = lambda account, now: (asked.append(step[0]) if account == key else None) or request(account, now)
        try:
            for _ in range(4):          # every account gets its plan, and the governor its 180 seconds, before the reset
                engine._schedule(engine.clock())
            self.assertEqual(started, ["before the reset"], "the scheduler did not read `wanted` at the start")
            for after in (5, 20, 30, 50, 120, 200, 310):
                step[0] = after
                engine.clock.advance(reset + after - engine.clock())
                self.check()
                engine._schedule(engine.clock())
        finally:
            del engine.cli.read, engine.cli.adapter.usage, engine.scheduler.tick, engine.scheduler.request
        self.assertEqual((asked, started[1:]), ([50], [50, 310]))
        self.assertEqual(self.started, [])

    def test_at_99_percent_the_account_that_is_ready_is_used_up_while_the_one_with_room_is_paused(self):
        """Below 99% the account in use is used on, and the hold names the pause. At 99% the rule used to wait
        for the pause to end until the account in use had run out."""
        self.read(first=98, second=92, wanted=30)
        self.engine.clock.advance(270)
        self.pause()
        self.read(first=98, second=92)
        self.check(2)
        self.assertEqual(self.started, [])
        self.assertEqual(self.hold(), ("none", False, self.PAUSED.format(self.wanted["label"])))
        self.engine.clock.advance(600)
        self.read(first=99, second=92)
        self.check()
        last = self.last()
        self.assertEqual((last.get("kind"), last.get("from"), last.get("to")), ("threshold", self.first["id"], self.second["id"]),
                         "the terminals did not move to the account that was ready")
        self.assertEqual(last["reason"], f"{self.first['label']} reached 99%; Claude paused the usage checks of {self.wanted['label']}, "
                         f"so {self.second['label']} is used up to 99%.")
        self.assert_consistent("claude_cli", self.identity(self.second["id"]))


class TerminalForecastTest(DeckCase):
    """The forecast of the account in use, through the real engine: the rate its readings rise at, carried to the
    moment its next reading can land (`cli_auto._forecast`). Two things moved the terminals for no cause, in
    the installed build too (2.2.1, commit 41420bb). With no account with room, two accounts that both fill
    fast traded places at every check. And a rate from before the Mac slept was carried across the sleep, so
    the terminals moved at the wake. `second` is the only other account that can be used here, and every
    other account has run out."""
    DAY = 86400
    USING = "Every account is past 95% — using {} up to 99%"
    read, check, hold, forecast = (TerminalPausedAccountTest.read, TerminalPausedAccountTest.check,
                                   TerminalPausedAccountTest.hold, TerminalPausedAccountTest.forecast)

    def setUp(self):
        super().setUp()
        self.engine.sense_active()
        self.engine.clock.advance(2 * 3600 + 60)        # past the demo's own switch, as in TerminalSoonestWeekTest
        self.first = self.active("claude_cli")
        usable = [row for row in self.rows("claude_cli") if not row["active"] and row["switchable"]
                  and row.get("health", "ok") == "ok" and not row.get("excluded")]
        self.second, self.wanted = usable[:2]
        self.rest = usable[2:]
        self.watch()

    def watch(self):
        self.started, submit = [], self.engine.submit
        self.engine.submit = lambda op: self.started.append(op.account) or submit(op)

    def checks(self, times=8):
        """`times` checks, 5 seconds apart, as the engine makes them."""
        for _ in range(times):
            self.check()
            self.engine.clock.advance(5)

    def asleep(self, seconds):
        """The Mac sleeps: the engine looked at the clock just now, and it looks again `seconds` later."""
        self.engine._looked = self.engine.clock()
        self.engine.clock.advance(seconds)
        self.look()

    def look(self):
        """The engine's look at the clock before an auto-switch check."""
        self.engine._awake(self.engine.clock())

    def test_two_accounts_that_fill_fast_do_not_trade_places_at_every_check(self):
        """Found by the first review of the change for a paused account. The account in use and `second` both
        went from 82% to 96% in 4 minutes, and no account has room. The forecast of whichever was in use said
        99%, and by its reading each could still be used up: 8 switches in 8 checks. A forecast is made from
        the readings since the terminals came to the account, so an account they just came to has none."""
        engine = self.engine
        self.read(first=82, second=82, wanted=100)
        engine.clock.advance(240)
        self.read(first=96, second=96, wanted=100)
        self.assertGreaterEqual(self.forecast(), 99, "the forecast of the account in use is under 99%: this test proves nothing")
        self.checks()
        self.assertEqual(self.started, [self.second["id"]], "the terminals did not move once, to the account to be used up")
        self.assertEqual(self.forecast(), 0, "an account the terminals just came to has a forecast")
        self.assertEqual(self.hold(), ("none", False, self.USING.format(self.second["label"])))
        self.assert_consistent("claude_cli", self.identity(self.second["id"]))
        # The reverse: with two readings of its own since then that rise as fast, its forecast counts again.
        support.with_reading(engine, self.second["id"], weekly=96.5, ends=4 * self.DAY)
        engine.clock.advance(150)
        support.with_reading(engine, self.second["id"], weekly=98.5, ends=4 * self.DAY - 150)
        support.with_reading(engine, self.first["id"], weekly=96, ends=6 * self.DAY)
        self.assertGreaterEqual(self.forecast(), 99)
        self.checks()
        self.assertEqual(self.started, [self.second["id"], self.first["id"]])
        self.assertEqual(self.hold(), ("none", False, self.USING.format(self.first["label"])))
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))

    def before_the_sleep(self):
        """The account in use was read at 95% and, 4 minutes later, at 96%. `second` is at 92%."""
        self.read(first=95, second=92, wanted=100)
        self.engine.clock.advance(240)
        support.with_reading(self.engine, self.first["id"], weekly=96, ends=6 * self.DAY - 240)
        self.assertLess(self.forecast(), 97)
        self.check()
        self.assertEqual(self.started, [])

    def test_a_fill_rate_from_before_a_sleep_is_not_carried_across_it(self):
        """Found by the fourth review of the change for a paused account. The forecast carried the rate across
        the 1000 seconds the Mac slept, said 100%, and the terminals moved as soon as the reading of `second`
        landed. The Mac was asleep, so the account was not used from here in that time."""
        self.before_the_sleep()
        self.asleep(1000)
        self.assertLess(self.forecast(), 97, "the fill rate was carried across the sleep")
        self.check()
        support.with_reading(self.engine, self.second["id"], weekly=92, ends=4 * self.DAY - 1240)
        self.check(2)
        self.assertEqual(self.started, [])
        self.assertEqual(self.hold(), ("none", False, self.USING.format(self.first["label"])))
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))

    def test_the_same_time_with_the_app_running_is_counted(self):
        """The reverse. The app ran for those 1000 seconds and no reading of the account in use came. That is
        what the forecast is for: the account may have been used all that time."""
        self.before_the_sleep()
        self.engine.clock.advance(1000)
        self.assertGreaterEqual(self.forecast(), 99)
        support.with_reading(self.engine, self.second["id"], weekly=92, ends=4 * self.DAY - 1240)
        self.check()
        self.assertEqual(self.started, [self.second["id"]])
        self.assert_consistent("claude_cli", self.identity(self.second["id"]))

    def test_readings_over_half_an_hour_old_make_no_forecast(self):
        """The forecast goes by the readings of the last half hour, as in the installed build (2.2.1, commit
        41420bb). The account in use rose from 82% to 85% in 4 minutes and was not read again. With the app
        running, the time since counts for 25 minutes. Then the first of the two readings is over half an hour
        old, and one reading makes no forecast: an account whose reading is that old is judged as one that
        cannot be read."""
        self.read(first=82, second=92, wanted=100)
        self.engine.clock.advance(240)
        support.with_reading(self.engine, self.first["id"], weekly=85, ends=6 * self.DAY - 240)
        self.engine.clock.advance(1500)
        self.assertGreaterEqual(self.forecast(), 99)
        self.engine.clock.advance(250)
        self.assertEqual(self.forecast(), 0)

    def test_a_sleep_in_the_middle_of_a_check_is_not_counted_either(self):
        """Found by the review of the forecast change (Sol 2). The engine looked for a gap at the start of a
        tick only. The Mac going to sleep later in that tick, before the auto-switch check, was noted one tick
        too late: the check after the wake still carried the fill rate across the sleep and moved the
        terminals. Here the account in use went from 92% to 96% in 4 minutes, and the Mac sleeps for 150
        seconds in the middle of a tick, with the reading of `second` still new enough for a switch."""
        engine = self.engine
        engine._schedule = lambda now: None         # no read of the engine's own: the readings are this test's
        self.read(first=92, second=92, wanted=100)
        engine.clock.advance(240)
        self.read(first=96, second=92, wanted=100)
        self.assertLess(self.forecast(), 98)
        engine.tick()
        self.assertTrue(engine.runner.wait(30))
        self.assertEqual(self.started, [])
        slow = engine._refresh_slow

        def the_mac_sleeps(now):
            engine._refresh_slow = slow
            engine.clock.advance(150)
            return slow(now)
        engine._refresh_slow = the_mac_sleeps
        engine.clock.advance(5)
        engine.tick()
        self.assertTrue(engine.runner.wait(30))
        self.assertEqual(self.started, [], "the fill rate was carried across a sleep in the middle of a tick")
        self.assertLess(self.forecast(), 98)
        # The reverse: the same 150 seconds with the app running, a look at the clock every 5 seconds, are
        # counted, and the terminals move.
        for _ in range(30):
            engine.clock.advance(5)
            self.look()
        support.with_reading(engine, self.second["id"], weekly=92, ends=4 * self.DAY - 550)
        self.assertGreaterEqual(self.forecast(), 99)
        engine.tick()
        self.assertTrue(engine.runner.wait(30))
        self.assertEqual(self.started, [self.second["id"]])

    def test_a_sleep_between_two_ticks_is_noted_by_the_engine_itself(self):
        """Sonnet 4 of the review of the forecast change: the other sleep tests make the engine's look at the
        clock themselves. Here two whole ticks run, 1000 seconds apart."""
        engine = self.engine
        engine._schedule = lambda now: None         # no read of the engine's own: the readings are this test's
        self.before_the_sleep()
        engine.tick()
        self.assertTrue(engine.runner.wait(30))
        self.assertEqual(engine.gaps, ())
        engine.clock.advance(1000)
        engine.tick()
        self.assertTrue(engine.runner.wait(30))
        self.assertEqual(len(engine.gaps), 1)
        self.assertAlmostEqual(engine.gaps[0][1], 1000, delta=5)
        self.assertLess(self.forecast(), 97, "the fill rate was carried across the sleep")
        self.assertEqual(self.started, [])

    def test_a_sleep_before_the_last_reading_is_not_taken_off_the_time_since_it(self):
        """Sonnet 3 of the review of the forecast change. The time the app did not run counts from the last
        reading on. A sleep before that reading is not time since it."""
        engine = self.engine
        self.read(first=95, second=92, wanted=100)
        self.asleep(600)
        support.with_reading(engine, self.first["id"], weekly=96.5, ends=6 * self.DAY - 600)
        self.assertLess(self.forecast(), 97)
        engine.clock.advance(600)           # with the app running
        self.assertGreaterEqual(self.forecast(), 98, "a sleep before the last reading was taken off the time since it")

    def test_readings_from_before_the_clock_was_set_back_make_no_forecast(self):
        """Found by the review of the forecast change (Sol 1). The Mac's clock is set back by an hour. The
        readings from before are then dated after now. They passed for readings since the terminals came, the
        time since the last one counted as none, and the forecast said 99% for whichever account was in use:
        the two accounts traded places again. The engine sees the clock go back at its next look, as it
        makes one every 5 seconds, and starts the forecast over."""
        engine = self.engine
        self.read(first=82, second=82, wanted=100)
        engine.clock.advance(240)
        self.read(first=96, second=96, wanted=100)
        self.look()
        self.assertGreaterEqual(self.forecast(), 99)
        engine.clock.advance(-3600)
        self.look()
        self.assertEqual(self.forecast(), 0, "readings from before the clock was set back made a forecast")
        self.checks()
        self.assertEqual(self.started, [])
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))
        # Sonnet 1 of the second round of that review. An hour later the clock has passed those readings, and
        # the next reading of the account is dated 90 seconds after the last of them. It makes no pair with
        # them: the account's usage rose over that hour and more, not over those 90 seconds.
        for _ in range(73):
            engine.clock.advance(50)
            self.look()
        support.with_reading(engine, self.first["id"], weekly=97, ends=6 * self.DAY - 330)
        self.assertEqual(self.forecast(), 0, "a reading from before the clock was set back made a pair with one from after it")
        self.assertEqual(self.started, [])

    def stop_the_clock(self):
        """The demo's clock runs with real time. From here on it stands still and moves only when the test moves
        it, so a test that sets it back by a few seconds does not depend on how fast this Mac is."""
        clock = self.engine.clock
        at = clock()
        clock.speed = 0
        clock.advance(at - clock())

    def slowed(self):
        """The account in use rose from 92% to 96% in 4 minutes and to 96.5% in the 4 minutes after: it slowed
        down, and its forecast is 97%. `second` was just read at 96%, and no account has room. The engine's
        last look at the clock was 4 seconds before the last reading, and the clock stands still at the time of
        that reading."""
        engine = self.engine
        self.stop_the_clock()
        self.read(first=92, second=96, wanted=100)
        engine.clock.advance(240)
        support.with_reading(engine, self.first["id"], weekly=96, ends=6 * self.DAY - 240)
        engine.clock.advance(236)
        self.look()
        engine.clock.advance(4)
        support.with_reading(engine, self.first["id"], weekly=96.5, ends=6 * self.DAY - 480)
        support.with_reading(engine, self.second["id"], weekly=96, ends=4 * self.DAY - 480)
        self.assertLess(self.forecast(), 98)
        self.check()
        self.assertEqual(self.started, [])

    def test_a_clock_set_back_a_little_does_not_bring_back_the_faster_rise_from_before_the_last_reading(self):
        """Found by the second round of the review of the forecast change (Sol 1). Readings dated after now were
        left out one by one. With the clock set back 15 seconds, only the last reading went, the one that says
        the account slowed down. The forecast then went by the rise before it, counted the 4 minutes since
        that older reading again, said over 99%, and the terminals moved off an account at 96.5%."""
        self.slowed()
        read_at = self.engine.clock()
        self.engine.clock.advance(-15)
        self.look()
        self.assertAlmostEqual(read_at - self.engine.clock(), 15, places=3)
        # The engine's last look was 4 seconds before the last reading, so it sees a set-back of 15 seconds and
        # starts the forecast over. This test holds that. The test below holds that no reading is left out
        # for its date (Opus 1 of the fourth round: this one passed with either of the two taken out).
        self.assertEqual(self.engine.forecast_from["claude_cli"], self.engine.store.reading_mark())
        self.assertEqual(self.forecast(), 0, "the forecast went by the rise from before the last reading")
        self.checks()
        self.assertEqual(self.started, [])
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))

    def test_a_clock_set_back_by_less_than_the_time_between_two_checks_does_not_either(self):
        """The same with the clock set back 3 seconds, which the engine cannot see: its last look at the clock
        was 4 seconds ago, so the clock still reads later than it did then. The last reading is dated 3 seconds
        after now. It counts as taken now. Sol 4 of the third round: with the clock running in real time, a Mac
        that took over 3 seconds from the last reading to here had that reading dated before now again, and
        the test passed whatever the forecast did with a reading dated after now. The clock stands still."""
        self.slowed()
        read_at, mark = self.engine.clock(), self.engine.forecast_from["claude_cli"]
        self.engine.clock.advance(-3)
        self.look()
        self.assertAlmostEqual(read_at - self.engine.clock(), 3, places=3,
                               msg="the last reading is not dated 3 seconds after now: this test proves nothing")
        self.assertEqual(self.engine.forecast_from["claude_cli"], mark, "the engine saw the clock go back and started over")
        self.assertTrue(96.5 < self.forecast() < 98, self.forecast())
        self.checks()
        self.assertEqual(self.started, [])
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))

    def test_readings_from_before_the_terminals_came_do_not_count_once_the_clock_has_passed_them(self):
        """Found by the lead while proving Sol 1 of the second round. `second` rose from 82% to 96% in 4 minutes
        while it was not in use. The clock is set back an hour, and then the terminals come to `second`. Its two
        readings are dated after that, so by their dates they were taken since the terminals came. They were
        left out only while they were dated after now: an hour later the clock had passed them, and they made
        a forecast of over 99% for an account that had not been read since."""
        engine = self.engine
        self.read(first=82, second=82, wanted=100)
        engine.clock.advance(240)
        self.read(first=96, second=96, wanted=100)
        self.look()
        engine.clock.advance(-3600)
        self.look()
        support.with_reading(engine, self.first["id"], weekly=100, ends=6 * self.DAY + 3360)
        self.check()
        self.assertEqual(self.started, [self.second["id"]])
        self.assert_consistent("claude_cli", self.identity(self.second["id"]))
        for _ in range(73):                 # an hour and a minute with the app running, a look every 50 seconds
            engine.clock.advance(50)
            self.look()
        self.assertEqual(engine.gaps, ())
        self.assertEqual(self.forecast(), 0, "readings from before the terminals came made a forecast")

    def answered_before_the_clock_was_set_back(self, key, back, *, weekly, ends):
        """A reading of `key` that Claude answers now. Before it is stored, the Mac's clock is set back by `back`
        seconds, and the engine sees that. A read dates its answer when it comes and stores it a moment later,
        after a switch that is running."""
        engine, observe = self.engine, self.engine.store.observe

        def stored_after(*args, **kwargs):
            del engine.store.observe
            engine.clock.advance(-back)
            self.look()
            return observe(*args, **kwargs)
        engine.store.observe = stored_after
        support.with_reading(engine, key, weekly=weekly, ends=ends)

    def running(self, seconds):
        """`seconds` with the app running: the engine looks at the clock every 5 seconds."""
        for _ in range(seconds // 5):
            self.engine.clock.advance(5)
            self.look()

    def test_a_reading_answered_before_the_clock_was_put_right_and_stored_after_it_does_not_stop_the_forecast(self):
        """Found by the fourth round of the review of the forecast change (Sol 1). With the clock an hour ahead,
        Claude answered a read. The clock was put right, the engine saw it and started the forecast over, and
        then the reading was stored: the first of the new series, dated an hour after now. The time from it
        to each later reading was below zero, so two new readings that rose from 82% to 96% in 150 seconds
        made no forecast, for over an hour."""
        engine, key = self.engine, self.first["id"]
        self.stop_the_clock()
        self.read(first=80, second=92, wanted=100)
        self.look()
        engine.clock.advance(3600)              # the clock is set an hour ahead
        self.look()
        self.answered_before_the_clock_was_set_back(key, 3595, weekly=81, ends=6 * self.DAY - 3600)
        for used, since in ((82, 155), (96, 305)):
            self.running(150)
            support.with_reading(engine, key, weekly=used, ends=6 * self.DAY - since)
        self.assertGreaterEqual(self.forecast(), 99, "a reading dated before the clock was put right stopped the forecast")
        support.with_reading(engine, self.second["id"], weekly=92, ends=4 * self.DAY - 305)
        self.check()
        self.assertEqual(self.started, [self.second["id"]])
        self.assert_consistent("claude_cli", self.identity(self.second["id"]))

    def test_a_reading_answered_before_the_clock_was_set_back_does_not_make_the_rise_look_faster(self):
        """Found by the same round (Opus 2). The same reading with the clock set back by 10 minutes. The readings
        that follow, 12 minutes later and on, are dated after it, so nothing looks out of order. But by the
        dates the account went from 55% to 92.5% in 270 seconds, where it took 870: the forecast said 100%
        for an account that had slowed to half a point in 150 seconds, and the terminals moved off it."""
        engine, key = self.engine, self.first["id"]
        self.stop_the_clock()
        self.read(first=50, second=92, wanted=100)
        self.look()
        self.running(60)
        self.answered_before_the_clock_was_set_back(key, 600, weekly=55, ends=6 * self.DAY - 60)
        for used, since, seconds in ((92, 180, 720), (92.5, 330, 150)):
            self.running(seconds)
            support.with_reading(engine, key, weekly=used, ends=6 * self.DAY - since)
        self.assertTrue(92.5 < self.forecast() < 94, self.forecast())
        support.with_reading(engine, self.second["id"], weekly=92, ends=4 * self.DAY - 330)
        self.check()
        self.assertEqual(self.started, [])
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))

    def test_removing_another_account_does_not_leave_the_account_in_use_without_a_forecast(self):
        """Found by the third round of the review of the forecast change (Sol 2). The mark was the highest id in
        the store's table of readings. Removing an account deletes its readings, and when the newest are among
        them, the readings stored next get ids below the mark. The account in use then had no forecast until
        the next change of account, however fast it filled. Here the newest readings, when the terminals come
        to `second`, are those of `wanted`, which is removed right after."""
        engine, gone = self.engine, self.wanted
        self.read(first=100, second=30, wanted=100)
        for _ in range(3):
            support.with_reading(engine, gone["id"], weekly=100, ends=7 * self.DAY)
        self.check()
        self.assertEqual(self.started, [self.second["id"]])
        engine.action("remove", {"id": gone["id"]})
        self.assertTrue(engine.runner.wait(30))
        self.assertIsNone(engine.store.account(gone["id"]), "the account was not removed")
        support.with_reading(engine, self.second["id"], weekly=82, ends=4 * self.DAY)
        engine.clock.advance(240)
        support.with_reading(engine, self.second["id"], weekly=96, ends=4 * self.DAY - 240)
        self.assertGreaterEqual(self.forecast(), 99, "the account in use has no forecast once another account was removed")

    def test_the_readings_of_a_removed_account_leave_the_memory(self):
        """The store keeps the readings this run of the app stored in memory (`Store.taken`). Those of an account
        that is removed go with it."""
        engine, gone = self.engine, self.wanted
        self.read(first=30, second=30, wanted=100)
        week = next(window["id"] for window in engine.store.account(gone["id"])["last_good_usage"]["windows"]
                    if window.get("kind") == "weekly")
        self.assertTrue(engine.store.taken(gone["id"], week, 0))
        engine.action("remove", {"id": gone["id"]})
        self.assertTrue(engine.runner.wait(30))
        self.assertIsNone(engine.store.account(gone["id"]), "the account was not removed")
        self.assertEqual(engine.store.taken(gone["id"], week, 0), [], "the readings of a removed account are still kept in memory")

    def bring_over(self, account):
        """Imports a move bundle from a PC that lists `account`, with its history, as still signed in there."""
        words = "synthetic passphrase for tests"
        self.addCleanup(setattr, move_import, "MIN_SCRYPT_N", move_import.MIN_SCRYPT_N)
        move_import.MIN_SCRYPT_N = 2 ** 10
        payload = {"format": 1, "source_platform": "windows", "settings": {}, "accounts": [account]}
        path = Path(self.paths.temp_root) / f"main-{uuid.uuid4().hex}.adeck"
        path.write_bytes(move_bundle.seal(payload, words, "main", scrypt_n=2 ** 10, min_scrypt_n=2 ** 10))
        result = self.engine.action("import-bundle", {"path": str(path), "passphrase": words})["result"]
        self.assertTrue(self.engine.runner.wait(30))
        return result

    def test_readings_brought_over_from_another_computer_make_no_forecast(self):
        """Found by the same round (Sol 3). A move bundle holds the readings its accounts had on the other
        computer, and the import stores the ones this Mac does not have. It stored them after the terminals
        came to the account in use, so they counted as readings since then. Two of them, from before the
        terminals came, at 60% and at 90%, made a forecast of over 99% beside the one reading this Mac has
        taken since, at 96%, and the terminals moved off the account."""
        engine, key = self.engine, self.second["id"]
        self.read(first=100, second=30, wanted=100)
        self.check()
        self.assertEqual(self.started, [key])
        came = engine.clock()
        engine.clock.advance(60)
        self.read(first=96, second=96, wanted=100)
        self.assertEqual(self.forecast(), 0)
        row = engine.store.account(key)
        week = next(window for window in row["last_good_usage"]["windows"] if window.get("kind") == "weekly")
        before = [{"window_id": week["id"], "at": came - ago, "used": used, "reset": week["reset"]}
                  for ago, used in ((300, 60.0), (150, 90.0))]
        result = self.bring_over({
            "id": "pc-second", "provider": "claude_cli", "identity": row["identity"], "label": row["label"],
            "email": row["email"] or "", "plan": row["plan"] or "", "org": "", "created": 1_700_000_000,
            "live_on_pc": True, "refills": [],
            "history": {"last_good_usage": None, "last_good_at": None, "readings": before, "snapshots": []}})
        self.assertEqual(result["failures"], [])
        stored = [reading["used"] for reading in engine.store.readings(key, week["id"], since=came - 301)]
        self.assertEqual(stored, [60.0, 90.0, 30.0, 96.0], "the import did not store the two readings")
        self.assertEqual(self.forecast(), 0, "readings brought over from another computer made a forecast")
        self.checks()
        self.assertEqual(self.started, [key, None])         # the switch from before, and the import itself
        self.assert_consistent("claude_cli", self.identity(key))

    def test_a_sleep_from_before_the_clock_was_set_back_is_not_taken_off_a_later_reading(self):
        """Found by the review of the forecast change (Sol 3). A sleep was noted with the time of its wake. With
        the clock set back, that time was after every new reading, so the sleep was taken off the age of
        readings made after it, and a forecast that should say 95% said 92%."""
        engine, key = self.engine, self.first["id"]
        self.read(first=50, second=30, wanted=100)
        self.asleep(1005)
        engine.clock.advance(-3600)
        self.look()
        support.with_reading(engine, key, weekly=91, ends=6 * self.DAY)
        engine.clock.advance(240)
        support.with_reading(engine, key, weekly=92, ends=6 * self.DAY - 240)
        engine.clock.advance(720)
        self.assertGreaterEqual(self.forecast(), 95, "a sleep from before these readings was taken off their age")

    def test_only_a_gap_of_a_minute_or_more_counts_as_time_the_app_did_not_run(self):
        """A check comes every 5 seconds. Under load one can come 20 seconds late, with the Mac awake and
        Claude Code running, and that is not a sleep. The demo's clock runs on in real time, so each sum is
        right to within a second."""
        engine = self.engine
        for seconds in (5, 20, 59):
            self.asleep(seconds)
        self.assertEqual(engine.idle_since(0), 0)
        self.asleep(61)
        woke = engine.clock()
        self.assertAlmostEqual(engine.idle_since(woke - 2), 61, delta=1)
        self.assertEqual(engine.idle_since(woke), 0, "a reading taken at the wake or after it was taken with the app running")
        self.asleep(1000)
        self.assertAlmostEqual(engine.idle_since(woke - 2), 1061, delta=1)
        self.assertAlmostEqual(engine.idle_since(woke), 1000, delta=1)
        # The list does not grow for as long as the app runs: the newest 32 are kept.
        for _ in range(40):
            self.asleep(70)
        self.assertEqual(len(engine.gaps), 32)
        self.assertAlmostEqual(engine.idle_since(woke), 32 * 70, delta=2)

    def test_the_readings_kept_in_memory_do_not_grow_for_as_long_as_the_app_runs(self):
        """The forecast reads the readings this run of the app has stored from memory (`Store.taken`). The
        newest 64 of a window are kept, in the order they were stored. That is over 3 hours of reads at the
        fastest they come, and the forecast goes by the last half hour."""
        engine, key = self.engine, self.first["id"]
        mark = engine.store.reading_mark()
        for used in range(70):
            support.with_reading(engine, key, weekly=used, ends=6 * self.DAY)
        week = next(window["id"] for window in engine.store.account(key)["last_good_usage"]["windows"]
                    if window.get("kind") == "weekly")
        self.assertEqual([reading["used"] for reading in engine.store.taken(key, week, mark)], [float(used) for used in range(6, 70)])
        self.assertEqual(engine.store.reading_mark(), mark + 70)

    def test_a_store_that_cannot_say_its_newest_reading_does_not_fail_a_switch(self):
        """The engine reads the mark from the store right after a switch has ended. When that read fails, the
        switch is still a switch that worked, and the account it went to has no forecast."""
        engine = self.engine

        def broken():
            raise RuntimeError("the store cannot be read")
        engine.store.reading_mark = broken
        self.read(first=100, second=30, wanted=100)
        self.check()
        del engine.store.reading_mark
        self.assertEqual(self.started, [self.second["id"]])
        self.assert_consistent("claude_cli", self.identity(self.second["id"]))
        history = engine.store.setting("cli_history", {})
        self.assertEqual((history["last"]["to"], history.get("tried") or {}), (self.second["id"], {}))
        self.assertIsNone(engine.forecast_from["claude_cli"])
        support.with_reading(engine, self.second["id"], weekly=82, ends=4 * self.DAY)
        engine.clock.advance(240)
        support.with_reading(engine, self.second["id"], weekly=96, ends=4 * self.DAY - 240)
        self.assertEqual(self.forecast(), 0)

    def test_the_forecast_starts_over_at_every_change_of_account(self):
        """The mark the forecast reads from (`Engine.forecast_from`) is how many reads this run had stored
        when the terminals came to the account. Every way to a change of account sets it, and a look that
        finds the same account does not."""
        engine = self.engine

        def newest():
            return engine.store.reading_mark()
        start = engine.forecast_from["claude_cli"]
        self.assertIsNotNone(start, "the demo's own switch, made while it was built, set no mark")
        self.read(first=30, second=30, wanted=100)
        engine.sense_active()
        self.assertEqual(engine.forecast_from["claude_cli"], start, "a look that found the same account started the forecast over")
        self.assertLess(start, newest())
        # A switch.
        self.read(first=100, second=30, wanted=100)
        self.check()
        self.assertEqual(self.started, [self.second["id"]])
        switched = engine.forecast_from["claude_cli"]
        self.assertGreater(switched, start)
        # A sign-in inside Claude Code, which the engine's next look at who is signed in finds (Sonnet 8 of the
        # review of the forecast change). The account's readings from before do not count.
        support.with_reading(engine, self.first["id"], weekly=82, ends=6 * self.DAY)
        engine.clock.advance(240)
        support.with_reading(engine, self.first["id"], weekly=96, ends=6 * self.DAY - 240)
        TerminalFailedSwitchTest.claude_code_signs_in_as(self, self.first)
        engine.clock.advance(5)
        engine.sense_active()
        self.assertEqual(self.active("claude_cli")["id"], self.first["id"])
        self.assertEqual(engine.forecast_from["claude_cli"], newest())
        self.assertEqual(self.forecast(), 0)
        # The Mac's clock is set back: the forecast starts over from the readings stored from then on, and the
        # gaps noted so far go with the readings they were about.
        self.asleep(100)
        support.with_reading(engine, self.first["id"], weekly=96.2, ends=6 * self.DAY - 345)
        self.assertLess(engine.forecast_from["claude_cli"], newest())
        self.assertEqual(len(engine.gaps), 1)
        engine.clock.advance(-3600)
        self.look()
        self.assertEqual((engine.forecast_from["claude_cli"], engine.gaps), (newest(), ()))
        # A start of the app: no switch was made in this run, and its first look at who is signed in sets the
        # mark (Opus 3 of the review of the forecast change).
        support.with_reading(engine, self.first["id"], weekly=96.4, ends=6 * self.DAY + 3255)
        engine.clock.advance(600)
        TerminalFailedSwitchTest.restart(self)
        self.assertEqual(self.engine.forecast_from["claude_cli"], self.engine.store.reading_mark())
        self.assertEqual(self.engine.gaps, ())

class TerminalFailedSwitchTest(DeckCase):
    """An automatic switch that does not happen is not started again at every check. The account in use here has
    run out. `wanted` is first in the order, `second` is ready behind it, and every other account is used up.
    The rule's own answers are in `tests.test_readings` (`FailedSwitchTest`). The demo's clock runs with real
    time, so each wait here ends well before or well after the rule's limit, never on it."""
    DAY = 86400
    ONCE = "A switch to {} was tried a moment ago and is tried once more shortly"
    TWICE = "A switch to {} was tried and is tried again in 15 min at the earliest"

    def setUp(self):
        super().setUp()
        self.engine.sense_active()
        self.engine.clock.advance(2 * 3600 + 60)        # past the demo's own switch, as in TerminalSoonestWeekTest
        self.first = self.active("claude_cli")
        usable = [row for row in self.rows("claude_cli") if not row["active"] and row["switchable"]
                  and row.get("health", "ok") == "ok" and not row.get("excluded")]
        self.second, self.wanted = usable[:2]
        self.rest = usable[2:]
        self.read_all()
        self.watch()

    def read_all(self, second=30):
        """New readings of every account: the one in use is out, `wanted`'s week ends soonest."""
        self.read_others(second)
        support.with_reading(self.engine, self.wanted["id"], weekly=30, ends=2 * self.DAY)

    def read_others(self, second=30):
        """New readings of every account but `wanted`, whose reading goes on getting older."""
        for row in self.rest:
            support.with_reading(self.engine, row["id"], weekly=99, ends=7 * self.DAY)
        support.with_reading(self.engine, self.first["id"], weekly=100, ends=6 * self.DAY)
        support.with_reading(self.engine, self.second["id"], weekly=second, ends=4 * self.DAY)

    def later(self, seconds, **readings):
        """`seconds` later, with new readings of every account."""
        self.engine.clock.advance(seconds)
        self.read_all(**readings)

    def check(self, times):
        """`times` checks of auto-switch, each run to its end."""
        for _ in range(times):
            cli_auto.tick(self.engine)
            self.assertTrue(self.engine.runner.wait(30))

    def refuse(self, row):
        """The login of `row` cannot be renewed for a switch, as when Claude's token endpoint refuses it."""
        renew = self.engine.cli.refresh

        def refresh(key, **how):
            if key == row["id"]:
                raise DeckError("The terminal login could not be renewed. Its last reading is kept.", "rate_limited")
            return renew(key, **how)
        self.engine.cli.refresh = refresh

    def notes(self):
        return self.engine.store.setting("cli_history", {}).get("tried") or {}

    def tries(self, row):
        """Which try in a row the note of `row` says its last one was, or None."""
        return (self.notes().get(row["id"]) or {}).get("count")

    def hold(self):
        decision = cli_auto.decision(self.engine)
        return (decision["action"], decision["exhausted"], decision["hold"])

    def watch(self):
        self.started, submit = [], self.engine.submit
        self.engine.submit = lambda op: self.started.append(op.account) or submit(op)

    def restart(self):
        """Quits the app and starts it again on the same data folder."""
        old = self.engine
        old.stop()
        self.engine = Engine(old.paths, providers=old.providers, procs=old.procs,
                             vault=Vault(old.paths.data_dir / "vault", protect=test_cipher), status_sources={},
                             clock=old.clock, start=False, inline=True, mode="demo")
        self.addCleanup(self.engine.stop)
        self.vault = self.engine.vault
        self.engine.sense_active()
        self.watch()

    def claude_code_signs_in_as(self, row):
        saved = self.vault.get(row["id"])
        Path(self.paths.claude_cli_credentials).write_text(json.dumps(saved["credentials"]))
        config = json.loads(Path(self.paths.claude_user_config).read_text())
        config["oauthAccount"] = saved["oauthAccount"]
        Path(self.paths.claude_user_config).write_text(json.dumps(config))

    def test_a_switch_that_fails_is_tried_once_more_and_then_the_next_account_is_used(self):
        """Found by the fifth review of the soonest-week change, in the build that was installed. A switch that
        fails before it writes left nothing behind, so the next check, 5 seconds later, decided the same and
        started it again: a call to renew the login and an alert each time, with the terminals left on an
        account that had run out while another account was ready."""
        self.refuse(self.wanted)
        self.check(3)
        self.assertEqual(self.started, [self.wanted["id"]])
        self.assertEqual(self.hold(), ("none", False, self.ONCE.format(self.wanted["label"])))
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))
        self.later(60)
        self.check(1)
        self.assertEqual(self.started, [self.wanted["id"]], "tried again before the 90 seconds were over")
        self.later(40)
        self.check(3)
        self.assertEqual(self.started, [self.wanted["id"], self.wanted["id"], self.second["id"]])
        self.assert_consistent("claude_cli", self.identity(self.second["id"]))
        self.assertEqual((list(self.notes()), self.tries(self.wanted)), ([self.wanted["id"]], 2),
                         "a switch that worked left its note behind")

    def test_a_failure_that_passes_costs_one_wait_and_the_terminals_get_to_the_first_account(self):
        """Found by the review of this change. Claude Code holds its login lock for a moment when the switch
        runs. With the account passed over at once, the terminals went to the next account and stayed."""
        real, calls = switch_cli.perform, []

        def perform(service, key, **how):
            calls.append(key)
            if len(calls) == 1:
                raise DeckError("Claude Code is renewing its login — try again in a few seconds", "busy")
            return real(service, key, **how)
        with mock.patch.object(switch_cli, "perform", perform):
            self.check(3)
            self.assertEqual(self.started, [self.wanted["id"]])
            self.assert_consistent("claude_cli", self.identity(self.first["id"]))
            self.later(100)
            self.check(2)
        self.assertEqual(self.started, [self.wanted["id"]] * 2)
        self.assert_consistent("claude_cli", self.identity(self.wanted["id"]))
        self.assertEqual(self.notes(), {})

    def stopped_by(self, failure):
        """Every switch ends with `failure`. Each account is tried twice, and then nothing is started."""
        with mock.patch.object(switch_cli, "perform", side_effect=failure):
            self.check(2)
            self.later(100)
            self.check(2)
            self.later(100)
            self.check(3)
        self.assertEqual(self.started, [self.wanted["id"]] * 2 + [self.second["id"]] * 2)
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))
        action, exhausted, hold = self.hold()
        self.assertEqual((action, exhausted), ("none", False))
        self.assertIn(f"Switches to {self.wanted['label']}, {self.second['label']} were tried", hold)

    def test_it_is_not_started_again_after_an_error_nobody_planned_for(self):
        self.stopped_by(RuntimeError("no such thing"))

    def test_it_is_not_started_again_after_a_cancel(self):
        self.stopped_by(switch_cli.OperationCancelled(switch_cli.CHANGED_SINCE))

    def test_a_switch_that_claude_code_changed_accounts_under_counts_as_tried(self):
        """The switch itself finds that Claude Code is signed in to another account by the time its files are
        locked, and switches nothing. The rule now decides for that account, and the try is noted."""
        renew = self.engine.cli.refresh

        def refresh(key, **how):
            self.claude_code_signs_in_as(self.second)
            return renew(key, **how)
        self.engine.cli.refresh = refresh
        self.check(1)
        del self.engine.cli.refresh
        self.assertEqual(self.started, [self.wanted["id"]])
        report = self.engine.snapshot()["report"]
        self.assertEqual((report["outcome"], report["summary"]), ("cancelled", switch_cli.CHANGED_SINCE))
        self.assert_consistent("claude_cli", self.identity(self.second["id"]))
        self.assertEqual(self.tries(self.wanted), 1)
        self.read_all()
        self.check(2)
        self.assertEqual(self.started, [self.wanted["id"]])

    def test_an_early_switch_that_fails_is_noted_like_any_other(self):
        """Found by the review of this change: no test held it. The early switch waits its own 2 hours after a
        try. The note of that try still counts when another kind of switch wants the account: the account in
        use runs out a moment later, and the switch that just failed is not started again at once."""
        support.with_reading(self.engine, self.first["id"], weekly=20, ends=6 * self.DAY)
        self.refuse(self.wanted)
        self.check(2)
        self.assertEqual((self.started, self.tries(self.wanted)), ([self.wanted["id"]], 1))
        self.assertEqual(self.hold(), ("none", False, cli_policy.EARLY_PAUSE))
        support.with_reading(self.engine, self.first["id"], weekly=100, ends=6 * self.DAY)
        self.check(2)
        self.assertEqual(self.started, [self.wanted["id"]])
        self.assertEqual(self.hold(), ("none", False, self.ONCE.format(self.wanted["label"])))

    def test_the_only_account_with_room_is_waited_for_and_tried_again_15_minutes_later(self):
        """With no other account to use, the page says what is waited for. It does not say every account is
        low, which starts an alert and is not true."""
        self.read_all(second=99)
        self.refuse(self.wanted)
        self.check(2)
        self.later(100, second=99)
        self.check(3)
        self.assertEqual(self.started, [self.wanted["id"]] * 2)
        self.assertEqual(self.hold(), ("none", False, self.TWICE.format(self.wanted["label"])))
        self.assertNotIn("limit_reached", [alert["kind"] for alert in self.engine.store.alerts()])
        self.later(850, second=99)
        self.check(1)
        self.assertEqual(self.started, [self.wanted["id"]] * 2, "tried again before the 15 minutes were over")
        # The reverse: once the 15 minutes are over it is tried again, and when the login can be renewed it works.
        self.later(70, second=99)
        del self.engine.cli.refresh
        self.check(1)
        self.assertEqual(self.started, [self.wanted["id"]] * 3)
        self.assert_consistent("claude_cli", self.identity(self.wanted["id"]))
        self.assertEqual(self.notes(), {})

    def test_after_a_refused_renewal_the_next_try_waits_for_a_reading_the_app_cannot_take_yet(self):
        """Found by the review of this change. The login of `wanted` is due for a renewal and Claude's token
        endpoint refuses it. After that the app does not read the account for 30 minutes
        (`CLIAccounts._refresh`), and a switch needs a reading at most 3 minutes old. So 15 minutes after the
        last try nothing can be started, which is why the hold says "at the earliest"."""
        self.read_all(second=99)
        key, service = self.wanted["id"], self.engine.cli
        saved = self.vault.get(key)
        saved["credentials"]["claudeAiOauth"]["expiresAt"] = (self.engine.clock() + 400) * 1000     # due for a switch
        self.vault.put(key, saved)
        asked = []

        def refused(credentials, *, timeout=None):
            asked.append(self.engine.clock())
            error = DeckError("Too many requests.", "rate_limited")
            error.http, error.http_class = 429, "4xx"
            raise error
        service.adapter.refresh = refused
        self.check(2)
        self.engine.clock.advance(100)          # the reading of `wanted` is still new enough for a switch
        self.check(2)
        self.assertEqual((self.started, len(asked)), ([key] * 2, 2))
        self.assertEqual(self.hold(), ("none", False, self.TWICE.format(self.wanted["label"])))
        for seconds in (500, 420):              # 15 minutes after the last try, and past them
            self.engine.clock.advance(seconds)
            self.assertEqual(service.read(key), "busy")
            self.check(1)
        self.assertEqual((self.started, len(asked)), ([key] * 2, 2))
        # 30 minutes after the last try the app reads the account again, and that read asks for the renewal.
        self.engine.clock.advance(900)
        self.assertEqual(service.read(key), "rate_limited")
        self.assertEqual((self.started, len(asked)), ([key] * 2, 3))

    def through_the_scheduler(self):
        """Only a read that is asked for goes out: the scheduler's own next read of every account is put 2 hours
        off. A read that reaches Claude is answered with the numbers the account had, dated now."""
        engine, reading, read = self.engine, [], self.engine.cli.read
        for row in self.rows("claude_cli"):
            engine.scheduler.defer(row["id"], engine.clock() + 7200)

        def read_one(account):
            reading.append(account)
            try:
                return read(account)
            finally:
                reading.pop()

        def answers(credentials):
            answer = copy.deepcopy(engine.store.account(reading[-1])["last_good_usage"])
            answer["checked"] = engine.clock()
            return answer
        engine.cli.read, engine.cli.adapter.usage = read_one, answers

    def step(self):
        """A check, the reads it asked for, and the check after them. The scheduler starts the reads of two
        idle accounts at a time, so three turns of it reach every account, whatever their order."""
        self.check(1)
        for _ in range(3):
            self.engine._schedule(self.engine.clock())
        self.check(1)

    def refuse_the_renewal(self, wait=None):
        """The reading of `wanted` is 2 minutes old, its login runs out in 100 seconds, and Claude's token
        endpoint refuses to renew it. With `wait`, Claude says to ask again that many seconds later."""
        self.engine.clock.advance(120)
        saved = self.vault.get(self.wanted["id"])
        saved["credentials"]["claudeAiOauth"]["expiresAt"] = (self.engine.clock() + 100) * 1000
        self.vault.put(self.wanted["id"], saved)

        def refused(credentials, *, timeout=None):
            error = DeckError("Too many requests.", "rate_limited", wait)
            error.http, error.http_class = 429, "4xx"
            raise error
        self.engine.cli.adapter.refresh = refused

    def test_an_account_that_cannot_be_read_is_passed_over_for_the_next_one(self):
        """Found by the second review of the change for failed switches, in the installed build too (2.2.1,
        commit 41420bb). The first switch to `wanted` fails because Claude refuses to renew its login. It is due
        again 90 seconds later, and by then the reading of `wanted` is over 3 minutes old, so a new one is asked
        for first. That read needs the same renewal, and the app does not ask Claude for it again for 30
        minutes. The rule went on naming `wanted`, nothing started, the page said nothing, and the terminals
        stayed on an account that had run out until that reading was 15 minutes old, with `second` ready."""
        engine, key = self.engine, self.wanted["id"]
        self.through_the_scheduler()
        self.refuse_the_renewal()
        engine.store.set_setting("cli_renewal_wait", {"an account that is gone": engine.clock() + 7 * self.DAY})
        self.read_others()
        self.check(1)
        self.assertEqual((self.started, self.tries(self.wanted)), ([key], 1))
        engine.clock.advance(100)       # past the 90 seconds, with the reading of `wanted` 220 seconds old
        self.read_others()
        self.check(1)
        self.assertEqual(self.started, [key], "a switch was started on a reading over 3 minutes old")
        self.assertLessEqual(engine.scheduler.planned(key), engine.clock(), "no reading of the account was asked for")
        for _ in range(3):
            engine._schedule(engine.clock())
        self.assertEqual(engine.store.account(key)["error_code"], "busy", "the read that was asked for did not fail")
        # The read says until when no read of the account can be taken: 30 minutes after the switch asked Claude.
        wait = engine.cli.renewal_waits()[key] - engine.clock()
        self.assertTrue(1800 - 100 - 60 < wait < 1800 - 100, wait)
        self.check(1)
        self.assertEqual(self.started, [key, self.second["id"]], "the terminals stayed on an account that had run out")
        self.assert_consistent("claude_cli", self.identity(self.second["id"]))

        def skipped():
            """The account's words in the order, as the page is handed them."""
            return next(row["skip"] for row in self.engine.snapshot()["providers"]["claude_cli"]["autoswitch"]["next_up"]
                        if row["id"] == key)
        self.assertEqual(skipped(), "its login could not be renewed")
        # The app is quit and started again: the wait is kept with the app's data, so it is still passed over.
        self.restart()
        engine = self.engine
        self.through_the_scheduler()
        self.assertEqual(skipped(), "its login could not be renewed")
        # The reverse: once a read of it works, it is a target again, also when that reading is over 3 minutes old.
        del engine.cli.adapter.refresh
        engine.clock.advance(1800)
        engine.scheduler.request(key, engine.clock())
        for _ in range(3):
            engine._schedule(engine.clock())
        self.assertLess(engine.clock() - engine.store.account(key)["last_good_at"], 60, "the read did not work")
        self.assertEqual(engine.cli.renewal_waits(), {}, "a wait whose account is gone, or was read, is still noted")
        engine.clock.advance(cli_policy.READY + 60)
        self.assertIsNone(skipped())

    def test_the_only_account_left_is_asked_again_and_used_once_its_reading_comes(self):
        """The reverse. With no other account to go to, the page says that the login of `wanted` could not be
        renewed, nothing says that every account is low, and no switch is made on the old reading. Once
        Claude renews the login, the reading comes and the terminals move."""
        engine, key = self.engine, self.wanted["id"]
        self.through_the_scheduler()
        self.refuse_the_renewal()
        self.read_others(second=99)
        self.check(1)
        engine.clock.advance(100)
        self.read_others(second=99)
        self.step()
        self.assertEqual(self.started, [key])
        self.assertEqual(self.hold(), ("none", False, f"The login of {self.wanted['label']} could not be renewed"))
        self.assertNotIn("limit_reached", [alert["kind"] for alert in engine.store.alerts()])
        # 30 minutes after the refused renewal the app asks Claude again, and this time the login is renewed.
        del engine.cli.adapter.refresh
        engine.clock.advance(1800)
        self.read_others(second=99)
        self.step()
        self.assertEqual(self.started, [key, key])
        self.assert_consistent("claude_cli", self.identity(key))

    def test_an_account_whose_read_failed_for_another_cause_is_asked_again_and_waited_for(self):
        """Found by the review of the change for an account that cannot be read (Sol 1, Sonnet 1, Opus 2 and 3).
        That change passed an account over after any read that failed. A read that fails because the network
        is down for a moment says nothing about the next one, which goes out 3 minutes later at most. With
        `second` at 96% and ready, the terminals moved to it and, once the next read of `wanted` had worked,
        on to `wanted`: two switches where the installed build (2.2.1, commit 41420bb) made one."""
        engine, key = self.engine, self.wanted["id"]
        self.through_the_scheduler()
        answers = engine.cli.adapter.usage
        engine.clock.advance(240)

        def no_network(credentials):
            raise DeckError("No connection.", "network")
        engine.cli.adapter.usage = no_network
        self.read_others(second=96)
        self.step()
        self.assertEqual(engine.store.account(key)["error_code"], "network", "the read of `wanted` did not fail")
        self.assertNotIn(key, engine.cli.renewal_waits())
        decision = cli_auto.decision(engine)
        self.assertEqual((self.started, decision["action"], decision["target"]), ([], "switch", key),
                         "the terminals did not wait for the next reading of `wanted`")
        # The next read goes out 3 minutes after the one that failed, and works.
        engine.cli.adapter.usage = answers
        engine.clock.advance(200)
        self.read_others(second=96)
        self.step()
        self.assertEqual(self.started, [key])
        self.assert_consistent("claude_cli", self.identity(key))

    def test_a_renewal_claude_refuses_inside_a_read_is_not_followed_by_a_switch_on_the_last_reading(self):
        """Found by the review of the change for an account that cannot be read (Opus 1), in the installed build
        too (2.2.1, commit 41420bb). The app renews the login of an idle account inside a usage read. Claude
        answering that renewal with HTTP 429 was filed as a refused usage read, so the account counted as
        paused, and with nothing else left the terminals were moved to it on its last reading. That switch
        asks Claude for the same renewal. The read now says that the login was not renewed."""
        engine, key = self.engine, self.wanted["id"]
        self.through_the_scheduler()
        self.refuse_the_renewal()
        engine.clock.advance(100)       # the login of `wanted` has run out, and its reading is 220 seconds old
        self.read_others(second=100)
        engine.scheduler.request(key, engine.clock())
        for _ in range(3):
            engine._schedule(engine.clock())
        row = engine.store.account(key)
        self.assertEqual(row["error_code"], "rate_limited", "the renewal inside the read was not refused")
        self.assertGreater(row["paused_until"], engine.clock())
        self.assertTrue(1800 - 60 < engine.cli.renewal_waits()[key] - engine.clock() <= 1800)
        self.check(2)
        self.assertEqual(self.started, [], "the terminals were moved to an account whose login Claude had just refused to renew")
        self.assertEqual(self.hold(), ("none", False, f"The login of {self.wanted['label']} could not be renewed"))

    def test_a_renewal_claude_refuses_for_an_hour_is_not_asked_for_by_a_switch_within_that_hour(self):
        """Found by the second round of that review (Sol 2). Claude answers the renewal inside a read with HTTP
        429 and says to wait an hour. The login counted as not renewed for 30 minutes and as paused for the 30
        minutes after: with nothing else left, the terminals were then moved to the account on its last
        reading, and that switch asked Claude for the renewal inside the hour."""
        engine, key = self.engine, self.wanted["id"]
        self.through_the_scheduler()
        self.refuse_the_renewal(wait=3600)
        engine.clock.advance(100)
        self.read_others(second=100)
        engine.scheduler.request(key, engine.clock())
        for _ in range(3):
            engine._schedule(engine.clock())
        row = engine.store.account(key)
        self.assertEqual(row["error_code"], "rate_limited", "the renewal inside the read was not refused")
        self.assertGreater(row["paused_until"], engine.clock() + 3500)
        engine.clock.advance(1900)          # past the 30 minutes, inside the hour
        self.read_others(second=100)
        self.check(2)
        self.assertEqual(self.started, [], "a switch asked Claude for the renewal inside the hour it said to wait")
        # The reverse: after the hour a read asks Claude again, the login is renewed, and the terminals move.
        del engine.cli.adapter.refresh
        engine.clock.advance(1800)
        self.read_others(second=100)
        self.step()
        self.assertEqual(self.started, [key])
        self.assert_consistent("claude_cli", self.identity(key))

    def refused_inside_a_read(self, wait=None):
        """`wanted` is the only account left to go to, its reading is 220 seconds old, and the next read of it
        asks Claude to renew its login, which Claude refuses (HTTP 429). With `wait`, Claude says to ask again
        that many seconds later."""
        self.through_the_scheduler()
        self.refuse_the_renewal(wait)
        self.engine.clock.advance(100)
        self.read_others(second=100)

    def read_wanted(self):
        """The read of `wanted` that a check asked for."""
        self.engine.scheduler.request(self.wanted["id"], self.engine.clock())
        for _ in range(3):
            self.engine._schedule(self.engine.clock())

    def beside(self, action):
        """Runs `action` in another thread, to its end, when that thread can get at the store right now. A
        thread that holds the store's lock across two steps is not looked in on between them, and `action`
        then does not run. Says whether it ran."""
        lock, outcome = self.engine.store.lock, {}

        def other():
            if not lock.acquire(blocking=False):
                return
            lock.release()
            try:
                outcome["value"] = action()
            except BaseException as exc:
                outcome["error"] = exc
        thread = threading.Thread(target=other, daemon=True)
        thread.start()
        thread.join(60)
        self.assertFalse(thread.is_alive(), "the other thread did not come to an end")
        if "error" in outcome:
            raise outcome["error"]
        return "value" in outcome

    def on_a_last_reading(self, decisions):
        return [(decision["action"], decision["target"]) for decision in decisions if decision["last_reading"]]

    def test_a_check_in_the_middle_of_a_read_does_not_see_the_pause_without_the_wait(self):
        """Found by the third round of the review of these fixes (Sol 1). The wait after a refused renewal went
        from the row to a setting of its own, so the read that ends at a refused renewal makes two writes: the
        pause on the row, and the wait. A check that looked between the two saw a paused account with no wait,
        took it for one whose usage checks Claude paused, and with nothing else left moved the terminals to
        it on its last reading. That switch asks Claude for the renewal it just refused. The wait is written
        first now. Here a check looks before each write of the read."""
        engine, key = self.engine, self.wanted["id"]
        self.refused_inside_a_read()
        seen, update, set_setting = [], engine.store.update, engine.store.set_setting

        def looked_in_on(write):
            def writes(*args, **kwargs):
                self.beside(lambda: seen.append(cli_auto.decision(engine)))
                return write(*args, **kwargs)
            return writes
        engine.store.update, engine.store.set_setting = looked_in_on(update), looked_in_on(set_setting)
        self.read_wanted()
        del engine.store.update, engine.store.set_setting
        self.assertEqual(engine.store.account(key)["error_code"], "rate_limited", "the renewal inside the read was not refused")
        self.assertIn(key, engine.cli.renewal_waits())
        self.assertTrue(seen, "no check looked while the read wrote: this test proves nothing")
        self.assertEqual(self.on_a_last_reading(seen), [], "a check saw the pause of a refused renewal without its wait")
        self.check(2)
        self.assertEqual(self.started, [])

    def test_a_read_that_ends_in_the_middle_of_a_check_does_not_show_it_the_pause_without_the_wait(self):
        """The same from the other side. The check reads the waits and the rows. A read that ended between the
        two handed it the waits from before the read and the row from after it. It makes both looks under the
        store's lock now, which keeps a read's writes out until both are made."""
        engine, key = self.engine, self.wanted["id"]
        self.refused_inside_a_read()
        looks = {"waits": engine.cli.renewal_waits, "rows": engine.store.accounts}
        first, ended = [], []

        def then_the_read_ends(look):
            def looked(*args, **kwargs):
                value = look(*args, **kwargs)
                if not first:
                    first.append(look)      # the check's first look, and none of the read's own
                    ended.append(self.beside(self.read_wanted))
                return value
            return looked
        engine.cli.renewal_waits, engine.store.accounts = then_the_read_ends(looks["waits"]), then_the_read_ends(looks["rows"])
        decision = cli_auto.decision(engine)
        del engine.cli.renewal_waits, engine.store.accounts
        self.assertEqual(len(ended), 1, "the check made neither look: this test proves nothing")
        if not ended[0]:
            self.read_wanted()          # the read was held out until the check had made both looks
        self.assertEqual(engine.store.account(key)["error_code"], "rate_limited", "the renewal inside the read was not refused")
        self.assertEqual(self.on_a_last_reading([decision]), [], "a check saw the pause of a refused renewal without its wait")
        self.check(2)
        self.assertEqual(self.started, [])
        self.assertEqual(self.hold(), ("none", False, f"The login of {self.wanted['label']} could not be renewed"))

    def test_the_app_dying_in_the_middle_of_that_read_leaves_no_pause_without_its_wait(self):
        """The same read, and the app dies before its second write. The data it leaves is what the next start
        reads: with the pause written and the wait not, the account was a paused one for the rest of the pause,
        and the terminals were moved to it on its last reading. The wait is written first."""
        engine, key = self.engine, self.wanted["id"]
        self.refused_inside_a_read(wait=3600)
        asked, written, refused = [], [], engine.cli.adapter.refresh

        def asks(credentials, **how):
            asked.append(credentials)
            return refused(credentials, **how)
        engine.cli.adapter.refresh = asks

        def dies_at_the_second(write):
            def writes(*args, **kwargs):
                if asked:
                    written.append(write)
                    if len(written) == 2:
                        raise Crash
                return write(*args, **kwargs)
            return writes
        engine.store.update, engine.store.set_setting = (dies_at_the_second(engine.store.update),
                                                         dies_at_the_second(engine.store.set_setting))
        with self.assertRaises(Crash):
            self.read_wanted()
        self.restart()
        engine = self.engine
        self.through_the_scheduler()
        row = engine.store.account(key)
        self.assertTrue(row["paused_until"] is None or key in engine.cli.renewal_waits(),
                        "the app died with the pause of a refused renewal written and its wait not")
        self.read_others(second=100)
        self.check(2)
        self.assertEqual(self.started, [], "the terminals were moved to an account whose login Claude had just refused to renew")
        # Opus 3 of the fourth round. The row has no pause, so the read the check asked for goes out. It ended
        # at "checked recently", with a wait of 30 minutes from the renewal Claude refused. That wait was
        # noted over the hour, and 30 minutes later a read asked Claude again, inside the hour. A read does
        # not ask while the wait that is noted runs.
        self.read_wanted()
        self.assertEqual(engine.store.account(key)["error_code"], "busy", "the read that was asked for did not fail")
        self.assertGreater(engine.cli.renewal_waits()[key], engine.clock() + 3000, "a later read made the wait shorter")
        engine.clock.advance(1900)
        self.read_others(second=100)
        self.step()
        self.assertEqual((self.started, len(asked)), ([], 1), "Claude was asked for the renewal inside the hour it said to wait")
        # The reverse: after the hour a read asks Claude again.
        engine.clock.advance(1800)
        self.read_others(second=100)
        self.step()
        self.assertEqual((self.started, len(asked)), ([], 2))

    def test_a_wait_is_not_dropped_while_the_clock_is_ahead(self):
        """Found by the fourth round of the review of these fixes (Sol 2). A read of any account dropped the
        waits of all accounts that had ended. Claude refused the renewal of `wanted` for an hour. The clock was
        set two hours ahead, by which the wait had ended, and a read of another account dropped it. With the
        clock put right, the pause on the row of `wanted` ran again, with no wait: a paused account, and the
        terminals were moved to it on its last reading, inside the hour Claude said to wait."""
        engine, key = self.engine, self.wanted["id"]
        self.refused_inside_a_read(wait=3600)
        self.read_wanted()
        self.assertGreater(engine.cli.renewal_waits()[key], engine.clock() + 3500)
        engine.clock.advance(7200)              # the clock is set two hours ahead
        del engine.cli.adapter.refresh
        self.assertEqual(engine.cli.read(self.second["id"]), "ok")
        engine.clock.advance(-6600)             # and put right: 10 minutes after the refused renewal
        self.assertGreater(engine.store.account(key)["paused_until"], engine.clock() + 2900)
        self.read_others(second=100)
        self.check(2)
        self.assertEqual(self.started, [], "the terminals were moved to an account inside the hour Claude said to wait")
        self.assertEqual(self.hold(), ("none", False, f"The login of {self.wanted['label']} could not be renewed"))

    def test_a_wait_that_is_no_number_is_no_wait(self):
        """Found missing by the fourth round of that review (Sonnet 4). The waits are read back from the file,
        and no test had put a value there that is no number. Such a value is no wait: the read of that
        account asks Claude for the renewal, and the checks go on. A setting that is no table of waits holds
        none."""
        engine, key = self.engine, self.wanted["id"]
        engine.store.set_setting("cli_renewal_wait", ["no", "table"])
        self.assertEqual(engine.cli.rows_and_waits()[1], {})
        self.assertEqual(cli_auto.decision(engine)["action"], "switch")     # the rule reads it and goes on
        self.refused_inside_a_read()
        asked, refused = [], engine.cli.adapter.refresh

        def asks(credentials, **how):
            asked.append(credentials)
            return refused(credentials, **how)
        engine.cli.adapter.refresh = asks
        engine.store.set_setting("cli_renewal_wait", {key: "soon", self.second["id"]: None})
        self.step()
        self.assertEqual(len(asked), 1, "the read did not ask Claude for the renewal")
        self.assertGreater(engine.cli.renewal_waits()[key], engine.clock())
        self.assertEqual(self.started, [])
        self.assertEqual(self.hold(), ("none", False, f"The login of {self.wanted['label']} could not be renewed"))

    def test_a_writer_of_flags_cannot_hide_that_a_login_was_not_renewed(self):
        """Found by the second round of that review (Sol 3). Every writer of a row's `flags` writes the whole set
        back, and Leave Out does so under another lock than a read (`TerminalSoonestWeekTest` has the same
        for a pause). The time until when no read can be taken was kept in `flags`. Leave Out could write it
        away with the copy it had read before, and the rule went back to waiting on that account."""
        engine, key = self.engine, self.wanted["id"]
        self.through_the_scheduler()
        self.refuse_the_renewal()
        self.read_others()
        self.check(1)
        engine.clock.advance(100)
        self.read_others()
        self.check(1)
        before = engine.store.account(key)["flags"]                                 # what Leave Out read
        for _ in range(3):
            engine._schedule(engine.clock())
        self.assertEqual(engine.store.account(key)["error_code"], "busy", "the read that was asked for did not fail")
        engine.store.update(key, flags=dict(before, excluded=False))                # what it then wrote
        self.check(1)
        self.assertEqual(self.started, [key, self.second["id"]], "the rule waits on an account that cannot be read")
        self.assert_consistent("claude_cli", self.identity(self.second["id"]))

    def test_the_page_is_told_when_a_read_has_ended(self):
        """Found by the review of the change for an account that cannot be read (Sol 2), in the installed build
        too (2.2.1, commit 41420bb). A read tells the page that the state changed (`Engine.bump`) and only then
        does the scheduler note that the read no longer runs. A page that asked in between got the state from
        before, and nothing told it of the change: it went on saying that the account was being read."""
        engine, key = self.engine, self.wanted["id"]
        self.through_the_scheduler()
        engine.clock.advance(240)

        def no_network(credentials):
            raise DeckError("No connection.", "network")

        def page():
            return next(row["refreshing"] for row in engine.snapshot()["accounts"] if row["id"] == key)
        engine.cli.adapter.usage = no_network
        said, completed, read = [], engine.scheduler.completed, engine.scheduler._read

        def asked_in_between(target, outcome, now):
            if target.id == key:
                said.append(page())             # the page asks after the read's own last word
            completed(target, outcome, now)

        def asked_after(target, clock):
            read(target, clock)
            if target.id == key:
                said.append(page())             # and again once the scheduler is done with the read
        engine.scheduler.completed, engine.scheduler._read = asked_in_between, asked_after
        engine.scheduler.request(key, engine.clock())
        for _ in range(3):
            engine._schedule(engine.clock())
        self.assertEqual(said, [True, False])

    def test_a_try_within_the_hour_of_the_last_one_counts_on_from_it(self):
        key = self.wanted["id"]
        # A second try comes within 15 minutes of the first, and a later one within the hour of the last. From
        # the step of -60 on, the Mac's clock was set back and the note is dated after now. A first try dated
        # after now is never counted on from: the rule stops waiting for it once it is over 90 seconds off, so
        # the next try comes at once, and counting it as the second passed the account over with no wait.
        for wait, count in ((0, 1), (100, 2), (3500, 3), (3500, 4), (3700, 1), (1000, 1), (600, 2),
                            (-60, 3), (-3700, 1), (-1000, 1), (-200, 1), (-30, 1), (300, 2)):
            with self.subTest(clock_moved_by=wait):
                self.engine.clock.advance(wait)
                cli_auto.note_try(self.engine, key)
                self.assertEqual(self.tries(self.wanted), count)
        for found in (5, "soon", None, [1, 2], {"at": "soon", "count": 2}, {"at": self.engine.clock(), "count": 0}):
            with self.subTest(note=found):
                with cli_auto.history(self.engine) as history:
                    history["tried"] = {key: found, self.second["id"]: {"at": 1.0, "count": 7}}
                cli_auto.note_try(self.engine, key)
                self.assertEqual((self.tries(self.wanted), self.tries(self.second)), (1, 7))

    def test_the_note_is_still_there_after_the_app_starts_again(self):
        """The note is in the app's data file, so quitting does not start the switch again either."""
        self.refuse(self.wanted)
        self.check(1)
        self.restart()
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))
        self.assertEqual(self.hold(), ("none", False, self.ONCE.format(self.wanted["label"])))
        self.refuse(self.wanted)
        self.later(100)
        self.check(1)
        self.assertEqual(self.started, [self.wanted["id"]])
        self.restart()
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["target"]), ("switch", self.second["id"]))
        self.later(920)
        self.assertEqual(cli_auto.decision(self.engine)["target"], self.wanted["id"])

    def test_the_app_dies_at_each_write_of_an_automatic_switch(self):
        """The app dies in an automatic switch after each of its writes and is started again. Recover puts the
        account that was in use back. The switch that died is not started again at the next check. It is
        tried once more after the wait, and then it works."""
        for step in TerminalInterruptedSwitchTest.STEPS:
            with self.subTest(step=step):
                self.setUp()
                store = self.engine.cli.adapter.store

                def then_die(real):
                    def call(*args, **kwargs):
                        real(*args, **kwargs)
                        raise Crash()
                    return call
                steps = TerminalInterruptedSwitchTest.STEPS
                seam = {steps[0]: (store, "write", then_die(store.write)),
                        steps[1]: (switch_cli, "write_account", then_die(switch_cli.write_account)),
                        steps[2]: (switch_cli.CLITransaction, "commit", lambda tx: (_ for _ in ()).throw(Crash()))}[step]
                with mock.patch.object(*seam), mock.patch.object(switch_cli, "settle", side_effect=Crash()):
                    self.check(1)
                self.assertEqual(self.started, [self.wanted["id"]])
                self.restart()
                self.assertEqual(len(self.pending_records()), 1, "the switch that died left no record")
                self.check(1)
                self.assertEqual(self.started, [], "auto-switch acted while a switch waits for Recover")
                switching.dispatch(self.engine, "recover", {})
                self.assertEqual(self.pending_records(), [])
                self.assert_consistent("claude_cli", self.identity(self.first["id"]))
                self.read_all()
                self.check(2)
                self.assertEqual(self.started, [], "the switch that died was started again at the next check")
                self.later(100)
                self.check(2)
                self.assertEqual(self.started, [self.wanted["id"]])
                self.assert_consistent("claude_cli", self.identity(self.wanted["id"]))
                self.assertEqual(self.notes(), {})

    def test_a_switch_the_app_died_after_does_not_keep_its_account_passed_over(self):
        """Found by the review of this change. The app dies after the login is switched and before the history
        is written, so the note of that switch stays. The account is in use and works. When the user then moves
        on by hand and that account runs out, the first one was passed over until the note was 15 minutes old,
        with the terminals left on an account that had run out."""
        cli_auto.note_try(self.engine, self.wanted["id"])
        cli_auto.note_try(self.engine, self.wanted["id"])       # the second try in a row is the one that works
        switch_cli.perform(self.engine.cli, self.wanted["id"], leaving=self.identity(self.first["id"]))     # and the app dies here
        self.assertEqual(self.tries(self.wanted), 2)
        self.restart()
        self.assert_consistent("claude_cli", self.identity(self.wanted["id"]))
        self.check(1)       # the next check sees the account in use
        self.assertEqual(self.act("claude_cli", "switch", self.second["id"])["outcome"], "success")
        self.read_all(second=100)
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["target"], decision["hold"]), ("switch", self.wanted["id"], None))
        self.assertEqual(self.notes(), {})

    def test_a_switch_the_app_died_after_still_keeps_the_terminals_for_15_minutes(self):
        """Found by the fifth review of the change for a paused account. An account an automatic switch moved
        the terminals to is used for 15 minutes before its paused usage checks count (`cli_policy.STAY`), so
        that Claude refusing its first read does not move the terminals on at once. The rule took the time of
        that switch from what the switch writes at its end. The app dies after the login is switched and
        before that, so the time was missing, and the terminals left `wanted` for `second` as soon as Claude
        refused a read of it. The check that takes out the note of the try keeps its time (`came`)."""
        support.with_reading(self.engine, self.second["id"], weekly=96, ends=4 * self.DAY)
        support.with_reading(self.engine, self.wanted["id"], weekly=96, ends=2 * self.DAY)
        cli_auto.note_try(self.engine, self.wanted["id"], self.identity(self.first["id"]))
        tried_at = self.notes()[self.wanted["id"]]["at"]
        switch_cli.perform(self.engine.cli, self.wanted["id"], leaving=self.identity(self.first["id"]))     # and the app dies here
        self.restart()
        self.assert_consistent("claude_cli", self.identity(self.wanted["id"]))
        self.assertNotEqual((self.engine.store.setting("cli_history", {}).get("last") or {}).get("to"), self.wanted["id"])
        self.engine.clock.advance(200)      # the reading that switch went by is over 3 minutes old

        def refused(credentials):
            raise DeckError("Too many requests.", "rate_limited", 3600)
        self.engine.cli.adapter.usage = refused
        self.assertEqual(self.engine.cli.read(self.wanted["id"]), "rate_limited")
        del self.engine.cli.adapter.usage
        support.with_reading(self.engine, self.second["id"], weekly=96, ends=4 * self.DAY - 200)
        self.check(2)
        self.assertEqual(self.started, [], "the terminals left the account they had just come to")
        self.assertEqual(self.hold(), ("none", False, f"Claude paused the usage checks of {self.wanted['label']}"))
        history = self.engine.store.setting("cli_history", {})
        self.assertEqual((history.get("came"), self.notes()), ({"to": self.wanted["id"], "at": tried_at}, {}))
        # 16 minutes after that switch the pause counts, and the account that can be read is used up.
        self.engine.clock.advance(cli_policy.STAY + 60 - 200)
        support.with_reading(self.engine, self.second["id"], weekly=96, ends=4 * self.DAY - 960)
        self.check(1)
        self.assertEqual(self.started, [self.second["id"]])
        self.assert_consistent("claude_cli", self.identity(self.second["id"]))

    def test_a_note_of_a_try_that_cannot_be_read_keeps_no_time(self):
        """Found by the sixth review: no test held it. The check that finds the terminals on an account takes
        out the note of a try to it and keeps that note's time, for the 15 minutes the account is then used
        (`cli_policy._came`). A note with no time it can read is taken out too, and keeps none: no switch is
        known to have brought the terminals there, so no stay starts. Usage checks are paused here, so the
        check does nothing else."""
        self.engine.update_settings({"refresh_paused": True})
        with cli_auto.history(self.engine) as past:
            past["tried"] = {self.first["id"]: {"at": "soon", "count": 2}}
        self.check(1)
        self.assertEqual((self.started, self.notes(), self.engine.store.setting("cli_history", {}).get("came")), ([], {}, None))

    def test_an_account_used_while_usage_checks_were_paused_is_not_passed_over_afterwards(self):
        """Found by the second review of this change. With usage checks paused auto-switch does nothing, and it
        also left the note of the account in use where it was. The user signs in to that account in Claude Code,
        moves on by hand and turns the checks back on: the account was passed over until its note was 15
        minutes old."""
        cli_auto.note_try(self.engine, self.wanted["id"])
        cli_auto.note_try(self.engine, self.wanted["id"])
        self.engine.update_settings({"refresh_paused": True})
        self.claude_code_signs_in_as(self.wanted)
        self.engine.sense_active()
        self.assert_consistent("claude_cli", self.identity(self.wanted["id"]))
        self.read_all(second=100)       # the account in use is out by its reading, and nothing is to move
        tried_at = self.notes()[self.wanted["id"]]["at"]
        self.check(1)
        self.assertEqual((self.started, self.notes()), ([], {}))
        # The time of that try is kept: it may be the switch that brought the terminals here (`cli_policy._came`).
        self.assertEqual(self.engine.store.setting("cli_history", {}).get("came"), {"to": self.wanted["id"], "at": tried_at})
        self.assertEqual(self.act("claude_cli", "switch", self.second["id"])["outcome"], "success")
        self.engine.update_settings({"refresh_paused": False})
        self.read_all(second=100)
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["target"], decision["hold"]), ("switch", self.wanted["id"], None))

    def test_an_automatic_switch_asks_the_provider_itself_whose_login_is_live(self):
        """Found by the second review of this change. Claude Code renewed its login, so the app asks the
        provider whose login is live before it files it, and a lookup that failed is not repeated for a minute.
        Each check asks again when that minute is over, and a failure starts the next minute. So the second
        try could meet a failed lookup less than a minute old and fail on it without asking, with the provider
        answering again by then. The account was passed over and the terminals went to the next one. The build
        that was installed got them to the first account at the next lookup."""
        service = self.engine.cli
        answer = service.adapter.resolve_account
        service.adapter.resolve_account = mock.Mock(side_effect=DeckError("offline", "network"))
        support.claude_code_renews(self.engine)
        self.engine.sense_active()          # the app asks whose the new login is, and gets no answer
        self.check(1)
        self.assertEqual((self.started, self.tries(self.wanted)), ([self.wanted["id"]], 1))
        self.later(75)
        asked = service.adapter.resolve_account.call_count
        self.engine.sense_active()          # it asks again after its minute, and gets no answer
        self.assertEqual(service.adapter.resolve_account.call_count, asked + 1)
        service.adapter.resolve_account = answer
        self.later(20)
        self.engine.sense_active()          # not asked: the last lookup failed under a minute ago
        self.check(1)
        self.assertEqual(self.started, [self.wanted["id"]] * 2)
        self.assert_consistent("claude_cli", self.identity(self.wanted["id"]))
        self.assertEqual(self.notes(), {})

    def test_a_first_try_that_was_never_tried_again_does_not_count_against_a_later_one(self):
        """Found by the second review of this change. A first try failed and its second try was never made: the
        Mac slept. Half an hour later the terminals are on the same account, and one passing failure stops the
        switch. The old note made that the second try in a row, so the account was passed over for 15 minutes,
        which is what one failure was not to cost. The terminals do not move here, so only the age of the note
        ends the count (`test_a_try_after_the_user_moved_the_terminals_is_a_first_try_again` has the move)."""
        self.refuse(self.wanted)
        self.check(1)
        self.assertEqual(self.tries(self.wanted), 1)
        self.later(1800)
        self.check(2)
        self.assertEqual((self.started, self.tries(self.wanted)), ([self.wanted["id"]] * 2, 1))
        self.assertEqual(self.hold(), ("none", False, self.ONCE.format(self.wanted["label"])))
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))

    def said(self):
        """What the page is told about `wanted`: the hold, and why its row is skipped."""
        auto = self.engine.snapshot()["providers"]["claude_cli"]["autoswitch"]
        return auto["hold"], next(row["skip"] for row in auto["next_up"] if row["id"] == self.wanted["id"])

    def watch_the_page(self):
        """Has every switch from here on note what the page is told while it runs: (which try, hold, skip)."""
        real, seen = switch_cli.perform, []

        def perform(*args, **how):
            seen.append((self.tries(self.wanted), *self.said()))
            return real(*args, **how)
        patch = mock.patch.object(switch_cli, "perform", perform)
        patch.start()
        self.addCleanup(patch.stop)
        return seen

    def test_the_page_is_not_told_a_switch_was_tried_while_that_switch_runs(self):
        """Found by the second and third reviews of this change. The note is written when the switch starts, so
        a page that asked while the switch ran was told it "was tried a moment ago and is tried once more",
        and during a second try that its account is skipped and the next one is next."""
        seen = self.watch_the_page()
        self.refuse(self.wanted)
        self.check(1)
        self.assertEqual(seen, [(1, None, None)])
        # The reverse: once that switch has failed, the page says so.
        self.assertEqual(self.said(), (self.ONCE.format(self.wanted["label"]), None))
        self.later(100)
        self.check(1)
        self.assertEqual(seen[1:], [(2, None, None)])
        self.assertEqual(self.said(), (None, "switch tried 1 min ago"))

    def test_the_page_is_not_told_so_while_the_switch_runs_beside_another_clients_wait(self):
        """Found by the third review of this change, by two reviewers apart. A terminal switch that runs beside
        another client's wait is not the operation the page shows as pending, so the page was still told it
        "was tried a moment ago"."""
        waiting = support.WaitingOperation("codex")
        self.addCleanup(waiting.release.set)
        Engine.submit(self.engine, waiting)
        self.wait_for_phase("waiting_idle")
        seen = self.watch_the_page()
        self.refuse(self.wanted)
        cli_auto.tick(self.engine)
        deadline = time.monotonic() + 10
        while not (seen and self.engine.runner.free_for("claude_cli", beside_waits=True)) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.engine.snapshot()["pending"]["provider"], "codex")
        self.assertEqual(seen, [(1, None, None)])
        self.assertEqual(self.said(), (self.ONCE.format(self.wanted["label"]), None))
        waiting.release.set()
        self.assertTrue(self.engine.runner.wait(30))

    def test_the_page_is_not_told_the_early_switch_waits_while_an_early_switch_runs(self):
        """Found by the fourth review of this change, by two reviewers apart, and already in the build that was
        installed. `tick` writes the early switch's own mark as soon as it has handed the switch over, so while
        that switch ran the page said "The early switch waits 2 hours after an automatic switch"."""
        support.with_reading(self.engine, self.first["id"], weekly=20, ends=6 * self.DAY)
        real, seen = switch_cli.perform, []

        def mark():
            return self.engine.store.setting("cli_history", {}).get("early_tried")
        before = mark()

        def perform(*args, **how):
            deadline = time.monotonic() + 10
            while mark() == before and time.monotonic() < deadline:     # `tick` writes it on the test's thread
                time.sleep(0.02)
            seen.append((mark() != before, *self.said()))
            return real(*args, **how)
        self.refuse(self.wanted)
        with mock.patch.object(switch_cli, "perform", perform):
            self.check(1)
        self.assertEqual((self.started, seen), ([self.wanted["id"]], [(True, None, None)]))
        # The reverse: once that switch has failed, the page says that the early switch waits.
        self.assertEqual(self.said(), (cli_policy.EARLY_PAUSE, None))

    def test_the_page_is_not_told_so_by_a_state_that_was_being_built_when_the_switch_started(self):
        """Found by the fourth review of this change, by two reviewers apart. The state read which operations
        ran, then the accounts, then the notes. A second try that started in between had written its note by
        then, and that one answer told the page its account is skipped, until the page asked again."""
        self.refuse(self.wanted)
        self.check(1)
        self.later(100)
        real_perform, real_inputs = switch_cli.perform, cli_auto.inputs
        held, go, gap = threading.Event(), threading.Event(), []

        def perform(*args, **how):
            held.set()
            go.wait(30)
            return real_perform(*args, **how)

        def inputs(engine):
            if not gap:
                gap.append(True)
                cli_auto.tick(engine)                   # the next check, while the state is being built
                self.assertTrue(held.wait(30))          # the second try has written its note and is held
            return real_inputs(engine)
        with mock.patch.object(switch_cli, "perform", perform), mock.patch.object(cli_auto, "inputs", inputs):
            try:
                self.assertEqual((self.said(), self.tries(self.wanted), gap), ((None, None), 2, [True]))
            finally:
                go.set()
            self.assertTrue(self.engine.runner.wait(30))
        self.assertEqual(self.said(), (None, "switch tried 1 min ago"))

    def then_the_account_they_moved_to_runs_out(self, wait):
        """The terminals are on `second`. `wait` seconds later it has run out, and one failure stops the switch
        to `wanted`. That failure is the first of a new need, so it is waited out and tried once more."""
        self.assert_consistent("claude_cli", self.identity(self.second["id"]))
        self.started.clear()
        self.later(wait, second=100)
        self.check(2)
        self.assertEqual((self.started, self.tries(self.wanted)), ([self.wanted["id"]], 1))
        self.assertEqual(self.hold(), ("none", False, self.ONCE.format(self.wanted["label"])))

    def test_a_try_after_the_user_moved_the_terminals_is_a_first_try_again(self):
        """Found by the third review of this change. The count went on by the clock alone: 5 minutes after a
        first try, with the terminals moved by hand to another account since, one passing failure was counted
        as the second in a row and the wanted account was passed over for 15 minutes."""
        self.refuse(self.wanted)
        self.check(1)
        self.assertEqual(self.act("claude_cli", "switch", self.second["id"])["outcome"], "success")
        self.assertEqual(self.tries(self.wanted), 1)
        self.then_the_account_they_moved_to_runs_out(300)

    def test_a_try_after_the_next_account_was_used_is_a_first_try_again(self):
        """Found by the third review of this change. Two tries failed and the next account was used. When that
        one ran out 40 minutes later, one passing failure was counted as the third in a row."""
        self.refuse(self.wanted)
        self.check(1)
        self.later(100)
        self.check(3)
        self.assertEqual((self.started, self.tries(self.wanted)), ([self.wanted["id"]] * 2 + [self.second["id"]], 2))
        self.then_the_account_they_moved_to_runs_out(2500)

    def test_a_try_after_a_sign_in_inside_claude_code_is_a_first_try_again(self):
        """The terminals can move without the app: the user signs in to another account inside Claude Code. The
        note names the account its try was leaving, so the count starts again here too."""
        self.refuse(self.wanted)
        self.check(1)
        self.claude_code_signs_in_as(self.second)
        self.engine.sense_active()
        self.then_the_account_they_moved_to_runs_out(300)

    def test_a_try_after_the_terminals_went_away_and_came_back_counts_on(self):
        """A decision, held here. The note names the account its try was leaving and says nothing of where the
        terminals have been since: away and back within the 15 minutes, and the next try is the second."""
        self.refuse(self.wanted)
        self.check(1)
        for row in (self.second, self.first):
            self.claude_code_signs_in_as(row)
            self.engine.sense_active()
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))
        self.later(100)
        self.check(1)
        self.assertEqual((self.started, self.tries(self.wanted)), ([self.wanted["id"]] * 2, 2))

    def test_a_first_failure_then_the_clock_set_back_costs_one_wait_and_not_the_account(self):
        """Found by the third review of this change. With the Mac's clock set back by more than 90 seconds the
        rule no longer waits for the first try, whose note is now dated after now. The next check tries again
        at once. That try is a first try: the terminals stay, and the switch is tried once more."""
        self.refuse(self.wanted)
        self.check(1)
        self.engine.clock.advance(-200)
        self.read_all()
        self.check(2)
        self.assertEqual((self.started, self.tries(self.wanted)), ([self.wanted["id"]] * 2, 1))
        self.assertEqual(self.hold(), ("none", False, self.ONCE.format(self.wanted["label"])))

    def test_a_try_counts_on_only_from_a_note_that_names_the_account_it_leaves(self):
        """The note of a try names the account it was leaving. A note without one, as this change wrote before
        it stored that, counts on only from a try that leaves no saved account."""
        key, first = self.wanted["id"], self.engine.active_identity["claude_cli"]
        self.assertEqual(self.engine.store.find("claude_cli", first)["id"], self.first["id"])
        for note, leaving, count in (({}, first, 1), ({}, None, 2), ({"from": self.first["id"]}, first, 2),
                                     ({"from": self.second["id"]}, first, 1), ({"from": self.first["id"]}, None, 1),
                                     ({"from": None}, "an account that is not saved", 2)):
            with self.subTest(note=note, leaving=leaving):
                with cli_auto.history(self.engine) as history:
                    history["tried"] = {key: {"at": self.engine.clock(), "count": 1, **note}}
                cli_auto.note_try(self.engine, key, leaving)
                origin = self.first["id"] if leaving == first else None
                self.assertEqual((self.tries(self.wanted), self.notes()[key]["from"]), (count, origin))

    def test_a_check_with_nobody_signed_in_leaves_the_notes_alone(self):
        """A check takes out the note of the account in use. With Claude Code signed out there is none."""
        cli_auto.note_try(self.engine, self.wanted["id"])
        Path(self.paths.claude_cli_credentials).unlink()
        config = json.loads(Path(self.paths.claude_user_config).read_text())
        config.pop("oauthAccount", None)
        Path(self.paths.claude_user_config).write_text(json.dumps(config))
        self.engine.sense_active()
        self.assertTrue(self.engine.identity_known.get("claude_cli"))
        self.assertIsNone(self.engine.active_identity.get("claude_cli"))
        cli_auto.tick(self.engine)
        self.assertEqual(self.tries(self.wanted), 1)

    def test_a_switch_by_hand_that_works_takes_the_note_out(self):
        """The reverse of the note. The user signs the terminals in to the account themselves, so its login
        works. When they move on, the account is a target again at once."""
        self.read_all(second=99)
        self.refuse(self.wanted)
        self.check(1)
        self.assertEqual(list(self.notes()), [self.wanted["id"]])
        del self.engine.cli.refresh
        self.assertEqual(self.act("claude_cli", "switch", self.wanted["id"])["outcome"], "success")
        self.assertEqual(self.notes(), {})

    def test_a_note_that_is_no_table_of_notes_does_not_fail_a_switch_that_worked(self):
        """The note is taken out after the login was written. A history that holds something else under that
        name must not turn a switch that happened into one that reports a failure."""
        for found in (["x"], "x", 5, None):
            with self.subTest(tried=found):
                self.read_all()
                with cli_auto.history(self.engine) as history:
                    history["tried"] = found
                # By hand: nothing is noted first, so the end of the switch meets the history as it was found.
                self.assertEqual(self.act("claude_cli", "switch", self.second["id"])["outcome"], "success")
                self.assert_consistent("claude_cli", self.identity(self.second["id"]))
                with cli_auto.history(self.engine) as history:
                    history["tried"] = found
                self.engine.submit(switch_cli.CLISwitch(self.engine, self.first["id"], kind="out", reason="It ran out."))
                self.assertTrue(self.engine.runner.wait(30))
                self.assertEqual(self.engine.snapshot()["report"]["outcome"], "success")
                self.assert_consistent("claude_cli", self.identity(self.first["id"]))
                with cli_auto.history(self.engine) as history:
                    history["tried"] = found
                self.check(1)       # a check takes the note of the account in use out, when there is a table

    def test_a_switch_by_hand_that_fails_leaves_no_note(self):
        """The user saw it fail and chose it themselves. The wait is for switches nobody watches."""
        self.refuse(self.wanted)
        self.assertEqual(self.act("claude_cli", "switch", self.wanted["id"])["outcome"], "failed")
        self.assertEqual(self.notes(), {})
        self.assertEqual(cli_auto.decision(self.engine)["target"], self.wanted["id"])

    def test_a_switch_that_could_not_be_started_leaves_no_note(self):
        """The job runner is busy, so nothing ran: the next check starts the switch."""
        asked = []

        def busy(op):
            asked.append(op.account)
            raise DeckError("Finish the current operation first.", "busy")
        self.engine.submit = busy
        cli_auto.tick(self.engine)
        self.assertEqual(asked, [self.wanted["id"]])
        self.assertEqual(self.notes(), {})
        self.assertEqual(cli_auto.decision(self.engine)["target"], self.wanted["id"])


class TerminalFableLimitTest(DeckCase):
    """Auto-switch and the Fable weekly limit, through the real engine: who is signed in afterwards. The
    rule's own answers are in `tests.test_readings` (the decision table and `FableLimitTest`)."""

    def setUp(self):
        super().setUp()
        self.engine.sense_active()
        self.first = self.active("claude_cli")
        usable = [row for row in self.rows("claude_cli") if not row["active"] and row["switchable"]
                  and row.get("health", "ok") == "ok" and not row.get("excluded")]
        self.full, self.roomy = usable[:2]          # two accounts auto-switch may move to

    def read(self, row, **reading):
        support.with_reading(self.engine, row["id"], **reading)

    def switched(self):
        """Runs one check of auto-switch and the switch it starts. Returns the record of that switch. The demo
        starts with the record of an earlier switch, so a check that switches nothing is told apart from it."""
        before = self.engine.store.setting("cli_history", {}).get("last")
        cli_auto.tick(self.engine)
        self.assertTrue(self.engine.runner.wait(30))
        last = self.engine.store.setting("cli_history", {}).get("last")
        self.assertNotEqual(last, before, "auto-switch did not switch")
        return last

    def test_a_fable_limit_past_the_threshold_moves_the_terminals(self):
        self.read(self.first, weekly=30, fable=96)
        self.read(self.full, weekly=20, fable=20)
        self.read(self.roomy, weekly=40, fable=20)
        last = self.switched()
        self.assertEqual(last["kind"], "threshold")
        self.assertIn("reached 95% of its Fable weekly limit", last["reason"])
        self.assert_consistent("claude_cli", self.identity(self.full["id"]))

    def test_a_reading_an_older_build_saved_goes_by_the_name_fable(self):
        """Older builds saved the limit as "Fable 5.1 weekly". The store hands every reader the saved
        reading under the name now (`usage.saved_reading`): the page's rows, the menu bar's line and the
        reason auto-switch gives."""
        from accountdeck.platform import menubar
        now = self.engine.clock()
        self.engine.store.observe(self.first["id"], support.shipped_reading(now, 10.0, 30.0, 96.0), at=now)
        self.read(self.full, weekly=20, fable=20)
        self.read(self.roomy, weekly=40, fable=20)
        self.engine.bump()
        state = self.engine.snapshot()
        row = next(row for row in state["accounts"] if row["id"] == self.first["id"])
        self.assertEqual([(w["label"], w["short"]) for w in row["windows"]], [("Weekly", "7d"), ("Fable weekly", "Fable")])
        self.assertIn(f"{row['label']} Fable 96% used", menubar.tooltip(state, now))
        self.assertNotIn("5.1", menubar.tooltip(state, now))
        self.assertIn("reached 95% of its Fable weekly limit", self.switched()["reason"])

    def test_it_never_moves_to_an_account_whose_fable_limit_is_out(self):
        # `full` has the most room in its week, so the rule that looks at the week alone picks it
        self.read(self.first, weekly=97, fable=10)
        self.read(self.full, weekly=20, fable=100)
        self.read(self.roomy, weekly=40, fable=20)
        self.assertEqual(self.switched()["kind"], "threshold")
        self.assert_consistent("claude_cli", self.identity(self.roomy["id"]))

    def test_an_account_whose_week_ran_out_is_left_for_one_whose_fable_limit_alone_is_out(self):
        """Found by the review of this change: the rule refused every account whose Fable limit was out and
        kept the terminals on an account that answered for no model. Then the reverse: the account it moved to
        is not left again for the one whose week ran out."""
        others = [row for row in self.rows("claude_cli") if not row["active"]]
        for row in others:
            self.read(row, weekly=20, fable=100)
        # the usual road: at 99% the account still works, so the terminals stay and the app says all are low
        self.read(self.first, weekly=99, fable=10)
        before = self.engine.store.setting("cli_history", {}).get("last")
        cli_auto.tick(self.engine)
        self.assertTrue(self.engine.runner.wait(30))
        history = self.engine.store.setting("cli_history", {})
        self.assertEqual((history.get("last"), history.get("exhausted")), (before, True))
        self.read(self.first, weekly=100, fable=10)
        last = self.switched()
        self.assertEqual(last["kind"], "out", "no limit refilled")
        self.assertIn(last["to"], [row["id"] for row in (self.full, self.roomy)])
        self.assertRegex(last["reason"], r"reached its weekly limit; .+'s week can still be used, though its Fable weekly limit is at 100%\.$")
        self.assert_consistent("claude_cli", self.identity(last["to"]))
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["exhausted"]), ("none", True))
        cli_auto.tick(self.engine)
        self.assertTrue(self.engine.runner.wait(30))
        self.assertEqual(self.engine.store.setting("cli_history", {}).get("last"), last)
        self.assert_consistent("claude_cli", self.identity(last["to"]))

    def test_a_fable_limit_the_shipped_version_saved_counts_on_the_first_check(self):
        """Data the installed 2.0.0 wrote: until the next read, its saved reading is all the app knows."""
        now = self.engine.clock()
        for row, (weekly, fable) in ((self.first, (30.0, 100.0)), (self.full, (20.0, 100.0)), (self.roomy, (40.0, 20.0))):
            self.engine.store.observe(row["id"], support.shipped_reading(now, 10.0, weekly, fable), at=now)
        self.assertEqual(self.switched()["kind"], "out")
        self.assert_consistent("claude_cli", self.identity(self.roomy["id"]))

    def test_an_account_in_use_whose_fable_limit_is_out_is_still_read_often(self):
        """Its week can still run out, and only a new reading tells the rule so. Found by the review of this
        change: the read schedule took a Fable limit at 100% for an account with nothing left to learn."""
        from accountdeck import scheduler

        def schedule(**reading):
            self.read(self.first, **reading)
            target = next(target for target in self.engine.targets() if target.id == self.first["id"])
            return target.active, target.hot, target.exhausted, scheduler.base_interval(target)
        self.assertEqual(schedule(weekly=90, fable=100), (True, True, False, 75.0))
        self.assertEqual(schedule(weekly=30, fable=100), (True, True, False, 75.0))
        self.assertEqual(schedule(weekly=100, fable=20), (True, True, True, 600.0), "a week that ran out is waited for, as before")

    def test_an_account_with_an_old_reading_does_not_keep_the_terminals_on_one_that_cannot_work(self):
        """Found by the second review of this change. The first account in the user's order has a reading an
        hour old, so a check asks for a new one and switches nothing. While that read failed, every later
        check asked for the same account, and the one behind it with a new reading was never used."""
        self.engine.update_settings({"cli_order": "manual", "cli_manual_order": [self.full["id"], self.roomy["id"]]})
        self.read(self.first, weekly=100, fable=10)
        self.read(self.full, weekly=20, fable=100)
        self.read(self.roomy, weekly=40, fable=100)
        old = self.engine.store.account(self.full["id"])
        self.engine.store.observe(self.full["id"], old["last_good_usage"], at=self.engine.clock() - 3600)
        self.assertEqual([row["id"] for row in cli_auto.decision(self.engine)["next_up"]][:2], [self.full["id"], self.roomy["id"]])
        last = self.switched()
        self.assertEqual((last["to"], last["kind"]), (self.roomy["id"], "out"))
        self.assert_consistent("claude_cli", self.identity(self.roomy["id"]))

    def test_an_account_that_cannot_work_does_not_wait_for_a_better_accounts_reading(self):
        """Found by the second review of this change. An account with room and a reading an hour old is read
        again before it is used. The account in use works for no model, so the terminals move now to the
        account that is ready, and not when that read comes back."""
        self.read(self.first, weekly=100, fable=10)
        self.read(self.full, weekly=20, fable=20)
        self.read(self.roomy, weekly=40, fable=100)
        old = self.engine.store.account(self.full["id"])
        self.engine.store.observe(self.full["id"], old["last_good_usage"], at=self.engine.clock() - 3600)
        last = self.switched()
        self.assertEqual((last["to"], last["kind"]), (self.roomy["id"], "out"))
        self.assert_consistent("claude_cli", self.identity(self.roomy["id"]))
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["refresh"]), ("none", [self.full["id"]]), "the better account is still asked for")

    def test_a_fable_limit_that_claude_names_twice_never_looks_like_it_is_filling(self):
        """Found by the second review of this change. An older build saved the limit from Claude's list beside
        the same limit from its named key, each with a number of its own. The first fix gave the list's entry
        the named key's id. Its samples then followed the key's, a limit that stood still at 80% read as one
        that went from 1% to 80% within minutes, the forecast passed the threshold and the terminals moved."""
        from datetime import datetime, timezone
        from accountdeck.usage import claude_usage
        # The forecast reads the readings stored since the terminals came to the account, so it reads the two
        # older ones below (Sonnet 2 of the review of the forecast change: for one commit it went by their
        # dates, left them out, and this test passed whatever the samples).
        now = self.engine.clock()
        ends = float(int(now) + 4 * 86400)
        for row in (self.full, self.roomy):
            self.read(row, weekly=20, fable=20)
        mark = self.engine.forecast_from["claude_cli"]
        self.assertIsNotNone(mark, "no forecast is made for the account in use: this test proves nothing")
        self.assertLessEqual(mark, self.engine.store.reading_mark())
        for age in (180, 60):          # two readings as that build saved them
            saved = support.shipped_both_names(now, 30.0, 1.0, 80.0, reset=ends)
            self.engine.store.observe(self.first["id"], dict(saved, checked=now - age), at=now - age)
        iso = datetime.fromtimestamp(ends, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        answer = {"seven_day": {"utilization": 30, "resets_at": iso}, "seven_day_fable": {"utilization": 1, "resets_at": iso},
                  "limits": [{"kind": "weekly_all", "percent": 30, "resets_at": iso},
                             {"kind": "weekly_scoped", "percent": 80, "resets_at": iso, "scope": {"model": {"display_name": "Fable 5.1"}}}]}
        self.engine.store.observe(self.first["id"], claude_usage(answer, now=now), at=now)      # the same answer, read now
        in_use = next(row for row in cli_auto.inputs(self.engine) if row["active"])
        self.assertEqual([(w["kind"], w["used"]) for w in in_use["windows"]], [("weekly", 30.0), ("model", 80.0)])
        self.assertEqual(in_use["forecast"], {}, "nothing is filling")
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["hold"], decision["reason"]), ("none", None, ""))

    def test_the_sheet_warns_before_a_switch_to_an_account_past_its_fable_threshold(self):
        """The same words reach the menu bar's alert, which lists a plan's warnings."""
        self.read(self.roomy, weekly=40, fable=96)
        self.engine.bump()
        self.assertEqual([warning["message"] for warning in self.plan("claude_cli", "switch", self.roomy["id"])["warnings"]],
                         ["This account is over your auto-switch threshold. Auto-switch keeps your choice for 2 hours, "
                          "unless the account's week runs out or it needs sign-in."])

    def test_a_manual_choice_is_held_only_while_the_chosen_account_is_in_use(self):
        """Found by the third review of this change. The 2 hours ran from the moment of the choice, whichever
        account was in use afterwards. Here the chosen account's week runs out, auto-switch moves the terminals
        to the one account that is ready, and the hold then kept them on it (an account the user never chose,
        with no Fable left) after the better account had been read again. Then the reverse: an Undo is a
        new choice, and it is held."""
        hold = "Your manual choice is held for 2 hours"
        self.act("claude_cli", "switch", self.full["id"])
        self.assert_consistent("claude_cli", self.identity(self.full["id"]))
        self.assertEqual(self.engine.store.setting("cli_history", {}).get("manual_for"), self.full["id"])
        self.read(self.full, weekly=96, fable=10)
        self.read(self.roomy, weekly=40, fable=100)
        self.read(self.first, weekly=20, fable=20)
        old = self.engine.store.account(self.first["id"])
        self.engine.store.observe(self.first["id"], old["last_good_usage"], at=self.engine.clock() - 3600)
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["hold"]), ("none", hold), "the choice is held against the threshold")
        self.read(self.full, weekly=100, fable=10)          # the chosen account's week runs out
        last = self.switched()
        self.assertEqual((last["to"], last["kind"]), (self.roomy["id"], "out"))
        self.read(self.first, weekly=20, fable=20)          # the better account has been read again
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["target"], decision["hold"]), ("switch", self.first["id"], None))
        last = self.switched()
        self.assertEqual((last["to"], last["kind"]), (self.first["id"], "out"))
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))
        plan = self.engine.action("cli-undo", {"provider": "claude_cli"})["plan"]
        self.assertEqual(plan["blockers"], [])
        self.engine.action("switch", {"id": self.roomy["id"], "provider": "claude_cli", "plan_id": plan["plan_id"]})
        self.assertTrue(self.engine.runner.wait(30))
        self.assert_consistent("claude_cli", self.identity(self.roomy["id"]))
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["hold"]), ("none", hold), "an Undo is the user's choice")

    def test_a_switch_the_app_died_after_does_not_pass_the_hold_on(self):
        """Found by the fourth review of this change. Auto-switch writes the login and then its history. When
        the app dies between the two, the history still shows the user's choice as the last thing that
        happened. The hold is for the account the history names, so the account now in use is not held."""
        from accountdeck import switch_cli
        self.act("claude_cli", "switch", self.full["id"])
        self.read(self.full, weekly=100, fable=10)
        self.read(self.roomy, weekly=40, fable=100)
        self.read(self.first, weekly=20, fable=20)
        cli_auto.note_try(self.engine, self.roomy["id"])        # an automatic switch notes its start first
        history = self.engine.store.setting("cli_history", {})
        switch_cli.perform(self.engine.cli, self.roomy["id"], leaving=self.identity(self.full["id"]))     # and the app dies here
        self.assertEqual(self.engine.store.setting("cli_history", {}), history)
        self.assert_consistent("claude_cli", self.identity(self.roomy["id"]))
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["target"], decision["hold"]), ("switch", self.first["id"], None))

    def test_an_undo_of_a_switch_made_for_the_fable_limit_stands(self):
        """The reverse. The account auto-switch left still works for the other models, so the user's Undo is
        not reversed at the next check. Without that rule the terminals were moved away again within seconds."""
        self.read(self.first, weekly=30, fable=100)
        self.read(self.roomy, weekly=40, fable=20)
        self.read(self.full, weekly=60, fable=20)
        self.assertEqual(self.switched()["kind"], "out")
        self.assert_consistent("claude_cli", self.identity(self.roomy["id"]))
        plan = self.engine.action("cli-undo", {"provider": "claude_cli"})["plan"]
        self.assertEqual(plan["blockers"], [])
        self.engine.action("switch", {"id": self.first["id"], "provider": "claude_cli", "plan_id": plan["plan_id"]})
        self.assertTrue(self.engine.runner.wait(30))
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))
        decision = cli_auto.decision(self.engine)
        self.assertEqual((decision["action"], decision["hold"]), ("none", "Your manual choice is held for 2 hours"))
        cli_auto.tick(self.engine)
        self.assertTrue(self.engine.runner.wait(30))
        self.assert_consistent("claude_cli", self.identity(self.first["id"]))


class TerminalAutoSwitchTest(DeckCase):
    def setUp(self):
        super().setUp()
        self.engine.sense_active()
        self.active_row = self.active("claude_cli")
        support.with_reading(self.engine, self.active_row["id"], weekly=97)
        for row in self.engine.store.accounts():
            if row["provider"] == "claude_cli" and row["id"] != self.active_row["id"] and row["last_good_usage"]:
                support.with_reading(self.engine, row["id"], weekly=20)

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
        """Act on 4, for a Codex and a Muse wait."""
        for provider in ("codex", "muse"):
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
        """The reverse of the state above: Undo is a manual terminal switch, and it must not wait for that wait."""
        self.start(Waiting("codex"))
        self.assertTrue(self.tick_until_switched())
        deadline = time.monotonic() + 5

        def ended():
            # The switch writes its history before it ends, and the app refuses an Undo until it has ended.
            return bool(self.engine.store.setting("cli_history", {}).get("last")
                        and self.engine.runner.free_for("claude_cli", beside_waits=True))
        while time.monotonic() < deadline and not ended():
            time.sleep(0.05)
        self.assertTrue(ended(), "the automatic switch had not ended, so an Undo would be refused")
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


class WaitDeadlineTest(DeckCase):
    """Act on 4: a wait for sessions ends when the confirmation it runs on does, for every client that waits."""

    def setUp(self):
        super().setUp()
        self.engine.timings = dataclasses.replace(self.engine.timings, consent_ttl=1.0)

    def assert_the_wait_ended(self, provider, outgoing):
        self.assertTrue(self.engine.runner.wait(15), "the wait had no time limit")
        report = self.engine.snapshot()["report"]
        self.assertEqual((report["outcome"], report["error"]["code"]), ("failed", "stale_plan"), report)
        self.assertIn("still open when your confirmation ran out", report["error"]["message"])
        self.assert_consistent(provider, outgoing)
        self.assertEqual(self.pending_records(), [])

    def test_a_codex_wait(self):
        side, main = self.add_codex_account(), self.active("codex")
        self.act("codex", "switch", side, consent="idle", wait=False)
        self.assert_the_wait_ended("codex", self.identity(main["id"]))

    def test_a_muse_wait(self):
        outgoing = self.active("muse")
        self.procs.add_muse("muse_session")
        self.act("muse", "switch", self.spare("muse")["id"], consent="idle", wait=False)
        self.assert_the_wait_ended("muse", self.identity(outgoing["id"]))


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


class CodexRenewalTest(DeckCase):
    """Consider 2: a login Codex renewed is saved whatever expiry its new token has. An expiry does not say
    which of two copies is the newer one; the copy the renewal started from does."""

    OLD = "synthetic-refresh-side"
    SAVED_FOR = 12 * 3600

    def setUp(self):
        super().setUp()
        self.codex = self.engine.providers["codex"]
        now = self.codex.clock()
        self.codex.clock = lambda: now          # the fake stamps a renewed token from this clock
        self.side = self.add_codex_account(now=now, lifetime=self.SAVED_FOR)
        self.main = self.active("codex")
        # the demo scripts usage answers by account, and the new account needs one for a renewal to succeed
        self.codex.timelines[self.identity(self.side)] = self.codex.timelines[self.identity(self.main["id"])]

    def renewed_token_lasts(self, seconds):
        return mock.patch.object(fake_module, "ROTATED_LIFETIME", seconds)

    def read_in_the_background(self):
        self.engine.accounts.read(self.side, active_identity={"codex": self.identity(self.main["id"])})

    def saved_refresh_token(self):
        return self.vault.get(self.side)["tokens"]["refresh_token"]

    def work_folders(self):
        return sorted(path.name for path in self.codex.workspace.glob("probe-*"))

    def test_a_background_renewal_with_the_same_expiry_is_saved(self):
        with self.renewed_token_lasts(self.SAVED_FOR):
            self.read_in_the_background()
        self.assertNotEqual(self.saved_refresh_token(), self.OLD, "the renewed login was thrown away")
        self.assertEqual(self.work_folders(), [])

    def test_a_background_renewal_with_an_earlier_expiry_is_saved(self):
        with self.renewed_token_lasts(3600):
            self.read_in_the_background()
        self.assertNotEqual(self.saved_refresh_token(), self.OLD, "the renewed login was thrown away")
        self.assertEqual(self.work_folders(), [])

    def test_a_renewal_right_before_a_switch_is_the_login_codex_gets(self):
        self.close_codex()
        with self.renewed_token_lasts(3600):
            report = self.act("codex", "switch", self.side)
        self.assertEqual(report["outcome"], "success", report)
        live = read_json(Path(self.paths.codex_home) / "auth.json")
        self.assertNotEqual(live["tokens"]["refresh_token"], self.OLD, "Codex was given the login the renewal used up")
        self.assertEqual(self.saved_refresh_token(), live["tokens"]["refresh_token"])
        self.assert_consistent("codex", self.identity(self.side))

    def renew_with_a_vault_that_answers(self, answer):
        """One renewal whose save the vault answers with `answer`; returns what was handed over for review."""
        kept = []
        profile = self.vault.get(self.side)
        try:
            reading = self.codex.usage(profile, mode="refresh", persist=lambda auth: answer,
                                       quarantine=lambda raw: kept.append(json.loads(raw)) or "quarantine-test")
        except DeckError as exc:
            reading = exc
        return reading, kept

    def test_a_renewed_login_the_vault_will_not_file_is_kept_for_review(self):
        reading, kept = self.renew_with_a_vault_that_answers("kept")
        self.assertEqual(len(kept), 1, "the renewed login was thrown away")
        self.assertNotEqual(kept[0]["tokens"]["refresh_token"], self.OLD)
        self.assertEqual(reading.meta.get("needs_review"), "quarantine-test")
        self.assertEqual(self.work_folders(), [])

    def test_a_renewed_login_for_a_copy_of_another_account_is_kept_encrypted_not_in_a_plain_folder(self):
        reading, kept = self.renew_with_a_vault_that_answers("conflict")
        self.assertIsInstance(reading, DeckError)
        self.assertEqual(reading.code, "identity")
        self.assertEqual(len(kept), 1, "the renewed login was not kept")
        self.assertEqual(self.work_folders(), [], "the renewed login was left in a plain-text folder")


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


class LeftoverFoldersTest(DeckCase):
    """Consider 3: one cleanup at start of the private folders helpers ran in, for every client that has them.
    A login found in one is never lost and never left in plain text, and a folder is left alone while its
    helper still runs."""

    # folder name prefix -> (client, where its login is inside the folder)
    KINDS = {"probe-": ("codex", "auth.json"), "login-": ("codex", "auth.json"),
             "muse-probe-": ("muse", "muse/auth.json"), "muse-login-": ("muse", "muse/auth.json")}

    def setUp(self):
        super().setUp()
        self.work = Path(self.paths.data_dir) / "work"
        self.now = self.engine.clock()

    def leave(self, prefix, login):
        """The folder a crash left: `login` (a document, or raw bytes) where that kind of helper keeps it."""
        folder = self.work / (prefix + "left")
        path = folder / self.KINDS[prefix][1]
        path.parent.mkdir(parents=True)
        path.write_bytes(login if isinstance(login, bytes) else json.dumps(login).encode())
        return folder

    def stranger(self, client):
        """A complete login of an account that is not saved here."""
        if client == "codex":
            return codex_login("nobody@example.test", now=self.now, refresh="synthetic-refresh-nobody")
        return muse_auth("nobody@example.test", now=self.now)

    def kept_for_review(self, key):
        return sorted(path.name for path in self.vault.root.glob(f"quarantine-{key}-*"))

    def all_kept_for_review(self):
        return sorted(path.name for path in self.vault.root.glob("quarantine-*"))

    def sweep(self):
        self.engine._sweep_work()

    # ---- Codex ----
    def renewed(self, saved, *, lifetime):
        login = json.loads(json.dumps(saved))
        login["tokens"]["refresh_token"] = "synthetic-refresh-renewed"
        login["tokens"]["access_token"] = unsigned_jwt(
            {**codex_module.jwt_claims(saved["tokens"]["access_token"]), "exp": int(self.now) + lifetime})
        return login

    def test_a_codex_login_renewed_before_the_crash_is_saved_under_its_account(self):
        for prefix in ("probe-", "login-"):
            with self.subTest(folder=prefix):
                side = self.add_codex_account(email=f"{prefix}side@example.test", now=self.now, lifetime=3600)
                folder = self.leave(prefix, self.renewed(self.vault.get(side), lifetime=10 * 86400))
                self.sweep()
                self.assertEqual(self.vault.get(side)["tokens"]["refresh_token"], "synthetic-refresh-renewed")
                self.assertFalse(folder.exists(), "the plain-text copy is still on disk")
                self.assertEqual(self.all_kept_for_review(), [])

    def test_a_codex_login_the_vault_cannot_call_newer_is_kept_for_review_under_its_account(self):
        side = self.add_codex_account(now=self.now, lifetime=3600)
        folder = self.leave("probe-", self.renewed(self.vault.get(side), lifetime=3600))
        self.sweep()
        self.assertEqual(self.vault.get(side)["tokens"]["refresh_token"], "synthetic-refresh-side")
        self.assertEqual(len(self.kept_for_review(side)), 1, "the renewed login was thrown away")
        self.assertFalse(folder.exists())

    def test_a_codex_copy_that_was_given_no_refresh_token_is_just_deleted(self):
        side = self.add_codex_account(now=self.now)
        blank = self.vault.get(side)
        blank["tokens"]["refresh_token"] = ""           # what a read-only usage check is handed
        folder = self.leave("probe-", blank)
        self.sweep()
        self.assertEqual(self.vault.get(side)["tokens"]["refresh_token"], "synthetic-refresh-side")
        self.assertEqual(self.all_kept_for_review(), [])
        self.assertFalse(folder.exists())

    # ---- Muse ----
    def test_a_muse_copy_equal_to_the_saved_one_is_just_deleted(self):
        spare = self.spare("muse")
        folder = self.leave("muse-probe-", self.vault.get(spare["id"]))
        self.sweep()
        self.assertEqual(self.all_kept_for_review(), [])
        self.assertFalse(folder.exists())

    def test_a_muse_login_that_differs_from_the_saved_one_is_kept_for_review_under_its_account(self):
        spare = self.spare("muse")
        saved = self.vault.get(spare["id"])
        other = json.loads(json.dumps(saved))
        other["providers"]["meta"]["access_token"] = "synthetic-muse-access-renewed"
        folder = self.leave("muse-probe-", other)
        self.sweep()
        self.assertEqual(self.vault.get(spare["id"]), saved, "a copy of unknown age replaced the saved login")
        self.assertEqual(len(self.kept_for_review(spare["id"])), 1, "the other login was thrown away")
        self.assertFalse(folder.exists())

    def test_a_muse_copy_whose_refresh_token_was_blanked_is_just_deleted(self):
        spare = self.spare("muse")
        blank = self.vault.get(spare["id"])
        blank["providers"]["meta"]["refresh_token"] = ""    # what a check gets when another AccountDeck owns the logins
        folder = self.leave("muse-probe-", blank)
        self.sweep()
        self.assertEqual(self.all_kept_for_review(), [])
        self.assertFalse(folder.exists())

    # ---- every kind of folder ----
    def test_a_login_of_no_saved_account_is_kept_for_review(self):
        for prefix, (client, _) in self.KINDS.items():
            with self.subTest(folder=prefix):
                before = self.kept_for_review(client + "-leftover")
                folder = self.leave(prefix, self.stranger(client))
                self.sweep()
                self.assertEqual(len(self.kept_for_review(client + "-leftover")), len(before) + 1)
                self.assertFalse(folder.exists(), "the plain-text copy is still on disk")

    def test_a_login_that_cannot_be_read_is_kept_for_review(self):
        for prefix, (client, _) in self.KINDS.items():
            with self.subTest(folder=prefix):
                before = self.kept_for_review(client + "-leftover")
                folder = self.leave(prefix, b"\x00 not json")
                self.sweep()
                self.assertEqual(len(self.kept_for_review(client + "-leftover")), len(before) + 1)
                self.assertFalse(folder.exists())

    def test_a_folder_is_left_alone_while_its_helper_still_runs_and_cleaned_once_it_is_gone(self):
        for prefix, (client, _) in self.KINDS.items():
            with self.subTest(folder=prefix):
                before = self.all_kept_for_review()
                folder = self.leave(prefix, self.stranger(client))
                helper = self.procs.spawn_helper([sys.executable, "-c", "import time; time.sleep(120)"], home=folder)
                self.addCleanup(helper.wait)
                self.addCleanup(helper.kill)
                self.sweep()
                self.assertTrue((folder / self.KINDS[prefix][1]).exists(), "the folder of a running helper was deleted")
                self.assertEqual(self.all_kept_for_review(), before)
                helper.kill()
                helper.wait()
                self.sweep()
                self.assertFalse(folder.exists())
                self.assertEqual(len(self.all_kept_for_review()), len(before) + 1)

    def test_a_leftover_copy_of_claudes_cookies_is_deleted(self):
        folder = self.work / "cookies-left"
        folder.mkdir(parents=True)
        (folder / "Cookies").write_bytes(b"synthetic")
        self.sweep()
        self.assertFalse(folder.exists())
        self.assertEqual(self.all_kept_for_review(), [])

    def test_anything_else_in_the_work_folder_is_left_alone(self):
        other = self.work / "something-else"
        other.mkdir(parents=True)
        (other / "auth.json").write_bytes(b"{}")
        self.sweep()
        self.assertTrue((other / "auth.json").exists())

    def test_a_login_that_could_not_be_stored_stays_where_it_is(self):
        folder = self.leave("probe-", self.stranger("codex"))
        with mock.patch.object(Vault, "quarantine", side_effect=DeckError("The vault is locked.", "vault")):
            self.sweep()
        self.assertTrue((folder / "auth.json").exists(), "the only copy of a login was deleted")


class CodexHelperFolderTest(unittest.TestCase):
    """Consider 3: the real Codex adapter, with the stand-in app-server, notes its helper in the private folder
    it gives it, so the cleanup at the next start can tell the folder is in use."""

    def test_a_usage_check_notes_its_helper_in_its_private_folder(self):
        root = Path(tempfile.mkdtemp(prefix="ad-switch-safety-"))
        self.addCleanup(shutil.rmtree, root, True)
        paths = Paths.synthetic(root)
        build_fake_home(paths, codex_identity=str(uuid.uuid4()))
        procs = FakeProcessController(paths)
        seen, original = [], procs.spawn_helper

        def spawn(args, *, home=None, **options):
            process = original(args, home=home, **options)
            seen.append((Path(home).parent, Path(home).name[:6], helper_running(home)))
            return process
        with mock.patch.object(procs, "spawn_helper", spawn):
            reading = codex_module.Codex(paths, procs).usage(read_json(Path(paths.codex_home) / "auth.json"),
                                                             mode="refresh", persist=lambda auth: "accepted")
        self.assertIsNotNone(reading.usage, reading.error)
        self.assertEqual(seen, [(Path(paths.data_dir) / "work", "probe-", True)])
        self.assertEqual(list((Path(paths.data_dir) / "work").glob("probe-*")), [])


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
