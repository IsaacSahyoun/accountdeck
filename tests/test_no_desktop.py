"""Claude Desktop is not a client of this build. Builds before 2.1 switched it, and a data folder one of them
used still holds what it saved: account rows, saved logins, a switch record that never finished, settings.
This build leaves all of that where it is, never lists it, and never opens a file in Claude Desktop's data
folder, where its login is.
Each test builds the old data the way the 2.0.0 build wrote it, on the demo's fake disk in safe mode."""
import contextlib
import os
from pathlib import Path
import plistlib
import sqlite3
import sys
import time
from unittest import mock
import uuid

from tests import support
from tests.test_dashboard import DashboardTestCase
from tests.test_switch_safety import Crash, DeckCase, codex_login

from accountdeck import contract, core, engine as engine_module, fixtures, move_bundle, move_import, recovery
from accountdeck.core import RECORD_TTL, DeckError, FileTransaction, Store, Vault, read_json
from accountdeck.engine import Engine
from accountdeck.platform import libproc, menubar, procs_mac
from accountdeck.procs import consent_refusal
from accountdeck.providers import codex as codex_module
from accountdeck.providers.fake import test_cipher

THREE = ("claude_cli", "codex", "muse")
PASSPHRASE = "synthetic passphrase for tests"
SCRYPT_N = 2 ** 10

# ---- which files the app used ----
_watched = []          # (path prefixes, what was seen under them), one entry per open `touches` block


def _audit(event, args):
    """Python reports every open, listing, rename and removal of a file here. Off unless a block watches."""
    if not _watched:
        return
    for arg in args:
        if not isinstance(arg, (str, bytes, os.PathLike)):
            continue
        try:
            text = os.fsdecode(arg)
        except (TypeError, ValueError):
            continue
        for prefixes, seen in _watched:
            if text.startswith(prefixes):
                seen.append((event, text))


sys.addaudithook(_audit)


@contextlib.contextmanager
def touches(*paths):
    """Yields the list of uses, inside the block, of any path that starts with one of `paths`: an open, a
    listing, a rename, a removal, a change of its dates or its mode."""
    entry = (tuple(str(path) for path in paths), [])
    _watched.append(entry)
    try:
        yield entry[1]
    finally:
        _watched.remove(entry)


def tree(folder):
    """Every file under `folder` with its bytes and the moment it last changed, and every folder."""
    found = {}
    for path in sorted(Path(folder).rglob("*")):
        found[str(path.relative_to(folder))] = (path.read_bytes(), path.stat().st_mtime_ns) if path.is_file() else "folder"
    return found


class LeftByAnOlderBuild(DeckCase):
    """The `terminal` demo (accounts of all three clients), and what the 2.0.0 build kept for Claude Desktop."""

    def restart(self, owner_lock=None):
        """Quits the app and starts it again on the same data folder."""
        old = self.engine
        old.stop()
        self.engine = Engine(old.paths, owner_lock=owner_lock, providers=old.providers, procs=old.procs,
                             vault=Vault(old.paths.data_dir / "vault", protect=test_cipher), status_sources={},
                             clock=old.clock, start=False, inline=True, mode="demo")
        self.addCleanup(self.engine.stop)
        self.vault = self.engine.vault
        return self.engine

    def start_that_dies(self, patch):
        """A start the app dies in. The data file that start had opened is closed here, as the dying app's was."""
        opened, make = [], engine_module.Store

        def tracked(*args, **kwargs):
            opened.append(make(*args, **kwargs))
            return opened[-1]
        try:
            with patch, mock.patch.object(engine_module, "Store", tracked), self.assertRaises(Crash):
                self.restart()
        finally:
            for store in opened:
                store.close()

    def old_store(self):
        """The data file as it is, with no list of clients: how the 2.0.0 build opened it."""
        store = Store(self.paths.data_dir, clock=self.engine.clock)
        self.addCleanup(store.close)
        return store

    def leave_desktop_account(self, label="Desk One"):
        """A Claude Desktop account as the 2.0.0 build left it: its row, a reading with its history, an alert
        about it, a line of history about a switch to it, two generations of its saved login, and a PC import of it
        that was still waiting."""
        store, now = self.old_store(), self.engine.clock()
        key = store.save_account("claude", str(uuid.uuid4()), label, label.lower().replace(" ", "-") + "@example.test",
                                 "max", "org of " + label)
        store.observe(key, support.shipped_reading(now, 40.0, 55.0))
        store.update(key, flags={"restorable": True, "deferred_desktop": "newer"}, credential_state="fresh",
                     paused_until=now + 3 * 365 * 86400)        # a pause the store caps on the accounts it manages
        store.record_generation(key, "0" * 64, source="switch")
        store.record_refill(key, "seven_day", reset_at=now - 3600, confirmed_at=now - 3000, previous_used=91.0)
        store.add_alert({"kind": "low", "level": "warning", "account": key, "window": "seven_day",
                         "title": f"{label} is getting low", "message": "Claude Desktop is close to its weekly limit."})
        store.event("switch", f"Switched Claude to {label}.")
        store.close()
        self.vault.put(key, {"version": 3, "identity": "desk", "cookies": "synthetic cookies, the older copy"})
        self.vault.put(key, {"version": 3, "identity": "desk", "cookies": "synthetic cookies, the newer copy"})
        self.vault.put("deferred-desktop-" + key, {"version": 1, "identity": "desk", "captured": now - 60,
                                                   "portable": {"cookies": "synthetic cookies from the PC"}},
                       generation=False)
        return key

    def on_file(self, key):
        """Everything the data file holds about one account, read past the app's own store."""
        db = sqlite3.connect(self.paths.data_dir / "accounts.sqlite3")
        try:
            found = {"accounts": db.execute("SELECT * FROM accounts WHERE id=?", (key,)).fetchall()}
            for table in ("snapshots", "readings", "refills", "generations", "alerts"):
                found[table] = db.execute(f"SELECT * FROM {table} WHERE account=? ORDER BY rowid", (key,)).fetchall()
            return found
        finally:
            db.close()

    def saved_logins(self, key):
        """The vault files of a Desktop account: every generation of its login and its waiting PC import."""
        return {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in sorted(self.vault.root.iterdir())
                if path.name.startswith((key, "deferred-desktop-" + key))}

    def login_paths(self, key):
        return self.vault.root / key, self.vault.root / ("deferred-desktop-" + key)

    def leave_desktop_folder(self):
        """Claude Desktop's data folder on this Mac, with a login in it."""
        folder = self.paths.home / "Library" / "Application Support" / "Claude"
        for name, data in {"Cookies": b"synthetic cookies of the account before the switch",
                           "config.json": b'{"oauth:tokenCache": "synthetic, before the switch"}',
                           "Local Storage/leveldb/000003.log": b"synthetic settings journal",
                           "claude-code-sessions/list.json": b"[]"}.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(data)
        return folder

    def leave_open_record(self, folder, *, named=True, outgoing=None):
        """The record of a Desktop switch the 2.0.0 build died in, written the way that build wrote it: the old
        bytes of a file go into the record, then the file is replaced. `named=False` is the record that build
        made to put Claude's session list back, which names no client; only its Desktop switch wrote one."""
        tx = FileTransaction(self.vault, [folder])
        if named:
            tx.doc.update(provider="claude", purpose="switch", outgoing_id=outgoing)
            tx.save()
            tx.write(folder / "Cookies", b"synthetic cookies of the account the switch was going to")
            tx.write(folder / "config.json", b'{"oauth:tokenCache": "synthetic, after the switch"}')
        else:
            tx.write(folder / "claude-code-sessions" / "list.json", b'["a session made after the switch"]')
        return tx.key

    def aside(self):
        return sorted(path.name for path in self.vault.root.glob("set-aside-*"))

    def desktop_notices(self):
        return [notice for notice in self.engine.snapshot()["notices"] if notice["message"] == recovery.UNMANAGED]


class HiddenAccountTest(LeftByAnOlderBuild):
    """Rule 1 and rule 2. A Desktop account an older build saved is never listed, counted, read, changed or
    removed, and its saved login is never opened. The row and the login stay in the data folder."""

    def setUp(self):
        super().setUp()
        self.before = [(row["id"], row["label"]) for row in self.engine.store.accounts()]
        self.key = self.leave_desktop_account()
        self.row, self.logins = self.on_file(self.key), self.saved_logins(self.key)
        self.assertEqual(len(self.row["accounts"]), 1)
        self.assertEqual(len(self.logins), 3, self.logins.keys())
        self.restart()

    def assert_untouched(self):
        self.assertEqual(self.on_file(self.key), self.row, "the row or its history changed")
        self.assertEqual(self.saved_logins(self.key), self.logins, "a saved login changed")

    def test_it_is_not_listed_or_counted_anywhere(self):
        engine, store = self.engine, self.engine.store
        state = engine.snapshot()
        self.assertEqual(contract.validate_state(state), [])
        self.assertEqual({account["provider"] for account in state["accounts"]}, set(THREE))
        self.assertEqual([(row["id"], row["label"]) for row in store.accounts()], self.before)
        self.assertEqual(sorted(state["providers"]), sorted(THREE))
        self.assertEqual(sorted(engine._counts), sorted(THREE))
        self.assertIsNone(store.account(self.key))
        self.assertIsNone(store.find("claude", self.row["accounts"][0][2]))
        self.assertNotIn(self.key, {alert["account"] for alert in store.alerts(include_dismissed=True)})
        # A line of history the older build wrote stays in the Activity list, as written, until newer lines push
        # it out. Nothing else on the page names the account.
        self.assertEqual([event["message"] for event in state["events"] if "Desk One" in event["message"]],
                         ["Switched Claude to Desk One."])
        self.assertNotIn("Desk One", str({**state, "events": []}))
        with self.assertRaises(DeckError) as refused:
            engine.history(self.key)
        self.assertEqual(refused.exception.code, "not_found")
        self.assert_untouched()

    def test_no_action_reaches_it(self):
        named = [("rename", {"label": "Mine now"}), ("remove", {}), ("refresh", {}), ("switch", {}),
                 ("switch-plan", {"purpose": "switch"}), ("cli-exclude", {"excluded": True})]
        for name, body in named:
            with self.subTest(action=name):
                with self.assertRaises(DeckError) as refused:
                    self.engine.action(name, {"id": self.key, **body})
                self.assertIn(refused.exception.code, ("not_found", "invalid"))
        self.assertTrue(self.engine.runner.wait(30))
        self.assert_untouched()

    def test_claude_desktop_is_not_a_client_to_ask_for(self):
        for name, body in (("switch-plan", {"purpose": "add"}), ("switch-plan", {"purpose": "capture"}), ("add", {}),
                           ("capture", {}), ("open", {}), ("recover", {}), ("stop-consumer", {"pid": 4})):
            with self.subTest(action=name, body=body):
                with self.assertRaises(DeckError) as refused:
                    self.engine.action(name, {"provider": "claude", **body})
                self.assertEqual(str(refused.exception), "Choose Claude Code, Codex or Muse.")
        with self.assertRaises(ValueError):
            self.engine.store.save_account("claude", str(uuid.uuid4()), "A new one")
        self.assertIsNone(self.engine.login)
        self.assert_untouched()

    def test_a_write_aimed_at_its_row_changes_nothing(self):
        """The store is the one boundary. Code that still holds the id of a Desktop account cannot write its row."""
        store = self.engine.store
        store.update(self.key, label="Mine now", error="broken", flags={})
        store.observe(self.key, support.shipped_reading(self.engine.clock(), 1.0, 2.0))
        store.remove(self.key)
        self.add_codex_account()                 # made after the Desktop account, so a new order would move its place
        store.reorder([row["id"] for row in reversed(store.accounts())])
        self.assert_untouched()

    def test_its_saved_login_is_never_opened_changed_or_removed(self):
        """Starting, checking usage, switching every client and removing an account."""
        with touches(*self.login_paths(self.key)) as seen:
            self.restart()
            for _ in range(3):
                self.engine.tick()
                self.engine.clock.advance(400)
            self.act("claude_cli", "switch", self.spare("claude_cli")["id"])
            self.close_codex()
            self.act("codex", "switch", self.add_codex_account())
            self.act("muse", "switch", self.spare("muse")["id"])
            self.engine.action("remove", {"id": self.spare("claude_cli")["id"]})
            self.assertTrue(self.engine.runner.wait(30))
            self.engine.snapshot()
        self.assertEqual(seen, [])
        self.assert_untouched()
        with touches(*self.login_paths(self.key)) as seen:        # the watch does see a read
            self.vault.get(self.key)
        self.assertTrue(seen)

    def test_an_older_build_finds_the_account_as_it_left_it(self):
        """The way back. After this build ran on the data, a store that knows no list of clients, which is how
        the 2.0.0 build opened the file, still lists the account, and its saved login still opens."""
        for _ in range(2):
            self.engine.tick()
            self.engine.clock.advance(400)
        self.restart()
        store = self.old_store()
        row = store.account(self.key)
        self.assertEqual((row["provider"], row["label"], row["flags"]["deferred_desktop"]), ("claude", "Desk One", "newer"))
        self.assertEqual(len(store.history(self.key)["snapshots"]), 1)
        self.assertEqual([alert["account"] for alert in store.alerts() if alert["account"] == self.key], [self.key])
        self.assertEqual(self.vault.get(self.key)["cookies"], "synthetic cookies, the newer copy")
        self.assertEqual(self.vault.get(self.key, 1)["cookies"], "synthetic cookies, the older copy")
        self.assertEqual(self.vault.get("deferred-desktop-" + self.key)["portable"]["cookies"], "synthetic cookies from the PC")

    def test_the_menu_bar_has_a_switch_menu_for_three_clients(self):
        state = self.engine.snapshot()
        items, _targets = menubar.menu_model(state, self.engine.clock(), start_on_login=None)
        self.assertEqual([item.text for item in items if item.submenu],
                         ["Switch Claude Code (terminal) To", "Switch Codex To", "Switch Muse To"])
        self.assertNotIn("Desk One", str(items))


class UnfinishedDesktopSwitchTest(LeftByAnOlderBuild):
    """Rule 3. A Desktop switch an older build never finished left an open record. This build cannot put Claude
    Desktop right, so the copy that owns the logins sets the record aside, bytes unchanged, and says so. The
    record never counts as waiting for Recover, and no file it names is opened."""

    def setUp(self):
        super().setUp()
        self.folder = self.leave_desktop_folder()

    def leave(self, **how):
        key = self.leave_open_record(self.folder, **how)
        return key, self.vault.path(key).read_bytes(), tree(self.folder)

    def assert_set_aside(self, key, record, files):
        self.assertEqual(self.engine.recovery, [])
        self.assertFalse(self.vault.path(key).exists(), "the record is still open")
        self.assertEqual(self.vault.path("set-aside-" + key).read_bytes(), record, "the record was not kept as it was")
        self.assertEqual(tree(self.folder), files, "a file in Claude Desktop's data folder changed")

    def test_the_next_start_sets_the_record_aside_and_says_so_once(self):
        for named in (True, False):
            with self.subTest(names_its_client=named):
                key, record, files = self.leave(named=named)
                with touches(self.folder) as seen:
                    self.restart()
                    state = self.engine.snapshot()
                self.assertEqual(seen, [], "the app used a file in Claude Desktop's data folder")
                self.assert_set_aside(key, record, files)
                self.assertEqual(state["recovery"], {"pending": False, "count": 0})
                self.assertNotIn("recovery", [banner["kind"] for banner in state["banners"]])
                self.assertEqual(len(self.desktop_notices()), 1)
                self.assertEqual(self.desktop_notices()[0]["level"], "warning")
                self.restart()           # the notice was for that start; this one finds nothing to say
                self.assertEqual(self.desktop_notices(), [])
                self.assertIn("set-aside-" + key + ".dpapi", self.aside())

    def test_two_records_of_one_unfinished_switch_get_one_notice(self):
        """The 2.0.0 build could leave two: the switch's own record, and the one it made while it put Claude's
        session list back. Both are set aside, and the page says so once."""
        first, second = self.leave(named=True), self.leave(named=False)
        files = second[2]               # the folder once both records were written
        with touches(self.folder) as seen:
            self.restart()
        self.assertEqual(seen, [], "the app used a file in Claude Desktop's data folder")
        for key, record, _files in (first, second):
            self.assert_set_aside(key, record, files)
        self.assertEqual(len(self.desktop_notices()), 1)

    def test_a_record_set_aside_at_a_later_look_gets_no_second_notice(self):
        """The set-aside of one of the two records fails at start, and works when the app looks again after a
        job. The page already says the switch was set aside, so it does not say it twice."""
        first, second = self.leave(named=True), self.leave(named=False)
        set_aside = Vault.set_aside

        def fails_for_the_first(vault, key):
            if key == first[0]:
                raise OSError(28, "No space left on device")
            return set_aside(vault, key)

        files = second[2]               # the folder once both records were written
        with touches(self.folder) as seen:
            with mock.patch.object(Vault, "set_aside", fails_for_the_first):
                self.restart()
            self.assertTrue(self.vault.path(first[0]).exists())
            self.assertEqual(len(self.desktop_notices()), 1)
            self.engine.look_for_unfinished()
        self.assertEqual(seen, [], "the app used a file in Claude Desktop's data folder")
        for key, record, _files in (first, second):
            self.assert_set_aside(key, record, files)
        self.assertEqual(len(self.desktop_notices()), 1)
        self.restart()                  # a new run of the app has nothing left to set aside
        self.assertEqual(self.desktop_notices(), [])

    def test_no_file_the_record_names_is_ever_used(self):
        """Starting, ticking, a switch of each client and a press of Recover, with the record still open because
        the set-aside failed every time. That press names no client and is refused: nothing waits for Recover.
        A press that does settle another client's record beside this one is in
        `test_recover_settles_another_clients_record_and_leaves_this_one`."""
        key, record, files = self.leave()
        with mock.patch.object(Vault, "set_aside", side_effect=OSError(28, "No space left on device")):
            with touches(self.folder) as seen:
                self.restart()
                self.engine.tick()
                self.act("claude_cli", "switch", self.spare("claude_cli")["id"])
                self.close_codex()
                self.act("codex", "switch", self.add_codex_account())
                self.act("muse", "switch", self.spare("muse")["id"])
                with self.assertRaises(DeckError) as refused:
                    self.engine.action("recover", {})
        self.assertEqual(refused.exception.code, "invalid")
        self.assertEqual(seen, [])
        self.assertEqual((self.vault.path(key).read_bytes(), tree(self.folder)), (record, files))
        with touches(self.folder) as seen:               # the watch does see a read
            (self.folder / "Cookies").read_bytes()
        self.assertTrue(seen)

    def test_it_never_holds_up_another_client(self):
        """A record that cannot be set aside stays open, and still blocks nothing: every client switches, the
        terminals' automatic switch runs, and nothing asks the user to press Recover."""
        key, record, files = self.leave()
        with mock.patch.object(Vault, "set_aside", side_effect=OSError(28, "No space left on device")):
            self.restart()
            self.assertEqual((self.engine.recovery, self.desktop_notices()), ([], []))
            side = self.add_codex_account()
            self.close_codex()
            for provider, target in (("claude_cli", self.spare("claude_cli")["id"]), ("codex", side),
                                     ("muse", self.spare("muse")["id"])):
                with self.subTest(client=provider):
                    plan = self.plan(provider, "switch", target)
                    self.assertEqual(plan["blockers"], [])
                    report = self.act(provider, "switch", target)
                    self.assertEqual(report["outcome"], "success", report)
                    self.assert_consistent(provider, self.identity(target))
            state = self.engine.snapshot()
            self.assertEqual(state["recovery"], {"pending": False, "count": 0})
            self.assertTrue(self.vault.path(key).exists())
        self.assertEqual(tree(self.folder), files)
        self.restart()                     # the disk has room again: the next start does the step
        self.assert_set_aside(key, record, files)
        self.assertEqual(len(self.desktop_notices()), 1)

    def test_a_copy_that_does_not_own_the_logins_leaves_the_record(self):
        key, record, files = self.leave()

        class OtherOwner:
            held = False
        self.restart(owner_lock=OtherOwner())
        self.assertTrue(self.engine.read_only)
        self.assertEqual((self.engine.recovery, self.desktop_notices(), self.aside()), ([], [], []))
        self.assertEqual((self.vault.path(key).read_bytes(), tree(self.folder)), (record, files))
        self.restart()                     # the copy that owns them
        self.assert_set_aside(key, record, files)

    def test_the_app_dies_at_each_step_of_the_set_aside(self):
        """The set-aside is one change of the record's date and one rename. Dying before either, or between
        them, leaves the record open, and the next start does the step again."""
        real_utime, real_replace = os.utime, os.replace

        def dies_at_the_date(path, *args, **kwargs):
            if Path(path).name.startswith("transaction-"):
                raise Crash()
            return real_utime(path, *args, **kwargs)

        def dies_at_the_rename(source, target, *args, **kwargs):
            if Path(target).name.startswith("set-aside-"):
                raise Crash()
            return real_replace(source, target, *args, **kwargs)

        for step, patch in (("the date", mock.patch.object(core.os, "utime", dies_at_the_date)),
                            ("the rename", mock.patch.object(core.os, "replace", dies_at_the_rename))):
            with self.subTest(dies_at=step):
                key, record, files = self.leave()
                self.start_that_dies(patch)
                self.assertEqual(self.vault.path(key).read_bytes(), record)
                self.assertNotIn("set-aside-" + key + ".dpapi", self.aside())
                self.assertEqual(tree(self.folder), files)
                self.restart()
                self.assert_set_aside(key, record, files)
                self.assertEqual(len(self.desktop_notices()), 1)

    def test_recover_settles_another_clients_record_and_leaves_this_one(self):
        for named in (True, False):
            with self.subTest(names_its_client=named):
                self.recover_beside(*self.leave(named=named))

    def recover_beside(self, key, record, files):
        side, auth = self.add_codex_account(f"side-{uuid.uuid4().hex}@example.test"), Path(self.paths.codex_home) / "auth.json"
        self.close_codex()
        before = read_json(auth)
        self.engine.accounts.capture("codex", owner="v2")
        tx = FileTransaction(self.vault, self.engine.roots())
        tx.doc.update(provider="codex", purpose="switch")
        self.engine.providers["codex"].restore(self.vault.get(side), tx)
        with mock.patch.object(Vault, "set_aside", side_effect=OSError(28, "No space left on device")):
            self.restart()
            self.assertEqual(self.engine.recovery, [tx.key])
            with touches(self.folder) as seen:
                report = self.act("codex", "recover")
            self.assertEqual(report["outcome"], "success", report)
            self.assertEqual((seen, self.engine.recovery, read_json(auth)), ([], [], before))
            with self.assertRaises(DeckError) as refused:       # nothing of this build's is left to recover
                self.engine.action("switch-plan", {"provider": "claude", "purpose": "recover"})
            self.assertEqual(refused.exception.code, "invalid")
        self.assertEqual((self.vault.path(key).read_bytes(), tree(self.folder)), (record, files))

    def test_this_build_has_no_way_to_write_a_file_the_record_names(self):
        key, record, files = self.leave()
        with mock.patch.object(Vault, "set_aside", side_effect=OSError(28, "No space left on device")):
            self.restart()
        tx = FileTransaction.load(self.vault, self.engine.roots(), key)
        with self.assertRaises(DeckError) as refused:
            tx.rollback()
        self.assertEqual(refused.exception.code, "safety")
        self.assertEqual((self.vault.path(key).read_bytes(), tree(self.folder)), (record, files))

    def test_the_set_aside_record_is_kept_for_30_days(self):
        key, record, files = self.leave()
        os.utime(self.vault.path(key), (time.time() - 45 * 86400,) * 2)      # the switch died 45 days ago
        self.restart()
        self.assert_set_aside(key, record, files)
        kept = self.vault.path("set-aside-" + key)
        self.restart()                    # the 30 days count from the set-aside, not from the switch
        self.assertEqual(kept.read_bytes(), record)
        os.utime(kept, (time.time() - RECORD_TTL + 3600,) * 2)
        self.restart()
        self.assertEqual(kept.read_bytes(), record)
        os.utime(kept, (time.time() - RECORD_TTL - 3600,) * 2)
        self.restart()
        self.assertFalse(kept.exists())
        self.assertEqual(tree(self.folder), files)

    def test_a_desktop_sign_in_an_older_build_was_waiting_for_is_left_alone(self):
        """The 2.0.0 build kept "add an account" for Claude Desktop in a record of its own kind and a setting
        that pointed at it. Neither is read: nothing waits for a sign-in, and both stay."""
        tx = FileTransaction(self.vault, [self.folder])
        tx.doc.update(provider="claude", purpose="add", outgoing_id=None, status="enrolling")
        tx.write(self.folder / "Cookies", b"")
        store = self.old_store()
        store.set_setting("claude_enrollment", {"transaction": tx.key, "previous": None, "started": self.engine.clock()})
        store.close()
        record, files = self.vault.path(tx.key).read_bytes(), tree(self.folder)
        with touches(self.folder) as seen:
            self.restart()
            state = self.engine.snapshot()
            for name in ("finish-login", "cancel-login"):
                with self.subTest(action=name), self.assertRaises(DeckError):
                    self.engine.action(name, {})
        self.assertEqual((seen, state["login"], self.engine.recovery, self.aside()), ([], None, [], []))
        self.assertEqual((self.vault.path(tx.key).read_bytes(), tree(self.folder)), (record, files))
        self.assertEqual(self.engine.store.setting("claude_enrollment")["transaction"], tx.key)


class OldSettingsTest(LeftByAnOlderBuild):
    """Rule 4. A setting only Claude Desktop used is not a setting of this build. Its stored value stays."""

    def setUp(self):
        super().setUp()
        self.key = self.leave_desktop_account()
        store = self.old_store()
        store.set_settings({"cookie_route": False, "confirm_when_idle": False,
                            "recommender_state": {"claude": {"account": self.key, "since": self.engine.clock() - 60},
                                                  "codex": {"account": "gone", "since": 1}},
                            "alert_latches": {"low": {f"{self.key}|seven_day": self.engine.clock() - 60}}})
        store.close()
        self.restart()

    def test_a_desktop_setting_is_not_offered_and_its_value_stays(self):
        settings = self.engine.settings()
        self.assertNotIn("cookie_route", settings)
        self.assertEqual(settings["confirm_when_idle"], False, "a setting this build has keeps its value")
        with self.assertRaises(DeckError) as refused:
            self.engine.update_settings({"cookie_route": True})
        self.assertEqual(refused.exception.code, "invalid")
        self.engine.update_settings({"sort": "name"})
        self.assertIs(self.engine.store.setting("cookie_route"), False)

    def test_what_an_older_build_remembered_about_a_desktop_account_breaks_nothing(self):
        for _ in range(2):
            self.engine.tick()
            self.engine.clock.advance(400)
        state = self.engine.snapshot()
        self.assertEqual(contract.validate_state(state), [])
        self.assertEqual(self.engine.health()["failing_phases"], {})
        self.assertNotIn("claude", state["now_band"]["best"])


class DesktopProcessTest(LeftByAnOlderBuild):
    """A running Claude Desktop is not a session of any client. It is in no plan, and it is never signalled."""

    def test_a_running_claude_desktop_is_in_no_plan_and_cannot_be_stopped(self):
        app = self.paths.home / "Applications" / "Claude.app" / "Contents"
        main = self.procs.add_row("Claude", path=app / "MacOS" / "Claude")
        helper = self.procs.add_row("Claude Helper", path=app / "Frameworks" / "Claude Helper.app" / "Claude Helper",
                                    parent=main.pid)
        code = self.procs.add_row("claude", path=app / "Resources" / "claude-code" / "claude", parent=main.pid)
        self.engine.tick()
        rows = {row.pid: row for row in self.procs.snapshot()}
        for row in (main, helper, code):
            self.assertEqual(rows[row.pid].kind, "other", row.name)
        side = self.add_codex_account()
        for provider, target in (("claude_cli", self.spare("claude_cli")["id"]), ("codex", side),
                                 ("muse", self.spare("muse")["id"])):
            plan = self.plan(provider, "switch", target)
            listed = {pid for pid, _ctime in plan["process_set"]} | {c["pid"] for c in plan["running"]["codex_consumers"]}
            self.assertFalse(listed & {main.pid, helper.pid, code.pid}, provider)
        plan = self.plan("codex", "switch", side)
        claimed = self.engine.action("switch", {"provider": "codex", "id": side, "plan_id": plan["plan_id"], "consent": "idle"})
        self.wait_for_phase("waiting_idle")
        token = self.engine.runner.current.token
        for row in (main, helper, code):
            self.assertEqual(consent_refusal(token, rows[row.pid], now=time.time()), "kind", row.name)
            with self.assertRaises(DeckError):
                self.engine.action("stop-consumer", {"provider": "codex", "pid": row.pid, "op": claimed.get("op")})
        self.assertEqual({row.pid for row in self.procs.snapshot()} & {main.pid, helper.pid, code.pid},
                         {main.pid, helper.pid, code.pid}, "a process of Claude Desktop's was stopped")
        self.engine.action("cancel", {})
        self.assertTrue(self.engine.runner.wait(30))

    def test_the_scan_of_a_real_mac_reads_one_file_of_claude_desktops_app(self):
        """The process scan on a real Mac knows Codex's app by the bundle id in a running app's Info.plist, so it
        reads that file of every running app, Claude Desktop's among them. It reads nothing else of Claude.app,
        and no process of it counts as one of a client's."""
        app = self.paths.home / "Applications" / "Claude.app" / "Contents"
        running = {"Claude": "MacOS/Claude", "Claude Helper": "Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper",
                   "claude": "Resources/claude-code/claude", "cowork-svc": "Resources/cowork-svc",
                   "ShipIt": "Frameworks/Squirrel.framework/Resources/ShipIt"}
        for path in running.values():
            (app / path).parent.mkdir(parents=True, exist_ok=True)
            (app / path).write_bytes(b"synthetic program")
        (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.anthropic.claudefordesktop"}))
        (app / "Resources" / "app.asar").write_bytes(b"synthetic app")
        scan = procs_mac.MacProcessController(self.paths, allow_readonly=True)
        libproc._bundle_cache.clear()
        with touches(app.parent) as seen:
            ours = {name: scan._relevant(name, str(app / path)) for name, path in running.items()}
            families = {libproc.bundle_family(str(app / path)) for path in running.values()}
        self.assertEqual(set(ours.values()), {False}, ours)
        self.assertEqual(families, {"com.anthropic.claudefordesktop"})
        self.assertEqual({(event, path) for event, path in seen}, {("open", str(app / "Info.plist"))})


# ---- a PC bundle that holds a Claude Desktop account ----

def account(provider, name, **more):
    return {"id": "pc-" + name, "provider": provider, "identity": str(uuid.uuid4()), "label": name,
            "email": name + "@example.test", "plan": "max", "org": "", "created": 1_700_000_000, "live_on_pc": False,
            "refills": [], "history": {"last_good_usage": None, "last_good_at": None, "readings": [], "snapshots": []},
            **more}


def desktop_account(name="desk"):
    return account("claude", name, secret={"type": "claude-desktop-portable",
                                           "data": {"cookies": "synthetic cookies from the PC", "version": 1}})


class PCBundleWithDesktopTest(LeftByAnOlderBuild):
    """Rule 5. A PC still packs its Claude Desktop accounts. The import leaves them out, says how many, and
    never gives a confirmation code for that bundle, so the PC keeps the accounts it alone holds."""

    def bundle(self, kind, accounts, settings=None):
        payload = {"format": 1, "source_platform": "windows", "settings": settings or {}, "accounts": accounts}
        self.addCleanup(setattr, move_import, "MIN_SCRYPT_N", move_import.MIN_SCRYPT_N)
        move_import.MIN_SCRYPT_N = SCRYPT_N
        path = Path(self.paths.temp_root) / f"{kind}-{uuid.uuid4().hex}.adeck"
        path.write_bytes(move_bundle.seal(payload, PASSPHRASE, kind, scrypt_n=SCRYPT_N, min_scrypt_n=SCRYPT_N))
        return {"path": str(path), "passphrase": PASSPHRASE}

    def codex_account(self, name, **more):
        login = codex_login(name + "@example.test", now=self.engine.clock())
        return account("codex", name, identity=codex_module.identity_key(login), secret={"type": "codex-auth", "data": login},
                       **more)

    def everything(self):
        """The whole data file and the name and bytes of every vault file but the import ledger."""
        db = sqlite3.connect(self.paths.data_dir / "accounts.sqlite3")
        try:
            rows = db.execute("SELECT id, provider, identity, label, owner FROM accounts ORDER BY rowid").fetchall()
        finally:
            db.close()
        return rows, {path.name: path.read_bytes() for path in sorted(self.vault.root.iterdir())
                      if not path.name.startswith(move_import.LEDGER_KEY)}

    def test_a_desktop_account_is_left_out_and_the_others_are_saved(self):
        body = self.bundle("main", [desktop_account(), self.codex_account("codex-pc"),
                                    account("claude_cli", "term-pc", live_on_pc=True)])
        rows, files = self.everything()
        preview = self.engine.action("import-bundle-preview", body)["preview"]
        self.assertEqual(self.everything(), (rows, files), "a preview writes nothing")
        self.assertEqual([(a["label"], a["action"]) for a in preview["accounts"]],
                         [("desk", "left_out"), ("codex-pc", "new"), ("term-pc", "on_pc")])
        self.assertEqual((preview["counts"]["left_out"], preview["refused"]), (1, False))
        self.assertIn("1 Claude Desktop account will be left out. This version of AccountDeck does not manage Claude "
                      "Desktop. There will be no confirmation code, so the PC keeps its accounts.", preview["message"])
        result = self.engine.action("import-bundle", body)["result"]
        self.assertEqual((result["complete"], result["code"], result["failures"]), (False, None, []))
        self.assertEqual({k: v for k, v in result["counts"].items() if v}, {"new": 1, "on_pc": 1, "left_out": 1})
        self.assertIn("1 Claude Desktop account was left out.", result["message"])
        self.assertIn("The other accounts are saved on this Mac.", result["message"])
        self.assertTrue(self.engine.runner.wait(30))
        report = self.engine.snapshot()["report"]
        self.assertEqual(report["outcome"], "success", report)
        self.assertIn("There is no confirmation code", report["summary"])
        after, after_files = self.everything()
        self.assertEqual([(row[1], row[3]) for row in after[len(rows):]], [("codex", "codex-pc"), ("claude_cli", "term-pc")])
        self.assertNotIn("claude", {row[1] for row in after}, "a Desktop account was stored")
        added = set(after_files) - set(files)
        self.assertEqual(len(added), 1, added)                 # the Codex login, and nothing of the Desktop account
        self.assertEqual(added, {after[len(rows)][0] + ".dpapi"})
        state = self.engine.snapshot()
        self.assertEqual(contract.validate_state(state), [])
        self.assertIsNone(state["move_import"]["imports"][0]["code"])

    def test_the_same_bundle_again_gives_the_same_answer(self):
        """The way back is on the PC: with no code it keeps every account, and the bundle can be applied again."""
        body = self.bundle("main", [desktop_account(), self.codex_account("codex-pc")])
        first = self.engine.action("import-bundle", body)["result"]
        self.assertTrue(self.engine.runner.wait(30))
        rows, files = self.everything()
        second = self.engine.action("import-bundle", body)["result"]
        self.assertTrue(self.engine.runner.wait(30))
        self.assertEqual((first["code"], second["code"], second["complete"]), (None, None, False))
        self.assertEqual({k: v for k, v in second["counts"].items() if v}, {"kept": 1, "left_out": 1})
        self.assertEqual(self.everything(), (rows, files))

    def test_a_bundle_of_desktop_accounts_alone_stores_nothing(self):
        body = self.bundle("main", [desktop_account("desk-a"), desktop_account("desk-b")])
        rows, files = self.everything()
        preview = self.engine.action("import-bundle-preview", body)["preview"]
        self.assertIn("2 Claude Desktop accounts will be left out.", preview["message"])
        result = self.engine.action("import-bundle", body)["result"]
        self.assertTrue(self.engine.runner.wait(30))
        self.assertEqual((result["complete"], result["code"], result["counts"]["left_out"]), (False, None, 2))
        self.assertNotIn("The other accounts", result["message"])
        self.assertEqual(self.everything(), (rows, files))

    def test_a_hand_over_fills_the_waiting_account_and_leaves_the_desktop_one_out(self):
        waiting = self.codex_account("codex-pc")
        listed = {key: value for key, value in waiting.items() if key != "secret"}
        self.engine.action("import-bundle", self.bundle("main", [dict(listed, live_on_pc=True)]))
        self.assertTrue(self.engine.runner.wait(30))
        row = self.engine.store.find("codex", waiting["identity"])
        self.assertEqual(row["owner"], "on_pc")
        body = self.bundle("handover", [waiting, desktop_account()])
        preview = self.engine.action("import-bundle-preview", body)["preview"]
        self.assertEqual(([a["action"] for a in preview["accounts"]], preview["refused"]), (["fill", "left_out"], False))
        result = self.engine.action("import-bundle", body)["result"]
        self.assertTrue(self.engine.runner.wait(30))
        self.assertEqual((result["complete"], result["code"]), (False, None))
        self.assertEqual({k: v for k, v in result["counts"].items() if v}, {"filled": 1, "left_out": 1})
        self.assertEqual(self.engine.store.account(row["id"])["owner"], "v2")
        self.assertEqual(codex_module.identity_key(self.vault.get(row["id"])), waiting["identity"])
        self.assertNotIn("claude", {found[1] for found in self.everything()[0]})

    def test_an_import_that_also_fails_an_account_still_says_what_was_left_out(self):
        """A bundle with a Desktop account and an account whose sign-in is another account's. The result, the
        report and the banner name both, and say why no export of this bundle can get a code."""
        wrong = self.codex_account("codex-wrong")
        wrong["identity"] = str(uuid.uuid4())                 # the sign-in inside belongs to someone else
        body = self.bundle("main", [desktop_account(), self.codex_account("codex-pc"), wrong])
        result = self.engine.action("import-bundle", body)["result"]
        self.assertTrue(self.engine.runner.wait(30))
        self.assertEqual((result["complete"], result["code"], len(result["failures"])), (False, None, 1))
        self.assertEqual({k: v for k, v in result["counts"].items() if v}, {"new": 1, "left_out": 1, "failed": 1})
        report = self.engine.snapshot()["report"]
        self.assertEqual(report["outcome"], "failed")
        for words in (result["message"], report["summary"], self.engine.snapshot()["move_import"]["imports"][0]["message"]):
            self.assertIn("1 account could not be imported.", words)
            self.assertIn("1 Claude Desktop account was left out. This version of AccountDeck does not manage Claude "
                          "Desktop. There is no confirmation code, because the code covers every account in the bundle.",
                          words)
            self.assertNotIn("export again", words, "a new export of this PC would hold the Desktop account again")

    def test_a_bundle_without_a_desktop_account_still_gets_its_code(self):
        result = self.engine.action("import-bundle", self.bundle("main", [self.codex_account("codex-pc")]))["result"]
        self.assertTrue(self.engine.runner.wait(30))
        self.assertTrue(result["complete"])
        self.assertRegex(result["code"], r"^[A-Z2-7]{4}-[A-Z2-7]{4}$")

    def test_a_desktop_setting_in_a_bundle_is_dropped(self):
        body = self.bundle("main", [self.codex_account("codex-pc")], {"cookie_route": False, "sort": "name"})
        preview = self.engine.action("import-bundle-preview", body)["preview"]
        self.assertEqual(preview["settings"], ["account sorting"])
        self.engine.action("import-bundle", body)
        self.assertTrue(self.engine.runner.wait(30))
        self.assertEqual(self.engine.settings()["sort"], "name")
        self.assertIsNone(self.engine.store.setting("cookie_route"))

    def test_an_import_an_older_build_recorded_is_still_shown(self):
        """The 2.0.0 build counted a Desktop account it had saved for later as "deferred", and gave the bundle
        its code. This build shows that import as it was recorded."""
        desk = self.leave_desktop_account()
        entry = {"bundle_id": uuid.uuid4().hex, "kind": "main", "created": self.engine.clock() - 600,
                 "at": self.engine.clock() - 500, "complete": True, "code": "ABCD-2345", "dismissed": False,
                 "counts": {"new": 1, "updated": 0, "filled": 0, "kept": 0, "on_pc": 0, "deferred": 1, "failed": 0},
                 "failures": [], "accounts": {"pc-codex": self.active("codex")["id"], "pc-desk": desk}, "started": {}}
        self.vault.put(move_import.LEDGER_KEY, {"imports": [entry]}, generation=False)
        ledger = self.vault.path(move_import.LEDGER_KEY).read_bytes()
        self.restart()
        state = self.engine.snapshot()
        self.assertEqual(contract.validate_state(state), [])
        shown = state["move_import"]["imports"]
        self.assertEqual([(item["complete"], item["code"]) for item in shown], [(True, "ABCD-2345")])
        self.assertEqual(sorted(shown[0]["counts"]), sorted(move_import.OUTCOMES))
        self.assertEqual(self.vault.path(move_import.LEDGER_KEY).read_bytes(), ledger)

    def test_a_second_import_of_a_bundle_an_older_build_finished_has_no_code(self):
        """The older build stored the bundle's Desktop account too, so it gave the bundle its code. This build
        leaves that account out, and an import by this build says what this build did: no code, and the PC keeps
        its accounts. The old code would tell the PC to drop a Desktop account this build cannot use."""
        body = self.bundle("main", [desktop_account(), self.codex_account("codex-pc")])
        header, _payload = move_import.open_file(body["path"], body["passphrase"], paths=self.paths)
        entry = {"bundle_id": header.bundle_id, "kind": "main", "created": header.created,
                 "at": self.engine.clock() - 500, "complete": True, "code": "ABCD-2345", "dismissed": False,
                 "counts": {"new": 1, "updated": 0, "filled": 0, "kept": 0, "on_pc": 0, "deferred": 1, "failed": 0},
                 "failures": [], "accounts": {}, "started": {}}
        self.vault.put(move_import.LEDGER_KEY, {"imports": [entry]}, generation=False)
        self.restart()
        self.assertEqual(self.engine.snapshot()["move_import"]["imports"][0]["code"], "ABCD-2345")
        result = self.engine.action("import-bundle", body)["result"]
        self.assertTrue(self.engine.runner.wait(30))
        self.assertEqual((result["complete"], result["code"], result["failures"]), (False, None, []))
        self.assertIn("1 Claude Desktop account was left out.", result["message"])
        self.assertIn("The PC keeps its accounts: use Cancel move there.", result["message"])
        shown = self.engine.snapshot()["move_import"]["imports"]
        self.assertEqual([(item["complete"], item["code"]) for item in shown], [(False, None)])


class ImportResultOnThePageTest(DashboardTestCase):
    """The banner of an import that has no code says the server's own words for why, and lists what failed."""
    BANNER = "(document.querySelector('#banners .banner[data-kind^=move-import]') || {innerText: null}).innerText"

    def banner(self, counts, failures):
        entry = {"bundle_id": uuid.uuid4().hex, "kind": "main", "at": time.time(), "code": None, "dismissed": False,
                 "counts": {**dict.fromkeys(move_import.OUTCOMES, 0), **counts}, "failures": failures}
        state = fixtures.example_state("typical", now=time.time())
        state["move_import"] = {"imports": [move_import._entry_public(entry)], "on_pc": 0}
        self.assertEqual(contract.validate_state(state), [])
        self.show(state)
        words = self.browser.js(self.BANNER)
        self.assertEqual(self.browser.problems, [])
        return " ".join(words.split())

    def test_an_account_left_out_and_an_account_that_failed_are_both_named(self):
        failed = {"label": "codex-wrong", "provider": "codex", "reason": move_import.REASONS["identity"]}
        words = self.banner({"new": 1, "left_out": 1, "failed": 1}, [failed])
        self.assertIn("Import incomplete 1 account could not be imported. 1 Claude Desktop account was left out. This version "
                      "of AccountDeck does not manage Claude Desktop. There is no confirmation code, because the code covers "
                      "every account in the bundle. The PC keeps its accounts: use Cancel move there. "
                      "codex-wrong: Its sign-in in the bundle belongs to another account.", words)
        self.assertNotIn("export again", words)

    def test_an_account_that_failed_alone_can_be_exported_again(self):
        failed = {"label": "codex-wrong", "provider": "codex", "reason": move_import.REASONS["identity"]}
        words = self.banner({"new": 1, "failed": 1}, [failed])
        self.assertIn("Import incomplete 1 account could not be imported, so there is no confirmation code. Use Cancel move "
                      "on the PC and export again. codex-wrong: Its sign-in in the bundle belongs to another account.", words)

    def test_the_sheet_of_an_import_that_still_runs_promises_its_result_not_a_code(self):
        """A bundle with a Claude Desktop account gets no code, so the sheet must not say one is coming."""
        self.show(fixtures.example_state("typical", now=time.time()))
        browser = self.browser

        def button(name):
            return f"[...document.querySelectorAll('#modal button')].find((b) => b.textContent.trim() === '{name}')"

        browser.js("AD.move.open()")
        browser.click("document.getElementById('move-path')")
        browser.type("/tmp/accounts.adeck")
        browser.click("document.getElementById('move-passphrase')")
        browser.type("a passphrase")
        browser.click(button("Preview Import"))
        browser.click(button("Import"))
        modal = "document.getElementById('modal').innerText"
        words = " ".join(browser.wait(f"{modal}.includes('Importing') && {modal}").split())
        self.assertIn("The result appears here when the import finishes. It also appears on the dashboard.", words)
        self.assertNotIn("code", words.lower())
        self.assertEqual(browser.problems, [])

    def test_an_account_left_out_alone(self):
        words = self.banner({"new": 1, "left_out": 1}, [])
        self.assertIn("Import incomplete 1 Claude Desktop account was left out. This version of AccountDeck does not manage "
                      "Claude Desktop. The other accounts are saved on this Mac. There is no confirmation code", words)
