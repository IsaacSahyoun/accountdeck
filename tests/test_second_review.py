"""One test per finding of the second review (2026-11-12, of the refactor itself) that the orchestrator
proved by run. Each failed before its fix. Synthetic data only."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import types
import unittest
from unittest import mock

from tests import support, test_dashboard, test_switch_safety
from tests.test_one_writer import Beside, WaitThenWrite, until
from tests.test_switch_safety import Crash, DeckCase, Waiting

from accountdeck import cli_auto, switch_claude, switch_cli, switching
from accountdeck.core import MAX_PAUSE, DeckError, FileTransaction, Store
from accountdeck.operations import OperationRunner
from accountdeck.providers import claude as claude_provider


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
    """While another client's switch waits for its sessions (for hours, sometimes), the terminal auto-switch
    still acts. It must act on the account Claude Code uses now, not the one it used when the wait began."""

    def setUp(self):
        super().setUp()
        self.engine.update_settings({"cli_early_switch": False})      # only the threshold rule is under test
        self.engine.sense_active()
        self.was_active = self.active("claude_cli")
        support.with_reading(self.engine, self.was_active["id"], session=97)
        for row in self.engine.store.accounts():
            if row["provider"] == "claude_cli" and row["id"] != self.was_active["id"] and row["last_good_usage"]:
                support.with_reading(self.engine, row["id"], session=20, weekly=20)
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
        waiting = Waiting("claude")
        self.addCleanup(waiting.release.set)
        self.engine.submit(waiting)
        self.wait_for_phase("waiting_idle")
        # The user moves Claude Code to a healthy account by hand while Desktop waits.
        self.claude_code_signs_in_as(self.user_choice["id"])
        # The readings are the hand-made ones above for the whole test: no usage check replaces them.
        self.engine._schedule = self.engine._refresh_slow = lambda now: None
        for _ in range(3):
            self.engine.tick()
            time.sleep(0.2)
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


class ConfirmationStillHoldsAfterTheLaneWaitTest(unittest.TestCase):
    """A switch that waited for sessions may also have to wait for the terminal switch running beside it. If
    the user's confirmation runs out during that second wait, the switch stops before it changes anything."""

    def setUp(self):
        self.runner = OperationRunner()
        self.main, self.beside = WaitThenWrite(), Beside()
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


class DesktopLoginCase(DeckCase):
    """Claude Desktop closed, an account in use and a spare one, and a way to tell whose cookies are whose."""

    def setUp(self):
        super().setUp()
        self.outgoing, self.target = self.active("claude"), self.spare("claude")
        self.ctx = switching.context(self.engine)
        self.close_claude()
        # Whose cookies are whose: the saved copy of each account, and what Claude holds now for the one in use.
        self.own = {row["id"]: [self.session_cookie(self.vault.get(row["id"]))] for row in (self.outgoing, self.target)}
        self.own[self.outgoing["id"]].append(self.session_cookie(self.ctx.claude.capture()))
        for cookie in self.own[self.outgoing["id"]]:
            self.assertNotIn(cookie, self.own[self.target["id"]])

    def session_cookie(self, profile):
        """The sealed web sign-in cookie of a profile: each account's is its own."""
        rows = claude_provider._claude_rows(profile["cookies"], claude_provider.SESSION_COOKIES)
        return sorted(bytes(sealed) for _host, _name, _value, sealed in rows)

    def record(self, purpose):
        tx = FileTransaction(self.vault, self.engine.roots())
        tx.doc.update(provider="claude", purpose=purpose, outgoing_id=self.outgoing["id"] if purpose == "switch" else None)
        tx.save()
        return tx

    def write_login(self, account, record, dies_before=None):
        """Writes `account`'s saved login through `record`, as a switch or an undo does. The app dies just
        before write number `dies_before`, leaving the record open. Returns how many writes were made."""
        write, made = record.write, []

        def counted(path, data):
            if len(made) == dies_before:
                raise Crash()
            made.append(path)
            write(path, data)

        record.write = counted
        try:
            switch_claude.restore_profile(self.ctx, account["id"], record)
        except Crash:
            pass
        return len(made)

    def recover_once(self):
        self.pending_records()
        self.assertEqual(self.act("claude", "recover")["outcome"], "success")
        self.assertEqual(self.pending_records(), [], "a record still waits after one press of Recover")
        for row in (self.outgoing, self.target):
            self.assertTrue(self.session_cookie(self.vault.get(row["id"])) in self.own[row["id"]],
                            "a saved login now holds another account's cookies")
        live = self.ctx.claude.capture()
        self.assertEqual(live["identity"], self.identity(self.outgoing["id"]))
        self.assertTrue(self.session_cookie(live) in self.own[self.outgoing["id"]],
                        "Claude was left with one account's name and another account's cookies")



class HalfWrittenDesktopLoginTest(DesktopLoginCase):
    """A Claude Desktop login is several files: the cookies, and the settings file that names the account. A
    write that dies part way must never leave one account's name beside another account's cookies, because
    Recover saves the login it finds before it puts the previous one back."""

    def fresh_mac(self, journal):
        """A new fake Mac. With `journal`, Claude was killed mid-save before all this: its settings journal
        holds the newer copy of config.json, as on a real Mac after a hard stop."""
        self.setUp()
        if journal:
            config = Path(self.paths.claude_profile) / "config.json"
            body = json.dumps({**json.loads(config.read_text()), "userThemeMode": "dark"}).encode()
            head = f"{hashlib.sha256(body).hexdigest()} {hashlib.sha256(config.read_bytes()).hexdigest()} 1\n"
            (config.parent / claude_provider.JOURNAL).write_bytes(head.encode() + body)
            self.assertEqual(self.ctx.claude.capture()["journal"], {"present": True, "consistent": True, "used": True})

    def at_every_write(self, scene):
        """Runs `scene(n)` and then one Recover, on a fresh fake Mac for each write `n` a login write can die
        before. The last round makes every write and never closes the record."""
        for journal in (False, True):
            self.fresh_mac(journal)
            writes = self.write_login(self.target, self.record("switch"))
            self.assertGreater(writes, 1)
            for n in range(writes + 1):
                with self.subTest(journal=journal, died_before_write=n):
                    self.fresh_mac(journal)
                    scene(n)
                    self.recover_once()

    def test_a_switch_that_died_before_any_of_its_writes_is_put_right_by_one_recover(self):
        self.at_every_write(lambda n: self.write_login(self.target, self.record("switch"), dies_before=n))

    def test_a_recover_that_died_before_any_of_its_writes_is_put_right_by_the_next_one(self):
        def scene(n):
            self.write_login(self.target, self.record("switch"))                     # the switch wrote it all, then died
            self.write_login(self.outgoing, self.record("recover"), dies_before=n)   # so did the first Recover

        self.at_every_write(scene)


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


class DesktopRecoverThatFailsPartWayTest(DesktopLoginCase):
    """The same for Claude Desktop: a Recover whose own write stops part way leaves Claude signed out, and
    the record stays so the next press finishes the job."""

    def test_a_recover_whose_own_write_fails_part_way_can_be_pressed_again(self):
        self.write_login(self.target, self.record("switch"))       # the switch wrote it all, then the app died
        self.pending_records()
        write, failed = FileTransaction.write, []

        def the_disk_refuses_the_cookies_once(tx, path, data):
            if Path(path).name == "Cookies" and data is not None and not failed:
                failed.append(path)
                raise OSError(28, "No space left on device")
            return write(tx, path, data)

        with mock.patch.object(FileTransaction, "write", the_disk_refuses_the_cookies_once):
            self.assertNotEqual(self.act("claude", "recover")["outcome"], "success")
            self.assertTrue(failed)
            self.assertIsNone(self.ctx.claude.capture(config_only=True)["identity"], "a half-written login names an account")
            self.assertTrue(self.pending_records(), "Claude is signed out part way and no record is left to finish from")
            self.recover_once()


class DesktopCancelDuringTheLaneWaitTest(DeckCase):
    """A real Desktop switch that waited for its sessions also waits for the terminal switch running beside
    it. A Cancel pressed during that second wait is accepted, so Claude must not be closed after it."""

    def test_claude_stays_open_after_a_cancel_made_while_the_terminal_switch_finished(self):
        outgoing, target = self.active("claude"), self.spare("claude")
        busy = [s for s in self.engine.snapshot()["providers"]["claude"]["running"]["sessions"] if s["status"] == "busy"]
        self.assertTrue(busy)
        self.act("claude", "switch", target["id"], consent="idle", wait=False)
        self.wait_for_phase("waiting_idle")
        beside = Beside()
        self.addCleanup(beside.finish.set)
        self.engine.submit(beside)
        self.assertTrue(beside.running.wait(5))
        for session in busy:
            self.procs.set_session_status(session["pid"], "idle", age_s=60)
        time.sleep(1)                                   # the sessions are idle: only the terminal switch holds it now
        running = {row.pid for row in self.procs.claude_desktop()}
        self.assertTrue(running, "Claude was closed while the terminal switch was still writing")
        self.assertTrue(self.engine.runner.cancel())
        beside.finish.set()
        self.assertTrue(self.engine.runner.wait(30))
        self.assertEqual({row.pid for row in self.procs.claude_desktop()}, running, "Claude was closed after the user cancelled")
        self.assertEqual(self.signed_in("claude"), self.identity(outgoing["id"]))
        self.assertEqual(self.pending_records(), [])
        self.assertEqual(self.engine.snapshot()["report"]["outcome"], "cancelled")


class SetAsideRecordsAreKeptTest(TerminalRecoverCase):
    """The notice says a record that was set aside is kept for 30 days. That has to hold for a record that
    was already older than that when it was set aside."""

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


class UndoBesideAWaitTest(DeckCase, test_dashboard.DashboardTestCase):
    """The dashboard, clicked in headless Chrome against the real engine. An Undo confirmed while another
    client waits for its sessions runs beside that wait. The sheet must not turn into the progress view of
    the other client's job: its Cancel button would end that wait, not the Undo."""

    def test_the_sheet_does_not_offer_to_cancel_the_other_clients_wait(self):
        self.engine.update_settings({"cli_early_switch": False})
        self.engine.sense_active()
        first = self.active("claude_cli")
        support.with_reading(self.engine, first["id"], session=97)
        for row in self.engine.store.accounts():
            if row["provider"] == "claude_cli" and row["id"] != first["id"] and row["last_good_usage"]:
                support.with_reading(self.engine, row["id"], session=20, weekly=20)
        waiting = Waiting("codex")
        self.addCleanup(waiting.release.set)
        self.engine.submit(waiting)
        self.wait_for_phase("waiting_idle")
        cli_auto.tick(self.engine)                      # the automatic switch, made beside the Codex wait
        self.assertTrue(until(lambda: self.engine.store.setting("cli_history", {}).get("last")))
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
        self.browser.click(test_dashboard.button("Remove", test_dashboard.SHEET))
        self.browser.wait(f"!{test_dashboard.row(last)}", "the row gone")
        self.assertIsNone(self.engine.store.account(last["id"]))
        self.assertEqual(self.browser.problems, [])
