"""Reset verification must cover every limit and survive a local write failure."""
import copy
from unittest import mock

from accountdeck.usage import claude_usage
from tests import test_claude_resets


class ResetReconciliationTest(test_claude_resets.UltraResetCase):
    restart = test_claude_resets.UltraResetTest.restart

    def pair(self, before, after):
        self.engine.store.observe(self.key, before)
        self.engine.cli.adapter.usage = lambda _: copy.deepcopy(after if self.sent else before)

    def full(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "seven_day_oauth_apps"])

    def fail_final_save(self):
        save = self.engine.resets._save
        def fail(row, journal):
            if journal["attempt"]["state"] == "settled":
                raise OSError("Synthetic final journal write failure")
            return save(row, journal)
        return mock.patch.object(self.engine.resets, "_save", side_effect=fail)

    def test_partial_baseline_does_not_spend_a_reset(self):
        self.full()
        saved = claude_usage({"seven_day": {"utilization": 100}, "seven_day_oauth_apps": {"utilization": 50}})
        self.engine.store.observe(self.key, saved)
        self.engine.cli.adapter.usage = lambda _: claude_usage({"seven_day": {"utilization": 0 if self.sent else 100}})
        self.assertEqual(self.use(self.check())["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_previously_reported_internal_counter_cannot_disappear_before_a_claim(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "seven_day_overage_included"])
        self.engine.store.observe(self.key, claude_usage({
            "seven_day": {"utilization": 100}, "seven_day_overage_included": {"utilization": 50}}))
        self.engine.cli.adapter.usage = lambda _: claude_usage({"seven_day": {"utilization": 0 if self.sent else 100}})
        self.assertEqual(self.use(self.check())["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_two_post_reset_limits_cannot_be_matched_to_one_baseline(self):
        self.raw["grants"][0]["clears"].append("seven_day_fable")
        before = claude_usage({"seven_day": {"utilization": 100}, "seven_day_fable": {"utilization": 60}})
        after = claude_usage({"seven_day": {"utilization": 50}, "limits": [
            {"kind": "weekly_scoped", "percent": used, "scope": {"model": {"display_name": "Fable"}}}
            for used in (0, 1)]})
        self.pair(before, after)
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.assertEqual(len(self.sent), 1)

    def test_a_previously_reported_second_model_limit_cannot_disappear_from_the_baseline(self):
        self.raw["grants"][0]["clears"].append("seven_day_fable")
        self.engine.store.observe(self.key, claude_usage({
            "seven_day": {"utilization": 100}, "seven_day_fable": {"utilization": 60}}))
        plan = self.check()
        saved = claude_usage({"seven_day": {"utilization": 100}, "limits": [
            {"kind": "weekly_scoped", "percent": used, "scope": {"model": {"display_name": "Fable"}}}
            for used in (100, 20)]})
        self.engine.store.observe(self.key, saved)
        self.engine.cli.adapter.usage = lambda _: claude_usage({
            "seven_day": {"utilization": 50 if self.sent else 100},
            "seven_day_fable": {"utilization": 0 if self.sent else 60}})
        self.assertEqual(self.use(plan)["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_old_ambiguous_baseline_stays_unconfirmed(self):
        self.raw["grants"][0]["clears"].append("seven_day_fable")
        baseline = [{"id": "seven_day", "key": "seven_day", "used": 100},
                    {"id": "seven_day_fable", "key": "seven_day_fable", "used": 100},
                    {"id": "seven_day_fable_2", "key": "seven_day_fable", "used": 20}]
        before = claude_usage({"seven_day": {"utilization": 100}, "seven_day_fable": {"utilization": 60}})
        after = claude_usage({"seven_day": {"utilization": 50}, "seven_day_fable": {"utilization": 0}})
        self.pair(before, after)
        with mock.patch.object(self.engine.resets, "_baseline", return_value=baseline):
            self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.restart()
        self.assertTrue(self.check()["unconfirmed"])
        self.assertEqual(len(self.sent), 1)

    def test_proved_reset_settles_after_usage_returns_to_its_old_level(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day"])
        before = claude_usage({"seven_day": {"utilization": 100}})
        after = claude_usage({"seven_day": {"utilization": 0}})
        self.pair(before, after)
        with self.fail_final_save():
            self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.restart()
        after.update(before)
        self.engine.clock.advance(3601)
        self.assertFalse(self.check()["unconfirmed"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn(0, [r["used"] for r in self.engine.store.readings(self.key, "seven_day")])

    def test_a_duplicate_model_baseline_never_spends(self):
        self.raw["grants"][0]["clears"].append("seven_day_fable")
        before = claude_usage({"seven_day": {"utilization": 100}, "limits": [
            {"kind": "weekly_scoped", "percent": used, "scope": {"model": {"display_name": "Fable"}}}
            for used in (100, 20)]})
        after = claude_usage({"seven_day": {"utilization": 50}, "limits": [
            {"kind": "weekly_scoped", "percent": 0, "scope": {"model": {"display_name": "Fable"}}}]})
        self.engine.store.observe(self.key, claude_usage({
            "seven_day": {"utilization": 100}, "seven_day_fable": {"utilization": 60}}))
        self.engine.cli.adapter.usage = lambda _: copy.deepcopy(after if self.sent else before)
        self.assertEqual(self.use(self.check())["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_structured_optional_zero_is_observed(self):
        self.full()
        def reading(week, apps):
            return claude_usage({"seven_day": {"utilization": week}, "limits": [
                {"kind": "weekly_scoped", "percent": apps, "scope": {"surface": "oauth_apps"}}]})
        self.pair(reading(100, 50), reading(0, 0))
        self.assertEqual(self.use(self.check())["verified"], "verified")

    def test_retry_spelling_change_cannot_restore_pre_reset_history(self):
        self.raw["grants"][0]["clears"].append("seven_day_fable")
        before = claude_usage({"seven_day": {"utilization": 100}, "seven_day_fable": {"utilization": 60}})
        after = claude_usage({"seven_day": {"utilization": 40}, "seven_day_fable": {"utilization": 0}})
        historical = claude_usage({"seven_day": {"utilization": 90}, "limits": [
            {"kind": "weekly_scoped", "percent": 55, "scope": {"model": {"display_name": "Fable 5.1"}}}]})
        self.engine.store.observe(self.key, historical)
        self.pair(before, after)
        with self.fail_final_save():
            self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.restart()
        after.update(claude_usage({"seven_day": {"utilization": 45}, "limits": [
            {"kind": "weekly_scoped", "percent": 5, "scope": {"model": {"display_name": "Fable 5.1"}}}]}))
        self.assertFalse(self.check()["unconfirmed"])
        self.assertEqual([r["used"] for r in self.engine.store.readings(self.key, "seven_day_fable_5_1")], [5])

    def test_success_also_clears_prior_model_spellings(self):
        self.raw["grants"][0]["clears"].append("seven_day_fable")
        self.engine.store.observe(self.key, claude_usage({"limits": [
            {"kind": "weekly_scoped", "percent": 55, "scope": {"model": {"display_name": "Fable 5.1"}}}]}))
        before = claude_usage({"seven_day": {"utilization": 100}, "seven_day_fable": {"utilization": 60}})
        after = claude_usage({"seven_day": {"utilization": 40}, "seven_day_fable": {"utilization": 0}})
        self.pair(before, after)
        self.assertEqual(self.use(self.check())["verified"], "verified")
        self.assertEqual(self.engine.store.readings(self.key, "seven_day_fable_5_1"), [])

    def test_boundary_retry_adds_new_windows_without_clearing_proved_readings(self):
        store = self.engine.store
        store.observe(self.key, {"windows": []}, reset=("synthetic", ["seven_day"], store.account(self.key)))
        store.observe(self.key, claude_usage({"seven_day": {"utilization": 7}, "seven_day_fable": {"utilization": 55}}))
        store.observe(self.key, {"windows": []}, reset=("synthetic", ["seven_day", "seven_day_fable"], store.account(self.key)))
        self.assertEqual([r["used"] for r in store.readings(self.key, "seven_day")], [7])
        self.assertEqual(store.readings(self.key, "seven_day_fable"), [])
        self.assertTrue(store.reset_verified(self.key, "synthetic"))

    def test_failed_verification_transaction_leaves_no_proof_or_forecast_boundary(self):
        store = self.engine.store
        original, mark = store.readings(self.key, "seven_day"), store.reading_mark()
        store.db.execute("CREATE TEMP TRIGGER reject_reading BEFORE INSERT ON readings BEGIN SELECT RAISE(ABORT, 'Synthetic write failure'); END")
        try:
            with self.assertRaisesRegex(Exception, "Synthetic write failure"):
                store.observe(self.key, claude_usage({"seven_day": {"utilization": 0}}),
                              reset=("synthetic", ["seven_day"], store.account(self.key)))
        finally:
            store.db.execute("DROP TRIGGER reject_reading")
        self.assertFalse(store.reset_verified(self.key, "synthetic"))
        self.assertEqual(store.readings(self.key, "seven_day"), original)
        self.assertEqual(store.reading_mark(), mark)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM reset_applied").fetchone()[0], 0)

    def test_durable_proof_survives_a_lost_acceptance_and_disappearing_grant(self):
        self.raw["grants"][0]["clears"].append("seven_day_fable")
        from accountdeck.core import DeckError
        before = claude_usage({"seven_day": {"utilization": 100}, "seven_day_fable": {"utilization": 60}})
        after = claude_usage({"seven_day": {"utilization": 50}, "seven_day_fable": {"utilization": 0}})
        self.pair(before, after)
        def lost(*args):
            self.claim(*args)
            raise DeckError("Synthetic reply lost", "timeout")
        self.engine.cli.adapter.redeem_reset = lost
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        with self.fail_final_save(), self.assertRaises(OSError):
            self.check()
        self.raw["grants"] = []
        after.update(before)
        self.restart()
        self.engine.clock.advance(3601)
        self.assertFalse(self.check()["unconfirmed"])
        self.assertEqual(len(self.sent), 1)
