"""One test per bug the 2026-11-12 review proved (the "Act on" list). Each failed on the shipped code and
defines what "fixed" means. Synthetic data only. The orchestrator owns this file: builders make these pass
by changing the app, never by changing the test."""
import copy
import json
from pathlib import Path
import socket
import threading
import time
import unittest

from tests import support

from accountdeck import cli_auto, cli_policy, fixtures, switch_cli, switching
from accountdeck.core import DeckError

NOW = 1_800_000_000.0
SETTINGS = {"cli_autoswitch": True, "cli_threshold": 95, "cli_order": "soonest_reset", "cli_early_switch": True,
            "pinned_model": ""}


class TerminalSwitchSafetyTest(unittest.TestCase):
    """Act on 1. Whatever happens during a terminal switch, the account named in .claude.json and the
    account the login token belongs to are the same one, AccountDeck reports that one, and nothing is left
    that blocks the next switch after one press of Recover."""

    def setUp(self):
        self.engine = support.demo_engine(self, "terminal")
        self.service = self.engine.cli
        self.rows = support.terminal_rows(self.engine)
        self.identity = {a["id"]: self.service.row(a["id"])["identity"] for a in self.rows}
        usable = [a for a in self.rows if not a["active"] and a.get("health", "ok") == "ok" and not a.get("excluded")]
        self.target, self.third = usable[0], usable[1]

    def saved_refresh_token(self, key):
        return self.service.vault.get(key)["credentials"]["claudeAiOauth"]["refreshToken"]

    def switch(self, key):
        try:
            switch_cli.perform(self.service, key)
            return None
        except DeckError as exc:
            return exc

    def assert_one_account_everywhere(self, expected_identity=None):
        disk = support.terminal_on_disk(self.engine)
        self.assertEqual(disk["name"], disk["token"], "the account name and the login token belong to different accounts")
        if expected_identity is not None:
            self.assertEqual(disk["name"], expected_identity)
        self.engine.sense_active()
        active = [a["id"] for a in support.terminal_rows(self.engine) if a["active"]]
        self.assertEqual([self.identity[key] for key in active], [disk["name"]], "AccountDeck reports a different account")
        for key, identity in self.identity.items():      # no saved login is filed under another account's name
            saved = self.service.vault.get(key)["credentials"]
            if (saved.get("claudeAiOauth") or {}).get("accessToken"):
                self.assertEqual(self.service.adapter.resolve_account(saved)["accountUuid"], identity)

    def assert_recover_clears_everything(self, then_switch_to):
        self.engine.recovery = self.engine._scan_recovery()
        if self.engine.recovery:
            switching.dispatch(self.engine, "recover", {})
            self.engine.recovery = self.engine._scan_recovery()
        self.assertEqual(self.engine.recovery, [], "a record still waits after one press of Recover")
        plan = switch_cli.plan(self.engine, then_switch_to)
        self.assertEqual([b["message"] for b in plan["blockers"]], [])

    def test_a_renewal_by_claude_code_during_the_switch_is_not_a_failure(self):
        fired = support.once_the_login_is(self.engine, self.saved_refresh_token(self.target["id"]),
                                          lambda: support.claude_code_renews(self.engine))
        error = self.switch(self.target["id"])
        self.assertTrue(fired, "the renewal never happened inside the switch, so this run proved nothing")
        self.assertIsNone(error, f"the switch was reported as failed: {error}")
        self.assert_one_account_everywhere(self.identity[self.target["id"]])
        self.assert_recover_clears_everything(then_switch_to=self.third["id"])
        # The renewed login is the only one that still works, so it is the one kept for this account.
        self.service.mirror()
        self.assertTrue(self.saved_refresh_token(self.target["id"]).endswith("-renewed"))

    def test_a_sign_in_as_someone_else_during_the_switch_never_blocks_switching_for_good(self):
        other = self.service.vault.get(self.third["id"])
        paths = self.service.paths

        def claude_code_signs_in_as_third():
            Path(paths.claude_cli_credentials).write_text(json.dumps(other["credentials"]))
            config = json.loads(Path(paths.claude_user_config).read_text())
            config["oauthAccount"] = other.get("oauthAccount") or {"accountUuid": other["identity"]}
            Path(paths.claude_user_config).write_text(json.dumps(config))

        fired = support.once_the_login_is(self.engine, self.saved_refresh_token(self.target["id"]),
                                          claude_code_signs_in_as_third)
        self.switch(self.target["id"])     # success or failure are both honest answers here
        self.assertTrue(fired, "the sign-in never happened inside the switch, so this run proved nothing")
        self.engine.recovery = self.engine._scan_recovery()
        if self.engine.recovery:
            switching.dispatch(self.engine, "recover", {})
        self.assert_one_account_everywhere(self.identity[self.third["id"]])
        self.assert_recover_clears_everything(then_switch_to=self.target["id"])


class AutoSwitchWhileAnotherClientWaitsTest(unittest.TestCase):
    """Act on 4. A Desktop, Codex or Muse switch can wait up to 30 minutes for a session to close. The terminal
    auto-switch keeps working during that wait."""

    def test_the_terminal_auto_switch_acts_during_another_clients_wait(self):
        engine = support.demo_engine(self, "terminal")
        engine.sense_active()

        def in_use():
            identity = engine.cli.adapter.current()["identity"]
            return next(r for r in engine.store.accounts() if r["provider"] == "claude_cli" and r["identity"] == identity)

        active = in_use()
        support.with_reading(engine, active["id"], session=97)
        for row in engine.store.accounts():
            if row["provider"] == "claude_cli" and row["id"] != active["id"] and row["last_good_usage"]:
                support.with_reading(engine, row["id"], session=20, weekly=20)
        self.assertEqual(cli_auto.decision(engine)["action"], "switch")
        waiting = support.WaitingOperation("codex")
        self.addCleanup(waiting.release.set)
        engine.submit(waiting)
        deadline = time.monotonic() + 5
        while waiting.phase != "waiting_idle" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(waiting.phase, "waiting_idle")
        for _ in range(3):
            cli_auto.tick(engine)
            time.sleep(0.3)
        self.assertNotEqual(in_use()["id"], active["id"], "the terminal stayed on the account at 97% while Codex waited")
        self.assertEqual(waiting.phase, "waiting_idle", "the other client's wait was disturbed")
        waiting.release.set()
        self.assertTrue(waiting.wait(10))


def reading(key, label, five_used, five_reset, weekly_used, *, active=False, out=False):
    return {"id": key, "label": label, "active": active, "forecast": {}, "owner": "v2", "excluded": False,
            "health": "ok", "live_expired": False, "last_good_at": NOW - 60, "out": out,
            "windows": [{"id": "five_hour", "kind": "session", "used": five_used, "reset": five_reset, "started": True},
                        {"id": "seven_day", "kind": "weekly", "used": weekly_used, "reset": NOW + 3 * 86400}]}


class AutoSwitchPolicyTest(unittest.TestCase):
    """Act on 2 and 3, on the pure decision rule `cli_policy.evaluate(accounts, settings, history, now)`."""
    BETA = reading("beta", "Beta", 20, NOW + 4 * 3600, 30)

    def decide(self, active, history=None, now=NOW):
        return cli_policy.evaluate([active, copy.deepcopy(self.BETA)], SETTINGS, history or {}, now)

    def test_stays_when_the_five_hour_limit_refills_within_ten_minutes(self):
        self.assertEqual(self.decide(reading("alpha", "Alpha", 96, NOW + 240, 40, active=True))["action"], "none")

    def test_stays_when_the_five_hour_limit_has_just_refilled(self):
        # The reading is from before the refill: its 96% describes a window that has ended.
        for ago in (1, 40, 300):
            with self.subTest(refilled_seconds_ago=ago):
                result = self.decide(reading("alpha", "Alpha", 96, NOW - ago, 40, active=True))
                self.assertEqual(result["action"], "none", result["reason"])

    def test_still_leaves_for_a_weekly_limit_when_the_five_hour_limit_has_refilled(self):
        result = self.decide(reading("alpha", "Alpha", 96, NOW - 40, 96, active=True))
        self.assertEqual((result["action"], result["target"]), ("switch", "beta"))

    def undone(self, seconds_ago):
        """Auto-switch left Alpha for Beta; the user pressed Undo `seconds_ago`, so Alpha is in use again."""
        at = NOW - seconds_ago
        return {"left": "beta", "left_at": at, "manual_at": at,
                "last": {"from": "alpha", "to": "beta", "at": at - 55, "kind": "threshold", "reason": "", "undo_until": at}}

    def test_an_undo_is_not_reversed_at_the_next_check(self):
        over = reading("alpha", "Alpha", 30, NOW + 3600, 96, active=True)
        for seconds_ago in (5, 600, 7000):
            with self.subTest(undone_seconds_ago=seconds_ago):
                result = self.decide(over, self.undone(seconds_ago))
                self.assertEqual(result["action"], "none", result["reason"])
                self.assertTrue(result["hold"], "the dashboard needs a line saying why auto-switch is waiting")

    def test_the_users_choice_is_held_for_two_hours_then_the_threshold_applies_again(self):
        over = reading("alpha", "Alpha", 30, NOW + 3600, 96, active=True)
        self.assertEqual(self.decide(over, self.undone(2 * 3600 + 5))["action"], "switch")

    def test_the_users_choice_does_not_strand_them_on_an_account_that_ran_out(self):
        out = reading("alpha", "Alpha", 30, NOW + 3600, 100, active=True, out=True)
        result = self.decide(out, self.undone(5))
        self.assertEqual((result["action"], result["target"]), ("switch", "beta"))


class TerminalAccountUpkeepTest(unittest.TestCase):
    """Act on 7 and 9."""

    def setUp(self):
        self.engine = support.demo_engine(self, "terminal")

    def test_a_refill_is_recorded_for_a_terminal_account(self):
        engine = self.engine
        now = engine.clock()
        row = next(r for r in engine.store.accounts()
                   if r["provider"] == "claude_cli" and r["identity"] == engine.active_identity["claude_cli"])
        before = copy.deepcopy(row["last_good_usage"])
        next(w for w in before["windows"] if w.get("kind") == "session").update(used=92.0, reset=now - 120)
        after = copy.deepcopy(before)
        next(w for w in after["windows"] if w.get("kind") == "session").update(used=0.0, reset=now + 5 * 3600)
        before["checked"], after["checked"] = now - 600, now
        engine.store.observe(row["id"], before, at=now - 600)
        had = len(engine.store.refills(0))
        engine.cli.adapter.usage = lambda credentials: copy.deepcopy(after)
        engine.cli.read(row["id"])
        self.assertEqual(len(engine.store.refills(0)) - had, 1)

    def test_a_saved_terminal_account_can_be_removed_while_claude_code_is_signed_out(self):
        engine = self.engine
        rows = support.terminal_rows(engine)
        victim = next(a for a in rows if not a["active"])
        Path(engine.cli.paths.claude_cli_credentials).unlink()      # what signing out of Claude Code leaves
        engine.action("remove", {"id": victim["id"]})
        engine.runner.wait(30)
        self.assertNotIn(victim["id"], [a["id"] for a in support.terminal_rows(engine)])
        self.assertEqual(len(support.terminal_rows(engine)), len(rows) - 1)


class DashboardLoadTest(unittest.TestCase):
    """Act on 5. A browser opens many connections at once to fetch the dashboard's files; none is dropped."""

    def test_every_file_arrives_when_fetched_in_parallel(self):
        server = support.serve(self, fixtures.example_state("typical"))
        web = Path(switch_cli.__file__).parent / "web"
        paths = ["/", "/icon.svg"] + ["/static/" + str(p.relative_to(web)) for p in sorted(web.rglob("*"))
                                      if p.is_file() and p.name not in ("index.html", "icon.svg")]
        failures, lock = [], threading.Lock()

        def fetch(path):
            try:
                with socket.create_connection(("127.0.0.1", server.server_port), timeout=10) as conn:
                    conn.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n\r\n".encode())
                    data = b""
                    while chunk := conn.recv(65536):
                        data += chunk
                if not data.startswith(b"HTTP/1.0 200") and not data.startswith(b"HTTP/1.1 200"):
                    raise OSError(data[:40].decode("latin-1") or "empty reply")
            except OSError as exc:
                with lock:
                    failures.append(f"{path}: {exc}")

        for _ in range(12):
            for start in range(0, len(paths), 16):
                threads = [threading.Thread(target=fetch, args=(path,)) for path in paths[start:start + 16]]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
        self.assertEqual(failures, [])


class DashboardRowsTest(unittest.TestCase):
    """Act on 6 and 8, on the real dashboard files drawn by headless Chrome."""

    def test_a_terminal_account_with_an_old_reading_says_so(self):
        real = fixtures.terminal_accounts

        def aged(scenario):
            rows = real(scenario)
            next(row for row in rows if row["label"] == "Beta")["checked"] = -3 * 3600
            return rows

        fixtures.terminal_accounts = aged
        self.addCleanup(setattr, fixtures, "terminal_accounts", real)
        state = fixtures.example_state("typical", now=time.time())
        beta = next(a for a in state["accounts"] if a["id"] == "cli_beta")
        self.assertEqual(beta["headline"]["kind"], "stale")
        page, errors = support.render(self, state)
        self.assertEqual(errors, [])
        row = support.element(page, "li", 'id="row-cli_beta"')
        self.assertTrue(row, "the terminal row for Beta was not drawn")
        self.assertIn(beta["headline"]["text"], support.text_of(row))

    def test_every_saved_account_of_every_client_offers_rename_and_remove(self):
        state = fixtures.example_state("typical", now=time.time())
        page, errors = support.render(self, state)
        self.assertEqual(errors, [])
        providers = {a["provider"] for a in state["accounts"]}
        self.assertEqual(providers, {"claude_cli", "claude", "codex", "muse"})
        for account in state["accounts"]:
            with self.subTest(provider=account["provider"], account=account["label"]):
                row = support.element(page, "li", f'data-id="{account["id"]}"')
                self.assertTrue(row, "no row carries this account's id")
                self.assertIn(f'aria-label="More actions for {account["label"]}"', row)


if __name__ == "__main__":
    unittest.main()
