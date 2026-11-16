"""Move previews, unknown-organization listings and exact-bundle retries."""
import copy
import dataclasses
import threading
from pathlib import Path
from unittest import mock

from accountdeck import move_bundle, move_import
from accountdeck.core import DeckError
from tests import test_claude_resets, test_reset_closure
from tests.test_dashboard import DashboardTestCase, SHEET, button, serve


class ResetHandoverTest(test_claude_resets.UltraResetCase):
    pair = test_reset_closure.ResetClosureTest.pair
    fail_final_save = test_reset_closure.ResetClosureTest.fail_final_save
    verified_before_removal = test_reset_closure.ResetClosureTest.verified_before_removal
    imported_last_good = test_reset_closure.ResetClosureTest.imported_last_good

    def account(self, *, listing=False):
        row = self.engine.store.account(self.key)
        usage, at = self.imported_last_good()
        account = {"id": "pc-account", "provider": "claude_cli", "identity": row["identity"],
                   "label": row["label"], "email": row["email"], "plan": row["plan"], "org": row["org"],
                   "created": at - 3600, "live_on_pc": listing, "refills": [],
                   "history": {"last_good_usage": usage, "last_good_at": at, "readings": [], "snapshots": []}}
        if not listing:
            account["secret"] = {"type": move_bundle.SECRET_TYPES["claude_cli"], "data": copy.deepcopy(self.saved)}
        return account

    def bundle(self, accounts, *, kind="main", settings=None):
        path = Path(self.paths.temp_root) / f"retry-{kind}.adeck"
        words = "synthetic exact bundle retry passphrase"
        payload = {"format": 1, "source_platform": "windows", "settings": settings or {}, "accounts": accounts}
        path.write_bytes(move_bundle.seal(payload, words, kind, scrypt_n=1024, min_scrypt_n=1024))
        return {"path": str(path), "passphrase": words}

    def action(self, name, body):
        with mock.patch.object(move_import, "MIN_SCRYPT_N", 1024):
            result = self.engine.action(name, body)
        if name == "import-bundle":
            self.assertTrue(self.engine.runner.wait(15))
        return result

    def placeholder(self, *, proof=False, unknown=True):
        request = self.verified_before_removal() if proof else None
        account = self.account()
        self.engine.action("remove", {"id": self.key})
        self.assertTrue(self.engine.runner.wait(15))
        listing = copy.deepcopy(account)
        listing.pop("secret")
        listing["live_on_pc"] = True
        if unknown:
            listing["org"] = ""
        result = self.action("import-bundle", self.bundle([listing]))["result"]
        self.assertFalse(result["failures"])
        self.key = self.engine.store.find("claude_cli", account["identity"])["id"]
        self.assertFalse(self.vault.path(self.key).exists())
        return account, request

    def fill_unknown(self, *, proof):
        account, request = self.placeholder(proof=proof)
        body = self.bundle([account], kind="handover")
        preview = self.action("import-bundle-preview", body)["preview"]
        self.assertFalse(preview["refused"])
        self.assertEqual(preview["counts"]["fill"], 1)
        result = self.action("import-bundle", body)["result"]
        self.assertFalse(result["failures"])
        row = self.engine.store.account(self.key)
        self.assertEqual((row["owner"], row["org"]), ("v2", account["org"]))
        self.assertEqual(self.vault.get(self.key)["organizationUuid"], account["org"])
        if proof:
            self.assertTrue(self.engine.store.reset_verified(self.key, request))
            self.assertIsNone(row["last_good_usage"])
        fresh = test_reset_closure.ResetClosureTest.cowork_usage(self, 1)
        self.engine.cli.adapter.usage = lambda _: fresh
        self.engine.cli.read(self.key)
        self.assertEqual(self.engine.store.account(self.key)["last_good_usage"], fresh)

    def test_unknown_listing_accepts_its_known_handover(self):
        self.fill_unknown(proof=False)

    def test_unknown_listing_with_proof_accepts_its_known_handover(self):
        self.fill_unknown(proof=True)

    def test_unknown_placeholder_with_a_saved_login_is_not_adopted(self):
        account, _ = self.placeholder()
        previous = copy.deepcopy(self.saved)
        previous["organizationUuid"] = ""
        self.vault.put(self.key, previous)
        body = self.bundle([account], kind="handover")
        preview = self.action("import-bundle-preview", body)["preview"]
        self.assertTrue(preview["refused"])
        with self.assertRaises(DeckError):
            self.action("import-bundle", body)
        self.assertEqual(self.vault.get(self.key), previous)
        self.assertEqual(self.engine.store.account(self.key)["org"], "")

    def test_known_placeholder_does_not_adopt_another_organization(self):
        account, _ = self.placeholder(unknown=False)
        account["org"] = "11111111-1111-4111-8111-111111111111"
        preview = self.action("import-bundle-preview", self.bundle([account], kind="handover"))["preview"]
        self.assertTrue(preview["refused"])

    def test_unknown_local_row_can_recover_its_known_login(self):
        account, _ = self.placeholder()
        self.engine.store.update(self.key, owner="v2")
        body = self.bundle([account])
        self.assertFalse(self.action("import-bundle-preview", body)["preview"]["refused"])
        self.assertFalse(self.action("import-bundle", body)["result"]["failures"])
        self.assertEqual(self.engine.store.account(self.key)["org"], account["org"])

    def test_import_rechecks_organization_after_admission(self):
        account = self.account(listing=True)
        body = self.bundle([account])
        self.assertFalse(self.action("import-bundle-preview", body)["preview"]["refused"])
        before = self.vault.get(self.key)
        save = move_import._Importer._save
        def changed(importer, incoming):
            self.engine.store.update(self.key, org="11111111-1111-4111-8111-111111111111")
            return save(importer, incoming)
        with mock.patch.object(move_import._Importer, "_save", changed):
            result = self.action("import-bundle", body)["result"]
        self.assertTrue(result["failures"])
        self.assertEqual(self.vault.get(self.key), before)

    def test_valid_import_waits_before_changing_metadata(self):
        account = self.account()
        record = account["secret"]["data"]
        record.update(email="newer@example.test", plan="Newer plan", refreshed=self.engine.clock() + 100)
        record["credentials"]["claudeAiOauth"]["accessToken"] += "-newer"
        account.update(email=record["email"], plan=record["plan"])
        body = dict(self.bundle([account]), wait=False)
        lock = self.engine.scheduler.account_lock(self.key)
        started, held = threading.Event(), []
        save, update = move_import._Importer._save, self.engine.store.update
        def entering(importer, incoming):
            started.set()
            return save(importer, incoming)
        def updating(key, **fields):
            if key == self.key and "email" in fields:
                held.append(lock._is_owned())
            return update(key, **fields)
        before = self.engine.store.account(self.key)
        with mock.patch.object(move_import._Importer, "_save", entering), \
                mock.patch.object(self.engine.store, "update", updating):
            with lock, mock.patch.object(move_import, "MIN_SCRYPT_N", 1024):
                self.engine.action("import-bundle", body)
                self.assertTrue(started.wait(10))
                self.assertEqual(self.engine.store.account(self.key), before)
            self.assertTrue(self.engine.runner.wait(15))
        after = self.engine.store.account(self.key)
        self.assertEqual((after["email"], after["plan"]), (record["email"], record["plan"]))
        self.assertEqual(held, [True])

    def test_failed_handover_write_leaves_unknown_placeholder_retryable(self):
        account, _ = self.placeholder()
        body = self.bundle([account], kind="handover")
        put = self.vault.put
        def fail_account(key, value, **kwargs):
            if key == self.key:
                raise OSError("synthetic account write failure")
            return put(key, value, **kwargs)
        with mock.patch.object(self.vault, "put", side_effect=fail_account):
            result = self.action("import-bundle", body)["result"]
        self.assertTrue(result["failures"])
        self.assertEqual(self.engine.store.account(self.key)["owner"], "on_pc")
        self.assertFalse(self.vault.path(self.key).exists())
        self.assertFalse(self.action("import-bundle", body)["result"]["failures"])

    def test_handover_can_resume_after_its_first_login_write(self):
        account, _ = self.placeholder()
        body = self.bundle([account], kind="handover")
        put = self.vault.put
        def fail_after_write(key, value, **kwargs):
            result = put(key, value, **kwargs)
            if key == self.key:
                raise OSError("synthetic failure after first login write")
            return result
        with mock.patch.object(self.vault, "put", side_effect=fail_after_write):
            result = self.action("import-bundle", body)["result"]
        self.assertTrue(result["failures"])
        self.assertEqual(self.engine.store.account(self.key)["owner"], "on_pc")
        self.assertFalse(self.action("import-bundle", body)["result"]["failures"])
        row = self.engine.store.account(self.key)
        self.assertEqual((row["owner"], row["org"]), ("v2", account["org"]))
        self.assertTrue(row["flags"]["restorable"])
        self.assertEqual(self.vault.get(self.key)["credentials"], self.saved["credentials"])

    def test_known_login_import_can_finish_metadata_after_a_failed_write(self):
        account = self.account()
        account["secret"]["data"]["refreshed"] = self.engine.clock() + 100
        account["secret"]["data"]["credentials"]["claudeAiOauth"]["accessToken"] += "-new"
        previous = copy.deepcopy(self.saved)
        previous["organizationUuid"] = previous["oauthAccount"]["organizationUuid"] = ""
        self.vault.put(self.key, previous)
        self.engine.store.update(self.key, org="")
        body, put = self.bundle([account]), self.vault.put
        def fail_after_write(key, value, **kwargs):
            result = put(key, value, **kwargs)
            if key == self.key:
                raise OSError("synthetic failure after known login write")
            return result
        with mock.patch.object(self.vault, "put", side_effect=fail_after_write):
            self.assertTrue(self.action("import-bundle", body)["result"]["failures"])
        self.assertFalse(self.action("import-bundle", body)["result"]["failures"])
        self.assertEqual(self.engine.store.account(self.key)["org"], account["org"])
        self.assertEqual(self.vault.get(self.key)["credentials"], account["secret"]["data"]["credentials"])

    def test_preview_refuses_organization_conflict_before_any_account_changes(self):
        account = self.account()
        account["org"] = "11111111-1111-4111-8111-111111111111"
        account["secret"]["data"]["organizationUuid"] = account["org"]
        account["secret"]["data"]["oauthAccount"]["organizationUuid"] = account["org"]
        allowed = self.account(listing=True)
        allowed.update(id="pc-new", identity="new-listing-identity", label="New listing")
        body = self.bundle([allowed, account], settings={"cli_threshold": 80})
        before = self.engine.store.accounts(), self.engine.settings(), self.vault.get(self.key)
        preview = self.action("import-bundle-preview", body)["preview"]
        self.assertTrue(preview["refused"])
        self.assertEqual(preview["counts"]["refused"], 1)
        self.assertEqual(preview["settings"], [])
        self.assertIn("organization", preview["accounts"][1]["reason"])
        self.assertNotIn("not waiting", preview["message"])
        with self.assertRaises(DeckError) as refused:
            self.action("import-bundle", body)
        self.assertIn("organization", str(refused.exception))
        self.assertEqual((self.engine.store.accounts(), self.engine.settings(), self.vault.get(self.key)), before)

    def retry_exact(self, *, handover):
        if handover:
            account, _ = self.placeholder(unknown=False)
        else:
            account = self.account(listing=True)
        body = self.bundle([account], kind="handover" if handover else "main")
        sealed = Path(body["path"]).read_bytes()
        preview = self.action("import-bundle-preview", body)["preview"]
        with self.engine.scheduler.account_lock(self.key), mock.patch.object(
                self.engine, "timings", dataclasses.replace(self.engine.timings, lock_wait=0)):
            failed = self.action("import-bundle", body)["result"]
        self.assertTrue(failed["failures"])
        self.assertIn("same bundle", failed["message"])
        entry = move_import._ledger(self.engine).entry(preview["bundle_id"])
        self.assertIsNotNone(entry)
        if handover:
            self.assertEqual(entry["started"][account["id"]], self.key)
        self.assertEqual(Path(body["path"]).read_bytes(), sealed)
        result = self.action("import-bundle", body)["result"]
        self.assertFalse(result["failures"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["bundle_id"], preview["bundle_id"])
        self.assertEqual(Path(body["path"]).read_bytes(), sealed)

    def test_same_listing_bundle_retries_after_busy(self):
        self.retry_exact(handover=False)

    def test_same_handover_bundle_retries_after_busy(self):
        self.retry_exact(handover=True)


class ImportOrganizationDashboardTest(DashboardTestCase, test_claude_resets.UltraResetCase):
    account = ResetHandoverTest.account
    bundle = ResetHandoverTest.bundle
    imported_last_good = test_reset_closure.ResetClosureTest.imported_last_good

    def setUp(self):
        test_claude_resets.UltraResetCase.setUp(self)
        self.browser.open(serve(self, self.engine))

    def preview_account(self, account):
        body = self.bundle([account])
        self.browser.js("AD.move.open()")
        self.browser.wait(f"{SHEET}.open", "the import sheet")
        self.browser.click("document.getElementById('move-path')")
        self.browser.type(body["path"])
        self.browser.click("document.getElementById('move-passphrase')")
        self.browser.type(body["passphrase"])
        with mock.patch.object(move_import, "MIN_SCRYPT_N", 1024):
            self.browser.click(button("Preview Import", SHEET))
            self.browser.wait("document.getElementById('modal-title').textContent === 'Review Import'",
                              "the import preview")

    def test_conflicting_organization_preview_has_no_import_button(self):
        account = self.account(listing=True)
        account["org"] = "11111111-1111-4111-8111-111111111111"
        self.preview_account(account)
        text = self.browser.js(f"{SHEET}.textContent")
        self.assertIn("different organization", text)
        self.assertIn("Nothing will be imported", text)
        self.assertNotIn("not waiting", text)
        self.assertFalse(self.browser.js(f"!!({button('Import', SHEET)})"))
        self.assertTrue(self.browser.js(f"!!({button('Cancel', SHEET)})"))

    def test_busy_result_gives_retry_advice_once(self):
        self.preview_account(self.account(listing=True))
        with self.engine.scheduler.account_lock(self.key), mock.patch.object(
                self.engine, "timings", dataclasses.replace(self.engine.timings, lock_wait=0)), \
                mock.patch.object(move_import, "MIN_SCRYPT_N", 1024):
            self.browser.click(button("Import", SHEET))
            self.browser.wait("document.getElementById('modal-title').textContent === 'Import Incomplete'",
                              "the busy import result")
        text = self.browser.js(f"{SHEET}.textContent")
        self.assertEqual(text.count("Import the same bundle again."), 1)

    def test_history_failure_says_login_saved_and_offers_the_same_bundle(self):
        self.preview_account(self.account())
        with mock.patch.object(move_import, "_copy_history", side_effect=OSError("synthetic copy failure")), \
                mock.patch.object(move_import, "MIN_SCRYPT_N", 1024):
            self.browser.click(button("Import", SHEET))
            self.browser.wait("document.getElementById('modal-title').textContent === 'Import Incomplete'",
                              "the history copy result")
        text = self.browser.js(f"{SHEET}.textContent")
        self.assertIn("sign-in was saved", text)
        self.assertIn("Import the same bundle again.", text)
        self.assertNotIn("Cancel move", text)
