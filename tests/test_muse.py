"""Muse Code support: the usage parser, process recognition, the adapter against a synthetic `muse` binary,
and saving, adding, switching and recovering Muse accounts through a whole engine. Everything runs in safe mode
on synthetic folders; the real Muse, its Keychain item and ~/.config/muse are never touched."""
import os

os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"

import errno
import io
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

from tests import support

from accountdeck import diagnostics
from accountdeck.config import Paths, Timings
from accountdeck.core import DeckError, FileTransaction, Vault, atomic_write, json_bytes, read_json, recover_transactions
from accountdeck.engine import Engine
from accountdeck.procs import FakeProcessController, helper_running, process_name
from accountdeck.providers import muse as muse_module
from accountdeck.providers.fake import (STUB_MUSE, FakeCodex, _write_stub, build_fake_home, muse_auth,
                                        test_cipher)
from accountdeck.providers.errors import ProviderError
from accountdeck.providers.muse import (KEYCHAIN_MESSAGE, REJECTED_MESSAGE, LoginOutput, Muse, classify_failure,
                                        cleared_env, identity_key)
from accountdeck.scheduler import RefreshScheduler, Target
from accountdeck.switching import DryRun
from accountdeck.usage import muse_plan, muse_usage

NOW_MS = 1_793_000_000_000


def payload(window=8.0, weekly=81.0, **extra):
    allowance = {"tier": "Muse Code Max Usage",
                 "window": {"usedPercent": window, "resetsAtMs": NOW_MS + 3_600_000, "windowDurationMins": 300},
                 "weekly": {"usedPercent": weekly, "resetsAtMs": NOW_MS + 86_400_000}}
    allowance.update(extra.pop("allowance", {}))
    return {"allowance": allowance, "asOfMs": NOW_MS, "source": "mint", **extra}


class ParserTest(unittest.TestCase):
    def test_usage_windows(self):
        usage = muse_usage(payload(), now=NOW_MS / 1000)
        windows = {w["id"]: w for w in usage["windows"]}
        self.assertEqual(set(windows), {"muse:window", "muse:weekly"})
        self.assertEqual((windows["muse:window"]["kind"], windows["muse:window"]["label"],
                          windows["muse:window"]["minutes"], windows["muse:window"]["used"]), ("session", "5-hour", 300, 8.0))
        # Muse states no length for the weekly allowance; it is read as 7 days.
        self.assertEqual((windows["muse:weekly"]["kind"], windows["muse:weekly"]["label"],
                          windows["muse:weekly"]["minutes"], windows["muse:weekly"]["used"]), ("weekly", "Weekly", 10080, 81.0))
        self.assertEqual(windows["muse:window"]["reset"], NOW_MS / 1000 + 3600)
        self.assertEqual(muse_plan(payload()), "Max")
        self.assertEqual(muse_plan({}), "")

    def test_reason_blocks_only_a_full_window(self):
        for data in (payload(window=104.0, weekly=50.0, reason="usage_limited"),
                     payload(window=104.0, weekly=50.0, allowance={"reason": "usage_limited"})):
            windows = {w["id"]: w for w in muse_usage(data, now=NOW_MS / 1000)["windows"]}
            self.assertEqual(windows["muse:window"]["blocked"], "usage_limited")
            self.assertIsNone(windows["muse:weekly"]["blocked"])

    def test_login_output(self):
        text = ("Open this page to sign in:\n  https://auth.meta.com/oauth/device/?code=ABCD-1234\n"
                "confirm this code matches:\n  ABCD-1234\n\nWaiting for approval…\n")
        output = LoginOutput(io.StringIO(text))
        self.assertTrue(output.wait_ready(5))
        self.assertEqual(output.url, "https://auth.meta.com/oauth/device/?code=ABCD-1234")
        self.assertEqual(output.code, "ABCD-1234")
        self.assertFalse(LoginOutput(io.StringIO("Error: offline\n")).wait_ready(5))

    def test_process_names(self):
        self.assertEqual(process_name("/Users/me/.local/bin/muse-bin-1.4.13-R6996.1"), "muse")
        self.assertEqual(process_name("/Users/me/.local/bin/muse-bin-1.4.13-R6996"), "muse")
        self.assertEqual(process_name("/Applications/museum"), "museum")


class AdapterTest(unittest.TestCase):
    """The real adapter against the synthetic `muse` binary (a real process, no network)."""

    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="ad-muse-adapter-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.paths = Paths.synthetic(root)
        build_fake_home(self.paths, codex_identity=str(uuid.uuid4()), muse_email="main@example.test")
        self.procs = FakeProcessController(self.paths)
        self.muse = Muse(self.paths, self.procs)
        self.auth = Path(self.paths.muse_config) / "auth.json"

    def leftovers(self):
        return list((Path(self.paths.data_dir) / "work").glob("muse-*"))

    def test_live_file_login_is_read_in_file_mode(self):
        reading = self.muse.usage(live=True)
        self.assertIsNone(reading.error)       # the stub refuses a file login without TBH_CREDENTIAL_BACKEND=file
        self.assertEqual(len(reading.usage["windows"]), 2)
        self.assertEqual(reading.meta["plan"], "Max")

    def test_live_keychain_login_ignores_the_callers_environment(self):
        atomic_write(self.auth, json_bytes(muse_auth("main@example.test", storage="keychain")))
        leaked = {"TBH_CREDENTIAL_BACKEND": "file", "META_API_KEY": "synthetic-key", "XDG_CONFIG_HOME": "/nonexistent"}
        with mock.patch.dict(os.environ, leaked):
            reading = self.muse.usage(live=True)
        self.assertIsNone(reading.error)
        self.assertIsNotNone(reading.usage)

    def test_saved_login_renewed_by_muse_goes_back_to_the_vault(self):
        before = muse_auth("side@example.test", lifetime=-60)
        stored = []
        reading = self.muse.usage(before, persist=lambda doc: stored.append(doc) or "accepted")
        self.assertIsNotNone(reading.usage)
        self.assertTrue(reading.meta["rotated"])
        self.assertEqual(len(stored), 1)
        self.assertEqual(identity_key(stored[0]), "side@example.test")
        self.assertNotEqual(stored[0]["providers"]["meta"]["refresh_token"], before["providers"]["meta"]["refresh_token"])
        self.assertGreater(stored[0]["providers"]["meta"]["expires_at"], time.time())
        self.assertEqual(identity_key(read_json(self.auth)), "main@example.test")      # the live login is untouched
        self.assertEqual(self.leftovers(), [])

    def test_saved_login_that_did_not_change_is_not_stored(self):
        stored = []
        reading = self.muse.usage(muse_auth("side@example.test"), persist=stored.append)
        self.assertIsNotNone(reading.usage)
        self.assertEqual(stored, [])
        self.assertEqual(self.leftovers(), [])

    def test_refused_sign_in_asks_for_a_new_one(self):
        reading = self.muse.usage(muse_auth("side@example.test", refresh_token="synthetic-revoked"),
                                  persist=lambda doc: "accepted")
        self.assertEqual(reading.error.kind, "refresh_rejected")
        self.assertEqual(reading.credential, "reauth_required")
        self.assertEqual(str(reading.error), REJECTED_MESSAGE)
        self.assertEqual(self.leftovers(), [])

    def test_keychain_and_api_key_logins_cannot_be_saved_or_replaced(self):
        atomic_write(self.auth, json_bytes(muse_auth("main@example.test", storage="keychain")))
        with self.assertRaises(DeckError) as caught:
            self.muse.current()
        self.assertEqual((caught.exception.code, str(caught.exception)), ("unsupported", KEYCHAIN_MESSAGE))
        with self.assertRaises(DeckError):
            self.muse.restore(muse_auth("side@example.test"), DryRun())
        doc = muse_auth("main@example.test")
        doc["providers"]["meta"]["mechanism"] = "api_key"
        atomic_write(self.auth, json_bytes(doc))
        self.assertEqual((self.muse.active_identity().status, self.muse.active_identity().reason), ("unknown", "api_key"))

    def test_not_installed(self):
        root = Path(tempfile.mkdtemp(prefix="ad-muse-none-"))
        self.addCleanup(shutil.rmtree, root, True)
        muse = Muse(Paths.synthetic(root, muse_stub=False), self.procs)
        self.assertIsNone(muse.executable())
        with self.assertRaises(DeckError) as caught:
            muse.usage(muse_auth("side@example.test"))
        self.assertEqual(caught.exception.code, "not_installed")


class EngineCase(unittest.TestCase):
    """A whole engine (safe mode, synthetic machine) with the real Muse adapter and the synthetic binary."""

    def make(self, storage="file", *, timings=None, owner_lock=None, providers=None):
        root = Path(tempfile.mkdtemp(prefix="ad-muse-engine-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.paths = paths = Paths.synthetic(root)
        diagnostics.setup(paths)
        build_fake_home(paths, codex_identity=str(uuid.uuid4()), muse_email="main@example.test", muse_storage=storage)
        self.procs = FakeProcessController(paths)
        self.auth = Path(paths.muse_config) / "auth.json"
        if providers is None:
            providers = {"codex": FakeCodex(paths), "muse": Muse(paths, self.procs)}
        self.engine = Engine(paths, providers=providers, owner_lock=owner_lock, timings=timings,
                             procs=self.procs, vault=Vault(paths.data_dir / "vault", protect=test_cipher),
                             start=False, inline=True, mode="live")
        self.addCleanup(self.engine.stop)
        return self.engine

    def plan(self, purpose, account=None, provider="muse"):
        body = {"purpose": purpose, "provider": provider}
        if account:
            body["id"] = account
        return self.engine.action("switch-plan", body)["plan"]

    def act(self, name, account=None, consent="now", wait=True):
        plan = self.plan("switch" if name == "switch" else name, account)
        self.assertEqual(plan["blockers"], [], plan["blockers"])
        body = {"provider": "muse", "plan_id": plan["plan_id"], "consent": consent}
        if account:
            body["id"] = account
        self.engine.action(name, body)
        if wait:
            self.assertTrue(self.engine.runner.wait(60))
        return self.engine.snapshot()

    def muse_rows(self):
        return {a["email"]: a for a in self.engine.snapshot()["accounts"] if a["provider"] == "muse"}

    def saved(self, email):
        return self.engine.store.find("muse", email)


class EngineTest(EngineCase):
    def test_keychain_login_shows_usage_but_cannot_be_saved(self):
        engine = self.make(storage="keychain")
        engine.tick()
        row = self.muse_rows()["main@example.test"]
        self.assertEqual((row["id"], row["saved"], row["active"], row["label"]), ("unsaved-muse", False, True, "main"))
        self.assertEqual(len(row["windows"]), 2)
        plan = self.plan("capture")
        self.assertEqual([b["message"] for b in plan["blockers"]], [KEYCHAIN_MESSAGE])

    def test_save_add_and_switch(self):
        engine = self.make()
        engine.tick()
        self.assertEqual(self.muse_rows()["main@example.test"]["id"], "unsaved-muse")
        report = self.act("capture")["report"]
        self.assertEqual(report["outcome"], "success", report)
        main = self.saved("main@example.test")
        self.assertIsNotNone(main)

        # Add Account: `muse login` in a private folder; approving it there leaves the live login alone.
        self.act("add")
        login = engine.snapshot()["login"]
        self.assertEqual((login["provider"], login["url"], login["user_code"]),
                         ("muse", "https://auth.meta.com/oauth/device/?code=SYNT-HETC", "SYNT-HETC"))
        approve = next((Path(self.paths.data_dir) / "work").glob("muse-login-*")) / "muse" / "stub-approve"
        approve.parent.mkdir(parents=True, exist_ok=True)
        approve.write_text("side@example.test", encoding="utf-8")
        for _ in range(100):
            if not approve.exists():
                break
            time.sleep(0.05)
        report = self.act("finish-login")["report"]
        self.assertEqual(report["outcome"], "success", report)
        side = self.saved("side@example.test")
        self.assertIsNotNone(side)
        self.assertNotIn("storage", engine.vault.get(side["id"])["providers"]["meta"])   # as Muse 1.4.13 writes it
        self.assertEqual(identity_key(read_json(self.auth)), "main@example.test")
        self.assertEqual(list((Path(self.paths.data_dir) / "work").glob("muse-login-*")), [])

        # Switch to the new account.
        report = self.act("switch", side["id"])["report"]
        self.assertEqual((report["outcome"], report["verified"]), ("success", "verified"), report)
        self.assertEqual(identity_key(read_json(self.auth)), "side@example.test")
        rows = self.muse_rows()
        self.assertTrue(rows["side@example.test"]["active"])
        self.assertFalse(rows["main@example.test"]["active"])

        # Muse renews the signed-in login; switching away saves that newer copy, not the older one.
        live = read_json(self.auth)
        live["providers"]["meta"].update(refresh_token="synthetic-muse-refresh-newest", expires_at=int(time.time()) + 7200)
        atomic_write(self.auth, json_bytes(live))
        report = self.act("switch", main["id"])["report"]
        self.assertEqual(report["outcome"], "success", report)
        self.assertEqual(identity_key(read_json(self.auth)), "main@example.test")
        self.assertEqual(engine.vault.get(side["id"])["providers"]["meta"]["refresh_token"], "synthetic-muse-refresh-newest")

    def test_switch_waits_for_open_muse_sessions(self):
        engine = self.make()
        engine.tick()
        self.act("capture")
        side = muse_auth("side@example.test")
        info = engine.providers["muse"].identity(side)
        key = engine.accounts.save("muse", side, info, source="login")
        session = self.procs.add_muse("muse_session")
        plan = self.plan("switch", key)
        self.assertEqual([c["kind"] for c in plan["running"]["codex_consumers"]], ["tui"])
        self.assertFalse(plan["idle"])
        report = self.act("switch", key, consent="now")["report"]
        self.assertEqual(report["outcome"], "failed")
        self.assertEqual(identity_key(read_json(self.auth)), "main@example.test")
        self.act("switch", key, consent="idle", wait=False)
        time.sleep(0.5)
        self.assertEqual(engine.snapshot()["pending"]["phase"], "waiting_idle")
        self.assertEqual(identity_key(read_json(self.auth)), "main@example.test")
        self.procs.exit_process(session.pid)
        self.assertTrue(engine.runner.wait(60))
        self.assertEqual(engine.snapshot()["report"]["outcome"], "success")
        self.assertEqual(identity_key(read_json(self.auth)), "side@example.test")

    def test_codex_ignores_muse_sessions(self):
        engine = self.make()
        self.procs.add_muse("muse_session")
        self.procs.add_muse("muse_host")
        plan = self.plan("capture", provider="codex")
        self.assertEqual(plan["running"]["codex_consumers"], [])
        kinds = engine.snapshot()["providers"]["muse"]["running"]["consumers"]
        self.assertEqual(sorted(c["kind"] for c in kinds), ["app_server_child", "tui"])

    def test_signed_in_account_is_never_renewed_from_its_saved_copy(self):
        engine = self.make()
        engine.tick()
        self.act("capture")
        main = self.saved("main@example.test")
        self.auth.write_text("{not json", encoding="utf-8")
        with mock.patch.object(Muse, "usage", side_effect=AssertionError("read")) as usage:
            outcome = engine.accounts.read(main["id"], active_identity={"muse": "main@example.test"})
        self.assertEqual(outcome, "skipped")
        usage.assert_not_called()

    def test_saved_account_renewal_is_stored(self):
        engine = self.make()
        expired = muse_auth("side@example.test", lifetime=-60)
        key = engine.accounts.save("muse", expired, engine.providers["muse"].identity(expired), source="login")
        outcome = engine.accounts.read(key, active_identity={"muse": "main@example.test"})
        self.assertEqual(outcome, "ok")
        stored = engine.vault.get(key)["providers"]["meta"]
        self.assertNotEqual(stored["refresh_token"], expired["providers"]["meta"]["refresh_token"])
        self.assertIsNotNone(engine.store.account(key)["last_good_usage"])

    def test_interrupted_switch_is_recovered(self):
        engine = self.make()
        before = self.auth.read_bytes()
        tx = FileTransaction(engine.vault, engine.roots())
        tx.doc.update(provider="muse", purpose="switch")
        tx.write(self.auth, json_bytes(muse_auth("side@example.test")))
        engine.recovery = recover_transactions(engine.vault)
        self.assertEqual(len(engine.recovery), 1)
        plan = self.plan("recover")
        self.assertEqual(plan["provider"], "muse")
        self.engine.action("recover", {"provider": "muse", "plan_id": plan["plan_id"], "consent": "now"})
        self.assertTrue(engine.runner.wait(30))
        self.assertEqual(engine.snapshot()["report"]["outcome"], "success")
        self.assertEqual(self.auth.read_bytes(), before)


class Unheld:
    """The owner lock as a second AccountDeck 2 sees it: another copy owns the sign-ins."""
    held = False


class ReviewFindingsTest(EngineCase):
    """One test per finding of the 2026-10-26 reviews of the Muse support; each failed before its fix."""

    def save_side(self, **spec):
        side = muse_auth("side@example.test", **spec)
        return self.engine.accounts.save("muse", side, self.engine.providers["muse"].identity(side), source="login"), side

    def test_switch_waits_for_a_running_read_of_the_signed_in_account(self):
        engine = self.make()
        engine.tick()
        key, _side = self.save_side()
        started, release = threading.Event(), threading.Event()
        original = Muse._run_usage

        def slow_live_read(adapter, config_home, *, file_mode, isolated):
            if not isolated and threading.current_thread().name == "reader":
                path = Path(config_home) / "muse" / "auth.json"
                doc = read_json(path)                  # Muse loads its login, renews it, then writes it back
                started.set()
                release.wait(20)
                doc["providers"]["meta"].update(refresh_token="renewed-by-the-read", expires_at=int(time.time()) + 3600)
                atomic_write(path, json_bytes(doc))
                return {"allowance": {"tier": "Muse Code Max Usage"}}
            return original(adapter, config_home, file_mode=file_mode, isolated=isolated)

        with mock.patch.object(Muse, "_run_usage", slow_live_read):
            reader = threading.Thread(name="reader", target=lambda: engine.accounts.read(
                "unsaved-muse", active_identity=dict(engine.active_identity)))
            reader.start()
            self.assertTrue(started.wait(10))
            self.act("switch", key, wait=False)
            time.sleep(0.5)
            self.assertEqual(identity_key(read_json(self.auth)), "main@example.test")    # still waiting for the read
            release.set()
            reader.join(20)
            self.assertTrue(engine.runner.wait(60))
        self.assertEqual(engine.snapshot()["report"]["outcome"], "success")
        self.assertEqual(identity_key(read_json(self.auth)), "side@example.test")
        main = self.saved("main@example.test")
        self.assertEqual(engine.vault.get(main["id"])["providers"]["meta"]["refresh_token"], "renewed-by-the-read")

    def test_failed_check_after_muse_rewrote_the_file_leaves_nothing_to_recover(self):
        engine = self.make()
        engine.tick()
        self.act("capture")
        key, _side = self.save_side()
        original = Muse._run_usage

        def refused(adapter, config_home, *, file_mode, isolated):
            path = Path(config_home) / "muse" / "auth.json"
            if not isolated and identity_key(read_json(path)) == "side@example.test":
                path.write_text(json.dumps(read_json(path), indent=2), encoding="utf-8")    # Muse's own format
                raise ProviderError("refresh_rejected", message=REJECTED_MESSAGE)
            return original(adapter, config_home, file_mode=file_mode, isolated=isolated)

        with mock.patch.object(Muse, "_run_usage", refused):
            report = self.act("switch", key)["report"]
        self.assertEqual(report["outcome"], "failed")
        self.assertEqual(identity_key(read_json(self.auth)), "main@example.test")
        self.assertEqual(engine.recovery, [])
        self.assertEqual(self.plan("capture", provider="codex")["blockers"], [])

    def test_recovery_keeps_both_renewed_sign_ins(self):
        engine = self.make()
        engine.tick()
        self.act("capture")
        main = self.saved("main@example.test")
        key, side = self.save_side()
        tx = FileTransaction(engine.vault, engine.roots())
        tx.doc.update(provider="muse", purpose="switch")
        tx.write(self.auth, json_bytes(side))
        # Then the app stopped. Since: Muse renewed the signed-in side, and a usage read renewed the saved main.
        live = read_json(self.auth)
        live["providers"]["meta"].update(refresh_token="side-renewed", expires_at=int(time.time()) + 7200)
        atomic_write(self.auth, json_bytes(live))
        saved = engine.vault.get(main["id"])
        newest = json.loads(json.dumps(saved))
        newest["providers"]["meta"].update(refresh_token="main-renewed", expires_at=int(time.time()) + 7200)
        # stored the way a usage read stores a renewal: against the copy it renewed from
        self.assertEqual(engine.accounts._put_muse(main["id"], newest, "probe", expected=saved), "accepted")
        engine.recovery = recover_transactions(engine.vault)
        plan = self.plan("recover")
        engine.action("recover", {"provider": "muse", "plan_id": plan["plan_id"], "consent": "now"})
        self.assertTrue(engine.runner.wait(30))
        self.assertEqual(engine.snapshot()["report"]["outcome"], "success")
        self.assertEqual(read_json(self.auth)["providers"]["meta"]["refresh_token"], "main-renewed")
        self.assertEqual(engine.vault.get(key)["providers"]["meta"]["refresh_token"], "side-renewed")
        self.assertEqual(engine.recovery, [])

    def test_renewals_of_the_signed_in_account_reach_the_vault(self):
        engine = self.make()
        engine.tick()
        self.act("capture")
        main = self.saved("main@example.test")
        live = read_json(self.auth)
        live["providers"]["meta"].update(refresh_token="renewed-in-place", expires_at=int(time.time()) + 7200)
        atomic_write(self.auth, json_bytes(live))
        self.assertEqual(engine.accounts.read(main["id"], active_identity={"muse": "main@example.test"}), "ok")
        self.assertEqual(engine.vault.get(main["id"])["providers"]["meta"]["refresh_token"], "renewed-in-place")

    def test_read_only_copy_never_renews_a_saved_sign_in(self):
        engine = self.make(owner_lock=Unheld())
        self.assertTrue(engine.read_only)
        expired, _ = self.save_side(lifetime=-60)
        self.assertEqual(engine.accounts.read(expired, active_identity={"muse": "main@example.test"}, read_only=True),
                         "skipped")
        valid = muse_auth("third@example.test")
        key = engine.accounts.save("muse", valid, engine.providers["muse"].identity(valid), source="login")
        self.assertEqual(engine.accounts.read(key, active_identity={"muse": "main@example.test"}, read_only=True), "ok")
        self.assertEqual(engine.vault.get(key), valid)
        # The fake Muse refuses this refresh token whenever it sees it, so "ok" proves Muse got none
        withheld = muse_auth("fourth@example.test", refresh_token="synthetic-revoked")
        key = engine.accounts.save("muse", withheld, engine.providers["muse"].identity(withheld), source="login")
        self.assertEqual(engine.accounts.read(key, active_identity={"muse": "main@example.test"}, read_only=True), "ok")
        self.assertEqual(engine.vault.get(key), withheld)
        self.assertNotEqual(engine.vault.get(expired)["providers"]["meta"]["expires_at"], None)
        self.assertLess(engine.vault.get(expired)["providers"]["meta"]["expires_at"], time.time())

    def test_retryable_failures_do_not_ask_for_a_new_sign_in(self):
        retry = "error: the request failed (code a). retry the command. if the problem continues, run `muse login`."
        self.assertNotEqual(classify_failure(1, retry).kind, "refresh_rejected")
        for text in ("not logged in. run `muse login`.", "stored login is no longer valid",
                     "still unauthorized after a token refresh; run `muse login` again"):
            self.assertEqual(classify_failure(1, text).kind, "refresh_rejected", text)
        self.assertIn("subscription", str(classify_failure(1, "subscribe at https://accountscenter.meta.com, then log in again.")))

    def test_helpers_never_inherit_sign_in_redirects(self):
        with mock.patch.dict(os.environ, {"TBH_AUTH_BASE_URL": "https://elsewhere.test", "MUSE_CUSTOM_HEADERS": "x",
                                          "META_TOKEN": "y"}):
            self.assertTrue({"TBH_AUTH_BASE_URL", "MUSE_CUSTOM_HEADERS", "META_TOKEN"} <= set(cleared_env()))

    REDIRECTS = {"TBH_AUTH_BASE_URL": "https://elsewhere.test", "TBH_CA_CERTIFICATE": "/nonexistent",
                 "MUSE_CUSTOM_HEADERS": "x", "META_TOKEN": "y"}

    def test_no_muse_helper_is_started_with_a_sign_in_redirect(self):
        """Consider 7: each helper is really started with the redirects set in AccountDeck's own environment.
        The stand-in Muse refuses to run when one reaches it."""
        engine = self.make()
        engine.tick()
        muse = engine.providers["muse"]
        with mock.patch.dict(os.environ, self.REDIRECTS):
            with self.subTest(helper="a usage check of the signed-in account"):
                reading = muse.usage(live=True)
                self.assertIsNone(reading.error)
                self.assertIsNotNone(reading.usage)
            with self.subTest(helper="a usage check of a saved account"):
                reading = muse.usage(muse_auth("side@example.test"), persist=lambda doc: "accepted")
                self.assertIsNone(reading.error)
                self.assertIsNotNone(reading.usage)
            with self.subTest(helper="a sign-in"):
                self.act("add")
                login = engine.snapshot()["login"]
                self.assertIsNotNone(login, engine.snapshot()["report"])
                self.assertEqual(login["url"], "https://auth.meta.com/oauth/device/?code=SYNT-HETC")

    def test_a_renewal_the_vault_refuses_is_kept_for_review(self):
        self.make()
        kept = []
        reading = self.engine.providers["muse"].usage(muse_auth("side@example.test", lifetime=-60),
                                                      persist=lambda doc: "kept",
                                                      quarantine=lambda raw: kept.append(raw) or "quarantine-test")
        self.assertEqual(len(kept), 1)
        self.assertTrue(reading.meta["needs_review"])

    def test_leftover_private_folders_are_swept_into_the_vault(self):
        engine = self.make()
        left = Path(self.paths.data_dir) / "work" / "muse-probe-left" / "muse" / "auth.json"
        atomic_write(left, json_bytes(muse_auth("side@example.test")))
        engine._sweep_work()
        self.assertFalse(left.parent.parent.exists())
        self.assertTrue(list((Path(self.paths.data_dir) / "vault").glob("quarantine-muse-leftover-*")))

    def helper_homes(self):
        """Records, for each helper started from now on, the private folder it was noted in (None: no note)
        and whether the note already says the helper runs."""
        seen, original = [], self.procs.spawn_helper

        def spawn(args, *, home=None, **options):
            process = original(args, home=home, **options)
            seen.append((home, home is not None and helper_running(home)))
            return process
        patcher = mock.patch.object(self.procs, "spawn_helper", spawn)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def test_a_usage_check_of_a_saved_account_notes_its_helper_in_its_private_folder(self):
        engine = self.make()
        seen = self.helper_homes()
        engine.providers["muse"].usage(muse_auth("side@example.test"), persist=lambda doc: "accepted")
        work = Path(self.paths.data_dir) / "work"
        self.assertEqual([(Path(home).parent, Path(home).name[:11], running) for home, running in seen],
                         [(work, "muse-probe-", True)])

    def test_a_usage_check_of_the_signed_in_account_writes_no_note_into_muses_own_folder(self):
        engine = self.make()
        seen = self.helper_homes()
        engine.providers["muse"].usage(live=True)
        self.assertEqual(seen, [(None, False)])
        config = Path(self.paths.muse_config)
        self.assertEqual([path.name for folder in (config, config.parent) for path in folder.glob(".helper")], [])

    def test_the_start_up_cleanup_leaves_a_sign_in_that_is_still_running_alone(self):
        engine = self.make()
        engine.tick()
        seen = self.helper_homes()
        self.act("add")
        home = next((Path(self.paths.data_dir) / "work").glob("muse-login-*"))
        self.assertEqual(seen, [(home, True)])
        engine._sweep_work()                    # what the next start would do if AccountDeck had died here
        self.assertTrue(home.exists(), "the folder a running sign-in writes its login to was deleted")
        approve = home / "muse" / "stub-approve"
        approve.parent.mkdir(parents=True, exist_ok=True)
        approve.write_text("side@example.test", encoding="utf-8")
        for _ in range(100):
            if not approve.exists():
                break
            time.sleep(0.05)
        report = self.act("finish-login")["report"]
        self.assertEqual(report["outcome"], "success", report)
        self.assertIsNotNone(self.saved("side@example.test"))

    def test_only_muse_codes_own_binary_counts_as_a_session(self):
        self.make()
        self.procs.add_row("Muse", path="/Applications/Muse.app/Contents/MacOS/Muse")
        self.procs.add_row("muse", path="/opt/homebrew/bin/muse")
        real = self.procs.add_row("muse", path=Path(self.paths.home) / ".local" / "bin" / "muse-bin-1.5.0")
        self.assertEqual([row.pid for row in self.procs.muse_consumers()], [real.pid])

    def test_cancel_is_refused_once_the_swap_starts(self):
        engine = self.make()
        engine.tick()
        self.act("capture")
        key, _side = self.save_side()
        entered, release = threading.Event(), threading.Event()
        original = Muse.usage

        def slow_check(adapter, *args, **kwargs):
            if kwargs.get("live"):
                entered.set()
                release.wait(20)
            return original(adapter, *args, **kwargs)

        with mock.patch.object(Muse, "usage", slow_check):
            self.act("switch", key, wait=False)
            self.assertTrue(entered.wait(20))
            with self.assertRaises(DeckError):
                engine.runner.cancel()
            release.set()
            self.assertTrue(engine.runner.wait(60))
        self.assertEqual(engine.snapshot()["report"]["outcome"], "success")

    def test_waiting_for_sessions_ends_when_the_confirmation_expires(self):
        engine = self.make(timings=Timings(consent_ttl=1.0))
        engine.tick()
        self.act("capture")
        key, _side = self.save_side()
        self.procs.add_muse("muse_host")
        self.act("switch", key, consent="idle")
        self.assertEqual(engine.snapshot()["report"]["outcome"], "failed")
        self.assertEqual(identity_key(read_json(self.auth)), "main@example.test")

    def test_signed_out_or_tokenless_muse_still_switches_with_a_warning(self):
        engine = self.make()
        key, _side = self.save_side()
        self.auth.unlink()
        engine.sense_active()
        self.assertEqual([w["code"] for w in self.plan("switch", key)["warnings"]], ["file_login"])
        self.assertEqual(self.act("switch", key)["report"]["outcome"], "success")
        tokenless = muse_auth("main@example.test")
        for name in ("access_token", "refresh_token", "expires_at"):
            del tokenless["providers"]["meta"][name]
        atomic_write(self.auth, json_bytes(tokenless))
        engine.sense_active()
        self.assertEqual([w["code"] for w in self.plan("switch", key)["warnings"]], ["incomplete"])
        self.assertEqual(self.act("switch", key)["report"]["outcome"], "success")
        self.assertEqual(identity_key(read_json(self.auth)), "side@example.test")

    def test_sign_in_that_ended_says_so(self):
        engine = self.make()
        self.act("add")
        deny = next((Path(self.paths.data_dir) / "work").glob("muse-login-*")) / "muse" / "stub-deny"
        deny.parent.mkdir(parents=True, exist_ok=True)
        deny.write_text("", encoding="utf-8")
        time.sleep(0.5)
        report = self.act("finish-login")["report"]
        self.assertEqual(report["outcome"], "failed")
        self.assertIn("ended without signing in", report["error"]["message"])
        self.assertEqual(self.act("cancel-login")["report"]["outcome"], "success")
        self.assertIsNone(engine.snapshot()["login"])

    def test_saving_again_keeps_the_plan_name(self):
        engine = self.make()
        engine.tick()
        self.act("capture")
        main = self.saved("main@example.test")
        engine.accounts.read(main["id"], active_identity={"muse": "main@example.test"})
        self.assertEqual(self.saved("main@example.test")["plan"], "Max")
        self.act("capture")
        self.assertEqual(self.saved("main@example.test")["plan"], "Max")

    def test_engine_without_a_muse_adapter_still_builds_a_valid_state(self):
        root = Path(tempfile.mkdtemp(prefix="ad-muse-two-"))
        self.addCleanup(shutil.rmtree, root, True)
        paths = Paths.synthetic(root)
        engine = Engine(paths, providers={"codex": FakeCodex(paths)},
                        procs=FakeProcessController(paths), vault=Vault(paths.data_dir / "vault", protect=test_cipher),
                        start=False, inline=True, mode="live")
        self.addCleanup(engine.stop)
        self.assertIn("muse", engine.snapshot()["providers"])

    def test_switch_needs_muse_installed(self):
        engine = self.make()
        key, _side = self.save_side()
        Path(self.paths.muse_exe).unlink()
        self.assertEqual([b["code"] for b in self.plan("switch", key)["blockers"]], ["not_installed"])


    def use_stub(self, stub):
        self.assertNotEqual(stub, STUB_MUSE)
        _write_stub(Path(self.paths.muse_exe), self.paths.python_exe, stub)

    def test_a_child_muse_leaves_running_does_not_hold_up_the_read(self):
        self.make()
        self.use_stub(STUB_MUSE.replace("def main(argv):", "def main(argv):\n    if os.fork() == 0:\n"
                                        "        time.sleep(6)\n        os._exit(0)\n", 1))
        start = time.monotonic()
        with mock.patch.object(muse_module, "USAGE_TIMEOUT", 3.0):
            reading = self.engine.providers["muse"].usage(live=True)
        self.assertLess(time.monotonic() - start, 3.0)
        self.assertIsNotNone(reading.usage)

    def test_switching_away_keeps_a_renewal_with_a_shorter_expiry(self):
        engine = self.make()
        engine.tick()
        live = read_json(self.auth)
        live["providers"]["meta"].update(refresh_token="main-1", expires_at=int(time.time()) + 7200)
        atomic_write(self.auth, json_bytes(live))
        self.act("capture")
        key, _side = self.save_side()
        live = read_json(self.auth)          # Muse renewed early (after a refused request): a shorter-lived token
        live["providers"]["meta"].update(refresh_token="main-2", expires_at=int(time.time()) + 3600)
        atomic_write(self.auth, json_bytes(live))
        self.assertEqual(self.act("switch", key)["report"]["outcome"], "success")
        main = self.saved("main@example.test")
        self.assertEqual(engine.vault.get(main["id"])["providers"]["meta"]["refresh_token"], "main-2")

    def test_a_renewal_is_stored_whatever_its_expiry_looks_like(self):
        engine = self.make()
        self.use_stub(STUB_MUSE.replace('float(meta["expires_at"] or 0) <= now',
                                        'str(meta.get("expires_at")) < time.strftime("%Y-%m-%dT%H:%M:%SZ")')
                               .replace("expires_at=int(now + 3600))",
                                        'expires_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 3600)))'))
        side = muse_auth("side@example.test", refresh_token="side-1")
        side["providers"]["meta"]["expires_at"] = "2020-01-01T00:00:00Z"
        key = engine.accounts.save("muse", side, engine.providers["muse"].identity(side), source="login")
        self.assertEqual(engine.accounts.read(key, active_identity={"muse": "main@example.test"}), "ok")
        self.assertNotEqual(engine.vault.get(key)["providers"]["meta"]["refresh_token"], "side-1")
        self.assertEqual(list(Path(engine.vault.root).glob("quarantine-*")), [])

    def test_read_only_never_asks_to_sign_in_again_for_a_renewal_it_withheld(self):
        engine = self.make(owner_lock=Unheld())
        self.use_stub(STUB_MUSE.replace(
            "    now = time.time()\n    if file_backend",
            "    now = time.time()\n"
            "    if file_backend and float(meta.get('expires_at') or 0) - now < 300 and not meta.get('refresh_token'):\n"
            "        return fail('Error: no login to refresh. Run `muse login`.')\n"
            "    if file_backend", 1))
        key, side = self.save_side(lifetime=240)        # Muse renews five minutes early
        self.assertEqual(engine.accounts.read(key, active_identity={"muse": "main@example.test"}, read_only=True),
                         "skipped")
        self.assertNotEqual(engine.store.account(key)["credential_state"], "reauth_required")
        self.assertEqual(engine.vault.get(key), side)

    def test_saving_while_a_read_is_starting_does_not_stall(self):
        engine = self.make(timings=Timings(lock_wait=2.0))
        engine.tick()
        self.act("capture")
        main = self.saved("main@example.test")
        holding = threading.Event()

        def starting_read():        # a read's order: the account's lock, then the Muse read lock
            with engine.scheduler.account_lock(main["id"]):
                holding.set()
                time.sleep(0.5)
                lock = engine.scheduler.helper_lock("muse")
                if lock.acquire(timeout=10):
                    lock.release()

        reader = threading.Thread(target=starting_read)
        reader.start()
        self.assertTrue(holding.wait(10))
        report = self.act("capture")["report"]
        reader.join(15)
        self.assertEqual(report["outcome"], "success", report)

    def test_a_renewal_that_cannot_be_stored_is_kept_encrypted(self):
        self.make()
        kept = []
        with self.assertRaises(DeckError):
            self.engine.providers["muse"].usage(muse_auth("side@example.test", lifetime=-60),
                                                persist=lambda doc: "conflict",
                                                quarantine=lambda raw: kept.append(raw) or "quarantine-test")
        self.assertEqual(len(kept), 1)
        self.assertEqual(list((Path(self.paths.data_dir) / "work").glob("muse-*")), [])

    def test_recover_after_an_unfinished_undo_restores_the_outgoing_account(self):
        for undo_record_first in (False, True):
            with self.subTest(undo_record_first=undo_record_first):
                engine = self.make()
                engine.tick()
                self.act("capture")
                key, _side = self.save_side()
                failed, usage, commit = [], Muse.usage, FileTransaction.commit

                def check_fails(adapter, *args, **kwargs):
                    if kwargs.get("live"):
                        raise DeckError("Muse could not report usage right now.", "network")
                    return usage(adapter, *args, **kwargs)

                def undo_dies(tx):          # the undo wrote auth.json, then its own record could not be closed
                    if tx.doc.get("purpose") == "recover" and not failed:
                        failed.append(tx)
                        raise OSError(errno.ENOSPC, "No space left on device")
                    return commit(tx)

                with mock.patch.object(Muse, "usage", check_fails), mock.patch.object(FileTransaction, "commit", undo_dies):
                    self.act("switch", key)
                pending = recover_transactions(engine.vault)
                self.assertEqual(len(pending), 2)
                order = sorted(pending, key=lambda k: engine.vault.get(k)["purpose"] == "recover", reverse=undo_record_first)
                plan = self.plan("recover")
                with support.recover_reads(order) as reached:
                    engine.action("recover", {"provider": "muse", "plan_id": plan["plan_id"], "consent": "now"})
                    self.assertTrue(engine.runner.wait(30))
                self.assertEqual(reached, order)
                self.assertEqual(engine.snapshot()["report"]["outcome"], "success")
                self.assertEqual(identity_key(read_json(self.auth)), "main@example.test")

    def test_recover_works_when_the_saved_copy_cannot_be_read(self):
        engine = self.make()
        engine.tick()
        self.act("capture")
        main = self.saved("main@example.test")
        key, side = self.save_side()
        tx = FileTransaction(engine.vault, engine.roots())
        tx.doc.update(provider="muse", purpose="switch")
        tx.write(self.auth, json_bytes(side))
        engine.vault._file(main["id"]).write_bytes(b"damaged")
        engine.recovery = recover_transactions(engine.vault)
        plan = self.plan("recover")
        engine.action("recover", {"provider": "muse", "plan_id": plan["plan_id"], "consent": "now"})
        self.assertTrue(engine.runner.wait(30))
        self.assertEqual(engine.snapshot()["report"]["outcome"], "success")
        self.assertEqual(identity_key(read_json(self.auth)), "main@example.test")
        self.assertEqual(engine.recovery, [])

    def test_paused_reads_never_wait_on_a_switchs_locks(self):
        gate, ran = threading.Event(), []

        def run(account_id):
            if account_id.startswith("busy"):
                gate.wait(10)
            ran.append(account_id)
            return "ok"

        scheduler = RefreshScheduler(run, workers=3)
        self.addCleanup(scheduler.shutdown)
        scheduler.tick([Target(f"busy{n}", "codex", f"k{n}") for n in range(3)] +
                       [Target(f"muse{n}", "muse", f"m{n}") for n in range(3)], time.time(), time.time)
        scheduler.pause("muse")             # a Muse switch starts and takes every Muse account's lock
        for n in range(3):
            lock = scheduler.account_lock(f"muse{n}")
            self.assertTrue(lock.acquire(timeout=1))
            self.addCleanup(lock.release)
        gate.set()
        time.sleep(0.3)
        scheduler.tick([Target("codex-active", "codex", "kc", active=True)], time.time(), time.time)
        deadline = time.monotonic() + 3
        while "codex-active" not in ran and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn("codex-active", ran)


    def test_an_older_copy_reappearing_in_muses_folder_is_never_saved(self):
        engine = self.make()
        engine.tick()
        self.act("capture")
        main = self.saved("main@example.test")
        key, _side = self.save_side()
        self.assertEqual(self.act("switch", key)["report"]["outcome"], "success")
        older = engine.vault.get(main["id"])
        expired = json.loads(json.dumps(older))
        expired["providers"]["meta"]["expires_at"] = int(time.time()) - 60
        engine.vault.put_guarded(main["id"], expired, "live", identity_key, lambda doc: None)
        self.assertEqual(engine.accounts.read(main["id"], active_identity={"muse": "side@example.test"}), "ok")
        renewed = engine.vault.get(main["id"])["providers"]["meta"]["refresh_token"]
        self.assertNotEqual(renewed, older["providers"]["meta"]["refresh_token"])
        atomic_write(self.auth, json_bytes(older))      # a restored backup puts main's replaced copy back
        engine.sense_active()
        self.assertEqual(self.act("switch", key)["report"]["outcome"], "success")
        self.assertEqual(engine.vault.get(main["id"])["providers"]["meta"]["refresh_token"], renewed)



class RealFormatTest(EngineCase):
    """The file login Muse 1.4.13 really writes (seen 2026-10-26): an access token and an API key, with no
    storage field, refresh token or expiry. The synthetic Muse refuses to read it outside file mode."""

    def save_real(self, email):
        doc = muse_auth(email, storage=None)
        return self.engine.accounts.save("muse", doc, self.engine.providers["muse"].identity(doc), source="login"), doc

    def test_the_signed_in_login_is_saved_and_read(self):
        engine = self.make(storage=None)
        engine.tick()
        self.assertEqual(self.plan("capture")["blockers"], [])
        self.assertEqual(self.act("capture")["report"]["outcome"], "success")
        main = self.saved("main@example.test")
        self.assertTrue(main["flags"]["restorable"])
        self.assertEqual(engine.accounts.read(main["id"], active_identity={"muse": "main@example.test"}), "ok")
        self.assertEqual(engine.vault.get(main["id"]), read_json(self.auth))

    def test_switching_there_and_back(self):
        engine = self.make(storage=None)
        engine.tick()
        self.act("capture")
        main = self.saved("main@example.test")
        before = engine.vault.get(main["id"])
        key, side = self.save_real("side@example.test")
        self.assertEqual(self.act("switch", key)["report"]["outcome"], "success")
        self.assertEqual(read_json(self.auth), side)
        self.assertEqual(engine.vault.get(main["id"]), before)
        self.assertEqual(self.act("switch", main["id"])["report"]["outcome"], "success")
        self.assertEqual(read_json(self.auth), before)

    def test_a_saved_login_is_read_as_it_is(self):
        engine = self.make(storage=None)
        key, side = self.save_real("side@example.test")
        self.assertEqual(engine.accounts.read(key, active_identity={"muse": "main@example.test"}), "ok")
        self.assertEqual(engine.vault.get(key), side)
        self.assertEqual(engine.store.account(key)["credential_state"], "fresh")

    def test_a_second_accountdeck_reads_it_too(self):
        engine = self.make(storage=None, owner_lock=Unheld())
        key, side = self.save_real("side@example.test")
        self.assertEqual(engine.accounts.read(key, active_identity={"muse": "main@example.test"}, read_only=True), "ok")
        self.assertEqual(engine.vault.get(key), side)

    def test_a_keychain_login_still_cannot_be_saved(self):
        self.make(storage="keychain")
        self.engine.tick()
        blockers = self.plan("capture")["blockers"]
        self.assertEqual([b["code"] for b in blockers], ["unsupported"])
        self.assertIn("muse logout", blockers[0]["message"])


    def test_a_login_without_a_mechanism_field_is_saved_before_a_switch(self):
        engine = self.make(storage=None)
        live = read_json(self.auth)
        del live["providers"]["meta"]["mechanism"]
        atomic_write(self.auth, json_bytes(live))
        engine.tick()
        key, _side = self.save_real("side@example.test")
        self.assertEqual(self.act("switch", key)["report"]["outcome"], "success")
        main = self.saved("main@example.test")
        self.assertIsNotNone(main)
        self.assertEqual(engine.vault.get(main["id"]), live)

    def test_only_a_login_marked_keychain_is_read_in_keychain_mode(self):
        self.make(storage=None)
        modes = []

        def record(adapter, config_home, *, file_mode, isolated):
            modes.append(file_mode)
            return {"allowance": {"tier": "Muse Code Max Usage"}}

        partial = muse_auth("main@example.test", storage=None)
        del partial["providers"]["meta"]["access_token"], partial["providers"]["meta"]["api_key"]
        with mock.patch.object(Muse, "_run_usage", record):
            for doc in (partial, muse_auth("main@example.test", storage="keychain")):
                atomic_write(self.auth, json_bytes(doc))
                self.engine.providers["muse"].usage(live=True)
        self.assertEqual(modes, [True, False])

    def test_a_changed_copy_the_vault_refuses_is_kept_for_review(self):
        self.make(storage=None)
        kept = []

        def rewrite(adapter, config_home, *, file_mode, isolated):
            path = Path(config_home) / "muse" / "auth.json"
            doc = read_json(path)
            doc["providers"]["meta"]["access_token"] = "replaced-by-muse"
            atomic_write(path, json_bytes(doc))
            return {"allowance": {"tier": "Muse Code Max Usage"}}

        with mock.patch.object(Muse, "_run_usage", rewrite):
            self.engine.providers["muse"].usage(muse_auth("side@example.test", storage=None),
                                                persist=lambda doc: "kept",
                                                quarantine=lambda raw: kept.append(raw) or "quarantine-test")
        self.assertEqual(len(kept), 1)

    def test_an_older_copy_reappearing_is_never_saved(self):
        engine = self.make(storage=None)
        engine.tick()
        self.act("capture")
        main = self.saved("main@example.test")
        older = read_json(self.auth)
        newer = json.loads(json.dumps(older))
        newer["providers"]["meta"]["access_token"] = "replaced-by-muse"
        atomic_write(self.auth, json_bytes(newer))
        self.assertEqual(engine.accounts.read(main["id"], active_identity={"muse": "main@example.test"}), "ok")
        self.assertEqual(engine.vault.get(main["id"]), newer)
        atomic_write(self.auth, json_bytes(older))          # a restored backup
        engine.accounts.read(main["id"], active_identity={"muse": "main@example.test"})
        self.assertEqual(engine.vault.get(main["id"]), newer)


if __name__ == "__main__":
    unittest.main()
