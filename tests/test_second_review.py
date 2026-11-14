"""One test per finding of the second review (2026-11-12, of the refactor itself) that the orchestrator
proved by run. Each failed before its fix. Synthetic data only."""
import dataclasses
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from tests import support, test_dashboard, test_switch_safety
from tests.test_one_writer import Beside, until
from tests.test_switch_safety import DeckCase, Waiting

from accountdeck import cli_auto, recovery, switch_cli, switching
from accountdeck.core import MAX_PAUSE, DeckError, FileTransaction, Store
from accountdeck.operations import Operation, OperationRunner


class UnreadableLoginIsKeptTest(DeckCase):
    """Recover puts the previous login back over whatever the client holds. A login file it cannot read may
    hold the only copy of a renewed login, so it is never replaced unless its bytes were kept first."""

    def interrupted_switch(self, provider, path, marker):
        """The app died right after writing the target's login; the client then renewed that login, and the
        file ended up unreadable. `marker` stands for the renewed token: it exists nowhere else."""
        outgoing, target = self.active(provider), self.spare(provider)
        tx = FileTransaction(self.vault, self.engine.roots())
        tx.doc.update(provider=provider, purpose="switch", outgoing=self.identity(outgoing["id"]),
                      target=self.identity(target["id"]))
        self.engine.providers[provider].restore(self.vault.get(target["id"]), tx)
        renewed = json.loads(path.read_text())
        renewed["renewed_by_the_client"] = marker
        path.write_text(json.dumps(renewed))
        path.chmod(0)
        self.addCleanup(lambda: path.exists() and path.chmod(0o600))
        self.assertEqual(len(self.pending_records()), 1)      # what the next start of the app finds

    def kept_somewhere(self, path, marker):
        if path.exists():
            path.chmod(0o600)
            if marker in path.read_text():
                return True                       # the file was left as it was
        for record in self.vault.root.glob("quarantine-*"):
            if marker.encode() in self.vault.protect(record.read_bytes(), decrypt=True):
                return True                       # its bytes are kept, encrypted
        return False

    def test_recover_keeps_a_muse_login_it_could_not_read(self):
        path = Path(self.paths.muse_config) / "auth.json"
        self.interrupted_switch("muse", path, "only-copy-of-the-renewed-muse-login")
        self.act("muse", "recover")
        self.assertTrue(self.kept_somewhere(path, "only-copy-of-the-renewed-muse-login"),
                        "Recover replaced a login file it could not read and kept no copy of it")
        self.assertEqual(self.pending_records(), [])

    def left_as_it_is(self, cannot_keep):
        """Third review: under the patch `cannot_keep(path)` the bytes cannot be kept, so Recover stops
        before the file and sets the record aside."""
        path = Path(self.paths.muse_config) / "auth.json"
        self.interrupted_switch("muse", path, "only-copy-of-the-renewed-muse-login")
        key = self.pending_records()[0]
        with cannot_keep(path):
            report = self.act("muse", "recover")
        path.chmod(0o600)
        self.assertTrue("only-copy-of-the-renewed-muse-login" in path.read_text(),
                        "Recover replaced a login file it could not read and kept no copy of it")
        self.assertEqual(self.pending_records(), [])
        self.assertTrue(self.vault.path("set-aside-" + key).exists(), "the record was not kept")
        self.assertIn("set aside", report["summary"])

    def test_a_login_the_vault_cannot_take_is_left_as_it_is(self):
        full = OSError(28, "No space left on device")
        self.left_as_it_is(lambda path: mock.patch.object(self.vault, "quarantine", side_effect=full))

    def test_a_login_that_cannot_be_made_readable_is_left_as_it_is(self):
        chmod = Path.chmod

        def refused(target, mode, **options):
            if mode == 0o600 and target.name == "auth.json":
                raise PermissionError(1, "Operation not permitted")
            return chmod(target, mode, **options)

        self.left_as_it_is(lambda path: mock.patch.object(Path, "chmod", refused))

    def test_recover_keeps_a_codex_login_it_could_not_read(self):
        self.add_codex_account()
        self.close_codex()
        path = Path(self.paths.codex_home) / "auth.json"
        self.interrupted_switch("codex", path, "only-copy-of-the-renewed-codex-login")
        self.act("codex", "recover")
        self.assertTrue(self.kept_somewhere(path, "only-copy-of-the-renewed-codex-login"),
                        "Recover replaced a login file it could not read and kept no copy of it")
        self.assertEqual(self.pending_records(), [])


class AutoSwitchKnowsWhoIsSignedInTest(DeckCase):
    """While another client's switch waits for its sessions (up to 30 minutes), the terminal auto-switch
    still acts. It must act on the account Claude Code uses now, not the one it used when the wait began."""

    def setUp(self):
        super().setUp()
        self.engine.update_settings({"cli_early_switch": False})      # only the threshold rule is under test
        self.engine.sense_active()
        self.was_active = self.active("claude_cli")
        support.with_reading(self.engine, self.was_active["id"], weekly=97)
        for row in self.engine.store.accounts():
            if row["provider"] == "claude_cli" and row["id"] != self.was_active["id"] and row["last_good_usage"]:
                support.with_reading(self.engine, row["id"], weekly=20)
        # The account the rule would move to, and another healthy one the user picks by hand instead.
        self.rule_target = cli_auto.decision(self.engine)["target"]
        self.user_choice = next(a for a in self.rows("claude_cli") if not a["active"] and a["id"] != self.rule_target
                                and a.get("health", "ok") == "ok" and not a.get("excluded"))

    def claude_code_signs_in_as(self, key):
        saved = self.vault.get(key)
        Path(self.paths.claude_cli_credentials).write_text(json.dumps(saved["credentials"]))
        config = json.loads(Path(self.paths.claude_user_config).read_text())
        config["oauthAccount"] = saved["oauthAccount"]
        Path(self.paths.claude_user_config).write_text(json.dumps(config))

    def test_a_sign_in_made_during_another_clients_wait_is_seen(self):
        waiting = Waiting("codex")
        self.addCleanup(waiting.release.set)
        self.engine.submit(waiting)
        self.wait_for_phase("waiting_idle")
        # The user moves Claude Code to a healthy account by hand while Codex waits.
        self.claude_code_signs_in_as(self.user_choice["id"])
        # The readings are the hand-made ones above for the whole test: no usage check replaces them.
        self.engine._schedule = self.engine._refresh_slow = lambda now: None
        submit, submitted = self.engine.submit, []
        self.engine.submit = lambda op: submitted.append(op) or submit(op)
        for _ in range(3):
            self.engine.tick()
            time.sleep(0.2)
        # Seen, not only survived: the app knows the new account, so it never starts a switch off the old one.
        self.assertEqual(self.engine.active_identity["claude_cli"], self.identity(self.user_choice["id"]))
        self.assertEqual(submitted, [], "auto-switch started a switch away from an account no longer in use")
        waiting.release.set()
        self.assertTrue(self.engine.runner.wait(30))
        self.assertEqual(support.terminal_on_disk(self.engine)["name"], self.identity(self.user_choice["id"]),
                         "auto-switch moved the terminals off the healthy account the user had just chosen")

    def test_an_automatic_switch_is_dropped_when_the_account_it_was_leaving_is_no_longer_in_use(self):
        decide, decisions = cli_auto.decision, []

        def decide_then_the_user_signs_in(engine, settings=None):
            result = decide(engine, settings)
            if not decisions:        # right after the decision, before the switch it asks for
                self.claude_code_signs_in_as(self.user_choice["id"])
            decisions.append(result)
            return result

        cli_auto.decision = decide_then_the_user_signs_in
        self.addCleanup(setattr, cli_auto, "decision", decide)
        cli_auto.tick(self.engine)
        self.assertEqual(decisions[0]["action"], "switch")
        self.assertTrue(self.engine.runner.wait(30))
        self.assertEqual(support.terminal_on_disk(self.engine)["name"], self.identity(self.user_choice["id"]),
                         "an automatic switch went ahead although the account it meant to leave was not in use")


def watch_the_lane_wait(runner):
    """Two events about the job that has to wait for the one running beside it: that wait began, and it is
    over. For a test that must act while the job is held there. Call it before the job is submitted."""
    leave, began, over = runner._leave_wait, threading.Event(), threading.Event()

    def watched(op):
        began.set()
        waited = leave(op)
        if waited:
            over.set()
        return waited

    runner._leave_wait = watched
    return began, over


class WaitThenStop(Operation):
    """A switch that leaves its wait as the Codex one does: it names its next phase from inside the wait
    step. No new step begins, so only the change of phase itself can stop it."""

    def __init__(self):
        super().__init__("switch", provider="codex", label="stand-in", consent="idle",
                         steps=(("wait", "Waiting for sessions"),))
        self.sessions_closed, self.writing, self.finish = threading.Event(), threading.Event(), threading.Event()

    def run(self):
        with self.step("wait", phase="waiting_idle", message="Waiting for sessions to close…"):
            self.sessions_closed.wait(10)
            self.set_phase("stopping", "Closing…", cancellable=False)
            self.writing.set()
            self.finish.wait(10)


class ConfirmationStillHoldsAfterTheLaneWaitTest(unittest.TestCase):
    """A switch that waited for sessions may also have to wait for the terminal switch running beside it. If
    the user's confirmation runs out during that second wait, the switch stops before it changes anything."""

    def setUp(self):
        self.runner = OperationRunner()
        self.held, _over = watch_the_lane_wait(self.runner)
        self.main, self.beside = WaitThenStop(), Beside()
        for op in (self.main, self.beside):
            self.addCleanup(op.finish.set)
        self.addCleanup(self.main.sessions_closed.set)

    def start(self, expires_in):
        self.main.token = types.SimpleNamespace(issued=time.time() - 600, expires=time.time() + expires_in, pairs=())
        self.runner.submit(self.main)
        self.assertTrue(until(lambda: self.main.phase == "waiting_idle"))
        self.runner.submit(self.beside)
        self.assertTrue(self.beside.running.wait(5))
        self.main.sessions_closed.set()               # sessions closed in time, but the terminal switch is mid-way
        self.assertTrue(self.held.wait(5))

    def test_it_stops_when_the_confirmation_ran_out_while_it_waited_for_the_other_job(self):
        self.start(expires_in=0.3)
        time.sleep(0.5)                               # the confirmation runs out during the second wait
        self.beside.finish.set()
        self.assertFalse(self.main.writing.wait(1), "it wrote on a confirmation that had run out")
        self.assertTrue(self.main.wait(5))
        self.assertEqual(self.runner.report()["error"]["code"], "stale_plan")

    def test_it_goes_on_when_the_confirmation_still_holds(self):
        self.start(expires_in=600)
        self.beside.finish.set()
        self.assertTrue(self.main.writing.wait(5))
        self.main.finish.set()
        self.assertTrue(self.runner.wait(5))
        self.assertEqual(self.runner.report()["outcome"], "success")

    def test_a_cancel_made_during_the_second_wait_is_honoured(self):
        self.start(expires_in=600)
        self.assertTrue(self.runner.cancel())
        self.beside.finish.set()
        self.assertTrue(self.main.wait(5))
        self.assertFalse(self.main.writing.is_set(), "it wrote after the user cancelled")


class OldPausesAreCappedTest(unittest.TestCase):
    """The live version could store a pause of any length when a provider asked for one (years, once). The
    one-hour cap also has to reach a pause that is already saved."""

    def test_a_pause_saved_by_the_old_version_is_cut_to_the_cap_when_the_data_is_opened(self):
        root = Path(tempfile.mkdtemp(prefix="ad-second-review-"))
        self.addCleanup(shutil.rmtree, root, True)
        now = 1_800_000_000.0
        store = Store(root, clock=lambda: now)
        years = store.save_account("codex", "identity-years", "Paused for years")
        minutes = store.save_account("codex", "identity-minutes", "Paused for ten minutes")
        never = store.save_account("codex", "identity-never", "Not paused")
        store.update(years, paused_until=now + 3 * 365 * 86400)
        store.update(minutes, paused_until=now + 600)
        store.close()
        store = Store(root, clock=lambda: now)          # the next start of the app
        self.addCleanup(store.close)
        self.assertEqual(store.account(years)["paused_until"], now + MAX_PAUSE)
        self.assertEqual(store.account(minutes)["paused_until"], now + 600)
        self.assertIsNone(store.account(never)["paused_until"])


class TerminalRecoverCase(DeckCase):
    """A terminal switch that died part way, with the builders' own helpers for it."""

    STEPS = test_switch_safety.TerminalInterruptedSwitchTest.STEPS
    die = test_switch_safety.TerminalInterruptedSwitchTest.die
    press_recover_once = test_switch_safety.TerminalInterruptedSwitchTest.press_recover_once
    assert_settled = test_switch_safety.TerminalInterruptedSwitchTest.assert_settled

    def setUp(self):
        super().setUp()
        self.service = self.engine.cli
        self.outgoing, self.target = self.active("claude_cli"), self.spare("claude_cli")


class TerminalRecordStaysWhileTheLoginIsMixedTest(TerminalRecoverCase):
    """Recover sets a record aside when it cannot settle it. For Claude Code that is only safe once the
    account name and the login token belong to one account: while they do not, the record is the one thing
    another press of Recover can repair them from."""

    def mixed(self):
        disk = support.terminal_on_disk(self.engine)
        return disk["name"] != disk["token"]

    def test_a_recover_whose_own_write_fails_part_way_can_be_pressed_again(self):
        self.die("after the account name is written")              # the terminals are on the target, whole
        refuse = OSError(28, "No space left on device")
        with mock.patch.object(switch_cli, "write_account", side_effect=refuse):     # the token goes back, the name cannot
            with self.assertRaises(DeckError):
                self.press_recover_once()
        self.assertTrue(self.mixed())
        self.assertTrue(self.pending_records(), "the login is mixed and no record is left to repair it from")
        self.press_recover_once()                                  # the disk has room again
        self.assert_settled(self.outgoing)

    def test_a_recover_that_cannot_read_the_login_keeps_the_record(self):
        self.die("after the login is written")                     # the outgoing name beside the target's token
        self.assertTrue(self.mixed())
        store = self.service.adapter.store
        with mock.patch.object(store, "read", side_effect=DeckError("The keychain did not answer.", "vault")):
            with self.assertRaises(DeckError):
                self.press_recover_once()
        self.assertTrue(self.pending_records(), "the login is mixed and no record is left to repair it from")
        self.press_recover_once()                                  # the keychain answers again
        self.assert_settled(self.outgoing)

    def test_the_record_stays_when_the_check_of_the_login_itself_breaks(self):
        """Third review: the provider answers with something that is not an account at all."""
        self.die("after the login is written")                     # the outgoing name beside the target's token
        support.claude_code_renews(self.engine)                    # so only the provider can say whose it is
        broken = AttributeError("'NoneType' object has no attribute 'get'")
        with mock.patch.object(self.service.adapter, "resolve_account", side_effect=broken):
            with self.assertRaises(DeckError):
                self.press_recover_once()
        self.assertTrue(self.pending_records(), "the login is mixed and no record is left to repair it from")
        self.press_recover_once()
        self.assert_settled()

    def test_a_terminal_record_that_stays_does_not_keep_recover_from_the_other_clients(self):
        """Third review: Recover goes to one client per press, terminals first. A terminal record that has
        to stay must not be the one every press goes to while another client's record waits."""
        self.add_codex_account()
        self.close_codex()
        codex_before, codex_target = self.active("codex"), self.spare("codex")
        record = FileTransaction(self.vault, self.engine.roots())
        record.doc.update(provider="codex", purpose="switch", outgoing=self.identity(codex_before["id"]),
                          target=self.identity(codex_target["id"]))
        self.engine.providers["codex"].restore(self.vault.get(codex_target["id"]), record)   # a Codex switch died
        self.die("after the login is written")                                                # and a terminal one
        self.assertEqual(len(self.pending_records()), 2)

        def press():
            plan = self.engine.action("switch-plan", {"purpose": "recover"})["plan"]
            self.engine.action("recover", {"plan_id": plan["plan_id"], "consent": "now", "provider": plan["provider"]})
            self.assertTrue(self.engine.runner.wait(30))
            return plan["provider"]

        store = self.service.adapter.store
        with mock.patch.object(store, "read", side_effect=DeckError("The keychain did not answer.", "vault")):
            with self.assertRaises(DeckError):
                press()                                            # the terminal record cannot be settled yet
            self.assertEqual(press(), "codex", "the next press went to the stuck terminal record again")
            self.assertEqual(self.signed_in("codex"), self.identity(codex_before["id"]))
            self.assertEqual(len(self.pending_records()), 1)
            with self.assertRaises(DeckError):
                press()                                            # only the terminal record is left, still stuck
        self.assertEqual(press(), "claude_cli")
        self.assert_settled(self.outgoing)

    def test_records_of_two_clients_that_both_stay_take_turns(self):
        """Neither can be settled yet. Each press goes to the client that was tried longest ago."""
        self.die("after the login is written")                     # a terminal switch died
        record = FileTransaction(self.vault, self.engine.roots())
        record.doc.update(provider="codex", purpose="switch")       # and a Codex one
        record.save()
        self.assertEqual(len(self.pending_records()), 2)

        def stays(tx):
            raise recovery.StillNeeded(OSError(28, "No space left on device"), "The login is not whole yet.")

        went = []
        for _ in range(4):
            went.append(switching._recovery_provider(self.engine))
            with self.assertRaises(recovery.StillNeeded):
                recovery.press(self.engine, went[-1], stays)
        self.assertEqual(went, ["claude_cli", "codex", "claude_cli", "codex"])
        recovery.press(self.engine, "codex", lambda tx: tx.close() or "Done.")       # the Codex one settles
        self.assertEqual(switching._recovery_provider(self.engine), "claude_cli")
        self.assertEqual(self.engine.recovery_left, ["claude_cli"])

    def test_a_record_is_still_set_aside_when_the_login_is_whole(self):
        self.die("after the account name is written")              # the terminals are on the target, whole
        key = self.pending_records()[0]
        with mock.patch.object(switch_cli, "_keep", side_effect=DeckError("The saved copy could not be written.", "vault")):
            self.press_recover_once()
        self.assertTrue(self.vault.path("set-aside-" + key).exists())
        self.assert_settled(self.target)


class RecoverAsksTheProviderAgainTest(TerminalRecoverCase):
    """When nobody can say whose login Claude Code holds, Recover puts the previous account back and files
    the live login where no account uses it. A failed lookup is remembered for a minute, and a press inside
    that minute must still ask: the provider may be back, and the live login may be the only renewed copy."""

    def test_a_press_made_right_after_a_failed_lookup_still_asks(self):
        self.die("after the account name is written")              # the terminals are on the target
        support.claude_code_renews(self.engine)                    # a login the vault has not seen
        live = self.service.adapter.store.read()
        answer = self.service.adapter.resolve_account
        self.service.adapter.resolve_account = mock.Mock(side_effect=DeckError("offline", "network"))
        self.assertIsNone(self.service.owner(live))                # as when the switch failed a moment ago
        self.service.adapter.resolve_account = answer              # the provider is back
        self.press_recover_once()
        self.assert_settled(self.outgoing)
        self.assertEqual(list(self.vault.root.glob("quarantine-cli-recover-*")), [],
                         "the renewed login was filed where no account uses it, without asking whose it is")
        saved = self.vault.get(self.target["id"])["credentials"]["claudeAiOauth"]["refreshToken"]
        self.assertEqual(saved, live["claudeAiOauth"]["refreshToken"], "the target's renewed login was not saved")


class CodexCancelDuringTheLaneWaitTest(DeckCase):
    """A real Codex switch that waited for its sessions also waits for the terminal switch running beside
    it. A Cancel pressed during that second wait is accepted, so nothing of Codex is stopped or written
    after it. A session opened during that wait stops the switch too."""

    def start_a_switch_when_idle_with_a_terminal_switch_beside_it(self, daemon=True):
        self.outgoing, self.target = self.active("codex"), self.add_codex_account()
        kinds = self.procs.classify(self.procs.snapshot())
        if not daemon:
            for row in kinds["codex_daemon"] + kinds["codex_daemon_updater"]:
                self.procs.exit_process(row.pid)
        self.assertEqual(self.procs.codex_daemon() is not None, daemon)     # the daemon is what the switch stops
        self.sessions = self.procs.codex_consumers()
        self.assertTrue(self.sessions)
        self.held, self.released = watch_the_lane_wait(self.engine.runner)
        self.act("codex", "switch", self.target, consent="idle", wait=False)
        self.wait_for_phase("waiting_idle")
        self.beside = Beside()
        self.addCleanup(self.beside.finish.set)
        self.engine.submit(self.beside)
        self.assertTrue(self.beside.running.wait(5))
        for row in self.sessions:                       # the sessions close while the terminal switch is mid-way
            self.procs.exit_process(row.pid)
        self.assertTrue(self.held.wait(10))             # no session is open: only the terminal switch holds it now

    def let_the_terminal_switch_finish(self):
        """Ends the job beside the switch and waits for both. The test acted while that job held the switch only
        if the job was still holding: one that gave up waiting by itself proves nothing."""
        self.beside.finish.set()
        self.assertTrue(self.engine.runner.wait(30))
        self.assertTrue(self.beside.let_go_by_the_test, "the terminal switch gave up waiting before the test let it go")

    def assert_nothing_of_codex_changed(self):
        self.assertNotIn("codex_daemon_stop", [call[0] for call in self.procs.calls])
        self.assertEqual(self.signed_in("codex"), self.identity(self.outgoing["id"]))
        self.assertEqual(self.pending_records(), [])

    def test_codex_is_left_alone_after_a_cancel_made_while_the_terminal_switch_finished(self):
        self.start_a_switch_when_idle_with_a_terminal_switch_beside_it()
        self.assert_nothing_of_codex_changed()          # nothing yet: the terminal switch is still writing
        self.assertTrue(self.engine.runner.cancel())
        self.let_the_terminal_switch_finish()
        self.assert_nothing_of_codex_changed()
        self.assertEqual(self.engine.snapshot()["report"]["outcome"], "cancelled")

    def test_a_session_opened_during_the_second_wait_stops_the_switch_before_it_writes(self):
        self.start_a_switch_when_idle_with_a_terminal_switch_beside_it()
        self.procs.add_codex("codex_tui")               # the user opens Codex again
        self.let_the_terminal_switch_finish()
        self.assertTrue(self.released.is_set())
        report = self.engine.snapshot()["report"]
        self.assertEqual((report["outcome"], (report["error"] or {}).get("code")), ("failed", "busy"), report)
        self.assert_nothing_of_codex_changed()          # the daemon the new session talks to is still running

    def test_a_daemon_started_during_the_second_wait_is_not_stopped(self):
        """The user confirmed a plan with no daemon in it. One that starts later is not the switch's to stop."""
        self.start_a_switch_when_idle_with_a_terminal_switch_beside_it(daemon=False)
        self.procs.add_codex("codex_daemon")
        self.let_the_terminal_switch_finish()
        self.assertTrue(self.released.is_set())
        report = self.engine.snapshot()["report"]
        self.assertEqual((report["outcome"], (report["error"] or {}).get("code")), ("failed", "busy"), report)
        self.assert_nothing_of_codex_changed()


class SetAsideRecordsAreKeptTest(TerminalRecoverCase):
    """The notice says a record that was set aside is kept for 30 days. That has to hold for a record that
    was already older than that when it was set aside."""

    def test_a_record_is_dated_before_it_gets_its_set_aside_name(self):
        """A crash or another look between the two steps must not find an old record under its new name."""
        key = "transaction-" + "1" * 32
        self.vault.path(key).write_bytes(b"not a vault record")
        long_ago = time.time() - 45 * 86400
        os.utime(self.vault.path(key), (long_ago, long_ago))
        rename, ages = os.replace, []

        def renamed(source, target):
            ages.append(time.time() - os.stat(source).st_mtime)
            return rename(source, target)

        with mock.patch.object(os, "replace", renamed):
            self.vault.set_aside(key)
        self.assertLess(ages[0], 60, "the record got its set-aside name while it still looked 45 days old")

    def test_an_old_record_is_not_deleted_in_the_press_that_sets_it_aside(self):
        key = "transaction-" + "0" * 32
        self.vault.path(key).write_bytes(b"not a vault record")
        long_ago = time.time() - 45 * 86400
        os.utime(self.vault.path(key), (long_ago, long_ago))
        self.press_recover_once()
        self.assertEqual(self.pending_records(), [])          # this look also clears out what is too old to keep
        self.assertTrue(self.vault.path("set-aside-" + key).exists(), "the record was deleted, not kept")


class RecordStatusMatchesTheDiskTest(DeckCase):
    """A record that could not be saved as finished is not finished: the undo that follows a failed switch
    goes by the status in memory, so that status must be the one on disk."""

    def test_a_close_that_cannot_be_saved_leaves_the_record_open(self):
        tx = FileTransaction(self.vault, self.engine.roots())
        tx.save()
        with mock.patch.object(self.vault, "put", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                tx.commit()
        self.assertEqual(tx.doc["status"], "prepared")
        self.assertEqual(self.vault.get(tx.key)["status"], "prepared")

    def test_a_change_that_cannot_be_saved_is_not_in_the_record(self):
        """Third review: Recover goes by the record's list of changes to tell whether a write began."""
        tx = FileTransaction(self.vault, self.engine.roots())
        tx.save()
        path = Path(self.paths.codex_home) / "auth.json"
        before = path.read_bytes()
        with mock.patch.object(self.vault, "put", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                tx.write(path, b"{}")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(tx.doc["changes"]), 0, "the record lists a change that was never made")


class CodexRecoverWaitsForUsageChecksTest(DeckCase):
    """A Codex usage check can renew a login and save it. Recover reads the saved login and then writes it to
    Codex, so no check may run between the two: Recover takes the same locks a Codex switch takes."""

    def test_recover_waits_for_a_usage_check_and_runs_once_it_is_over(self):
        self.add_codex_account()
        self.close_codex()
        outgoing, target = self.active("codex"), self.spare("codex")
        tx = FileTransaction(self.vault, self.engine.roots())
        tx.doc.update(provider="codex", purpose="switch", outgoing=self.identity(outgoing["id"]),
                      target=self.identity(target["id"]))
        self.engine.providers["codex"].restore(self.vault.get(target["id"]), tx)       # the app died here
        self.pending_records()
        self.engine.timings = dataclasses.replace(self.engine.timings, lock_wait=0.2)
        check = self.engine.scheduler.account_lock(outgoing["id"])
        self.assertTrue(check.acquire(timeout=1))             # a usage check of the outgoing account is running
        try:
            report = self.act("codex", "recover")
            self.assertNotEqual(report["outcome"], "success", "Recover ran while a usage check held the account")
            self.assertEqual(report["error"]["code"], "busy")
            self.assertEqual(len(self.pending_records()), 1)
            self.assertEqual(self.signed_in("codex"), self.identity(target["id"]), "Recover wrote during a usage check")
        finally:
            check.release()
        self.assertEqual(self.act("codex", "recover")["outcome"], "success")
        self.assertEqual(self.pending_records(), [])
        self.assert_consistent("codex", self.identity(outgoing["id"]))


class WaitThenClose(Operation):
    """Another client's switch: it waits for its sessions, then starts to close the client. Both steps can
    still be cancelled."""

    def __init__(self):
        super().__init__("switch", provider="codex", label="stand-in", consent="idle",
                         steps=(("wait", "Waiting for sessions"), ("stop", "Closing Codex")))
        self.sessions_closed, self.finish = threading.Event(), threading.Event()

    def run(self):
        with self.step("wait", phase="waiting_idle", message="Waiting for sessions to close…"):
            self.sessions_closed.wait(60)
        with self.step("stop", phase="stopping", message="Closing Codex…"):
            self.finish.wait(60)


class UndoBesideAWaitTest(DeckCase, test_dashboard.DashboardTestCase):
    """The dashboard, clicked in headless Chrome against the real engine. An Undo confirmed while another
    client waits for its sessions runs beside that wait. The sheet must not turn into the progress view of
    the other client's job: its Cancel button would end that wait, not the Undo."""

    def the_automatic_switch_is_over(self):
        """The switch writes its history before it ends, and the app refuses an Undo until it has ended."""
        self.assertTrue(until(lambda: self.engine.store.setting("cli_history", {}).get("last")))
        self.assertTrue(until(lambda: self.engine.runner.free_for("claude_cli", beside_waits=True)))

    def test_the_sheet_does_not_offer_to_cancel_the_other_clients_wait(self):
        self.engine.update_settings({"cli_early_switch": False})
        self.engine.sense_active()
        first = self.active("claude_cli")
        support.with_reading(self.engine, first["id"], weekly=97)
        for row in self.engine.store.accounts():
            if row["provider"] == "claude_cli" and row["id"] != first["id"] and row["last_good_usage"]:
                support.with_reading(self.engine, row["id"], weekly=20)
        waiting = Waiting("codex")
        self.addCleanup(waiting.release.set)
        self.engine.submit(waiting)
        self.wait_for_phase("waiting_idle")
        cli_auto.tick(self.engine)                      # the automatic switch, made beside the Codex wait
        self.the_automatic_switch_is_over()
        self.browser.open(test_dashboard.serve(self, self.engine))
        self.browser.click("document.querySelector('[data-cli-undo]')", "the Undo button")
        self.browser.click(f"{test_dashboard.SHEET}.open && {test_dashboard.SHEET}.querySelector('[data-choice=now]')",
                           "the button that confirms the Undo")
        self.assertTrue(until(lambda: support.terminal_on_disk(self.engine)["name"] == self.identity(first["id"])),
                        "the Undo did not run")
        time.sleep(0.5)
        offered = self.browser.js(f"{test_dashboard.SHEET}.open ? [...{test_dashboard.SHEET}.querySelectorAll('button')]"
                                  ".map((b) => b.dataset.choice || b.textContent.trim()) : []")
        self.assertNotIn("cancel", offered, "the sheet offers to cancel the Codex wait as if it were the Undo")
        self.assertEqual(self.engine.snapshot()["pending"]["id"], waiting.id)
        self.assertEqual(self.browser.problems, [])


    def test_nor_when_the_other_job_leaves_its_wait_while_the_undo_is_confirmed(self):
        """Third review: the page cannot go by the last state it saw. By the time the Undo's answer is back,
        the other job may have left its wait, and it is the app that knows the Undo ran beside it."""
        self.engine.update_settings({"cli_early_switch": False})
        self.engine.sense_active()
        first = self.active("claude_cli")
        support.with_reading(self.engine, first["id"], weekly=97)
        for row in self.engine.store.accounts():
            if row["provider"] == "claude_cli" and row["id"] != first["id"] and row["last_good_usage"]:
                support.with_reading(self.engine, row["id"], weekly=20)
        other = WaitThenClose()
        for event in (other.sessions_closed, other.finish):
            self.addCleanup(event.set)
        self.engine.submit(other)
        self.wait_for_phase("waiting_idle")
        cli_auto.tick(self.engine)
        self.the_automatic_switch_is_over()
        self.browser.open(test_dashboard.serve(self, self.engine))
        # The page gets the Undo's answer only once it has seen the other job leave its wait.
        self.browser.js("""(() => { const post = AD.api.action; AD.api.action = async (name, body) => {
            const answer = await post(name, body);
            if (name === 'switch') await new Promise((go) => { window.seenTheOtherJobMove = go; });
            return answer; }; })()""")
        self.browser.click("document.querySelector('[data-cli-undo]')", "the Undo button")
        self.browser.click(f"{test_dashboard.SHEET}.open && {test_dashboard.SHEET}.querySelector('[data-choice=now]')",
                           "the button that confirms the Undo")
        self.assertTrue(until(lambda: support.terminal_on_disk(self.engine)["name"] == self.identity(first["id"])),
                        "the Undo did not run")
        other.sessions_closed.set()
        self.browser.wait("AD.app.state.pending && AD.app.state.pending.phase === 'stopping' && !!window.seenTheOtherJobMove",
                          "the other job closing its client")
        self.browser.js("window.seenTheOtherJobMove()")
        time.sleep(0.5)
        offered = self.browser.js(f"{test_dashboard.SHEET}.open ? [...{test_dashboard.SHEET}.querySelectorAll('button')]"
                                  ".map((b) => b.dataset.choice || b.textContent.trim()) : []")
        self.assertNotIn("cancel", offered, "the sheet offers to cancel the Codex switch as if it were the Undo")
        self.assertEqual(self.engine.snapshot()["pending"]["id"], other.id)


class RemoveTheSignedOutAccountTest(DeckCase, test_dashboard.DashboardTestCase):
    """Nobody is signed in to Claude Code, so no saved account is in use and the app lets any of them go. The
    dashboard has to offer that for the account Claude Code used last too, which it still draws first."""

    def test_the_account_claude_code_used_last_can_be_removed_from_its_menu_once_signed_out(self):
        Path(self.paths.claude_cli_credentials).unlink()          # the user signed out of Claude Code
        self.engine.sense_active()
        last = self.active("claude_cli")
        self.assertEqual(last["state"], "needs_signin")
        self.browser.open(test_dashboard.serve(self, self.engine))
        self.browser.click(test_dashboard.more(last))
        self.browser.wait(f"{test_dashboard.MENU}.matches(':popover-open')", "the More menu")
        remove = test_dashboard.button("Remove…", test_dashboard.MENU)
        self.assertIsNone(self.browser.js(f"{remove}.getAttribute('aria-disabled')"), "Remove is switched off")
        self.browser.click(remove)
        said = self.browser.wait(f"{test_dashboard.SHEET}.open && {test_dashboard.SHEET}.textContent", "the question")
        self.assertNotIn("stays signed in", said, "the sheet says Claude Code stays signed in, and it is signed out")
        self.browser.click(test_dashboard.button("Remove", test_dashboard.SHEET))
        self.browser.wait(f"!{test_dashboard.row(last)}", "the row gone")
        self.assertIsNone(self.engine.store.account(last["id"]))
        self.assertEqual(self.browser.problems, [])
