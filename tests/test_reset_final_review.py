"""Known verification gaps should be visible before confirming a reset."""
from unittest import mock

from accountdeck.usage import claude_usage
from tests import test_claude_resets, test_reset_reconciliation


class ResetFinalReviewTest(test_claude_resets.UltraResetCase):
    pair = test_reset_reconciliation.ResetReconciliationTest.pair

    def test_missing_saved_limit_is_not_offered_for_confirmation(self):
        self.engine.clock.speed = 0
        governor, now = self.engine.scheduler.governor, self.engine.clock()
        identity = ("claude_cli", self.saved["identity"])
        for i in range(16, 0, -1):
            governor.record(identity, now - i * 225 + 1)
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "seven_day_oauth_apps"])
        self.engine.store.observe(self.key, claude_usage({"seven_day": {"utilization": 100}, "seven_day_oauth_apps": None}))
        with mock.patch.object(self.engine.cli.adapter, "usage") as usage:
            result = self.check()
        self.assertIsNone(result["plan_id"])
        self.assertFalse(result["offers"][0]["usable"])
        self.assertIn("usage", result["offers"][0]["reason"].lower())
        usage.assert_not_called()
        self.assertEqual(self.sent, [])
        self.assertEqual(governor.reserve(identity, now + 300, 1, paced=True), 0)

    def test_an_older_observed_internal_limit_cannot_disappear_from_the_plan(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "seven_day_overage_included"])
        self.engine.store.observe(self.key, claude_usage({
            "seven_day": {"utilization": 100}, "seven_day_overage_included": {"utilization": 50}}))
        self.engine.store.observe(self.key, claude_usage({"seven_day": {"utilization": 100}}))
        self.assertIsNone(self.check()["plan_id"])
        self.assertEqual(self.sent, [])

    def test_execution_requires_older_observed_internal_usage(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "seven_day_overage_included"])
        self.engine.store.observe(self.key, claude_usage({
            "seven_day": {"utilization": 100}, "seven_day_overage_included": {"utilization": 50}}))
        plan = self.check()
        self.engine.store.observe(self.key, claude_usage({"seven_day": {"utilization": 100}}))
        self.engine.cli.adapter.usage = lambda _: claude_usage({"seven_day": {"utilization": 0 if self.sent else 100}})
        self.assertEqual(self.use(plan)["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_failed_known_unused_save_explains_the_block(self):
        save = self.engine.resets._save
        def fail(row, journal):
            if journal["attempt"]["state"] == "settled":
                raise OSError("Synthetic persistent write failure")
            return save(row, journal)
        self.engine.cli.adapter.redeem_reset = mock.Mock(return_value={"result": "ineligible"})
        with mock.patch.object(self.engine.resets, "_save", side_effect=fail):
            result = self.use(self.check())
        self.assertEqual(result["verified"], "failed")
        self.assertIn("No reset was used", result["summary"])
        self.assertIn("blocked", result["verify_reason"])
        self.assertIn("retry saving", result["verify_reason"].lower())
