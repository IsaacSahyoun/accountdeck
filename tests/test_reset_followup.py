"""Reconciliation uses the pre-claim facts and reports known local failures truthfully."""
import copy
import uuid
from unittest import mock

from accountdeck.core import DeckError
from accountdeck.usage import claude_usage
from tests import test_claude_resets, test_reset_opus, test_reset_reconciliation


class ResetFollowupTest(test_claude_resets.UltraResetCase):
    restart = test_claude_resets.UltraResetTest.restart
    pair = test_reset_reconciliation.ResetReconciliationTest.pair
    fail_settled_once = test_reset_opus.ResetOpusTest.fail_settled_once

    def test_retired_five_hour_history_never_blocks_a_reset(self):
        self.raw["grants"][0]["clears"].append("five_hour")
        self.engine.store.db.execute("INSERT INTO readings(account, window_id, at, used, reset) VALUES(?, ?, ?, ?, ?)",
                                     (self.key, "five_hour", self.engine.clock(), 90, None))
        self.engine.store.db.commit()
        self.pair(claude_usage({"seven_day": {"utilization": 100}}),
                  claude_usage({"seven_day": {"utilization": 0}}))
        plan = self.check()
        self.assertIsNotNone(plan["plan_id"])
        self.assertEqual(self.use(plan)["verified"], "verified")

    def next_offer_after_settlement(self, complete_after):
        old = {"seven_day": {"utilization": 100}}
        if not complete_after:
            old["seven_day_oauth_apps"] = {"utilization": 20}
        before = claude_usage(old)
        after = copy.deepcopy(before)
        self.pair(before, after)
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        grant = {**self.raw["grants"][0], "id": "synthetic-next", "resets_left": 1, "usable_now": True,
                 "gives_back_weekly": False, "clears": ["seven_day", "seven_day_oauth_apps"]}
        self.raw["grants"].append(grant)
        self.raw["next_grant_id"] = grant["id"]
        latest = {"seven_day": {"utilization": 0}}
        if complete_after:
            latest["seven_day_oauth_apps"] = {"utilization": 20}
        after.update(claude_usage(latest))
        checked = self.check()
        self.assertFalse(checked["unconfirmed"])
        self.assertEqual(checked["plan_id"] is not None, complete_after)
        self.assertEqual(len(self.sent), 1)

    def test_next_offer_uses_the_complete_reading_just_reconciled(self):
        self.next_offer_after_settlement(complete_after=True)

    def test_next_offer_rejects_the_incomplete_reading_just_reconciled(self):
        self.next_offer_after_settlement(complete_after=False)

    def late_counter(self, lost=False, internal=0, omit_last=False):
        before = claude_usage({"seven_day": {"utilization": 100}})
        after = copy.deepcopy(before)
        self.pair(before, after)
        if lost:
            def claim(*args):
                self.claim(*args)
                raise DeckError("Synthetic lost response", "timeout")
            self.engine.cli.adapter.redeem_reset = claim
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        after.update(claude_usage({"seven_day": {"utilization": 0},
                                  "seven_day_overage_included": {"utilization": internal}}))
        self.engine.store.observe(self.key, after)
        if omit_last:
            self.engine.store.observe(self.key, claude_usage({"seven_day": {"utilization": 0}}))
        self.restart()
        self.assertFalse(self.check()["unconfirmed"])
        self.assertEqual(len(self.sent), 1)

    def test_new_counter_after_an_accepted_claim_does_not_rewrite_the_baseline(self):
        self.late_counter()

    def test_new_counter_after_a_lost_reply_does_not_rewrite_the_baseline(self):
        self.late_counter(lost=True)

    def test_new_counter_in_later_history_does_not_rewrite_the_baseline(self):
        self.late_counter(internal=15, omit_last=True)

    def test_metadata_named_structured_history_does_not_break_resets(self):
        before = claude_usage({"seven_day": {"utilization": 100}, "limits": [
            {"kind": name, "percent": 12} for name in ("spend", "limits", "extra_usage")]})
        after = claude_usage({"seven_day": {"utilization": 0}})
        self.pair(before, after)
        self.assertEqual(self.use(self.check())["verified"], "verified")

    def test_missing_saved_usage_requests_a_real_scheduled_read(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "seven_day_oauth_apps"])
        self.engine.store.observe(self.key, claude_usage({"seven_day": {"utilization": 100}}))
        complete = claude_usage({"seven_day": {"utilization": 100}, "seven_day_oauth_apps": {"utilization": 20}})
        self.engine.scheduler.request(self.key, self.engine.clock())
        self.engine.scheduler._plans[self.key].next = self.engine.clock() + 3600
        with mock.patch.object(self.engine.cli.adapter, "usage", return_value=complete) as usage:
            checked = self.check()
            self.assertIsNone(checked["plan_id"])
            self.assertIn("missing or unclear", checked["offers"][0]["reason"])
            self.assertNotIn("next usage update", checked["offers"][0]["reason"])
            self.engine.clock.advance(361)
            targets = [t for t in self.engine.targets() if t.id == self.key]
            self.engine.scheduler.tick(targets, self.engine.clock(), self.engine.clock)
            usage.assert_called_once()
            self.assertIsNotNone(self.check()["plan_id"])

    def test_known_unused_result_is_failed_and_local_recheck_can_finish_it(self):
        self.engine.cli.adapter.redeem_reset = mock.Mock(return_value={"result": "ineligible"})
        with self.fail_settled_once(permanent=True):
            report = self.use(self.check())
            self.assertEqual(report["verified"], "failed")
            check = self.check()
            self.assertTrue(check["unconfirmed"])
            self.assertIn("unsaved local result", check["message"])
            self.assertNotIn("Check again later", check["message"])
        self.assertFalse(self.check()["unconfirmed"])
        self.engine.cli.adapter.redeem_reset.assert_called_once()

    def test_restart_cannot_invent_a_lost_known_unused_result(self):
        self.engine.cli.adapter.redeem_reset = mock.Mock(return_value={"result": "ineligible"})
        with self.fail_settled_once(permanent=True):
            self.use(self.check())
        self.restart()
        check = self.check()
        self.assertTrue(check["unconfirmed"])
        self.assertIn("still unconfirmed", check["message"])
        self.engine.cli.adapter.redeem_reset.assert_called_once()

    def test_known_unused_note_cannot_close_a_different_attempt(self):
        self.engine.cli.adapter.redeem_reset = mock.Mock(return_value={"result": "ineligible"})
        with self.fail_settled_once(permanent=True):
            self.use(self.check())
        row = self.engine.store.account(self.key)
        journal = self.engine.resets._journal(row)
        other = str(uuid.uuid4())
        journal["attempt"]["request"] = other
        self.engine.resets._save(row, journal)
        self.assertTrue(self.check()["unconfirmed"])
        kept = self.engine.resets._journal(row)["attempt"]
        self.assertEqual(kept["request"], other)
        self.assertEqual(kept["state"], "pending")
        self.engine.cli.adapter.redeem_reset.assert_called_once()

    def test_known_unused_note_cannot_close_another_organizations_attempt(self):
        self.engine.cli.adapter.redeem_reset = mock.Mock(return_value={"result": "ineligible"})
        with self.fail_settled_once(permanent=True):
            self.use(self.check())
        original = self.engine.store.account(self.key)
        journal = self.engine.resets._journal(original)
        self.saved["organizationUuid"] = str(uuid.uuid4())
        self.vault.put(self.key, self.saved)
        self.engine.store.update(self.key, org=self.saved["organizationUuid"])
        changed = self.engine.store.account(self.key)
        self.engine.resets._save(changed, journal)
        self.assertTrue(self.check()["unconfirmed"])
        self.assertEqual(self.engine.resets._journal(changed)["attempt"]["state"], "pending")
        self.assertEqual(self.engine.resets._journal(original)["attempt"]["state"], "pending")
        self.engine.cli.adapter.redeem_reset.assert_called_once()

    def test_expired_unsent_result_can_finish_saving_without_a_claim(self):
        with self.fail_settled_once(expire=True, permanent=True):
            self.assertEqual(self.use(self.check())["verified"], "failed")
        self.assertFalse(self.check()["unconfirmed"])
        self.assertEqual(self.sent, [])
