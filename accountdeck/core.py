"""Storage primitives: user-facing errors, atomic writes, the encrypted vault with its generation ring,
the SQLite store (schema v2), write-ahead file transactions and the per-user owner lock."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import uuid

# The owner lock (flock) lives with the other macOS code; it is re-exported here for its callers
# (run.py, cli_accounts). platform.locks imports nothing else from the package.
from .platform.locks import OwnerLock  # noqa: F401  (re-export)
from .usage import saved_reading


# "system": macOS refused something (a launch, a folder, free disk space) rather than a provider.
ERROR_CODES = frozenset({
    "invalid", "not_found", "busy", "login_needed", "reauth_required", "blocked", "identity",
    "unsupported", "network", "rate_limited", "timeout", "vault", "recovery", "not_installed",
    "system", "codex", "safety", "cancelled", "confirm_required", "needs_reconfirm", "stale_plan",
    "outage", "read_only",
})


class DeckError(Exception):
    """A failure the user can act on. `str(exc)` is the message shown and `code` one of
    ERROR_CODES; `retry_after` is set (seconds) when a provider asked us to wait; `plan` carries a
    fresh SwitchPlan for `confirm_required` and `needs_reconfirm`."""

    def __init__(self, message, code="invalid", retry_after=None, *, plan=None):
        super().__init__(message)
        self.code, self.retry_after, self.plan = code, retry_after, plan


def __getattr__(name):
    # SafetyViolation lives in config.py, which imports DeckError from this module; resolving it on
    # first use keeps the two modules free of an import cycle.
    if name == "SafetyViolation":
        from .config import SafetyViolation
        return SafetyViolation
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data):
    """Replaces `path` with `data` durably: the bytes reach the disk (F_FULLFSYNC, which a plain fsync
    does not guarantee on macOS) before the rename, and the rename itself is synced through the folder."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            fcntl.fcntl(stream.fileno(), fcntl.F_FULLFSYNC)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def vault_cipher(data_dir, *, keychain=None):
    """The Vault's `protect` cipher: AES-256-GCM under a per-data-folder key
    in the keychain (`keychain=None` is the login keychain, which safe mode refuses)."""
    from .platform.keychain import VaultCipher   # imports core: taken in on use
    return VaultCipher(data_dir, keychain=keychain)


# Vault files end in ".dpapi" because the format predates the Mac port (the Windows build protected them
# with DPAPI). The suffix is part of the on-disk format: existing data folders keep loading as they are.
VAULT_SUFFIX = ".dpapi"
GENERATIONS = 3          # current copy + g1 + g2 of every account profile
PREV_TTL = 2 * 3600      # how long a guarded overwrite keeps the displaced copy
_ORPHAN_TTL = 3600       # temporary files an interrupted atomic_write left behind
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,159}")
_SINGLE_COPY = ("transaction-", "quarantine-")
SET_ASIDE = "set-aside-"
OPEN_STATUSES = ("prepared", "verifying", "conflict")     # a switch record that still waits for recovery
MAX_PAUSE = 3600        # the longest an account's usage checks stay paused on a provider's word; then it is asked again
RECORD_TTL = 30 * 86400                                   # closed and set-aside records are kept this long


def _blob_digest(blob):
    return digest(blob)[:16]


class Vault:
    """Encrypted JSON records, one file per key; `protect(data, decrypt=False)` is the cipher
    (vault_cipher in the service, a stand-in in tests and the demo).

    Account profiles keep a ring of GENERATIONS copies (`<key>.dpapi`, `<key>.g1.dpapi`,
    `<key>.g2.dpapi`); transaction and quarantine records are single-copy. Keys are ASCII letters,
    digits and dashes, so a key can never name a path outside the vault."""

    def __init__(self, root, protect):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.protect = protect
        self.lock = threading.RLock()

    def path(self, key):
        return self._file(key)

    def put(self, key, value, *, generation=True):
        """Store value as the current copy and return its digest.

        With `generation` (account profiles) the replaced copy moves to g1 and g1 to g2. Storing a
        value equal to the current one changes nothing, so repeated captures never push the older
        fallbacks out of the ring."""
        data = json_bytes(value)
        with self.lock:
            current = self._file(key)
            rotate = generation and not key.startswith(_SINGLE_COPY) and current.exists()
            if rotate and self._holds(current, data):
                return _blob_digest(current.read_bytes())
            blob = self.protect(data)
            if rotate:
                self._shift(key, GENERATIONS - 1)
            atomic_write(current, blob)
        return _blob_digest(blob)

    def get(self, key, generation=0):
        """Decrypt one generation (0 = current); DeckError("vault") when missing or unreadable."""
        try:
            raw = self._slot(key, generation).read_bytes()
        except FileNotFoundError:
            raise DeckError("This account's saved sign-in is missing. Reconnect it.", "vault") from None
        return self._decode(raw)

    def generations(self, key):
        """Stored generations, newest first: [{"n", "at", "digest"}]. The digest (of the encrypted
        file) links a generation to its Store.generations() metadata and reveals nothing secret."""
        found = []
        for n in range(GENERATIONS):
            path = self._slot(key, n)
            try:
                raw, at = path.read_bytes(), path.stat().st_mtime
            except FileNotFoundError:
                continue
            found.append({"n": n, "at": at, "digest": _blob_digest(raw)})
        return found

    def newest_good(self, key, is_good):
        """(n, profile) of the newest generation that decrypts and satisfies is_good(profile), or
        None. Restores prefer it, so one bad capture never strands an account."""
        for generation in self.generations(key):
            try:
                profile = self.get(key, generation["n"])
            except DeckError:
                continue
            if is_good(profile):
                return generation["n"], profile
        return None

    def put_guarded(self, key, incoming, source, identity_of, freshness_of, *, expected=None):
        """Freshness-guarded overwrite for rotating credentials (Codex, A9).

        `incoming` replaces the stored copy only when identity_of() gives the same identity and
        freshness_of() says it is at least as fresh; a tie goes to incoming only when `source` is
        "live" (read from the real credential file), since that copy is the one the app itself uses.
        `expected`: the stored copy `incoming` was renewed from; while the vault still holds it,
        incoming replaces it whatever freshness_of() says.
        The displaced copy is kept as `<key>.prev` for PREV_TTL. Returns "accepted", "kept"
        (the stored copy is fresher) or "conflict" (identity differs or is unknown); the caller
        records conflicts. The generation ring is never used here: an older refresh token is spent,
        and restoring one would trip the provider's reuse detection."""
        identity = identity_of(incoming)
        data = json_bytes(incoming)
        with self.lock:
            current = self._file(key)
            if identity is None:
                return "conflict"
            if current.exists():
                try:
                    stored = self._decode(current.read_bytes())
                except DeckError:
                    stored = None       # unreadable: replace it and keep its bytes as .prev
                if stored is not None and identity_of(stored) is not None:
                    if identity_of(stored) != identity:
                        return "conflict"
                    if stored == json.loads(data):
                        return "accepted"
                    if expected is not None and stored == expected:
                        atomic_write(self._file(key, ".prev"), current.read_bytes())
                        atomic_write(current, self.protect(data))
                        return "accepted"
                    fresh, held = freshness_of(incoming), freshness_of(stored)
                    if held is not None and (fresh is None or fresh < held):
                        return "kept"
                    if fresh == held and source != "live":
                        return "kept"
                atomic_write(self._file(key, ".prev"), current.read_bytes())
            atomic_write(current, self.protect(data))
        return "accepted"

    def quarantine(self, key, raw_bytes):
        """Keep unreadable credential bytes, encrypted, as `quarantine-<key>-<ms>` for review instead
        of deleting them; returns the record key."""
        self._file(key)                         # refuses a key that is not a plain identifier
        with self.lock:
            stamp = int(time.time() * 1000)
            while self._file(f"quarantine-{key}-{stamp}").exists():
                stamp += 1
            name = f"quarantine-{key}-{stamp}"
            atomic_write(self._file(name), self.protect(bytes(raw_bytes)))
        return name

    def set_aside(self, key):
        """Moves a record out of its key, bytes unchanged, to `set-aside-<key>`: a switch record that
        cannot be read or applied stops counting as pending, and what it holds is kept. Returns the new key."""
        name = SET_ASIDE + key
        with self.lock:
            os.utime(self._file(key))        # kept for RECORD_TTL from now, however old the record already was
            os.replace(self._file(key), self._file(name))
        return name

    def prune(self, now=None):
        """Delete .prev copies older than PREV_TTL and orphaned temporary files; returns their names."""
        now = time.time() if now is None else now
        expired = [(path, PREV_TTL) for path in self.root.glob("*.prev" + VAULT_SUFFIX)]
        expired += [(path, _ORPHAN_TTL) for path in self.root.glob(".write-*")]
        removed = []
        with self.lock:
            for path, ttl in expired:
                try:
                    if now - path.stat().st_mtime > ttl:
                        path.unlink()
                        removed.append(path.name)
                except OSError:
                    pass                        # already gone, or briefly held open: the next prune retries
        return removed

    def remove(self, key):
        """Delete every copy of key: all generations, the .prev copy, and the `.restore` copy an older
        version of the app may have left."""
        with self.lock:
            for n in range(GENERATIONS):
                self._slot(key, n).unlink(missing_ok=True)
            self._file(key, ".prev").unlink(missing_ok=True)
            self._file(key, ".restore").unlink(missing_ok=True)

    def _file(self, key, suffix=""):
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise DeckError("Invalid credential identifier.")
        return self.root / f"{key}{suffix}{VAULT_SUFFIX}"

    def _slot(self, key, n):
        if n not in range(GENERATIONS):
            raise ValueError(f"generation must be between 0 and {GENERATIONS - 1}")
        return self._file(key, f".g{n}" if n else "")

    def _decode(self, raw):
        try:
            return json.loads(self.protect(raw, decrypt=True))
        except ValueError:
            raise DeckError("This saved sign-in could not be read. Reconnect this account.", "vault") from None

    def _holds(self, path, data):
        try:
            return self._decode(path.read_bytes()) == json.loads(data)
        except (DeckError, OSError):
            return False

    def _shift(self, key, count):
        """Move generations 0..count-1 one slot older (overwriting slot count). The current copy
        stays in place until the caller replaces it, so a crash never leaves a key without one."""
        for n in range(count - 1, 0, -1):
            if self._slot(key, n).exists():
                os.replace(self._slot(key, n), self._slot(key, n + 1))
        current = self._slot(key, 0)
        if current.exists():
            self._copy(current, self._slot(key, 1))

    @staticmethod
    def _copy(source, target):
        """Copy bytes and modification time, so a generation's age survives rotation."""
        stat = source.stat()
        atomic_write(target, source.read_bytes())
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))


SCHEMA_VERSION = 2
# Who manages an account's sign-in. "v2": this app. "on_pc": imported from the PC while it was still
# signed in there; the row has history but no secret until a hand-over bundle fills it. "moved": the PC
# build's mark for an account it exported. "v1" / "v2-pending": AccountDeck 1 era owners on the PC.
# Every value stays valid so any data folder loads; only "v2" (and the AccountDeck 1 owners, which behave
# like it for reads) has a usable sign-in here.
OWNERS = ("v2", "v2-pending", "v1", "moved", "on_pc")
FROZEN_OWNERS = frozenset({"moved", "on_pc"})


ON_PC_MESSAGE = ("This account is still signed in on the PC, so this Mac never uses it. Hand it over from the PC "
                 "to use it here.")
MOVED_MESSAGE = "This account was moved to another computer, so this Mac does not use or renew its sign-in."


def is_frozen(row, store=None):
    """True for an account whose sign-in belongs to another computer ("moved", "on_pc"): it is never
    read, renewed, saved over or switched to. `store` is accepted for older call sites and unused."""
    return bool(row) and row.get("owner") in FROZEN_OWNERS


class FrozenAccountError(DeckError):
    """Refuses to use or save the sign-in of an account another computer owns (code `read_only`)."""

    def __init__(self, row=None):
        super().__init__(ON_PC_MESSAGE if (row or {}).get("owner") == "on_pc" else MOVED_MESSAGE, "read_only")


EVENT_LIMIT = 500
HISTORY_LIMIT = 700
SNAPSHOT_EVERY = 900                     # one v1-style snapshot per account per quarter hour
GENERATION_ROWS = 10                     # metadata rows kept per account (the vault ring holds 3)
_DAY = 86400
_SNAPSHOT_DAYS, _READING_DAYS, _REFILL_DAYS, _ALERT_DAYS = 90, 7, 30, 7

_ACCOUNTS = """
CREATE TABLE accounts(id TEXT PRIMARY KEY, provider TEXT NOT NULL, identity TEXT NOT NULL,
  label TEXT NOT NULL, email TEXT, plan TEXT, org TEXT, created REAL NOT NULL, updated REAL NOT NULL,
  error TEXT, error_code TEXT, next_refresh REAL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 0,
  imported_from TEXT, owner TEXT NOT NULL DEFAULT 'v2', credential_state TEXT NOT NULL DEFAULT 'unknown',
  last_good_usage TEXT, last_good_at REAL, expires_at REAL, last_outcome TEXT, paused_until REAL,
  flags TEXT, UNIQUE(provider, identity));
"""

_TABLES = """
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at REAL, kind TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, account TEXT, at REAL, usage TEXT);
CREATE TABLE IF NOT EXISTS readings(id INTEGER PRIMARY KEY, account TEXT NOT NULL,
  window_id TEXT NOT NULL, at REAL NOT NULL, used REAL NOT NULL, reset REAL);
CREATE TABLE IF NOT EXISTS refills(id INTEGER PRIMARY KEY, account TEXT NOT NULL,
  window_id TEXT NOT NULL, reset_at REAL NOT NULL, confirmed_at REAL NOT NULL, previous_used REAL,
  UNIQUE(account, window_id, reset_at));
CREATE TABLE IF NOT EXISTS generations(id INTEGER PRIMARY KEY, account TEXT NOT NULL,
  digest TEXT NOT NULL, at REAL NOT NULL, source TEXT NOT NULL, health TEXT, meta TEXT,
  UNIQUE(account, digest));
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, level TEXT,
  account TEXT, window_id TEXT, title TEXT NOT NULL, message TEXT, at REAL NOT NULL,
  snoozed_until REAL, dismissed INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS snapshots_by_account ON snapshots(account, at);
CREATE INDEX IF NOT EXISTS readings_by_window ON readings(account, window_id, at);
CREATE INDEX IF NOT EXISTS readings_by_time ON readings(at);
CREATE INDEX IF NOT EXISTS refills_by_time ON refills(confirmed_at);
CREATE INDEX IF NOT EXISTS generations_by_account ON generations(account, at);
CREATE INDEX IF NOT EXISTS alerts_by_time ON alerts(at);
"""

_ACCOUNT_COLUMNS = (
    "id", "provider", "identity", "label", "email", "plan", "org", "created", "updated", "error",
    "error_code", "next_refresh", "sort_order", "imported_from", "owner", "credential_state",
    "last_good_usage", "last_good_at", "expires_at", "last_outcome", "paused_until", "flags",
)
_SELECT_ACCOUNTS = "SELECT " + ", ".join(_ACCOUNT_COLUMNS) + " FROM accounts"
_UPDATABLE = frozenset(_ACCOUNT_COLUMNS) - {"id", "provider", "identity", "created", "updated"}
_JSON_COLUMNS = ("last_good_usage", "flags")


def _statements(script):
    return [part.strip() for part in script.split(";") if part.strip()]


def _encode(value):
    return None if value is None else json.dumps(value, separators=(",", ":"))


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _account(row):
    account = dict(row)
    saved = json.loads(account["last_good_usage"]) if account["last_good_usage"] else None
    account["last_good_usage"] = saved_reading(account.get("provider"), saved)
    account["flags"] = json.loads(account["flags"]) if account["flags"] else {}
    return account


class Store:
    """Accounts, readings and app state in SQLite (schema v2, `PRAGMA user_version`).

    No credentials or raw provider payloads enter this database: `last_good_usage` holds the last
    successful normalized reading and every other column is metadata. `clock` supplies timestamps
    (the demo passes its virtual clock)."""

    def __init__(self, root, *, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "accounts.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self._migrate()
            self._cap_pauses()
        except BaseException:
            self.db.close()
            raise

    def close(self):
        with self.lock:
            self.db.close()

    def _cap_pauses(self):
        """A version before MAX_PAUSE stored whatever pause a provider asked for, years included, and nothing
        reads such an account again to correct it. No saved pause outlasts the cap."""
        limit = self.clock() + MAX_PAUSE
        with self.lock:
            self.db.execute("UPDATE accounts SET paused_until=? WHERE paused_until>?", (limit, limit))
            self.db.commit()

    def _migrate(self):
        """Create schema v2 in a new folder. One transaction: all or nothing. A schema-v2 database (every
        AccountDeck 2 build) opens unchanged; an older or newer one is refused before anything changes."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise DeckError("This data was saved by a newer AccountDeck. Update AccountDeck 2 to open it.",
                                    "unsupported")
                if version < SCHEMA_VERSION:
                    if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone():
                        # Only AccountDeck 1 (Windows) wrote an unversioned accounts table.
                        raise DeckError("This folder holds AccountDeck 1 data, which this Mac build cannot open.",
                                        "unsupported")
                    for statement in _statements(_ACCOUNTS + _TABLES):
                        self.db.execute(statement)
                    self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    # ---- accounts ----
    def accounts(self):
        """Every account in creation order per provider; JSON columns decoded (flags defaults to {})."""
        with self.lock:
            rows = self.db.execute(_SELECT_ACCOUNTS + " ORDER BY provider, created, rowid").fetchall()
        return [_account(row) for row in rows]

    def account(self, key):
        with self.lock:
            row = self.db.execute(_SELECT_ACCOUNTS + " WHERE id=?", (key,)).fetchone()
        return _account(row) if row else None

    def find(self, provider, identity):
        with self.lock:
            row = self.db.execute(_SELECT_ACCOUNTS + " WHERE provider=? AND identity=?",
                                  (provider, identity)).fetchone()
        return _account(row) if row else None

    def save_account(self, provider, identity, label, email="", plan="", org="", *,
                     imported_from=None, owner="v2"):
        """Add an account, or refresh email/plan/org of the saved one with the same identity (its
        label, owner and position stay). Returns the account id."""
        if owner not in OWNERS:
            raise ValueError(f"Invalid account owner {owner!r}")
        now = self.clock()
        with self.lock:
            old = self.db.execute("SELECT id FROM accounts WHERE provider=? AND identity=?",
                                  (provider, identity)).fetchone()
            if old:
                self.db.execute("UPDATE accounts SET email=?, plan=?, org=?, updated=? WHERE id=?",
                                (email, plan, org, now, old["id"]))
                self.db.commit()
                return old["id"]
            key = uuid.uuid4().hex
            self.db.execute(
                """INSERT INTO accounts(id, provider, identity, label, email, plan, org, created, updated,
                     sort_order, imported_from, owner)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(sort_order) + 1, 0) FROM accounts), ?, ?)""",
                (key, provider, identity, label or email or provider.title(), email, plan, org, now, now,
                 imported_from, owner))
            self.db.commit()
        return key

    def update(self, key, **fields):
        """Change account columns other than id/provider/identity/created/updated (ValueError
        otherwise); last_good_usage and flags are stored as JSON."""
        if not fields or not set(fields) <= _UPDATABLE:
            raise ValueError("Invalid account update")
        if "owner" in fields and fields["owner"] not in OWNERS:
            raise ValueError(f"Invalid account owner {fields['owner']!r}")
        values = [_encode(value) if name in _JSON_COLUMNS else value for name, value in fields.items()]
        with self.lock:
            self.db.execute("UPDATE accounts SET " + ", ".join(name + "=?" for name in fields) + " WHERE id=?",
                            (*values, key))
            self.db.commit()

    def reorder(self, ids):
        """Manual order: the given accounts first, in that order, then the rest as they were."""
        if len(set(ids)) != len(ids):
            raise DeckError("Each account can appear only once in the order.")
        with self.lock:
            existing = [row[0] for row in self.db.execute("SELECT id FROM accounts ORDER BY sort_order, created, rowid")]
            if not set(ids) <= set(existing):
                raise DeckError("That order names an account that no longer exists.")
            order = list(ids) + [key for key in existing if key not in set(ids)]
            self.db.executemany("UPDATE accounts SET sort_order=? WHERE id=?",
                                [(position, key) for position, key in enumerate(order)])
            self.db.commit()

    def remove(self, key):
        with self.lock:
            self.db.execute("DELETE FROM accounts WHERE id=?", (key,))
            for table in ("snapshots", "readings", "refills", "generations", "alerts"):
                self.db.execute(f"DELETE FROM {table} WHERE account=?", (key,))
            self.db.commit()

    # ---- readings ----
    def observe(self, key, usage, *, at=None):
        """Persist a successful reading: the account's last good reading, one `readings` row per
        window with a known percentage (kept 7 days for pace, trend and history) and at most one
        v1-style snapshot per quarter hour (kept 90 days). Clears the account's error."""
        now = self.clock()
        at = now if at is None else at
        payload = _encode(usage)
        rows = [(key, window["id"], at, window["used"], window.get("reset"))
                for window in usage.get("windows") or ()
                if isinstance(window, dict) and isinstance(window.get("id"), str) and _is_number(window.get("used"))]
        with self.lock:
            self.db.execute("UPDATE accounts SET last_good_usage=?, last_good_at=?, error=NULL, error_code=NULL "
                            "WHERE id=?", (payload, at, key))
            self.db.executemany("INSERT INTO readings(account, window_id, at, used, reset) VALUES(?, ?, ?, ?, ?)", rows)
            last = self.db.execute("SELECT MAX(at) FROM snapshots WHERE account=?", (key,)).fetchone()[0]
            if last is None or at - last >= SNAPSHOT_EVERY:
                self.db.execute("INSERT INTO snapshots(account, at, usage) VALUES(?, ?, ?)", (key, at, payload))
            self.db.execute("DELETE FROM snapshots WHERE at < ?", (now - _SNAPSHOT_DAYS * _DAY,))
            self.db.execute("DELETE FROM readings WHERE at < ?", (now - _READING_DAYS * _DAY,))
            self.db.commit()

    def readings(self, key, window, since=None):
        """[{"at", "used", "reset"}] of one window, oldest first."""
        with self.lock:
            rows = self.db.execute("SELECT at, used, reset FROM readings WHERE account=? AND window_id=? AND at>=? "
                                   "ORDER BY at, id", (key, window, since or 0)).fetchall()
        return [dict(row) for row in rows]

    def history(self, key, since=None, limit=HISTORY_LIMIT):
        """/api/history payload, oldest first, each list capped at limit (at most 700):
        {"account", "readings": [{"at", "windows": {window: used}}], "snapshots": [{"at", "usage"}]}."""
        limit = max(1, min(int(limit), HISTORY_LIMIT))
        with self.lock:
            rows = self.db.execute("SELECT at, window_id, used FROM readings WHERE account=? AND at>=? ORDER BY at, id",
                                   (key, since or 0)).fetchall()
            snapshots = self.db.execute("SELECT at, usage FROM snapshots WHERE account=? AND at>=? "
                                        "ORDER BY at DESC, id DESC LIMIT ?", (key, since or 0, limit)).fetchall()
        grouped = {}
        for row in rows:
            grouped.setdefault(row["at"], {})[row["window_id"]] = row["used"]
        return {"account": key,
                "readings": [{"at": at, "windows": windows} for at, windows in grouped.items()][-limit:],
                "snapshots": [{"at": row["at"], "usage": json.loads(row["usage"])} for row in reversed(snapshots)]}

    def record_refill(self, account, window, *, reset_at, confirmed_at, previous_used):
        """Record a confirmed refill once per (account, window, reset); True when it is new."""
        with self.lock:
            cursor = self.db.execute("INSERT OR IGNORE INTO refills(account, window_id, reset_at, confirmed_at, "
                                     "previous_used) VALUES(?, ?, ?, ?, ?)",
                                     (account, window, reset_at, confirmed_at, previous_used))
            self.db.execute("DELETE FROM refills WHERE confirmed_at < ?", (self.clock() - _REFILL_DAYS * _DAY,))
            self.db.commit()
        return cursor.rowcount == 1

    def refills(self, since):
        """Refills confirmed at or after since, oldest first:
        [{"account", "window", "reset_at", "confirmed_at", "previous_used"}]."""
        with self.lock:
            rows = self.db.execute("SELECT account, window_id, reset_at, confirmed_at, previous_used FROM refills "
                                   "WHERE confirmed_at>=? ORDER BY confirmed_at, id", (since,)).fetchall()
        return [{"account": row["account"], "window": row["window_id"], "reset_at": row["reset_at"],
                 "confirmed_at": row["confirmed_at"], "previous_used": row["previous_used"]} for row in rows]

    # ---- vault generation metadata ----
    def record_generation(self, account, digest, *, source, health=None, meta=None, at=None):
        """Non-secret facts about a vault generation (digest from Vault.put): when and how it was
        captured, its health and e.g. the Desktop version or token-expiry summary in `meta`."""
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO generations(account, digest, at, source, health, meta) "
                            "VALUES(?, ?, ?, ?, ?, ?)",
                            (account, digest, self.clock() if at is None else at, source, health, _encode(meta)))
            self.db.execute("DELETE FROM generations WHERE account=? AND id NOT IN (SELECT id FROM generations "
                            "WHERE account=? ORDER BY at DESC, id DESC LIMIT ?)", (account, account, GENERATION_ROWS))
            self.db.commit()

    def generations(self, account):
        """Recorded generation metadata, newest first: [{"digest", "at", "source", "health", "meta"}]."""
        with self.lock:
            rows = self.db.execute("SELECT digest, at, source, health, meta FROM generations WHERE account=? "
                                   "ORDER BY at DESC, id DESC", (account,)).fetchall()
        return [{**dict(row), "meta": json.loads(row["meta"]) if row["meta"] else None} for row in rows]

    # ---- alerts ----
    def add_alert(self, alert):
        """Persist a delivered alert ({kind, title, level?, account?, window?, message?, at?,
        snoozed_until?}); returns its id "al_<n>". Alerts older than a week are dropped."""
        now = self.clock()
        with self.lock:
            cursor = self.db.execute(
                "INSERT INTO alerts(kind, level, account, window_id, title, message, at, snoozed_until) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (alert["kind"], alert.get("level"), alert.get("account"), alert.get("window"), alert["title"],
                 alert.get("message"), alert.get("at", now), alert.get("snoozed_until")))
            self.db.execute("DELETE FROM alerts WHERE at < ?", (now - _ALERT_DAYS * _DAY,))
            self.db.commit()
        return f"al_{cursor.lastrowid}"

    def alerts(self, *, include_dismissed=False, limit=50):
        """Newest first, as the rows the snapshot shows."""
        with self.lock:
            rows = self.db.execute("SELECT * FROM alerts" + ("" if include_dismissed else " WHERE dismissed=0")
                                   + " ORDER BY at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": f"al_{row['id']}", "kind": row["kind"], "level": row["level"], "account": row["account"],
                 "window": row["window_id"], "title": row["title"], "message": row["message"], "at": row["at"],
                 "snoozed_until": row["snoozed_until"], "dismissed": bool(row["dismissed"])} for row in rows]

    # ---- settings and events ----
    def setting(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        self.set_settings({key: value})

    def set_settings(self, values):
        """Stores several settings in one transaction: all of them or none."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.executemany("INSERT OR REPLACE INTO settings VALUES(?, ?)",
                                    [(key, json.dumps(value)) for key, value in values.items()])
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def event(self, kind, message):
        with self.lock:
            self.db.execute("INSERT INTO events(at, kind, message) VALUES(?, ?, ?)", (self.clock(), kind, message))
            self.db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                            (EVENT_LIMIT,))
            self.db.commit()

    def events(self, limit=12):
        """Newest first: [{"id", "at", "kind", "message"}]."""
        with self.lock:
            rows = self.db.execute("SELECT id, at, kind, message FROM events ORDER BY id DESC LIMIT ?",
                                   (limit,)).fetchall()
        return [dict(row) for row in rows]


class FileTransaction:
    """Encrypted write-ahead backup; rollback only bytes still owned by this transaction."""

    def __init__(self, vault, roots, key=None):
        self.vault = vault
        self.roots = [Path(p).resolve() for p in roots]
        self.key = key or "transaction-" + uuid.uuid4().hex
        self.doc = {"status": "prepared", "at": time.time(), "changes": []}

    @classmethod
    def load(cls, vault, roots, key):
        """Reopen a persisted transaction, e.g. one recover_transactions() reported."""
        tx = cls(vault, roots, key)
        tx.doc = vault.get(key)
        return tx

    def check(self, path):
        path = Path(path).resolve()
        if not any(path.is_relative_to(root) for root in self.roots):
            raise DeckError("A file is outside the permitted application folders.", "safety")
        return path

    def write(self, path, data):
        path = self.check(path)
        before = path.read_bytes() if path.exists() else None
        change = {"path": str(path), "before": base64.b64encode(before).decode() if before is not None else None,
                  "after": digest(data) if data is not None else None}
        self.doc["changes"].append(change)
        try:
            self.save()
        except BaseException:
            self.doc["changes"].pop()        # not on disk, so not in memory either: nothing was written
            raise
        if data is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, data)

    def save(self):
        """Persist doc, e.g. after the caller set doc["status"] = "verifying"."""
        self.vault.put(self.key, self.doc, generation=False)

    def commit(self):
        self.close("complete")

    def close(self, status="rolled_back"):
        """Marks the record finished without touching a file: the caller put the files right itself. When
        that cannot be saved, the status stays what the disk says."""
        previous, self.doc["status"] = self.doc["status"], status
        try:
            self.save()
        except BaseException:
            self.doc["status"] = previous
            raise

    def rollback(self):
        conflicts = []
        for item in reversed(self.doc["changes"]):
            path = self.check(item["path"])
            current = path.read_bytes() if path.exists() else None
            before = base64.b64decode(item["before"]) if item["before"] is not None else None
            if current == before:
                continue
            if (digest(current) if current is not None else None) != item["after"]:
                conflicts.append(path.name)
                continue
            if before is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, before)
        self.doc["status"] = "conflict" if conflicts else "rolled_back"
        self.save()
        if conflicts:
            raise DeckError("Recovery paused because the app changed files after the switch. Your encrypted backup is preserved.", "recovery")


def recover_transactions(vault):
    """The keys of the records a switch left open, which the Recover action settles (recovery.press). A
    record that cannot be read is listed too: Recover sets it aside. Closed records and set-aside ones
    older than RECORD_TTL are deleted."""
    pending, old = [], time.time() - RECORD_TTL
    for path in vault.root.glob("transaction-*" + VAULT_SUFFIX):
        key = path.name.removesuffix(VAULT_SUFFIX)
        if not _KEY.fullmatch(key):
            continue
        try:
            doc = vault.get(key)
        except DeckError:
            pending.append(key)
            continue
        if doc.get("status") in OPEN_STATUSES:
            pending.append(key)
        elif doc.get("at", time.time()) < old:
            path.unlink()
    for path in vault.root.glob(SET_ASIDE + "*" + VAULT_SUFFIX):
        if path.stat().st_mtime < old:
            path.unlink()
    return pending
