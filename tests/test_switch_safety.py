"""Everything that acts on a login, for all three clients: switching, waiting, recovery and the stored copies.
Each test runs a whole engine on the demo's fake data in safe mode; the real apps, their logins and the
Keychain are never touched."""
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

from accountdeck import cli_auto, switch_cli, switching
from accountdeck.config import Paths
from accountdeck.core import DeckError, FileTransaction, Vault, read_json, recover_transactions
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


class TerminalFableLimitTest(DeckCase):
    """Auto-switch and the Fable 5.1 weekly limit, through the real engine: who is signed in afterwards. The
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
        self.assertIn("reached 95% of its Fable 5.1 weekly limit", last["reason"])
        self.assert_consistent("claude_cli", self.identity(self.full["id"]))

    def test_it_never_moves_to_an_account_whose_fable_limit_is_out(self):
        # `full` has the most room in its week, so the rule that looks at the week alone picks it
        self.read(self.first, weekly=97, fable=10)
        self.read(self.full, weekly=20, fable=100)
        self.read(self.roomy, weekly=40, fable=20)
        self.assertEqual(self.switched()["kind"], "threshold")
        self.assert_consistent("claude_cli", self.identity(self.roomy["id"]))

    def test_an_account_whose_week_ran_out_is_left_for_one_whose_fable_limit_alone_is_out(self):
        """Found by the review of this change: the rule refused every account whose Fable 5.1 limit was out and
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
        self.assertRegex(last["reason"], r"reached its weekly limit; .+'s week can still be used, though its Fable 5\.1 weekly limit is at 100%\.$")
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
        change: the read schedule took a Fable 5.1 limit at 100% for an account with nothing left to learn."""
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
        now = self.engine.clock()
        ends = float(int(now) + 4 * 86400)
        for row in (self.full, self.roomy):
            self.read(row, weekly=20, fable=20)
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
        with no Fable 5.1 left) after the better account had been read again. Then the reverse: an Undo is a
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
