"""One test per finding of the second review (2026-11-12, of the refactor itself) that the orchestrator
proved by run. Each failed before its fix. Synthetic data only."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time
import types
import unittest

from tests import support
from tests.test_one_writer import Beside, WaitThenWrite, until
from tests.test_switch_safety import Crash, DeckCase, Waiting

from accountdeck import cli_auto, switch_claude, switching
from accountdeck.core import MAX_PAUSE, FileTransaction, Store
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


class HalfWrittenDesktopLoginTest(DeckCase):
    """A Claude Desktop login is several files: the cookies, and the settings file that names the account. A
    write that dies part way must never leave one account's name beside another account's cookies, because
    Recover saves the login it finds before it puts the previous one back."""

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
