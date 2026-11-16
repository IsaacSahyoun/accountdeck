"""Reset proof stays with its account and imported evidence remains historical."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from accountdeck import move_import
from accountdeck.core import DeckError
from accountdeck.usage import claude_usage
from tests import test_claude_resets, test_reset_closure


class ResetBoundaryTest(test_claude_resets.UltraResetCase):
    pair = test_reset_closure.ResetClosureTest.pair
    fail_final_save = test_reset_closure.ResetClosureTest.fail_final_save
    verified_before_removal = test_reset_closure.ResetClosureTest.verified_before_removal
    import_old_account = test_reset_closure.ResetClosureTest.import_old_account
    imported_last_good = test_reset_closure.ResetClosureTest.imported_last_good
    cowork_offer = test_reset_closure.ResetClosureTest.cowork_offer
    cowork_usage = test_reset_closure.ResetClosureTest.cowork_usage
    retired_usage = test_reset_closure.ResetClosureTest.retired_usage

    def test_hidden_raw_cowork_alias_cannot_hide_beside_a_canonical_counter(self):
        self.cowork_offer()
        saved = claude_usage({"seven_day": {"utilization": 80}, "seven_day_cowork": {"utilization": 20},
                              "cowork": {"utilization": 0}})
        saved["unstarted_windows"] = []
        self.engine.store.observe(self.key, saved)
        self.engine.cli.adapter.usage = lambda _: self.cowork_usage(0 if self.sent else 80)
        self.assertIsNone(self.check()["plan_id"])
        self.assertEqual(self.sent, [])

    def test_suffixed_cowork_history_gets_the_same_reset_boundary(self):
        self.cowork_offer()
        old = claude_usage({"seven_day": {"utilization": 80}, "limits": [
            {"kind": "other", "id": "cowork", "percent": n} for n in (20, 30)]})
        self.engine.store.observe(self.key, old)
        self.pair(self.cowork_usage(80), self.cowork_usage(1))
        self.assertEqual(self.use(self.check())["verified"], "verified")
        self.assertEqual(self.engine.store.readings(self.key, "cowork"), [])
        self.assertEqual(self.engine.store.readings(self.key, "cowork_2"), [])

    def test_canonical_readings_reset_a_prior_named_cowork_series(self):
        self.cowork_offer()
        self.engine.store.observe(self.key, self.cowork_usage(60, alias=True))
        self.pair(self.cowork_usage(80), self.cowork_usage(1))
        self.assertEqual(self.use(self.check())["verified"], "verified")
        self.assertEqual(self.engine.store.readings(self.key, "cowork"), [])

    def duplicate_cowork(self, used):
        return claude_usage({"seven_day": {"utilization": used},
                             "seven_day_cowork": {"utilization": used}, "cowork": {"utilization": used / 2}})

    def test_real_named_alias_duplicates_block_cached_admission(self):
        self.cowork_offer()
        self.pair(self.duplicate_cowork(80), self.cowork_usage(1))
        self.assertIsNone(self.check()["plan_id"])
        self.assertEqual(self.sent, [])

    def test_real_named_alias_duplicates_block_fresh_admission(self):
        self.cowork_offer()
        self.pair(self.cowork_usage(80), self.cowork_usage(1))
        plan = self.check()
        self.engine.cli.adapter.usage = lambda _: self.duplicate_cowork(80)
        self.assertEqual(self.use(plan)["outcome"], "failed")
        self.assertEqual(self.sent, [])

    def test_real_named_alias_duplicates_block_verification_until_clean(self):
        self.cowork_offer()
        self.pair(self.cowork_usage(80), self.duplicate_cowork(2))
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        row = self.engine.store.account(self.key)
        request = self.engine.resets._journal(row)["attempt"]["request"]
        self.assertFalse(self.engine.store.reset_verified(self.key, request))
        self.engine.cli.adapter.usage = lambda _: self.cowork_usage(1)
        self.assertFalse(self.check()["unconfirmed"])
        self.assertTrue(self.engine.store.reset_verified(self.key, request))
        self.assertEqual(len(self.sent), 1)

    def test_retired_named_number_stays_out_of_weekly_verification(self):
        self.retired_usage({"five_hour": {"utilization": 30}})

    def structured_cowork_alias(self, stage, value):
        self.cowork_offer()
        self.pair(self.cowork_usage(80), self.cowork_usage(1))
        def ambiguous(used):
            return claude_usage({"seven_day": {"utilization": used}, "seven_day_cowork": {"utilization": used},
                                 "limits": [{"kind": "other", "id": "cowork", "percent": value}]})
        if stage == "cached":
            self.engine.store.observe(self.key, ambiguous(80))
            self.assertIsNone(self.check()["plan_id"])
            self.assertEqual(self.sent, [])
            return
        plan = self.check()
        self.engine.cli.adapter.usage = lambda _: ambiguous(1 if self.sent else 80) if stage == "fresh" or self.sent else self.cowork_usage(80)
        report = self.use(plan)
        if stage == "fresh":
            self.assertEqual(report["outcome"], "failed")
            self.assertEqual(self.sent, [])
        else:
            self.assertEqual(report["verified"], "unconfirmed")
            self.assertEqual(len(self.sent), 1)
            row = self.engine.store.account(self.key)
            request = self.engine.resets._journal(row)["attempt"]["request"]
            self.assertFalse(self.engine.store.reset_verified(self.key, request))

    def test_structured_cowork_null_blocks_cached_admission(self):
        self.structured_cowork_alias("cached", None)

    def test_structured_cowork_null_blocks_fresh_admission(self):
        self.structured_cowork_alias("fresh", None)

    def test_structured_cowork_null_blocks_verification(self):
        self.structured_cowork_alias("result", None)

    def test_structured_cowork_number_blocks_cached_admission(self):
        self.structured_cowork_alias("cached", 20)

    def test_structured_cowork_number_blocks_fresh_admission(self):
        self.structured_cowork_alias("fresh", 20)

    def test_structured_cowork_number_blocks_verification(self):
        self.structured_cowork_alias("result", 0)

    def unknown_named_fields(self, stage, value=None):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "seven_day_opus", "seven_day_cowork"])
        def usage(used, unknown=False):
            payload = {key: {"utilization": used} for key in ("seven_day", "seven_day_opus", "seven_day_cowork")}
            if unknown:
                payload.update({key: value for key in ("seven_day_2", "seven_day_opus_4", "seven_day_cowork_3")})
            return claude_usage(payload)
        self.pair(usage(80, stage == "cached"), usage(1, stage == "result"))
        plan = self.check()
        self.assertIsNotNone(plan["plan_id"])
        if stage == "fresh":
            self.engine.cli.adapter.usage = lambda _: usage(1 if self.sent else 80, True)
        self.assertEqual(self.use(plan)["verified"], "verified")
        self.assertEqual(len(self.sent), 1)

    def test_unknown_named_suffixes_do_not_block_cached_admission(self):
        self.unknown_named_fields("cached")

    def test_unknown_named_suffixes_do_not_block_fresh_admission(self):
        self.unknown_named_fields("fresh")

    def test_unknown_named_suffixes_do_not_block_verification(self):
        self.unknown_named_fields("result")

    def test_numeric_unknown_named_suffixes_are_not_display_ids(self):
        self.unknown_named_fields("cached", {"utilization": 20})
        for key in ("seven_day_2", "seven_day_opus_4", "seven_day_cowork_3"):
            self.assertEqual([row["used"] for row in self.engine.store.readings(self.key, key)], [20])

    def test_unknown_fresh_named_suffix_cannot_add_a_cleared_limit(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "seven_day_overage_included"])
        before = claude_usage({"seven_day": {"utilization": 80}})
        self.pair(before, before)
        plan = self.check()
        self.engine.cli.adapter.usage = lambda _: claude_usage({
            "seven_day": {"utilization": 1 if self.sent else 80},
            "seven_day_overage_included_2": {"utilization": 20}})
        self.assertEqual(self.use(plan)["verified"], "verified")
        self.assertEqual(len(self.sent), 1)

    def archived_snapshot(self, collision):
        self.verified_before_removal()
        row = self.engine.store.account(self.key)
        self.engine.clock.advance(120)
        usage, at = self.imported_last_good()
        snapshots = []
        if collision == "empty":
            snapshots = [{"at": at, "usage": None}]
        elif collision == "local":
            self.engine.store.observe(self.key, self.cowork_usage(3), at=at)
            self.engine.store.db.execute("INSERT INTO snapshots(account, at, usage) VALUES(?, ?, ?)",
                                         (self.key, at, '{"windows":[]}'))
            self.engine.store.db.commit()
        self.import_old_account(row, usage=usage, at=at, snapshots=snapshots)
        self.assertIn({"at": at, "usage": usage}, self.engine.store.history(self.key)["snapshots"])
        self.assertNotEqual(self.engine.store.account(self.key)["last_good_usage"], usage)
        self.import_old_account(row, usage=usage, at=at, snapshots=snapshots)
        self.assertEqual(self.engine.store.history(self.key)["snapshots"].count({"at": at, "usage": usage}), 1)

    def test_empty_snapshot_does_not_discard_imported_last_good(self):
        self.archived_snapshot("empty")

    def test_different_local_snapshot_does_not_discard_imported_last_good(self):
        self.archived_snapshot("local")

    def pending_week(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day"])
        before = claude_usage({"seven_day": {"utilization": 100}})
        after = copy.deepcopy(before)
        self.pair(before, after)
        self.assertEqual(self.use(self.check())["verified"], "unconfirmed")
        row = self.engine.store.account(self.key)
        request = self.engine.resets._journal(row)["attempt"]["request"]
        after.update(claude_usage({"seven_day": {"utilization": 0}}))
        return row, request, after

    def test_conflicting_import_is_refused_while_a_reset_check_is_held(self):
        row, request, after = self.pending_week()
        usage_entered, release_usage = (threading.Event() for _ in range(2))
        self.addCleanup(release_usage.set)
        def held_usage(_):
            usage_entered.set()
            if not release_usage.wait(10):
                raise AssertionError("The test did not release the held usage read")
            return after
        other = dict(row, org="11111111-1111-4111-8111-111111111111")
        incoming = copy.deepcopy(self.saved)
        incoming["organizationUuid"] = other["org"]
        if isinstance(incoming.get("oauthAccount"), dict):
            incoming["oauthAccount"]["organizationUuid"] = other["org"]
        with mock.patch.object(self.engine.cli.adapter, "usage", side_effect=held_usage), \
                mock.patch.object(self, "saved", incoming), \
                ThreadPoolExecutor(max_workers=2) as pool:
            checking = pool.submit(self.check)
            try:
                self.assertTrue(usage_entered.wait(10))
                importing = pool.submit(self.import_old_account, other, allow_failures=True)
                with self.assertRaises(DeckError):
                    importing.result(timeout=10)
                during = self.engine.store.account(self.key)["org"]
            finally:
                release_usage.set()
            self.assertFalse(checking.result(timeout=10)["unconfirmed"])
        self.assertEqual(during, row["org"])
        self.assertEqual(self.engine.store.account(self.key)["org"], row["org"])
        self.assertEqual(self.vault.get(self.key)["organizationUuid"], row["org"])
        proofs = self.engine.store.db.execute("SELECT org FROM reset_proofs WHERE request=?", (request,)).fetchall()
        self.assertEqual([p[0] for p in proofs], [row["org"]])

    def test_verification_transaction_refuses_a_changed_organization(self):
        row, request, after = self.pending_week()
        def changed(_):
            self.engine.store.update(self.key, org="11111111-1111-4111-8111-111111111111")
            return after
        with mock.patch.object(self.engine.cli.adapter, "usage", side_effect=changed):
            with self.assertRaises(DeckError) as refused:
                self.check()
        self.assertEqual(refused.exception.code, "identity")
        self.assertEqual(self.engine.store.db.execute("SELECT COUNT(*) FROM reset_proofs WHERE request=?", (request,)).fetchone()[0], 0)
        self.assertEqual(self.engine.store.account(self.key)["last_good_usage"], row["last_good_usage"])

    def test_import_keeps_account_lock_through_history(self):
        held = []
        copy_history = move_import._copy_history
        def history(store, key, account, now):
            def probe():
                lock = self.engine.scheduler.account_lock(key)
                acquired = lock.acquire(blocking=False)
                if acquired:
                    lock.release()
                return not acquired
            with ThreadPoolExecutor(max_workers=1) as pool:
                held.append(pool.submit(probe).result(timeout=5))
            return copy_history(store, key, account, now)
        with mock.patch.object(move_import, "_copy_history", side_effect=history):
            self.import_old_account(self.engine.store.account(self.key))
        self.assertEqual(held, [True])

    def test_last_good_import_cannot_overwrite_a_concurrent_local_read(self):
        store = self.engine.store
        now = self.engine.clock()
        imported, fresh = self.cowork_usage(60), self.cowork_usage(10)
        store.observe(self.key, self.cowork_usage(40), at=now - 120)
        account = {"history": {"last_good_usage": imported, "last_good_at": now - 60,
                               "readings": [], "snapshots": []}, "refills": []}
        checked, writer_attempted = threading.Event(), threading.Event()
        self.addCleanup(writer_attempted.set)
        read_account = store.account
        def held_check(key):
            row = read_account(key)
            checked.set()
            if not writer_attempted.wait(10):
                raise AssertionError("The local writer did not reach the held import")
            return row
        def local_read():
            acquired = store.lock.acquire(blocking=False)
            if acquired:
                try:
                    store.observe(self.key, fresh, at=now)
                finally:
                    store.lock.release()
                    writer_attempted.set()
            else:
                writer_attempted.set()
                store.observe(self.key, fresh, at=now)
        with mock.patch.object(store, "account", side_effect=held_check), ThreadPoolExecutor(max_workers=2) as pool:
            importing = pool.submit(move_import._copy_history, store, self.key, account, now)
            try:
                self.assertTrue(checked.wait(10))
                reading = pool.submit(local_read)
                importing.result(timeout=10)
                reading.result(timeout=10)
            finally:
                writer_attempted.set()
        self.assertEqual(store.account(self.key)["last_good_usage"], fresh)
        self.assertEqual(store.account(self.key)["last_good_at"], now)

    def test_on_pc_proved_history_waits_for_signin_then_a_local_read(self):
        self.verified_before_removal()
        row = self.engine.store.account(self.key)
        self.engine.action("remove", {"id": self.key})
        self.assertTrue(self.engine.runner.wait(15))
        usage, at = self.imported_last_good()
        notes = []
        note = move_import.ImportBundle.note
        def capture(operation, detail, **kwargs):
            notes.append(detail)
            return note(operation, detail, **kwargs)
        with mock.patch.object(move_import.ImportBundle, "note", capture):
            self.import_old_account(row, usage=usage, at=at, on_pc=True)
        self.assertEqual(self.engine.store.account(self.key)["owner"], "on_pc")
        self.assertIsNone(self.engine.store.account(self.key)["last_good_usage"])
        self.assertIn({"at": at, "usage": usage}, self.engine.store.history(self.key)["snapshots"])
        self.assertNotIn(self.key, [t.id for t in self.engine.targets()])
        self.assertIn("sign-in is needed before this Mac can refresh usage", " ".join(notes))
        self.import_old_account(row, usage=usage, at=at, kind="handover")
        self.assertEqual(self.engine.store.account(self.key)["owner"], "v2")
        self.assertIsNone(self.engine.store.account(self.key)["last_good_usage"])
        self.assertIn(self.key, [t.id for t in self.engine.targets()])
        fresh = self.cowork_usage(1)
        self.engine.cli.adapter.usage = lambda _: fresh
        self.engine.cli.read(self.key)
        self.assertEqual(self.engine.store.account(self.key)["last_good_usage"], fresh)

    def test_verification_owner_mismatch_rolls_back_every_write(self):
        store = self.engine.store
        owner = store.account(self.key)
        before = store.history(self.key)
        for field, changed in (("provider", "codex"), ("identity", "another-account"), ("org", "another-org")):
            with self.subTest(field=field):
                store.db.execute(f"UPDATE accounts SET {field}=? WHERE id=?", (changed, self.key))
                store.db.commit()
                try:
                    with self.assertRaises(DeckError) as refused:
                        store.observe(self.key, self.cowork_usage(1), reset=("owner-mismatch", ["seven_day"], owner))
                    self.assertEqual(refused.exception.code, "identity")
                    self.assertEqual(store.account(self.key)["last_good_usage"], owner["last_good_usage"])
                    self.assertEqual(store.history(self.key), before)
                    self.assertEqual(store.db.execute("SELECT COUNT(*) FROM reset_proofs").fetchone()[0], 0)
                    self.assertEqual(store.db.execute("SELECT COUNT(*) FROM reset_attempts").fetchone()[0], 0)
                finally:
                    store.db.execute(f"UPDATE accounts SET {field}=? WHERE id=?", (owner[field], self.key))
                    store.db.commit()
