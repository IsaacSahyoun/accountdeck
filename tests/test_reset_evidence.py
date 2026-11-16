"""Unknown provider values and replacement account rows must preserve reset evidence."""
from accountdeck.usage import claude_usage
from tests import test_claude_resets, test_reset_fourth_review, test_reset_reconciliation


class ResetEvidenceTest(test_claude_resets.UltraResetCase):
    restart = test_claude_resets.UltraResetTest.restart
    pair = test_reset_reconciliation.ResetReconciliationTest.pair
    fail_final_save = test_reset_reconciliation.ResetReconciliationTest.fail_final_save
    verified_before_removal = test_reset_fourth_review.ResetFourthReviewTest.verified_before_removal
    remove_and_add_again = test_reset_fourth_review.ResetFourthReviewTest.remove_and_add_again

    def reading(self, kind, value, *, unknown=False, missing=False):
        scope = {"surface": "oauth_apps"} if kind == "surface" else {"model": {"display_name": "Fable"}}
        shape = {"kind": "weekly_all"} if kind == "weekly" else {"kind": "weekly_scoped", "scope": scope}
        limits = [{**shape, "percent": value, "group": "numeric"}]
        if unknown:
            limits.append({**shape, "group": "unknown", **({} if missing else {"percent": None})})
        return claude_usage({"seven_day": {"utilization": value}, "limits": limits})

    def offer(self, kind):
        key = {"weekly": "seven_day", "surface": "seven_day_oauth_apps", "model": "seven_day_fable"}[kind]
        self.raw["grants"][0].update(gives_back_weekly=False, clears=list({"seven_day", key}))

    def test_unknown_structured_values_remain_in_raw_evidence(self):
        for kind in ("weekly", "surface", "model"):
            for missing in (False, True):
                with self.subTest(kind=kind, missing=missing):
                    raw = self.reading(kind, 60, unknown=True, missing=missing)["raw_windows"]
                    unknown = [r for r in raw if r["key"].startswith("limits[") and r["used"] is None]
                    self.assertEqual(len(unknown), 1)

    def test_cached_unknown_sibling_never_offers_confirmation(self):
        for kind in ("weekly", "surface", "model"):
            for missing in (False, True):
                with self.subTest(kind=kind, missing=missing):
                    self.engine.clock.advance(3601)
                    self.offer(kind)
                    self.engine.store.observe(self.key, self.reading(kind, 60, unknown=True, missing=missing))
                    self.assertIsNone(self.check()["plan_id"])
                    self.assertEqual(self.sent, [])

    def reject_fresh_unknown(self, kind):
        self.offer(kind)
        self.pair(self.reading(kind, 60), self.reading(kind, 0))
        plan = self.check()
        self.engine.cli.adapter.usage = lambda _: self.reading(kind, 60, unknown=True)
        self.assertEqual(self.use(plan)["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_fresh_unknown_weekly_sibling_never_spends(self):
        self.reject_fresh_unknown("weekly")

    def test_fresh_unknown_surface_sibling_never_spends(self):
        self.reject_fresh_unknown("surface")

    def test_fresh_unknown_model_sibling_never_spends(self):
        self.reject_fresh_unknown("model")

    def unconfirmed_result(self, kind, missing=False):
        self.offer(kind)
        self.pair(self.reading(kind, 60), self.reading(kind, 0, unknown=True, missing=missing))
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.assertEqual(len(self.sent), 1)
        self.restart()
        self.assertTrue(self.check()["unconfirmed"])
        self.assertEqual(len(self.sent), 1)

    def test_unknown_surface_result_cannot_verify(self):
        self.unconfirmed_result("surface")

    def test_missing_model_result_cannot_verify(self):
        self.unconfirmed_result("model", missing=True)

    def test_unknown_weekly_result_cannot_verify(self):
        self.unconfirmed_result("weekly")

    def test_replacement_row_keeps_post_reset_readings_across_retries(self):
        request = self.verified_before_removal()
        self.remove_and_add_again()
        self.engine.cli.adapter.usage = lambda _: claude_usage(
            {"seven_day": {"utilization": 100}}, now=self.engine.clock())
        store = self.engine.store
        for value in (95, 100):
            store.observe(self.key, claude_usage({"seven_day": {"utilization": value}}))
        with self.fail_final_save(), self.assertRaises(OSError):
            self.check()
        self.assertEqual([r["used"] for r in store.readings(self.key, "seven_day")], [95, 100, 100])
        self.assertEqual([r["used"] for r in store.taken(self.key, "seven_day", 0)], [95, 100, 100])
        self.restart()
        self.assertFalse(self.check()["unconfirmed"])
        self.assertEqual([r["used"] for r in self.engine.store.readings(self.key, "seven_day")], [95, 100, 100, 100])
        self.assertTrue(self.engine.store.reset_verified(self.key, request))
        self.assertEqual(len(self.sent), 1)
        self.engine.clock.advance(3601)
        grant = self.raw["grants"][0]
        grant.update(resets_left=1, usable_now=True)
        self.raw["next_grant_id"] = grant["id"]
        self.engine.cli.adapter.usage = lambda _: claude_usage(
            {"seven_day": {"utilization": 100 if len(self.sent) == 1 else 0}}, now=self.engine.clock())
        self.assertEqual(self.use(self.check())["verified"], "verified")
        self.assertEqual(len(self.sent), 2)
        self.assertEqual([r["used"] for r in self.engine.store.readings(self.key, "seven_day")], [0])

    def test_unrelated_unknown_limit_does_not_block_a_weekly_reset(self):
        self.offer("weekly")
        self.pair(self.reading("surface", 60, unknown=True), self.reading("surface", 0, unknown=True))
        self.assertEqual(self.use(self.check())["verified"], "verified")

    def test_unknown_internal_counter_is_not_treated_as_absent(self):
        usage = claude_usage({"seven_day": {"utilization": 60}, "limits": [
            {"kind": "weekly_scoped", "scope": {"surface": "overage_included"}, "percent": None}]})
        self.pair(usage, usage)
        self.assertIsNone(self.check()["plan_id"])
        self.assertEqual(self.sent, [])

    def test_newly_reported_unknown_internal_result_stays_unconfirmed(self):
        before = claude_usage({"seven_day": {"utilization": 60}})
        after = claude_usage({"seven_day": {"utilization": 0}, "limits": [
            {"kind": "weekly_scoped", "scope": {"surface": "overage_included"}, "percent": None}]})
        self.pair(before, after)
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.assertEqual(len(self.sent), 1)
