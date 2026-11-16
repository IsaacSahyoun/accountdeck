"""Ambiguous limits and older reset records must not manufacture verification."""
import sqlite3
import uuid
from unittest import mock

from accountdeck.providers.errors import ProviderError
from accountdeck.usage import claude_usage
from tests import test_claude_resets, test_reset_reconciliation


class ResetThirdReviewTest(test_claude_resets.UltraResetCase):
    restart = test_claude_resets.UltraResetTest.restart
    pair = test_reset_reconciliation.ResetReconciliationTest.pair
    fail_final_save = test_reset_reconciliation.ResetReconciliationTest.fail_final_save

    def model_offer(self):
        self.raw["grants"][0]["clears"].append("seven_day_fable")

    def surface_offer(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "seven_day_oauth_apps"])

    def structured(self, weekly, values, *, surface=False):
        scope = {"surface": "oauth_apps"} if surface else {"model": {"display_name": "Fable"}}
        return claude_usage({"seven_day": {"utilization": weekly}, "limits": [
            {"kind": "weekly_scoped", "percent": value, "scope": scope, "group": str(index)}
            for index, value in enumerate(values)]})

    def test_equal_model_limits_remain_distinct(self):
        for values in ([60, 60], [0, 0]):
            with self.subTest(values=values):
                reading = self.structured(100, values)
                limits = [w for w in reading["windows"] if w["kind"] == "model"]
                self.assertEqual([(w["id"], w["group"]) for w in limits],
                                 [("seven_day_fable", "0"), ("seven_day_fable_2", "1")])

    def test_equal_model_baseline_does_not_spend(self):
        self.model_offer()
        self.pair(self.structured(100, [60, 60]), self.structured(50, [0]))
        self.assertIsNone(self.check()["plan_id"])
        self.assertEqual(self.sent, [])

    def test_equal_model_results_stay_unconfirmed(self):
        self.model_offer()
        self.pair(self.structured(100, [60]), self.structured(50, [0, 0]))
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.assertEqual(len(self.sent), 1)

    def test_previously_collapsed_model_limits_still_block_a_claim(self):
        self.model_offer()
        saved = self.structured(100, [60, 60])
        saved["windows"] = [w for w in saved["windows"] if w["id"] != "seven_day_fable_2"]
        self.engine.store.observe(self.key, saved)
        self.engine.cli.adapter.usage = lambda _: self.structured(50 if self.sent else 100, [0 if self.sent else 60])
        self.assertIsNone(self.check()["plan_id"])
        self.assertEqual(self.sent, [])

    def test_old_surface_ids_without_raw_still_block_a_claim(self):
        self.surface_offer()
        saved = self.structured(100, [60, 80], surface=True)
        for window in saved["windows"]:
            window["scope"] = None
        saved["raw_windows"] = []
        self.engine.store.observe(self.key, saved)
        self.engine.cli.adapter.usage = lambda _: self.structured(0 if self.sent else 100, [0 if self.sent else 60], surface=True)
        self.assertIsNone(self.check()["plan_id"])
        self.assertEqual(self.sent, [])

    def test_duplicate_surface_baseline_does_not_spend(self):
        self.surface_offer()
        self.pair(self.structured(100, [60, 80], surface=True), self.structured(0, [0], surface=True))
        self.assertIsNone(self.check()["plan_id"])
        self.assertEqual(self.sent, [])

    def test_duplicate_surface_results_stay_unconfirmed(self):
        self.surface_offer()
        self.pair(self.structured(100, [60], surface=True), self.structured(0, [0, 80], surface=True))
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.assertEqual(len(self.sent), 1)

    def test_surface_baseline_without_raw_does_not_spend(self):
        self.surface_offer()
        before, after = self.structured(100, [60, 80], surface=True), self.structured(0, [0], surface=True)
        before["raw_windows"], after["raw_windows"] = [], []
        self.pair(before, after)
        self.assertIsNone(self.check()["plan_id"])
        self.assertEqual(self.sent, [])

    def test_old_incomplete_attempt_stays_unconfirmed(self):
        self.surface_offer()
        row = self.engine.store.account(self.key)
        for state, values in (("accepted", []), ("accepted", [50]), ("pending", [50])):
            with self.subTest(state=state, values=values):
                self.engine.clock.advance(3601)
                self.engine.cli.adapter.usage = lambda _: self.structured(0, values, surface=True)
                request = str(uuid.uuid4())
                self.engine.resets._save(row, {"revision": 1, "attempt": {
                    "grant": "synthetic-fable", "request": request, "count": 2, "kind": "full",
                    "clears": ["seven_day", "seven_day_oauth_apps"], "state": state,
                    "before": [{"id": "seven_day", "key": "seven_day", "used": 100}]}})
                self.assertTrue(self.check()["unconfirmed"])
                self.assertFalse(self.engine.store.reset_verified(self.key, request))
                self.assertEqual(self.sent, [])

    def test_old_fable_attempt_missing_weekly_stays_unconfirmed(self):
        self.model_offer()
        self.engine.cli.adapter.usage = lambda _: self.structured(100, [0])
        row = self.engine.store.account(self.key)
        self.engine.resets._save(row, {"revision": 1, "attempt": {
            "grant": "synthetic-fable", "request": str(uuid.uuid4()), "count": 1, "kind": "fable",
            "clears": ["seven_day_overage_included", "seven_day_fable"], "state": "accepted",
            "before": [{"id": "seven_day_fable", "key": "seven_day_fable", "used": 60}]}})
        self.assertTrue(self.check()["unconfirmed"])
        self.assertEqual(self.sent, [])

    def legacy_pending(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day"])
        before = claude_usage({"seven_day": {"utilization": 100}})
        after = claude_usage({"seven_day": {"utilization": 0}})
        self.pair(before, after)
        with self.fail_final_save():
            self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.engine.store.observe(self.key, claude_usage({"seven_day": {"utilization": 7}}))
        db = self.engine.store.db
        db.execute("DROP TABLE IF EXISTS reset_proofs")
        db.execute("DROP TABLE reset_applied")
        db.execute("ALTER TABLE reset_attempts DROP COLUMN verified")
        db.commit()
        self.restart()
        return after

    def test_legacy_applied_boundary_is_not_applied_twice(self):
        self.legacy_pending()
        store = self.engine.store
        attempt = self.engine.resets._journal(store.account(self.key))["attempt"]
        self.assertFalse(store.reset_verified(self.key, attempt["request"]))
        self.assertFalse(self.check()["unconfirmed"])
        self.assertCountEqual([r["used"] for r in store.readings(self.key, "seven_day")], [0, 7, 0])
        self.assertEqual(store.db.execute("SELECT segment FROM reset_boundaries WHERE account=? AND window_id=?",
                                         (self.key, "seven_day")).fetchone()[0], 1)
        self.assertEqual(len(self.sent), 1)

    def test_legacy_marker_alone_does_not_prove_a_reset(self):
        after = self.legacy_pending()
        after.update(claude_usage({"seven_day": {"utilization": 100}}))
        self.assertTrue(self.check()["unconfirmed"])
        self.assertEqual([r["used"] for r in self.engine.store.readings(self.key, "seven_day")], [0, 7])
        self.assertEqual(len(self.sent), 1)

    def test_throttle_write_failure_preserves_definite_refusal(self):
        adapter = self.engine.cli.adapter
        adapter.redeem_reset = mock.Mock(side_effect=ProviderError("rate_limited", http=429, retry_after=900))
        update = self.engine.store.update
        failed = []
        def fail_pause(key, **changes):
            if "paused_until" in changes:
                failed.append(True)
                raise sqlite3.OperationalError("Synthetic throttle write failure")
            return update(key, **changes)
        with mock.patch.object(self.engine.store, "update", side_effect=fail_pause):
            self.assertEqual(self.use(self.check())["verified"], "failed")
        self.assertEqual(failed, [True])
        self.assertFalse(self.check()["unconfirmed"])
        adapter.redeem_reset.assert_called_once()
