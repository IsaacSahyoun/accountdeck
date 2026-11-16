"""Regressions reproduced from the independent Ultra reset review."""
from unittest import mock
import copy

from accountdeck.claude_resets import ResetOperation
from accountdeck.core import DeckError
from accountdeck.providers.errors import ProviderError
from accountdeck.scheduler import RefreshScheduler, Target
from accountdeck.usage import claude_usage
from tests import test_claude_resets


class ResetReviewTest(test_claude_resets.UltraResetCase):
    restart = test_claude_resets.UltraResetTest.restart

    def test_another_live_confirmation_cannot_replay_an_uncertain_claim(self):
        first, second = self.check(), self.check()
        def lost(*args):
            self.sent.append(args)
            raise DeckError("No response", "timeout")
        self.engine.cli.adapter.redeem_reset = lost
        self.assertEqual(self.use(first)["verified"], "unconfirmed")
        self.assertEqual(self.use(second)["outcome"], "failed")
        self.assertEqual(len(self.sent), 1)

    def test_a_gone_grant_is_a_definite_refusal(self):
        self.engine.cli.adapter.redeem_reset = mock.Mock(side_effect=ProviderError("network", http=410))
        self.assertEqual(self.use(self.check())["verified"], "failed")
        self.assertFalse(self.check()["unconfirmed"])

    def test_hot_account_can_make_room_for_a_complete_reset(self):
        governor, now = self.engine.scheduler.governor, self.engine.clock()
        key = ("claude_cli", self.saved["identity"])
        for i in range(16, 0, -1):
            governor.record(key, now - i * 225 + 1)
        with self.assertRaises(DeckError) as wait:
            self.check()
        self.assertEqual(wait.exception.code, "rate_limited")
        self.assertGreater(wait.exception.retry_after, 0)
        self.assertGreater(governor.reserve(key, now + 300, 1, paced=True), 0)
        self.engine.clock.advance(wait.exception.retry_after + 1)
        self.assertEqual(self.use(self.check())["verified"], "verified")

    def test_explicit_client_refusals_do_not_block_future_resets(self):
        saved = self.vault.get(self.key)
        saved["credentials"]["claudeAiOauth"]["expiresAt"] = (self.engine.clock() + 86400) * 1000
        self.vault.put(self.key, saved)
        for code in (400, 401, 403, 404, 422, 429):
            with self.subTest(code=code):
                self.engine.clock.advance(3601)
                self.engine.cli.adapter.redeem_reset = mock.Mock(side_effect=ProviderError("network", http=code))
                report = self.use(self.check())
                self.assertNotEqual(report["verified"], "unconfirmed")
                self.engine.clock.advance(3601)
                self.assertFalse(self.check()["unconfirmed"])

    def test_budget_wait_frees_the_whole_request_batch(self):
        governor = self.engine.scheduler.governor
        for i in range(18):
            governor.record("synthetic", i * 10)
        wait = governor.reserve("synthetic", 200, 6)
        self.assertGreater(wait, 0)
        self.assertEqual(governor.reserve("synthetic", 200 + wait, 6), 0)

    def test_accepted_grant_can_disappear_from_the_inventory(self):
        def claim(*args):
            result = self.claim(*args)
            self.raw["grants"] = []
            return result
        self.engine.cli.adapter.redeem_reset = claim
        self.assertEqual(self.use(self.check())["verified"], "verified")
        self.restart()
        self.assertFalse(self.check()["unconfirmed"])

    def test_disappearing_grant_without_acceptance_stays_unconfirmed(self):
        def claim(*args):
            self.claim(*args)
            self.raw["grants"] = []
            raise DeckError("No reply", "timeout")
        self.engine.cli.adapter.redeem_reset = claim
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.assertTrue(self.check()["unconfirmed"])

    def usage_pair(self, before, after):
        self.engine.store.observe(self.key, before)
        self.engine.cli.adapter.usage = lambda _: copy.deepcopy(after if self.sent else before)

    def full_offer(self, clears):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=clears)

    def test_full_reset_keeps_unchanged_model_forecasts(self):
        self.full_offer(["seven_day"])
        before = claude_usage({"seven_day": {"utilization": 100}, "seven_day_opus": {"utilization": 50}})
        after = claude_usage({"seven_day": {"utilization": 0}, "seven_day_opus": {"utilization": 50}})
        self.usage_pair(before, after)
        count = len(self.engine.store.readings(self.key, "seven_day_opus"))
        self.assertEqual(self.use(self.check())["verified"], "verified")
        self.assertGreater(len(self.engine.store.readings(self.key, "seven_day_opus")), count)

    def test_fable_offer_only_restarts_the_limits_it_changes(self):
        self.raw["grants"][0]["clears"] = ["seven_day_overage_included"]
        before = claude_usage({"seven_day": {"utilization": 80}, "seven_day_fable": {"utilization": 60}})
        after = claude_usage({"seven_day": {"utilization": 30}})
        self.usage_pair(before, after)
        previous = self.engine.store.readings(self.key, "seven_day_fable")
        self.assertEqual(self.use(self.check())["verified"], "verified")
        self.assertEqual(self.engine.store.readings(self.key, "seven_day_fable"), previous)

    def test_new_model_spelling_after_a_reset_keeps_the_same_limit(self):
        self.raw["grants"][0]["clears"].append("seven_day_fable")
        before = claude_usage({"seven_day": {"utilization": 80}, "seven_day_fable": {"utilization": 60}})
        after = claude_usage({"seven_day": {"utilization": 30}, "limits": [
            {"kind": "weekly_scoped", "percent": 0, "scope": {"model": {"display_name": "Fable 5.1"}}}]})
        self.usage_pair(before, after)
        self.assertEqual(self.use(self.check())["verified"], "verified")

    def test_no_readable_baseline_never_spends_a_reset(self):
        self.engine.cli.adapter.usage = lambda _: {"windows": []}
        self.assertEqual(self.use(self.check())["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_reported_optional_zero_confirms(self):
        self.full_offer(["seven_day", "seven_day_oauth_apps"])
        before = claude_usage({"seven_day": {"utilization": 100}, "seven_day_oauth_apps": {"utilization": 50}})
        after = claude_usage({"seven_day": {"utilization": 0}, "seven_day_oauth_apps": {"utilization": 0}})
        self.usage_pair(before, after)
        self.assertEqual(self.use(self.check())["verified"], "verified")
        self.assertNotIn("seven_day_oauth_apps", [w["id"] for w in after["windows"]])

    def test_missing_optional_value_stays_unconfirmed_until_reported(self):
        self.full_offer(["seven_day", "seven_day_oauth_apps"])
        before = claude_usage({"seven_day": {"utilization": 100}, "seven_day_oauth_apps": {"utilization": 50}})
        after = claude_usage({"seven_day": {"utilization": 0}})
        self.usage_pair(before, after)
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.engine.clock.advance(3601)
        after.update(claude_usage({"seven_day": {"utilization": 0}, "seven_day_oauth_apps": {"utilization": 0}}))
        self.assertFalse(self.check()["unconfirmed"])
        self.assertEqual(len(self.sent), 1)

    def test_accepted_reset_with_stale_exhausted_usage_stays_unconfirmed(self):
        self.full_offer(["seven_day"])
        before = claude_usage({"seven_day": {"utilization": 100}})
        self.usage_pair(before, before)
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.engine.clock.advance(3601)
        self.assertTrue(self.check()["unconfirmed"])
        self.assertEqual(len(self.sent), 1)

    def test_reconciliation_keeps_readings_from_the_first_settlement(self):
        self.full_offer(["seven_day"])
        before = claude_usage({"seven_day": {"utilization": 100}})
        after = claude_usage({"seven_day": {"utilization": 0}})
        self.usage_pair(before, after)
        save = self.engine.resets._save
        def fail_settled(row, journal):
            if journal["attempt"]["state"] == "settled":
                raise OSError("Synthetic settlement failure")
            return save(row, journal)
        with mock.patch.object(self.engine.resets, "_save", side_effect=fail_settled):
            self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.restart()
        self.engine.store.observe(self.key, claude_usage({"seven_day": {"utilization": 7}}))
        self.assertFalse(self.check()["unconfirmed"])
        self.assertIn(7, [r["used"] for r in self.engine.store.readings(self.key, "seven_day")])

    def test_background_read_cannot_overrun_a_manual_reservation(self):
        reads = []
        scheduler = RefreshScheduler(reads.append, inline=True)
        target = Target("account", "claude_cli", "identity")
        key = (target.provider, target.key)
        for _ in range(18):
            scheduler.governor.record(key, 1)
        due = scheduler.due
        def reserve_after_due(*args):
            result = due(*args)
            self.assertEqual(result, [target])
            self.assertEqual(scheduler.governor.reserve(key, 1000, 2), 0)
            return result
        with mock.patch.object(scheduler, "due", side_effect=reserve_after_due):
            self.assertEqual(scheduler.tick([target], 1000, lambda: 1000), [])
        self.assertEqual(reads, [])

    def test_a_read_rechecks_a_pause_written_while_waiting_for_the_account(self):
        import contextlib
        acquire = self.engine.scheduler.account_lock
        @contextlib.contextmanager
        def paused(key):
            with acquire(key):
                self.engine.store.update(key, paused_until=self.engine.clock() + 900)
                yield
        with mock.patch.object(self.engine.scheduler, "account_lock", side_effect=paused), \
                mock.patch.object(self.engine.cli.adapter, "usage", wraps=self.engine.cli.adapter.usage) as usage:
            self.assertEqual(self.engine.cli.read(self.key), "rate_limited")
        usage.assert_not_called()

    def test_cancel_at_the_send_boundary_spends_nothing(self):
        original, cancelled = ResetOperation._step, []
        def step(op, name):
            result = original(op, name)
            if name == "send" and not cancelled:
                cancelled.append(self.engine.runner.cancel(op.id))
            return result
        with mock.patch.object(ResetOperation, "_step", step):
            report = self.use(self.check())
        self.assertEqual(cancelled, [True])
        self.assertEqual(report["outcome"], "cancelled")
        self.assertEqual(self.sent, [])

    def test_unreadable_credentials_do_not_break_the_dashboard(self):
        original = self.vault.get
        def get(key, *args):
            if key == self.key:
                raise PermissionError("Synthetic unreadable file")
            return original(key, *args)
        with mock.patch.object(self.vault, "get", side_effect=get):
            self.engine.bump()
            state = self.engine.snapshot()
        row = next(row for row in state["accounts"] if row["id"] == self.key)
        self.assertFalse(row["can_reset"])
        self.assertTrue(any(row["provider"] == "codex" for row in state["accounts"]))

    def test_disk_failure_after_acceptance_is_unconfirmed(self):
        save, calls = self.engine.resets._save, []
        def fail_second(*args):
            calls.append(True)
            if len(calls) == 2:
                raise OSError("Synthetic disk full")
            return save(*args)
        with mock.patch.object(self.engine.resets, "_save", side_effect=fail_second):
            report = self.use(self.check())
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(report["verified"], "unconfirmed")
        self.assertIn("check", report["verify_reason"].lower())

    def test_expiry_during_intent_write_spends_nothing(self):
        save, calls = self.engine.resets._save, []
        def slow(*args):
            result = save(*args)
            calls.append(True)
            if len(calls) == 1:
                self.engine.clock.advance(121)
            return result
        with mock.patch.object(self.engine.resets, "_save", side_effect=slow):
            report = self.use(self.check())
        self.assertEqual(report["outcome"], "failed")
        self.assertEqual(self.sent, [])
        self.assertFalse(self.check()["unconfirmed"])
