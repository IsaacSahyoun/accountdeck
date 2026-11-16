"""Provider identities survive normalization, history storage and account import."""
import copy
from unittest import mock

from accountdeck.core import DeckError
from accountdeck.usage import claude_usage
from tests import test_claude_resets, test_reset_closure


class ResetIdentityTest(test_claude_resets.UltraResetCase):
    pair = test_reset_closure.ResetClosureTest.pair
    cowork_offer = test_reset_closure.ResetClosureTest.cowork_offer
    cowork_usage = test_reset_closure.ResetClosureTest.cowork_usage
    import_old_account = test_reset_closure.ResetClosureTest.import_old_account
    restart = test_claude_resets.UltraResetTest.restart

    def mixed_hidden(self, used, *, reverse=False):
        named, listed = ("cowork", "seven_day_cowork") if reverse else ("seven_day_cowork", "cowork")
        usage = claude_usage({"seven_day": {"utilization": used}, named: {"utilization": used},
                              "limits": [{"kind": "other", "id": listed, "percent": 0}]})
        usage.pop("unstarted_windows")
        return usage

    def test_hidden_structured_alias_blocks_cached_admission(self):
        self.cowork_offer()
        self.pair(self.mixed_hidden(80), self.cowork_usage(1))
        self.assertIsNone(self.check()["plan_id"])
        self.assertEqual(self.sent, [])

    def test_hidden_structured_canonical_blocks_named_alias(self):
        self.cowork_offer()
        self.pair(self.mixed_hidden(80, reverse=True), self.cowork_usage(1))
        self.assertIsNone(self.check()["plan_id"])

    def test_hidden_structured_alias_blocks_fresh_admission(self):
        self.cowork_offer()
        self.pair(self.cowork_usage(80), self.cowork_usage(1))
        plan = self.check()
        self.engine.cli.adapter.usage = lambda _: self.mixed_hidden(80)
        self.assertEqual(self.use(plan)["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_hidden_structured_alias_blocks_verification(self):
        self.cowork_offer()
        self.pair(self.cowork_usage(80), self.mixed_hidden(1))
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        row = self.engine.store.account(self.key)
        request = self.engine.resets._journal(row)["attempt"]["request"]
        self.assertFalse(self.engine.store.reset_verified(self.key, request))

    def test_same_id_structured_replacement_recovers_its_hidden_zero(self):
        self.cowork_offer()
        before = claude_usage({"seven_day": {"utilization": 80}, "seven_day_cowork": {"utilization": 20},
                               "limits": [{"kind": "other", "id": "seven_day_cowork", "percent": 40}]})
        after = claude_usage({"seven_day": {"utilization": 1}, "seven_day_cowork": {"utilization": 50},
                              "limits": [{"kind": "other", "id": "seven_day_cowork", "percent": 0}]})
        after.pop("unstarted_windows")
        self.pair(before, after)
        plan = self.check()
        self.assertIsNotNone(plan["plan_id"])
        self.assertEqual(self.use(plan)["verified"], "verified")

    def test_explicit_unknown_surface_is_not_a_model_alias(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "seven_day_fable"])
        def usage(used):
            return claude_usage({"seven_day": {"utilization": used}, "seven_day_fable": {"utilization": used},
                                 "limits": [{"kind": "weekly_scoped", "scope": {"surface": "fable_9"}, "percent": None}]})
        self.pair(usage(80), usage(1))
        plan = self.check()
        self.assertIsNotNone(plan["plan_id"])
        self.assertEqual(self.use(plan)["verified"], "verified")

    def test_unknown_structured_surface_is_not_a_display_suffix(self):
        self.cowork_offer()
        before = self.cowork_usage(80)
        after = self.cowork_usage(1)
        extra = {"limits": [{"kind": "weekly_scoped", "scope": {"surface": "cowork_3"}, "percent": None}]}
        before["raw_windows"].extend(claude_usage(extra)["raw_windows"])
        after["raw_windows"].extend(claude_usage(extra)["raw_windows"])
        self.pair(before, after)
        plan = self.check()
        self.assertIsNotNone(plan["plan_id"])
        self.assertEqual(self.use(plan)["verified"], "verified")

    def test_recorded_unknown_name_does_not_require_an_internal_counter(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "seven_day_overage_included"])
        self.engine.store.observe(self.key, claude_usage({"seven_day": {"utilization": 80},
                                                          "seven_day_overage_included_2": {"utilization": 20}}))
        self.pair(claude_usage({"seven_day": {"utilization": 80}}), claude_usage({"seven_day": {"utilization": 1}}))
        self.restart()
        plan = self.check()
        self.assertIsNotNone(plan["plan_id"])
        self.assertEqual(self.use(plan)["verified"], "verified")
        self.assertEqual([r["used"] for r in self.engine.store.readings(self.key, "seven_day_overage_included_2")], [20])

    def test_older_unrelated_structured_metadata_does_not_break_checks(self):
        self.cowork_offer()
        before, after = self.cowork_usage(80), self.cowork_usage(1)
        for usage in (before, after):
            usage["raw_windows"].append({"key": "limits[0].other", "limit_id": "extra_usage", "used": None})
        self.pair(before, after)
        self.assertEqual(self.use(self.check())["verified"], "verified")

    def test_old_reading_schema_retains_cowork_alias_boundaries(self):
        self.cowork_offer()
        self.engine.store.observe(self.key, claude_usage({"seven_day": {"utilization": 80}, "limits": [
            {"kind": "other", "id": "cowork", "percent": n} for n in (20, 30)]}))
        self.pair(self.cowork_usage(80), self.cowork_usage(1))
        self.engine.store.db.execute("ALTER TABLE readings DROP COLUMN window_meta")
        self.engine.store.db.commit()
        self.restart()
        self.assertEqual([r["used"] for r in self.engine.store.readings(self.key, "cowork_2")], [30])
        self.assertEqual(self.use(self.check())["verified"], "verified")
        self.assertEqual(self.engine.store.readings(self.key, "cowork_2"), [])

    def imported_record(self, *, other_org=False, newer=False):
        record = copy.deepcopy(self.saved)
        if other_org:
            record["organizationUuid"] = "11111111-1111-4111-8111-111111111111"
            if isinstance(record.get("oauthAccount"), dict):
                record["oauthAccount"]["organizationUuid"] = record["organizationUuid"]
        record.update(email="imported@example.test", plan="Imported plan")
        if newer:
            record["credentials"]["claudeAiOauth"]["accessToken"] += "-imported"
            record["refreshed"] = self.engine.clock() + 100
        return record

    def import_record(self, record, *, org=None, on_pc=False):
        local = self.engine.store.account(self.key)
        self.saved = record
        incoming = dict(local, org=record["organizationUuid"] if org is None else org,
                        email=record["email"], plan=record["plan"])
        return self.import_old_account(incoming, on_pc=on_pc, allow_failures=True)

    def local_pair(self):
        return self.engine.store.account(self.key), self.vault.get(self.key)

    def assert_kept_pair(self, before):
        row, record = self.local_pair()
        old_row, old_record = before
        self.assertEqual(record, old_record)
        self.assertEqual({k: row[k] for k in ("org", "email", "plan", "last_good_usage")},
                         {k: old_row[k] for k in ("org", "email", "plan", "last_good_usage")})

    def test_another_organization_cannot_replace_kept_login_metadata(self):
        before = self.local_pair()
        with self.assertRaises(DeckError):
            self.import_record(self.imported_record(other_org=True))
        self.assert_kept_pair(before)

    def test_newer_other_organization_requires_a_separate_import(self):
        before = self.local_pair()
        with self.assertRaises(DeckError):
            self.import_record(self.imported_record(other_org=True, newer=True))
        self.assert_kept_pair(before)

    def test_on_pc_other_organization_cannot_replace_local_metadata(self):
        before = self.local_pair()
        with self.assertRaises(DeckError):
            self.import_record(self.imported_record(other_org=True), on_pc=True)
        self.assert_kept_pair(before)

    def test_unknown_organization_listing_keeps_local_login_and_metadata(self):
        before = self.local_pair()
        result = self.import_record(self.imported_record(), org="", on_pc=True)
        self.assertFalse(result["failures"])
        self.assert_kept_pair(before)

    def test_bundle_row_and_login_must_name_the_same_organization(self):
        before = self.local_pair()
        incoming = self.imported_record()
        incoming["organizationUuid"] = "11111111-1111-4111-8111-111111111111"
        result = self.import_record(incoming, org=before[0]["org"])
        self.assertTrue(result["failures"], "A mismatched bundle login was imported")
        self.assertIn("bundle name different organizations", result["failures"][0]["reason"])
        self.assert_kept_pair(before)

    def test_unknown_bundle_login_cannot_replace_a_known_local_login(self):
        before = self.local_pair()
        incoming = self.imported_record(newer=True)
        incoming["organizationUuid"] = ""
        incoming["oauthAccount"]["organizationUuid"] = ""
        result = self.import_record(incoming)
        self.assertFalse(result["failures"])
        self.assert_kept_pair(before)

    def test_newer_login_can_supply_an_unknown_local_organization(self):
        incoming = self.imported_record(newer=True)
        previous = copy.deepcopy(self.saved)
        previous["organizationUuid"] = previous["oauthAccount"]["organizationUuid"] = ""
        self.vault.put(self.key, previous)
        self.engine.store.update(self.key, org="")
        result = self.import_record(incoming)
        self.assertFalse(result["failures"])
        row, record = self.local_pair()
        self.assertEqual(row["org"], incoming["organizationUuid"])
        self.assertEqual(record["organizationUuid"], row["org"])
        self.assertEqual(record["credentials"], incoming["credentials"])

    def test_saved_login_organization_is_checked_beneath_the_row_guard(self):
        incoming = self.imported_record(newer=True)
        previous = copy.deepcopy(self.saved)
        previous["organizationUuid"] = "11111111-1111-4111-8111-111111111111"
        previous["oauthAccount"]["organizationUuid"] = previous["organizationUuid"]
        self.vault.put(self.key, previous)
        before = self.local_pair()
        result = self.import_record(incoming)
        self.assertTrue(result["failures"], "A login from another organization was replaced")
        self.assertIn("organization", result["failures"][0]["reason"])
        self.assert_kept_pair(before)

    def test_unknown_bundle_usage_stays_historical_beside_a_known_local_login(self):
        before = self.local_pair()
        row = dict(before[0], org="")
        self.saved = copy.deepcopy(before[1])
        self.saved["organizationUuid"] = self.saved["oauthAccount"]["organizationUuid"] = ""
        usage = claude_usage({"seven_day": {"utilization": 80}})
        at = self.engine.clock() + 1
        result = self.import_old_account(row, usage=usage, at=at)
        self.assertFalse(result["failures"])
        self.assert_kept_pair(before)
        self.assertIn({"at": at, "usage": usage}, self.engine.store.history(self.key)["snapshots"])

    def test_new_account_rejects_a_bundle_with_conflicting_organizations(self):
        incoming = self.imported_record()
        incoming.pop("oauthAccount", None)
        row = self.engine.store.account(self.key)
        self.engine.action("remove", {"id": self.key})
        self.assertTrue(self.engine.runner.wait(15))
        self.saved = incoming
        row["org"] = "11111111-1111-4111-8111-111111111111"
        result = self.import_old_account(row, allow_failures=True)
        self.assertTrue(result["failures"])
        self.assertIsNone(self.engine.store.find("claude_cli", row["identity"]))

    def test_nested_login_organization_must_match_its_record(self):
        before = self.local_pair()
        incoming = self.imported_record()
        incoming["oauthAccount"] = dict(incoming.get("oauthAccount") or {},
                                         organizationUuid="11111111-1111-4111-8111-111111111111")
        result = self.import_record(incoming)
        self.assertTrue(result["failures"])
        self.assert_kept_pair(before)

    def test_same_login_keeps_its_local_metadata(self):
        before = self.local_pair()
        self.assertFalse(self.import_record(self.imported_record())["failures"])
        self.assert_kept_pair(before)

    def test_failed_login_write_keeps_its_local_metadata(self):
        before = self.local_pair()
        put = self.vault.put
        def fail_account(key, value, **kwargs):
            if key == self.key:
                raise OSError("synthetic vault write failure")
            return put(key, value, **kwargs)
        with mock.patch.object(self.vault, "put", side_effect=fail_account):
            result = self.import_record(self.imported_record(newer=True))
        self.assertTrue(result["failures"])
        self.assert_kept_pair(before)

    def test_newer_same_organization_keeps_login_and_metadata_together(self):
        incoming = self.imported_record(newer=True)
        self.assertFalse(self.import_record(incoming)["failures"])
        row, record = self.local_pair()
        self.assertEqual(record["credentials"], incoming["credentials"])
        self.assertEqual(row["org"], record["organizationUuid"])
        self.assertEqual(row["email"], record["email"])
        self.assertEqual(row["plan"], record["plan"])
