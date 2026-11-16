"""Budget and refusal persistence cases from the final second-round review."""
from unittest import mock

from accountdeck.providers.errors import ProviderError
from tests import test_claude_resets


class ResetOpusTest(test_claude_resets.UltraResetCase):
    def test_no_usable_offer_does_not_hold_background_reads(self):
        self.raw["eligible"] = False
        governor, now = self.engine.scheduler.governor, self.engine.clock()
        key = ("claude_cli", self.saved["identity"])
        for i in range(16, 0, -1):
            governor.record(key, now - i * 225 + 1)
        result = self.check()
        self.assertIsNone(result["plan_id"])
        self.assertEqual(governor.reserve(key, now + 300, 1, paced=True), 0)
        self.assertEqual(self.sent, [])

    def fail_settled_once(self, *, expire=False, permanent=False):
        save, failed = self.engine.resets._save, []
        def write(row, journal):
            if journal["attempt"]["state"] == "settled" and (not failed or permanent):
                failed.append(True)
                raise OSError("Synthetic local write failure")
            result = save(row, journal)
            if expire and journal["attempt"]["state"] == "pending":
                self.engine.clock.advance(121)
            return result
        return mock.patch.object(self.engine.resets, "_save", side_effect=write)

    def test_unsent_expired_attempt_retries_the_local_settlement(self):
        with self.fail_settled_once(expire=True):
            self.assertEqual(self.use(self.check())["outcome"], "failed")
        self.assertEqual(self.sent, [])
        self.assertFalse(self.check()["unconfirmed"])

    def test_http_refusal_retries_the_local_settlement(self):
        self.engine.cli.adapter.redeem_reset = mock.Mock(side_effect=ProviderError("network", http=400))
        with self.fail_settled_once():
            self.use(self.check())
        self.assertEqual(self.engine.resets._journal(self.engine.store.account(self.key))["attempt"]["state"], "settled")
        self.assertFalse(self.check()["unconfirmed"])
        self.engine.cli.adapter.redeem_reset.assert_called_once()

    def test_result_refusal_retries_the_local_settlement(self):
        self.engine.cli.adapter.redeem_reset = mock.Mock(return_value={"result": "ineligible"})
        with self.fail_settled_once():
            self.use(self.check())
        self.assertEqual(self.engine.resets._journal(self.engine.store.account(self.key))["attempt"]["state"], "settled")
        self.assertFalse(self.check()["unconfirmed"])
        self.engine.cli.adapter.redeem_reset.assert_called_once()

    def test_repeated_local_write_failure_keeps_the_known_refusal(self):
        self.engine.cli.adapter.redeem_reset = mock.Mock(return_value={"result": "ineligible"})
        with self.fail_settled_once(permanent=True):
            self.assertEqual(self.use(self.check())["verified"], "failed")
            self.assertTrue(self.check()["unconfirmed"])
        self.assertFalse(self.check()["unconfirmed"])
        self.engine.cli.adapter.redeem_reset.assert_called_once()
