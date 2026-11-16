"""Additional reset cases from the independent second review."""
import copy
from unittest import mock

from accountdeck.core import DeckError
from accountdeck.usage import claude_usage
from tests import test_claude_resets


class ResetSonnetTest(test_claude_resets.UltraResetCase):
    def test_busy_rejection_preserves_the_unsubmitted_confirmation(self):
        key = ("claude_cli", self.saved["identity"])
        for _ in range(10):
            self.engine.scheduler.governor.record(key, self.engine.clock())
        plan = self.check()
        with mock.patch.object(self.engine, "submit", side_effect=DeckError("Synthetic busy job", "busy")):
            with self.assertRaises(DeckError) as error:
                self.engine.action("reset-use", {"id": self.key, "plan_id": plan["plan_id"]})
        self.assertEqual(error.exception.code, "busy")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.use(plan)["verified"], "verified")
        self.assertEqual(len(self.sent), 1)

    def test_busy_rejection_does_not_extend_confirmation_expiry(self):
        plan = self.check()
        def expired(op):
            self.engine.clock.advance(121)
            raise DeckError("Synthetic busy job", "busy")
        with mock.patch.object(self.engine, "submit", side_effect=expired), self.assertRaises(DeckError):
            self.engine.action("reset-use", {"id": self.key, "plan_id": plan["plan_id"]})
        with self.assertRaises(DeckError) as error:
            self.use(plan)
        self.assertEqual(error.exception.code, "stale_plan")
        self.assertEqual(self.sent, [])

    def test_busy_retry_still_rechecks_the_offer(self):
        plan = self.check()
        with mock.patch.object(self.engine, "submit", side_effect=DeckError("Synthetic busy job", "busy")), self.assertRaises(DeckError):
            self.engine.action("reset-use", {"id": self.key, "plan_id": plan["plan_id"]})
        self.raw["grants"][0]["resets_left"] = 2
        self.assertEqual(self.use(plan)["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def pair(self, before, after):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "seven_day_oauth_apps"])
        self.engine.store.observe(self.key, before)
        self.engine.cli.adapter.usage = lambda _: copy.deepcopy(after if self.sent else before)

    def test_previously_empty_limit_can_grow_after_an_accepted_reset(self):
        self.pair(claude_usage({"seven_day": {"utilization": 100}, "seven_day_oauth_apps": {"utilization": 0}}),
                  claude_usage({"seven_day": {"utilization": 20}, "seven_day_oauth_apps": {"utilization": 0.3}}))
        self.assertEqual(self.use(self.check())["verified"], "verified")

    def test_growth_without_any_observed_drop_stays_unconfirmed(self):
        self.pair(claude_usage({"seven_day": {"utilization": 0}, "seven_day_oauth_apps": {"utilization": 0}}),
                  claude_usage({"seven_day": {"utilization": 0.3}, "seven_day_oauth_apps": {"utilization": 0.3}}))
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")

    def test_a_lost_acceptance_keeps_the_strict_zero_test(self):
        self.pair(claude_usage({"seven_day": {"utilization": 100}, "seven_day_oauth_apps": {"utilization": 0}}),
                  claude_usage({"seven_day": {"utilization": 20}, "seven_day_oauth_apps": {"utilization": 0.3}}))
        def lost(*args):
            self.claim(*args)
            raise DeckError("Synthetic lost acceptance", "timeout")
        self.engine.cli.adapter.redeem_reset = lost
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.assertTrue(self.check()["unconfirmed"])
        self.assertEqual(len(self.sent), 1)

    def test_a_positive_limit_without_observed_drop_stays_unconfirmed(self):
        self.pair(claude_usage({"seven_day": {"utilization": 5}, "seven_day_oauth_apps": {"utilization": 20}}),
                  claude_usage({"seven_day": {"utilization": 12}, "seven_day_oauth_apps": {"utilization": 0}}))
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")

    def test_an_unmapped_model_cannot_silently_lose_its_verification(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "seven_day_opus"])
        before = claude_usage({"seven_day": {"utilization": 100}, "limits": [
            {"kind": "weekly_scoped", "percent": 50, "scope": {"model": {"display_name": "Opus 4.1"}}}]})
        self.engine.store.observe(self.key, before)
        self.engine.cli.adapter.usage = lambda _: before
        self.assertIsNone(self.check()["plan_id"])
        self.assertEqual(self.sent, [])
