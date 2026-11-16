"""Manual Ultra resets on synthetic accounts; no real provider or login is used."""
import copy
import contextlib
import json
import sqlite3
import threading
import unittest
from unittest import mock

from accountdeck.core import DeckError, Vault
from accountdeck.engine import Engine
from accountdeck.providers.claude_resets import status
from accountdeck.providers.errors import ProviderError
from accountdeck.providers.fake import test_cipher
from tests.test_switch_safety import Crash, DeckCase


class UltraResetCase(DeckCase):
    def setUp(self):
        super().setUp()
        self.target = self.spare("claude_cli")
        self.key = self.target["id"]
        saved = self.vault.get(self.key)
        saved["credentials"]["claudeAiOauth"]["rateLimitTier"] = "default_claude_max_20x"
        self.vault.put(self.key, saved)
        self.saved = saved
        self.sent = []
        self.raw = {"eligible": True, "event_props": {"tier": "claude_max_20x"},
                    "next_grant_id": "synthetic-fable", "grants": [{"id": "synthetic-fable", "label": "Fable reset",
                    "type": "full", "resets_left": 1, "resets_total": 1, "starts_at": None, "ends_at": None,
                    "paused": False, "usable_now": True, "use_requires_limit": False, "blocking": [],
                    "gives_back_weekly": True, "clears": ["seven_day_overage_included"]}]}
        adapter = self.engine.providers["claude_cli"]
        adapter.reset_status = lambda credentials: copy.deepcopy(self.raw)
        adapter.reset_profile = lambda credentials: {"accountUuid": saved["identity"], "organizationUuid": saved["organizationUuid"]}
        adapter.redeem_reset = self.claim

    def claim(self, credentials, org, grant, request_id):
        self.sent.append((org, grant, request_id))
        self.engine.cli.adapter.reset_counts[credentials["claudeAiOauth"]["accessToken"]] = (
            [0, 1] if self.raw["grants"][0]["gives_back_weekly"] else [0, 0])
        self.raw["grants"][0].update(resets_left=0, usable_now=False)
        self.raw["next_grant_id"] = None
        return {"result": "reset", "resets_left": 0}

    def check(self):
        return self.engine.action("reset-status", {"id": self.key})

    def use(self, plan):
        self.engine.action("reset-use", {"id": self.key, "plan_id": plan["plan_id"]})
        self.assertTrue(self.engine.runner.wait(15))
        return self.engine.snapshot()["report"]


class UltraResetTest(UltraResetCase):
    def test_reads_and_redeems_one_fable_reset_without_switching_or_renewing(self):
        before = self.signed_in("claude_cli")
        with mock.patch.object(self.engine.providers["claude_cli"], "refresh", side_effect=AssertionError("No renewal")):
            plan = self.check()
            self.assertEqual(plan["offers"][0]["title"], "Fable reset")
            self.assertEqual(plan["offers"][0]["count"], 1)
            self.assertEqual(self.sent, [])
            report = self.use(plan)
        self.assertEqual(report["verified"], "verified", report)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][:2], (self.saved["organizationUuid"], "synthetic-fable"))
        self.assertEqual(self.signed_in("claude_cli"), before)
        self.assertEqual(self.vault.get(self.key)["credentials"], self.saved["credentials"])

    def test_non_ultra_cannot_read_or_redeem(self):
        saved = self.vault.get(self.key)
        saved["credentials"]["claudeAiOauth"]["rateLimitTier"] = "default_claude_max_5x"
        self.vault.put(self.key, saved)
        with self.assertRaises(DeckError):
            self.check()
        self.assertEqual(self.sent, [])

    def test_the_current_ultra_account_can_reset_without_a_switch(self):
        self.target = self.active("claude_cli")
        self.key = self.target["id"]
        self.saved = self.vault.get(self.key)
        self.engine.cli.adapter.reset_profile = lambda c: {
            "accountUuid": self.saved["identity"], "organizationUuid": self.saved["organizationUuid"]}
        before = self.signed_in("claude_cli")
        self.assertEqual(self.use(self.check())["verified"], "verified")
        self.assertEqual(self.signed_in("claude_cli"), before)

    def test_a_changed_organization_never_claims(self):
        plan = self.check()
        self.engine.providers["claude_cli"].reset_profile = lambda c: {"accountUuid": self.saved["identity"], "organizationUuid": "other"}
        report = self.use(plan)
        self.assertEqual(report["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_a_lost_reply_never_replays_the_claim(self):
        def lose(*args):
            self.sent.append(args)
            raise DeckError("No reply", "timeout")
        self.engine.providers["claude_cli"].redeem_reset = lose
        plan = self.check()
        report = self.use(plan)
        self.assertEqual(report["verified"], "unconfirmed", report)
        with self.assertRaises(DeckError):
            self.use(plan)
        self.assertEqual(len(self.sent), 1)

    def restart(self):
        old = self.engine
        old.stop()
        self.engine = Engine(old.paths, providers=old.providers, procs=old.procs,
                             vault=Vault(old.paths.data_dir / "vault", protect=test_cipher), status_sources={},
                             clock=old.clock, start=False, inline=True, mode="demo")
        self.addCleanup(self.engine.stop)
        self.vault = self.engine.vault
        self.engine.sense_active()

    def test_full_reset_uses_the_affected_limits_not_the_misleading_type(self):
        self.raw["grants"][0].update(type="weekly", gives_back_weekly=False,
                                      clears=["seven_day", "seven_day_overage_included"])
        plan = self.check()
        self.assertEqual(plan["offers"][0]["title"], "Full reset")
        self.assertEqual(self.use(plan)["verified"], "verified")

    def test_expired_confirmation_never_claims(self):
        plan = self.check()
        self.engine.clock.advance(121)
        self.assertEqual(self.use(plan)["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_confirmation_that_expires_during_the_recheck_never_claims(self):
        plan = self.check()
        read = self.engine.cli.adapter.reset_status
        def slow(credentials):
            self.engine.clock.advance(121)
            return read(credentials)
        self.engine.cli.adapter.reset_status = slow
        self.assertEqual(self.use(plan)["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_changed_credentials_and_changed_offers_require_new_consent(self):
        for change in ("credentials", "count", "expiry", "next"):
            with self.subTest(change=change):
                self.engine.clock.advance(3601)
                plan = self.check()
                saved, raw = copy.deepcopy(self.vault.get(self.key)), copy.deepcopy(self.raw)
                if change == "credentials":
                    changed = copy.deepcopy(saved)
                    changed["credentials"]["claudeAiOauth"]["accessToken"] += "-new"
                    self.vault.put(self.key, changed)
                elif change == "count":
                    self.raw["grants"][0]["resets_left"] = 2
                elif change == "expiry":
                    self.raw["grants"][0]["ends_at"] = self.engine.clock() - 1
                else:
                    self.raw["next_grant_id"] = None
                self.assertEqual(self.use(plan)["outcome"], "failed")
                self.assertEqual(self.sent, [])
                self.vault.put(self.key, saved)
                self.raw = raw

    def test_status_and_report_do_not_expose_provider_ids_or_tokens(self):
        plan = self.check()
        report = self.use(plan)
        exposed = json.dumps([plan, report, self.engine.snapshot()])
        for secret in (self.saved["identity"], self.saved["organizationUuid"], "synthetic-fable",
                       self.saved["credentials"]["claudeAiOauth"]["accessToken"], self.sent[0][2]):
            self.assertNotIn(secret, exposed)

    def test_expired_login_is_not_renewed_or_sent_to_the_provider(self):
        saved = self.vault.get(self.key)
        saved["credentials"]["claudeAiOauth"]["expiresAt"] = (self.engine.clock() - 1) * 1000
        self.vault.put(self.key, saved)
        with mock.patch.object(self.engine.cli.adapter, "reset_profile", side_effect=AssertionError("No request")):
            with self.assertRaises(DeckError):
                self.check()

    def test_read_only_instance_cannot_read_or_submit(self):
        plan = self.check()
        with mock.patch.object(type(self.engine), "owns_login", new_callable=mock.PropertyMock, return_value=False):
            with self.assertRaises(DeckError):
                self.check()
            with self.assertRaises(DeckError):
                self.use(plan)
        self.assertEqual(self.sent, [])

    def test_duplicate_and_simultaneous_confirmations_send_once(self):
        plan = self.check()
        entered, release = threading.Event(), threading.Event()
        claim = self.claim
        def held(*args):
            entered.set()
            if not release.wait(10):
                raise AssertionError("The test did not release the claim")
            return claim(*args)
        self.engine.cli.adapter.redeem_reset = held
        self.engine.action("reset-use", {"id": self.key, "plan_id": plan["plan_id"]})
        try:
            self.assertTrue(entered.wait(10))
            with self.assertRaises(DeckError):
                self.use(plan)
            with self.assertRaises(DeckError):
                self.check()
            with self.assertRaises(DeckError):
                self.engine.action("cancel", {})
        finally:
            release.set()
        self.assertTrue(self.engine.runner.wait(10))
        self.assertEqual(len(self.sent), 1)

    def test_lost_success_is_settled_after_a_restart_without_replay(self):
        def lose(*args):
            self.claim(*args)
            raise DeckError("No reply", "timeout")
        self.engine.cli.adapter.redeem_reset = lose
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.restart()
        result = self.check()
        self.assertFalse(result["unconfirmed"])
        self.assertIsNone(result["plan_id"])
        self.assertEqual(len(self.sent), 1)

    def test_crash_after_saving_intent_cannot_replay_after_restart(self):
        save = self.engine.resets._save
        def crash(*args):
            save(*args)
            raise Crash()
        with mock.patch.object(self.engine.resets, "_save", side_effect=crash):
            self.use(self.check())
        self.restart()
        self.assertTrue(self.check()["unconfirmed"])
        self.assertEqual(self.sent, [])

    def test_crash_after_sending_cannot_replay_after_restart(self):
        def crash(*args):
            self.claim(*args)
            raise Crash()
        self.engine.cli.adapter.redeem_reset = crash
        self.use(self.check())
        self.restart()
        self.assertFalse(self.check()["unconfirmed"])
        self.assertEqual(len(self.sent), 1)

    def test_failed_usage_verification_is_reconciled_by_a_later_check(self):
        baseline = self.engine.cli.adapter.usage(self.saved["credentials"])
        with mock.patch.object(self.engine.cli.adapter, "usage", side_effect=[baseline, DeckError("No usage", "timeout")]):
            self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.engine.clock.advance(3601)
        self.assertFalse(self.check()["unconfirmed"])
        self.assertEqual(len(self.sent), 1)

    def test_unknown_reply_stays_unconfirmed_even_when_reopened(self):
        self.engine.cli.adapter.redeem_reset = lambda *a: {"result": "new_unknown_result"}
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.engine.clock.advance(3601)
        result = self.check()
        self.assertTrue(result["unconfirmed"])
        self.assertIsNone(result["plan_id"])

    def test_a_corrupt_attempt_cannot_be_overwritten_by_a_new_claim(self):
        row = self.engine.store.account(self.key)
        name = self.engine.resets._journal_key(row)
        self.vault.put(name, {"revision": 1, "attempt": {"state": "settled"}}, generation=False)
        with self.assertRaises(DeckError):
            self.check()
        self.assertEqual(self.sent, [])

    def test_a_throttle_is_shared_with_usage_checks(self):
        def throttled(*args):
            raise DeckError("Wait", "rate_limited", 987)
        self.engine.cli.adapter.reset_status = throttled
        with self.assertRaises(DeckError):
            self.check()
        self.assertGreaterEqual(self.engine.store.account(self.key)["paused_until"], self.engine.clock() + 980)
        self.assertEqual(self.sent, [])

    def test_claim_batch_cannot_overrun_the_hourly_check_budget(self):
        plan = self.check()
        governor = self.engine.scheduler.governor
        for _ in range(13):
            governor.record(("claude_cli", self.saved["identity"]), self.engine.clock())
        self.assertEqual(self.use(plan)["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_a_recent_background_read_does_not_block_a_budgeted_manual_check(self):
        self.engine.scheduler.governor.record(("claude_cli", self.saved["identity"]), self.engine.clock())
        self.assertIsNotNone(self.check()["plan_id"])

    def test_a_definite_http_rejection_does_not_leave_an_uncertain_attempt(self):
        self.engine.cli.adapter.redeem_reset = mock.Mock(side_effect=ProviderError("rate_limited", http=429, retry_after=900))
        report = self.use(self.check())
        self.assertNotEqual(report["verified"], "unconfirmed")
        self.engine.clock.advance(3601)
        self.assertFalse(self.check()["unconfirmed"])

    def test_the_installed_database_shape_gets_forecast_metadata_on_reopen(self):
        path = self.engine.paths.data_dir / "accounts.sqlite3"
        self.engine.stop()
        db = sqlite3.connect(path)
        try:
            db.execute("ALTER TABLE readings DROP COLUMN segment")
            db.execute("DROP TABLE reset_boundaries")
            db.commit()
        finally:
            db.close()
        self.restart()
        store = self.engine.store
        previous = store.readings(self.key, "seven_day")
        self.assertTrue(previous)
        store.observe(self.key, {"windows": []}, reset=("migration", ["seven_day"], store.account(self.key)))
        self.assertEqual(store.readings(self.key, "seven_day"), [])
        self.assertTrue(store.history(self.key)["readings"])

    def test_new_forecasts_survive_reused_reading_ids_and_a_clock_rollback(self):
        store, now = self.engine.store, self.engine.clock()
        usage = copy.deepcopy(store.account(self.key)["last_good_usage"])
        store.observe(self.key, {"windows": []}, reset=("clock-rollback", ["seven_day"], store.account(self.key)))
        store.db.execute("DELETE FROM readings")
        store.db.commit()
        store.observe(self.key, usage, at=now - 3600)
        self.assertTrue(store.readings(self.key, "seven_day"))
        store.remove(self.key)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM reset_boundaries WHERE account=?", (self.key,)).fetchone()[0], 0)

    def test_a_reset_starts_only_affected_forecasts_and_keeps_history(self):
        self.raw["grants"][0]["clears"].append("seven_day_fable")
        from tests.support import shipped_reading
        e = self.engine
        before = shipped_reading(e.clock(), 25, 80, 90)
        before["windows"] = [w for w in before["windows"] if w["id"] != "five_hour"]
        before["windows"].append(dict(before["windows"][1], id="seven_day_opus", model="Opus", used=50))
        e.store.observe(self.key, before)
        after = copy.deepcopy(before)
        after["windows"][0]["used"] = 30
        after["windows"][1]["used"] = 0
        e.cli.adapter.usage = lambda c: after if self.sent else before
        self.assertEqual(self.use(self.check())["verified"], "verified")
        for wid in ("seven_day", "seven_day_fable"):
            self.assertEqual(len(e.store.readings(self.key, wid)), 1)
        self.assertGreater(len(e.store.readings(self.key, "seven_day_opus")), 1)
        count = e.store.db.execute("SELECT COUNT(*) FROM readings WHERE account=?", (self.key,)).fetchone()[0]
        self.assertGreater(count, 3)
        self.assertEqual(e.store.account(self.key)["last_good_usage"]["windows"][0]["reset"], before["windows"][0]["reset"])
        self.restart()
        self.assertEqual(len(self.engine.store.readings(self.key, "seven_day")), 1)

    def test_missing_usage_is_unconfirmed_and_never_invented(self):
        before = self.engine.store.account(self.key)["last_good_usage"]
        self.engine.cli.adapter.usage = lambda c: {"windows": [], "checked": self.engine.clock()} if self.sent else before
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        self.assertEqual(self.engine.store.account(self.key)["last_good_usage"], before)

    def journal_crash(self, number):
        save, calls = self.engine.resets._save, []
        def crash(*args):
            save(*args)
            calls.append(True)
            if len(calls) == number:
                raise Crash()
        with mock.patch.object(self.engine.resets, "_save", side_effect=crash):
            self.use(self.check())
        self.restart()
        self.assertFalse(self.check()["unconfirmed"])
        self.assertEqual(len(self.sent), 1)

    def test_crash_after_acceptance_is_reconciled_after_restart(self):
        self.journal_crash(2)

    def test_crash_after_settlement_stays_settled_after_restart(self):
        self.journal_crash(3)

    def test_a_failed_intent_write_never_sends_a_claim(self):
        with mock.patch.object(self.engine.resets, "_save", side_effect=OSError("Synthetic disk full")):
            self.assertEqual(self.use(self.check())["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_crash_after_atomic_verification_is_reconciled(self):
        observe, hit = self.engine.store.observe, []
        def crash(*args, **kwargs):
            observe(*args, **kwargs)
            if kwargs.get("reset"):
                hit.append(True)
                raise Crash()
        with mock.patch.object(self.engine.store, "observe", side_effect=crash):
            self.use(self.check())
        self.assertEqual(hit, [True])
        self.restart()
        self.assertFalse(self.check()["unconfirmed"])
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.engine.store.readings(self.key, "seven_day"))

    def test_an_inflight_old_read_finishes_before_the_claim_and_cannot_replace_its_usage(self):
        plan = self.check()
        e = self.engine
        old = copy.deepcopy(e.store.account(self.key)["last_good_usage"])
        fresh = copy.deepcopy(old)
        for window in fresh["windows"]:
            window["used"] = 5.0
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def read(credentials):
            calls.append(True)
            if len(calls) == 1:
                entered.set()
                if not release.wait(10):
                    raise AssertionError("The test did not release the old read")
                return old
            return fresh if self.sent else old
        e.cli.adapter.usage = read
        lock_for = e.scheduler.account_lock
        @contextlib.contextmanager
        def lock(key):
            if threading.current_thread().name == "ad2-ops":
                waiting.set()
            with lock_for(key):
                yield
        thread = threading.Thread(target=e.cli.read, args=(self.key,))
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            with mock.patch.object(e.scheduler, "account_lock", side_effect=lock):
                e.action("reset-use", {"id": self.key, "plan_id": plan["plan_id"]})
                self.assertTrue(waiting.wait(10))
                self.assertEqual(self.sent, [])
                release.set()
                self.assertTrue(e.runner.wait(10))
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(e.store.account(self.key)["last_good_usage"], fresh)
        self.assertEqual([r["used"] for r in e.store.readings(self.key, "seven_day")], [5.0])


class ResetOfferParsingTest(unittest.TestCase):
    def test_only_complete_eligible_unexpired_ultra_offers_are_actionable(self):
        good = {"eligible": True, "event_props": {"tier": "claude_max_20x"}, "next_grant_id": "demo",
                "grants": [{"id": "demo", "resets_left": 1, "gives_back_weekly": True,
                            "clears": ["seven_day_overage_included"], "paused": False,
                            "usable_now": True, "use_requires_limit": False}]}
        self.assertTrue(status(good, 100)["offers"][0]["usable"])
        for target, field, value in (("block", "eligible", False), ("block", "event_props", {}),
                ("block", "cooldown_until", 200), ("block", "next_grant_id", "other"),
                ("offer", "resets_left", 0), ("offer", "paused", True), ("offer", "usable_now", False),
                ("offer", "blocking", ["unknown"]), ("offer", "starts_at", 200),
                ("offer", "unavailable_reason", "unavailable"),
                ("offer", "ends_at", 99), ("offer", "ends_at", "bad"),
                ("offer", "clears", ["new_limit"]), ("offer", "use_requires_limit", True)):
            with self.subTest(field=field, value=value):
                data = copy.deepcopy(good)
                (data if target == "block" else data["grants"][0])[field] = value
                self.assertFalse(status(data, 100)["offers"][0]["usable"])

    def test_missing_duplicate_and_malformed_offers_are_unknown_not_zero(self):
        for value in (None, {}, {"eligible": True, "grants": None},
                      {"eligible": True, "grants": [{"id": "a", "resets_left": True}]},
                      {"eligible": True, "grants": [{"id": "a", "resets_left": 1}] * 2}):
            with self.subTest(value=value), self.assertRaises(DeckError):
                status(value, 100)

    def test_reset_transport_headers_are_confined_to_the_exact_endpoints(self):
        from accountdeck.providers import http
        from accountdeck.providers.claude_resets import CLIENT, STATUS_URL
        from accountdeck.config import SafetyViolation
        headers = http.request_headers(STATUS_URL, token="synthetic", reset_api=True)
        self.assertEqual(headers["User-Agent"], CLIENT)
        self.assertEqual(headers["Authorization"], "Bearer synthetic")
        self.assertNotEqual(http.request_headers(STATUS_URL)["User-Agent"], CLIENT)
        for url in (STATUS_URL + "&extra=true", "https://example.test/", "https://api.anthropic.com/api/oauth/token"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                http.request_headers(url, reset_api=True)
        with self.assertRaises(ValueError):
            http.request_headers(STATUS_URL, reset_api=True, cookie="synthetic")
        with self.assertRaises(SafetyViolation):
            http.request("GET", STATUS_URL, reset_api=True)

    def test_provider_claim_sends_only_the_selected_grant_once(self):
        from accountdeck.providers.claude_cli import ClaudeCLI
        adapter = mock.Mock()
        adapter.http.return_value = ({"result": "reset"}, {})
        credentials = {"claudeAiOauth": {"accessToken": "synthetic", "refreshToken": "synthetic-refresh"}}
        org = "00000000-0000-0000-0000-000000000001"
        request = "00000000-0000-0000-0000-000000000002"
        self.assertEqual(ClaudeCLI.redeem_reset(adapter, credentials, org, "demo-grant", request), {"result": "reset"})
        adapter.http.assert_called_once_with("POST", f"https://api.anthropic.com/api/organizations/{org}/reset_rate_limits",
                                            token="synthetic", reset_api=True, timeout=25,
                                            json_body={"program": "cedar_ember", "grant_id": "demo-grant", "request_id": request})
