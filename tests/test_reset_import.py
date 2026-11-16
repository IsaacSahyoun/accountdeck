"""Import retries and creation collisions beside manual reset checks."""
import copy
import dataclasses
import json
from pathlib import Path
from unittest import mock

from accountdeck import move_import
from accountdeck.core import DeckError
from accountdeck.usage import claude_usage
from tests import test_claude_resets, test_reset_closure


class ResetImportTest(test_claude_resets.UltraResetCase):
    import_old_account = test_reset_closure.ResetClosureTest.import_old_account
    pair = test_reset_closure.ResetClosureTest.pair
    fail_final_save = test_reset_closure.ResetClosureTest.fail_final_save
    verified_before_removal = test_reset_closure.ResetClosureTest.verified_before_removal
    imported_last_good = test_reset_closure.ResetClosureTest.imported_last_good

    def test_busy_listing_can_retry_the_same_bundle(self):
        row = self.engine.store.account(self.key)
        before = self.vault.get(self.key)
        lock = self.engine.scheduler.account_lock(self.key)
        with lock, mock.patch.object(self.engine, "timings", dataclasses.replace(self.engine.timings, lock_wait=0)):
            result = self.import_old_account(row, on_pc=True, allow_failures=True)
        self.assertTrue(result["failures"])
        self.assertIn("same bundle", result["message"])
        self.assertNotIn("Cancel move", result["message"])
        self.assertIn("same bundle", self.engine.snapshot()["report"]["summary"])
        self.assertIn("using this account", result["failures"][0]["reason"])
        self.assertEqual(self.vault.get(self.key), before)
        path = Path(self.paths.temp_root) / "old-reset-account.adeck"
        sealed = path.read_bytes()
        with mock.patch.object(move_import, "MIN_SCRYPT_N", 1024):
            retry = self.engine.action("import-bundle", {
                "path": str(path), "passphrase": "synthetic reset history test passphrase"})["result"]
        self.assertFalse(retry["failures"])
        self.assertEqual(retry["bundle_id"], result["bundle_id"])
        self.assertEqual(path.read_bytes(), sealed)

    def test_structured_provider_suffixes_stay_unknown(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "seven_day_opus"])
        def usage(used):
            return claude_usage({"seven_day": {"utilization": used}, "seven_day_opus": {"utilization": used},
                                 "limits": [{"kind": "other", "id": key, "percent": value}
                                            for key, value in (("seven_day_2", 20), ("seven_day_opus_4", None))]})
        self.pair(usage(80), usage(1))
        plan = self.check()
        self.assertIsNotNone(plan["plan_id"])
        self.assertEqual(self.use(plan)["verified"], "verified")

    def test_import_decodes_only_snapshots_at_candidate_times(self):
        store, now = self.engine.store, self.engine.clock()
        unrelated = json.dumps({"windows": [], "probe": "unrelated snapshot"})
        store.db.executemany("INSERT INTO snapshots(account, at, usage) VALUES(?, ?, ?)",
                             [(self.key, now - index - 10, unrelated) for index in range(1000)])
        store.db.commit()
        loads, decoded = json.loads, []
        def observe(value, *args, **kwargs):
            if value == unrelated:
                decoded.append(value)
            return loads(value, *args, **kwargs)
        empty = {"history": {"last_good_usage": None, "last_good_at": None, "snapshots": [], "readings": []},
                 "refills": []}
        one = copy.deepcopy(empty)
        one["history"]["snapshots"] = [{"at": now, "usage": {"windows": [], "probe": "candidate"}}]
        existing = store.db.execute("SELECT COUNT(*) FROM snapshots WHERE account=? AND at=?", (self.key, now)).fetchone()[0]
        with mock.patch.object(move_import.json, "loads", side_effect=observe):
            move_import._copy_history(store, self.key, empty, now)
            move_import._copy_history(store, self.key, one, now)
        self.assertEqual(len(decoded), 0)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM snapshots WHERE account=? AND at=?",
                                          (self.key, now)).fetchone()[0], existing + 1)

    def test_import_never_deletes_a_concurrently_captured_account(self):
        row = self.engine.store.account(self.key)
        self.engine.action("remove", {"id": self.key})
        self.assertTrue(self.engine.runner.wait(15))
        captured, save = [], self.engine.store.save_account
        real_login = dict(copy.deepcopy(self.saved), email="captured@example.test", plan="captured-plan")
        def capture_first(provider, identity, label, *args, **kwargs):
            if provider == "claude_cli" and identity == row["identity"] and kwargs.get("owner") == "on_pc" and not captured:
                key = save(provider, identity, "Captured locally", real_login["email"], real_login["plan"], row["org"])
                self.vault.put(key, real_login)
                self.engine.store.update(key, flags={"restorable": True})
                captured.append(key)
            return save(provider, identity, label, *args, **kwargs)
        self.saved = dict(self.saved, identity="invalid-bundle-identity")
        with mock.patch.object(self.engine.store, "save_account", side_effect=capture_first):
            result = self.import_old_account(row, allow_failures=True)
        self.assertTrue(result["failures"])
        self.assertEqual(len(captured), 1)
        kept = self.engine.store.account(captured[0])
        self.assertIsNotNone(kept)
        self.assertEqual(kept["owner"], "v2")
        self.assertTrue(kept["flags"]["restorable"])
        self.assertEqual((kept["email"], kept["plan"]), (real_login["email"], real_login["plan"]))
        self.assertEqual(self.vault.get(captured[0]), real_login)

    def test_snapshot_batches_keep_every_candidate_and_deduplicate_retries(self):
        store, now = self.engine.store, self.engine.clock()
        samples = [{"at": now - index, "usage": {"windows": [], "candidate": index}} for index in range(501)]
        account = {"history": {"last_good_usage": None, "last_good_at": None, "snapshots": samples, "readings": []},
                   "refills": []}
        move_import._copy_history(store, self.key, account, now)
        move_import._copy_history(store, self.key, account, now)
        kept = [json.loads(row[0]) for row in store.db.execute("SELECT usage FROM snapshots WHERE account=?", (self.key,))]
        self.assertEqual(sorted(item["candidate"] for item in kept if "candidate" in item), list(range(501)))

    def capture_beside_placeholder(self, provider):
        key = self.key if provider == "claude_cli" else self.add_codex_account() if provider == "codex" else self.spare(provider)["id"]
        row = self.engine.store.account(key)
        profile = self.vault.get(row["id"])
        info = None if provider == "claude_cli" else self.engine.providers[provider].identity(profile)
        self.engine.action("remove", {"id": row["id"]})
        self.assertTrue(self.engine.runner.wait(15))
        self.assertIsNone(self.engine.store.account(row["id"]))
        created, save = [], self.engine.store.save_account
        def import_first(kind, identity, label, *args, **kwargs):
            if kind == provider and identity == row["identity"] and not created:
                created.append(save(kind, identity, "Import placeholder", row["email"], row["plan"], row["org"],
                                    owner="on_pc", imported_from="pc"))
            return save(kind, identity, label, *args, **kwargs)
        with mock.patch.object(self.engine.store, "save_account", side_effect=import_first):
            with self.assertRaises(DeckError) as refused:
                if provider == "claude_cli":
                    self.engine.cli.save(profile)
                else:
                    self.engine.accounts.save(provider, profile, info)
        self.assertEqual(refused.exception.code, "busy")
        self.assertEqual(self.engine.store.account(created[0])["owner"], "on_pc")
        with self.assertRaises(DeckError) as missing:
            self.vault.get(created[0])
        self.assertEqual(missing.exception.code, "vault")

    def test_claude_capture_cannot_take_an_import_placeholder(self):
        self.capture_beside_placeholder("claude_cli")

    def test_codex_capture_cannot_take_an_import_placeholder(self):
        self.capture_beside_placeholder("codex")

    def test_muse_capture_cannot_take_an_import_placeholder(self):
        self.capture_beside_placeholder("muse")

    def test_history_failure_keeps_a_newly_saved_login(self):
        row = self.engine.store.account(self.key)
        self.engine.action("remove", {"id": self.key})
        self.assertTrue(self.engine.runner.wait(15))
        notes, note = [], move_import.ImportBundle.note
        def capture(operation, detail, **kwargs):
            notes.append(detail)
            return note(operation, detail, **kwargs)
        with mock.patch.object(move_import, "_copy_history", side_effect=OSError("synthetic history failure")), \
                mock.patch.object(move_import.ImportBundle, "note", capture):
            result = self.import_old_account(row, allow_failures=True)
        self.assertIn("history could not be copied", result["failures"][0]["reason"])
        kept = self.engine.store.find("claude_cli", row["identity"])
        self.assertIsNotNone(kept)
        self.assertTrue(kept["flags"]["restorable"])
        self.assertEqual(self.vault.get(kept["id"])["credentials"], self.saved["credentials"])
        self.assertIn("same bundle", result["message"])
        self.assertNotIn("Not imported", " ".join(notes))
        self.assertIn("sign-in was saved", " ".join(notes))
        path = Path(self.paths.temp_root) / "old-reset-account.adeck"
        sealed = path.read_bytes()
        with mock.patch.object(move_import, "MIN_SCRYPT_N", 1024):
            retry = self.engine.action("import-bundle", {
                "path": str(path), "passphrase": "synthetic reset history test passphrase"})["result"]
        self.assertFalse(retry["failures"])
        self.assertEqual(retry["bundle_id"], result["bundle_id"])
        self.assertEqual(path.read_bytes(), sealed)

    def unknown_organization_import(self, *, on_pc=False, proof=True):
        request = self.verified_before_removal() if proof else None
        row = self.engine.store.account(self.key)
        self.engine.action("remove", {"id": self.key})
        self.assertTrue(self.engine.runner.wait(15))
        row["org"] = self.saved["organizationUuid"] = ""
        self.saved["oauthAccount"]["organizationUuid"] = ""
        usage, at = self.imported_last_good()
        self.import_old_account(row, usage=usage, at=at, on_pc=on_pc)
        current = self.engine.store.account(self.key)
        if proof:
            self.assertIsNone(current["last_good_usage"])
            self.assertEqual(self.engine.store.readings(self.key, "seven_day"), [])
            self.assertIn({"at": at, "usage": usage}, self.engine.store.history(self.key)["snapshots"])
            self.assertFalse(self.engine.store.reset_verified(self.key, request))
        else:
            self.assertEqual(current["last_good_usage"], usage)
            self.assertTrue(self.engine.store.readings(self.key, "seven_day"))

    def test_unknown_organization_cannot_promote_usage_after_identity_proof(self):
        self.unknown_organization_import()

    def test_unknown_organization_listing_archives_usage_after_identity_proof(self):
        self.unknown_organization_import(on_pc=True)

    def test_unknown_organization_without_proof_keeps_normal_import_behavior(self):
        self.unknown_organization_import(proof=False)

    def test_unknown_name_recorded_after_reset_does_not_block_the_next_offer(self):
        self.raw["grants"][0].update(gives_back_weekly=False, clears=["seven_day", "seven_day_overage_included"])
        self.pair(claude_usage({"seven_day": {"utilization": 80}}),
                  claude_usage({"seven_day": {"utilization": 1}, "seven_day_overage_included_2": {"utilization": 20}}))
        self.assertEqual(self.use(self.check())["verified"], "verified")
        self.raw["grants"][0].update(id="synthetic-next", resets_left=1, usable_now=True)
        self.raw["next_grant_id"] = "synthetic-next"
        self.assertIsNotNone(self.check()["plan_id"])
        self.assertEqual([r["used"] for r in self.engine.store.readings(self.key, "seven_day_overage_included_2")], [20])
