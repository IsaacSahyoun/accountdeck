"""All accepted usage shapes and imported history obey the same reset boundary."""
import copy
from pathlib import Path
from unittest import mock

from accountdeck import move_bundle, move_import
from accountdeck.core import DeckError
from accountdeck.providers.errors import ProviderError
from accountdeck.usage import claude_usage, saved_reading
from tests import test_claude_resets, test_reset_fourth_review, test_reset_opus, test_reset_reconciliation


class ResetClosureTest(test_claude_resets.UltraResetCase):
    pair = test_reset_reconciliation.ResetReconciliationTest.pair
    fail_final_save = test_reset_reconciliation.ResetReconciliationTest.fail_final_save
    fail_settled_once = test_reset_opus.ResetOpusTest.fail_settled_once
    restart = test_claude_resets.UltraResetTest.restart
    verified_before_removal = test_reset_fourth_review.ResetFourthReviewTest.verified_before_removal

    def reading(self, shape, used, *, unknown=True):
        data = {"seven_day": {"utilization": used}}
        if shape.startswith("named"):
            if unknown:
                data["seven_day_overage_included"] = None if shape == "named-null" else {"utilization": None}
        else:
            identity = ({shape.split("-")[0]: "seven_day_oauth_apps"} if shape in ("kind-null", "type-null")
                        else {"kind": "other", "id": "seven_day_oauth_apps"})
            data["limits"] = [{**identity, "percent": used}]
            if unknown:
                data["limits"].append({**identity, **({} if shape == "id-missing" else {"percent": None})})
        return claude_usage(data, now=self.engine.clock())

    def offer(self, shape):
        key = "seven_day_overage_included" if shape.startswith("named") else "seven_day_oauth_apps"
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", key])

    def test_unknown_cached_values_never_offer_a_confirmation(self):
        for shape in ("named-null", "named-object", "id-null", "id-missing", "kind-null", "type-null"):
            with self.subTest(shape=shape):
                self.engine.clock.advance(3601)
                self.offer(shape)
                self.engine.store.observe(self.key, self.reading(shape, 60))
                self.assertIsNone(self.check()["plan_id"])
                self.assertEqual(self.sent, [])

    def fresh_unknown(self, shape):
        self.offer(shape)
        self.pair(self.reading(shape, 60, unknown=False), self.reading(shape, 0, unknown=False))
        plan = self.check()
        self.engine.cli.adapter.usage = lambda _: self.reading(shape, 60)
        self.assertEqual(self.use(plan)["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_fresh_named_null_never_spends(self):
        self.fresh_unknown("named-null")

    def test_fresh_named_unknown_object_never_spends(self):
        self.fresh_unknown("named-object")

    def test_fresh_id_unknown_sibling_never_spends(self):
        self.fresh_unknown("id-null")

    def test_fresh_id_missing_sibling_never_spends(self):
        self.fresh_unknown("id-missing")

    def test_fresh_kind_named_unknown_sibling_never_spends(self):
        self.fresh_unknown("kind-null")

    def unknown_result(self, shape):
        self.offer(shape)
        self.pair(self.reading(shape, 60, unknown=False), self.reading(shape, 0))
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.restart()
        self.assertTrue(self.check()["unconfirmed"])
        self.assertEqual(len(self.sent), 1)

    def test_named_null_result_is_not_verified(self):
        self.unknown_result("named-null")

    def test_named_unknown_object_result_is_not_verified(self):
        self.unknown_result("named-object")

    def test_id_unknown_sibling_result_is_not_verified(self):
        self.unknown_result("id-null")

    def test_id_missing_sibling_result_is_not_verified(self):
        self.unknown_result("id-missing")

    def test_type_named_unknown_sibling_result_is_not_verified(self):
        self.unknown_result("type-null")

    def test_unknown_model_entry_cannot_keep_a_replaced_named_alias(self):
        usage = claude_usage({"seven_day_fable": {"utilization": 40}, "limits": [
            {"kind": "weekly_scoped", "scope": {"model": {"display_name": name}}, "percent": used}
            for name, used in (("Fable 5.1", 40), ("Fable", None))]})
        named = claude_usage({"seven_day_fable": {"utilization": 40}})["windows"][0]
        historical = copy.deepcopy(usage)
        historical["windows"] = [named, next(w for w in usage["windows"] if w["id"] == "seven_day_fable_5_1")]
        for source in (usage, saved_reading("claude_cli", historical)):
            with self.subTest(saved=source is not usage):
                self.assertEqual([w["id"] for w in source["windows"]], ["seven_day_fable_5_1"])
                self.assertTrue(any(w["used"] is None for w in source["raw_windows"]))

    def test_complete_id_based_limits_can_verify(self):
        self.offer("id-null")
        self.pair(self.reading("id-null", 60, unknown=False), self.reading("id-null", 0, unknown=False))
        self.assertEqual(self.use(self.check())["verified"], "verified")

    def test_fable_reset_also_rejects_an_unknown_named_internal_counter(self):
        self.pair(self.reading("named-null", 60, unknown=False), self.reading("named-null", 0))
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.assertEqual(len(self.sent), 1)

    def test_unaffected_named_unknown_does_not_block_a_weekly_reset(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day"])
        self.pair(self.reading("named-null", 60), self.reading("named-null", 0))
        self.assertEqual(self.use(self.check())["verified"], "verified")

    def test_local_save_block_does_not_need_provider_reads(self):
        adapter = self.engine.cli.adapter
        adapter.redeem_reset = mock.Mock(side_effect=ProviderError("rate_limited", http=429))
        with self.fail_settled_once(permanent=True):
            self.assertEqual(self.use(self.check())["verified"], "failed")
            with mock.patch.object(adapter, "reset_profile", side_effect=DeckError("Offline", "network")) as profile:
                for cause in ("provider-pause", "budget", "offline", "user-pause"):
                    with self.subTest(cause=cause):
                        if cause == "budget":
                            self.engine.store.update(self.key, paused_until=0)
                            for _ in range(20):
                                self.engine.scheduler.governor.record(("claude_cli", self.saved["identity"]), self.engine.clock())
                        elif cause == "offline":
                            self.engine.clock.advance(3601)
                        elif cause == "user-pause":
                            self.engine.prefs.update({"refresh_paused": True})
                        result = self.check()
                        self.assertIsNone(result["plan_id"])
                        self.assertTrue(result["unconfirmed"])
                        self.assertIn("unsaved local result", result["message"])
                        profile.assert_not_called()
        self.engine.prefs.update({"refresh_paused": False})
        self.assertFalse(self.check()["unconfirmed"])
        adapter.redeem_reset.assert_called_once()

    def clean_reconciliation(self, stale):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day"])
        before = claude_usage({"seven_day": {"utilization": 100}})
        after = copy.deepcopy(before)
        self.pair(before, after)
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.engine.store.observe(self.key, stale)
        after.update(claude_usage({"seven_day": {"utilization": 0}}))
        self.assertFalse(self.check()["unconfirmed"])
        request = self.engine.resets._journal(self.engine.store.account(self.key))["attempt"]["request"]
        self.assertTrue(self.engine.store.reset_verified(self.key, request))
        self.assertEqual(len(self.sent), 1)

    def test_fresh_clean_usage_can_verify_after_a_saved_unknown(self):
        self.clean_reconciliation(claude_usage({"seven_day": {"utilization": None}}))

    def test_fresh_clean_usage_can_verify_after_saved_duplicates(self):
        self.clean_reconciliation(claude_usage({"limits": [
            {"kind": "weekly_all", "percent": used} for used in (40, 60)]}))

    def test_refused_request_cannot_acquire_proof_from_another_reset(self):
        before = claude_usage({"seven_day": {"utilization": 100}})
        self.engine.store.observe(self.key, before)
        adapter = self.engine.cli.adapter
        adapter.usage = mock.Mock(return_value=before)
        adapter.redeem_reset = mock.Mock(return_value={"result": "ineligible"})
        with self.fail_settled_once(permanent=True):
            self.assertEqual(self.use(self.check())["verified"], "failed")
            request = self.engine.resets._journal(self.engine.store.account(self.key))["attempt"]["request"]
            self.raw["grants"][0].update(resets_left=0, usable_now=False)
            adapter.usage.return_value = claude_usage({"seven_day": {"utilization": 0}})
            try:
                result = self.check()
            finally:
                self.assertFalse(self.engine.store.reset_verified(self.key, request))
            self.assertTrue(result["unconfirmed"])
        self.assertFalse(self.check()["unconfirmed"])
        self.assertFalse(self.engine.store.reset_verified(self.key, request))
        self.assertEqual(self.engine.resets._journal(self.engine.store.account(self.key))["attempt"]["state"], "settled")
        adapter.redeem_reset.assert_called_once()

    def retired_usage(self, payload, *, fresh=False, verification=False):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "five_hour"])
        before = claude_usage({"seven_day": {"utilization": 100}})
        after = claude_usage({"seven_day": {"utilization": 0}, **payload})
        if not fresh and not verification:
            before = claude_usage({"seven_day": {"utilization": 100}, **payload})
        self.pair(before, after)
        plan = self.check()
        self.assertIsNotNone(plan["plan_id"])
        if fresh:
            before.update(claude_usage({"seven_day": {"utilization": 100}, **payload}))
        self.assertEqual(self.use(plan)["verified"], "verified")
        self.assertEqual(len(self.sent), 1)

    def test_retired_named_null_does_not_block_confirmation(self):
        self.retired_usage({"five_hour": None})

    def test_retired_structured_null_does_not_block_confirmation(self):
        self.retired_usage({"limits": [{"kind": "session", "percent": None}]})

    def test_retired_duplicates_do_not_block_confirmation(self):
        self.retired_usage({"limits": [{"kind": "session", "percent": n} for n in (10, 20)]})

    def test_retired_named_unknown_does_not_block_fresh_baseline(self):
        self.retired_usage({"five_hour": {"utilization": None}}, fresh=True)

    def test_retired_named_null_does_not_block_verification(self):
        self.retired_usage({"five_hour": None}, verification=True)

    def test_retired_id_based_null_does_not_block_verification(self):
        self.retired_usage({"limits": [{"kind": "other", "id": "five_hour", "percent": None}]}, verification=True)

    def test_retired_id_based_number_does_not_join_the_baseline(self):
        self.retired_usage({"limits": [{"kind": "other", "id": "five_hour", "percent": 25}]})

    def import_old_account(self, row, *, usage=None, at=None, snapshots=None, on_pc=False, kind="main", allow_failures=False):
        now = self.engine.clock()
        history = [{"window_id": "seven_day", "at": now - ago, "used": used, "reset": None}
                   for ago, used in ((300, 60), (150, 90))]
        account = {"id": "old-account", "provider": "claude_cli", "identity": row["identity"],
                   "label": row["label"], "email": row["email"] or "", "plan": row["plan"] or "", "org": row["org"],
                   "created": now - 3600, "live_on_pc": False, "refills": [],
                   "secret": {"type": move_bundle.SECRET_TYPES["claude_cli"], "data": copy.deepcopy(self.saved)},
                   "history": {"last_good_usage": usage, "last_good_at": at, "readings": history, "snapshots": snapshots or []}}
        if on_pc:
            account.pop("secret")
            account["live_on_pc"] = True
        payload = {"format": 1, "source_platform": "windows", "settings": {}, "accounts": [account]}
        path = Path(self.paths.temp_root) / "old-reset-account.adeck"
        words = "synthetic reset history test passphrase"
        path.write_bytes(move_bundle.seal(payload, words, kind, scrypt_n=1024, min_scrypt_n=1024))
        with mock.patch.object(move_import, "MIN_SCRYPT_N", 1024):
            result = self.engine.action("import-bundle", {"path": str(path), "passphrase": words})["result"]
        self.assertTrue(self.engine.runner.wait(15))
        if not allow_failures:
            self.assertEqual(result["failures"], [])
        found = self.engine.store.find("claude_cli", row["identity"])
        if found:
            self.key = found["id"]
        return result

    def cowork_usage(self, used, *, unknown=False, alias=False):
        data = {"seven_day": {"utilization": used},
                "cowork" if alias else "seven_day_cowork": {"utilization": used}}
        if unknown:
            data["cowork"] = None
        return claude_usage(data, now=self.engine.clock())

    def cowork_offer(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "seven_day_cowork"])

    def test_unknown_cowork_alias_blocks_cached_confirmation(self):
        self.cowork_offer()
        self.pair(self.cowork_usage(80, unknown=True), self.cowork_usage(0, unknown=True))
        self.assertIsNone(self.check()["plan_id"])
        self.assertEqual(self.sent, [])

    def test_unknown_cowork_alias_blocks_fresh_baseline(self):
        self.cowork_offer()
        self.pair(self.cowork_usage(80), self.cowork_usage(0))
        plan = self.check()
        self.engine.cli.adapter.usage = lambda _: self.cowork_usage(80, unknown=True)
        self.assertEqual(self.use(plan)["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_unknown_cowork_alias_blocks_result_and_restart(self):
        self.cowork_offer()
        self.pair(self.cowork_usage(80), self.cowork_usage(0, unknown=True))
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.restart()
        self.assertTrue(self.check()["unconfirmed"])
        self.assertEqual(len(self.sent), 1)

    def test_numeric_cowork_alias_is_covered_and_clears_its_history(self):
        self.cowork_offer()
        self.pair(self.cowork_usage(80, alias=True), self.cowork_usage(1))
        plan = self.check()
        self.assertIsNotNone(plan["plan_id"])
        self.assertEqual(self.use(plan)["verified"], "verified")
        self.assertEqual(self.engine.store.readings(self.key, "cowork"), [])
        self.assertEqual([r["used"] for r in self.engine.store.readings(self.key, "seven_day_cowork")], [1])

    def test_hidden_legacy_cowork_zero_keeps_its_affected_identity(self):
        self.cowork_offer()
        before = claude_usage({"seven_day": {"utilization": 80}, "cowork": {"utilization": 0}})
        before["unstarted_windows"] = []
        self.pair(before, self.cowork_usage(0, alias=True))
        self.assertEqual(self.use(self.check())["verified"], "verified")

    def test_cowork_alias_and_canonical_numbers_are_ambiguous(self):
        self.cowork_offer()
        before = self.cowork_usage(80)
        alias = self.cowork_usage(20, alias=True)["windows"]
        before["windows"].extend(w for w in alias if w["id"] == "cowork")
        self.pair(before, self.cowork_usage(0))
        self.assertIsNone(self.check()["plan_id"])

    def imported_last_good(self):
        at = self.engine.clock() - 60
        return claude_usage({"seven_day": {"utilization": 80, "resets_at": at + 3.5 * 86400}}, now=at), at

    def test_imported_last_good_after_proof_is_history_only(self):
        self.verified_before_removal()
        row = self.engine.store.account(self.key)
        self.engine.action("remove", {"id": self.key})
        self.assertTrue(self.engine.runner.wait(15))
        usage, at = self.imported_last_good()
        self.import_old_account(row, usage=usage, at=at)
        imported = self.engine.store.account(self.key)
        self.assertIsNone(imported["last_good_usage"])
        self.assertIsNone(imported["last_good_at"])
        public = next(a for a in self.engine.snapshot()["accounts"] if a["id"] == self.key)
        self.assertEqual(public["windows"], [])
        snapshots = self.engine.store.history(self.key)["snapshots"]
        self.assertIn({"at": at, "usage": usage}, snapshots)

    def test_imported_last_good_never_replaces_local_usage_after_proof(self):
        self.verified_before_removal()
        row = self.engine.store.account(self.key)
        self.engine.clock.advance(120)
        usage, at = self.imported_last_good()
        self.import_old_account(row, usage=usage, at=at)
        imported = self.engine.store.account(self.key)
        self.assertEqual(imported["last_good_usage"], row["last_good_usage"])
        self.assertEqual(imported["last_good_at"], row["last_good_at"])
        self.assertIn({"at": at, "usage": usage}, self.engine.store.history(self.key)["snapshots"])

    def test_imported_last_good_without_proof_can_be_current(self):
        row = self.engine.store.account(self.key)
        self.engine.clock.advance(120)
        usage, at = self.imported_last_good()
        self.import_old_account(row, usage=usage, at=at)
        self.assertEqual(self.engine.store.account(self.key)["last_good_usage"], usage)

    def test_reimported_history_cannot_join_a_proved_reset_forecast(self):
        request = self.verified_before_removal()
        row = self.engine.store.account(self.key)
        self.engine.action("remove", {"id": self.key})
        self.assertTrue(self.engine.runner.wait(15))
        self.import_old_account(row)
        self.assertNotEqual(self.key, row["id"])
        self.assertTrue(self.engine.store.reset_verified(self.key, request))
        self.engine.cli.adapter.usage = lambda _: claude_usage(
            {"seven_day": {"utilization": 100}}, now=self.engine.clock())
        self.engine.store.observe(self.key, claude_usage({"seven_day": {"utilization": 95}}))
        with self.fail_final_save(), self.assertRaises(OSError):
            self.check()
        self.assertEqual([r["used"] for r in self.engine.store.readings(self.key, "seven_day")], [95, 100])
        self.assertEqual([r["used"] for r in self.engine.store.taken(self.key, "seven_day", 0)], [95, 100])
        self.assertTrue({60, 90}.issubset({r["windows"]["seven_day"] for r in self.engine.store.history(self.key)["readings"]}))
        self.restart()
        self.assertFalse(self.check()["unconfirmed"])
        self.assertEqual([r["used"] for r in self.engine.store.readings(self.key, "seven_day")], [95, 100, 100])
        self.assertEqual(len(self.sent), 1)

    def test_import_keeps_its_readings_when_this_account_has_no_reset_proof(self):
        self.import_old_account(self.engine.store.account(self.key))
        self.assertTrue({60, 90}.issubset({r["used"] for r in self.engine.store.readings(self.key, "seven_day")}))

    def test_another_organizations_proof_does_not_archive_imported_readings(self):
        request = self.verified_before_removal()
        row = self.engine.store.account(self.key)
        self.engine.action("remove", {"id": self.key})
        self.assertTrue(self.engine.runner.wait(15))
        row["org"] = "11111111-1111-4111-8111-111111111111"
        self.saved["organizationUuid"] = row["org"]
        self.saved["oauthAccount"]["organizationUuid"] = row["org"]
        usage, at = self.imported_last_good()
        self.import_old_account(row, usage=usage, at=at)
        self.assertFalse(self.engine.store.reset_verified(self.key, request))
        self.assertEqual(self.engine.store.account(self.key)["last_good_usage"], usage)
        self.assertTrue({60, 90}.issubset({r["used"] for r in self.engine.store.readings(self.key, "seven_day")}))

    def test_first_proof_after_readdition_starts_a_new_forecast_but_keeps_history(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day"])
        before = claude_usage({"seven_day": {"utilization": 100}})
        after = copy.deepcopy(before)
        self.pair(before, after)
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        request = self.engine.resets._journal(self.engine.store.account(self.key))["attempt"]["request"]
        self.assertFalse(self.engine.store.reset_verified(self.key, request))
        test_reset_fourth_review.ResetFourthReviewTest.remove_and_add_again(self)
        for value in (20, 40):
            self.engine.clock.advance(1)
            self.engine.store.observe(self.key, claude_usage({"seven_day": {"utilization": value}}))
        self.engine.clock.advance(1)
        after.update(claude_usage({"seven_day": {"utilization": 50}}, now=self.engine.clock()))
        self.assertFalse(self.check()["unconfirmed"])
        self.assertEqual([r["used"] for r in self.engine.store.readings(self.key, "seven_day")], [50])
        self.assertTrue({20, 40, 50}.issubset({r["windows"]["seven_day"] for r in self.engine.store.history(self.key)["readings"]}))
        self.assertTrue(self.engine.store.reset_verified(self.key, request))
        self.assertEqual(len(self.sent), 1)
