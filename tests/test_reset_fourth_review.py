"""A retry must preserve its wait and durable proof across account removal."""
from unittest import mock

from accountdeck.core import DeckError
from accountdeck.usage import claude_usage
from tests import test_claude_resets, test_reset_reconciliation


class ResetFourthReviewTest(test_claude_resets.UltraResetCase):
    restart = test_claude_resets.UltraResetTest.restart
    pair = test_reset_reconciliation.ResetReconciliationTest.pair
    fail_final_save = test_reset_reconciliation.ResetReconciliationTest.fail_final_save

    def test_early_inventory_retries_keep_the_original_budget_deadline(self):
        self.engine.clock.speed = 0
        governor, now = self.engine.scheduler.governor, self.engine.clock()
        for i in range(16, 0, -1):
            governor.record(("claude_cli", self.saved["identity"]), now - i * 225 + 1)
        adapter = self.engine.cli.adapter
        with mock.patch.object(adapter, "reset_profile", wraps=adapter.reset_profile) as profile:
            with self.assertRaises(DeckError) as first:
                self.check()
            deadline = now + first.exception.retry_after
            for advance in (10, 10, 240):
                with self.subTest(advance=advance):
                    self.engine.clock.advance(advance)
                    with self.assertRaises(DeckError) as again:
                        self.check()
                    self.assertEqual(again.exception.code, "rate_limited")
                    self.assertEqual(self.engine.clock() + again.exception.retry_after, deadline)
                    self.assertEqual(profile.call_count, 1)
            self.engine.clock.advance(deadline - self.engine.clock() + 1)
            self.assertEqual(self.use(self.check())["verified"], "verified")

    def verified_before_removal(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day"])
        before = claude_usage({"seven_day": {"utilization": 100}})
        after = claude_usage({"seven_day": {"utilization": 0}})
        self.pair(before, after)
        with self.fail_final_save():
            self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        request = self.engine.resets._journal(self.engine.store.account(self.key))["attempt"]["request"]
        self.assertTrue(self.engine.store.reset_verified(self.key, request))
        after.update(before)
        return request

    def remove_and_add_again(self):
        old = self.engine.store.account(self.key)
        self.engine.action("remove", {"id": self.key})
        self.assertTrue(self.engine.runner.wait(15))
        self.assertIsNone(self.engine.store.account(self.key))
        self.key = self.engine.store.save_account("claude_cli", old["identity"], old["label"],
                                                 old["email"], old["plan"], old["org"])
        self.vault.put(self.key, self.saved)
        self.assertNotEqual(self.key, old["id"])

    def test_proof_survives_removing_and_adding_the_same_identity(self):
        request = self.verified_before_removal()
        self.remove_and_add_again()
        self.restart()
        self.assertTrue(self.engine.store.reset_verified(self.key, request))
        self.assertFalse(self.check()["unconfirmed"])
        self.assertEqual(len(self.sent), 1)

    def test_prior_verified_record_migrates_to_identity_proof(self):
        request = self.verified_before_removal()
        self.engine.store.db.execute("DROP TABLE IF EXISTS reset_proofs")
        self.engine.store.db.commit()
        self.restart()
        self.remove_and_add_again()
        self.assertTrue(self.engine.store.reset_verified(self.key, request))
        self.assertFalse(self.check()["unconfirmed"])
        self.assertEqual(len(self.sent), 1)

    def test_proof_does_not_transfer_to_another_identity_or_organization(self):
        request = self.verified_before_removal()
        store = self.engine.store
        other = store.save_account("claude_cli", "another-identity", "Other", org=self.saved["organizationUuid"])
        self.assertFalse(store.reset_verified(other, request))
        other_provider = store.save_account("codex", self.saved["identity"], "Other provider", org=self.saved["organizationUuid"])
        self.assertFalse(store.reset_verified(other_provider, request))
        store.update(self.key, org="another-organization")
        self.assertFalse(store.reset_verified(self.key, request))
        self.restart()
        self.assertFalse(self.engine.store.reset_verified(self.key, request))

    def test_null_cleared_limit_is_unknown_and_does_not_spend(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "seven_day_oauth_apps"])
        usage = claude_usage({"seven_day": {"utilization": 100}, "seven_day_oauth_apps": None})
        self.pair(usage, usage)
        self.assertIsNone(self.check()["plan_id"])
        self.assertEqual(self.sent, [])
