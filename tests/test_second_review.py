"""One test per finding of the second review (2026-11-12, of the refactor itself) that the orchestrator
proved by run. Each failed before its fix. Synthetic data only."""
import json
from pathlib import Path
import time

from tests import support
from tests.test_switch_safety import DeckCase, Waiting

from accountdeck import cli_auto
from accountdeck.core import FileTransaction


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
