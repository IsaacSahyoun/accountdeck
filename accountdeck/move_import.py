"""Importing the PC's accounts on the Mac: the receiving half of the move.

Actions (engine `_do_*`, contract.ACTIONS):
- `import-bundle-preview` {path, passphrase}: opens the bundle and says, per account, what an import would
  do, and which of this Mac's settings the PC's would replace. Nothing is written.
- `import-bundle` {path, passphrase, wait?}: ImportBundle, one step per account.
  Applying a bundle again is safe: every account is an identity-keyed upsert where the newer sign-in wins.
- `import-bundle-dismiss` {bundle_id}: hides that import's confirmation code or failure list.

What each account becomes:
- Claude Code and Codex: a vault record in the shape cli_accounts.CLIAccounts.save and AccountService.save
  write, with the same row flags, so the Mac's switch flows use it unchanged.
- Claude Desktop: left out. This build does not manage Claude Desktop, so nothing of such an account is
  stored: no row, no sign-in, no history. The preview and the result say how many were left out.
- An account `live_on_pc` (still signed in on the PC): a row with owner ON_PC ("on_pc") and its history,
  without any secret. Such a row is never read, refreshed or switched: Engine.targets skips it
  (core.is_frozen), check_action refuses account actions naming it, and its flags keep it out of switching
  and terminal auto-switch. A hand-over bundle fills it: the row gets its sign-in and becomes "v2".

The confirmation code is move_bundle.code_for_imported over the accounts this run stored, so it
exists only when every account in the bundle was stored (an "on_pc" row counts). A bundle that holds a
Claude Desktop account therefore never gets a code: the PC is never told that an account it still holds
alone arrived here. The code is kept in the encrypted ledger record `move-import` and shown in the state
until dismissed; it never goes into logs, events or error messages. Neither does any token or the
passphrase.
"""
from __future__ import annotations

import copy
import json
import math
import threading
import time
import weakref

from . import config, contract, move_bundle as mb
from .config import SafetyViolation
from .core import ON_PC_MESSAGE, DeckError, is_frozen
from .diagnostics import account_ref, log
from .operations import Operation
from .procs import is_test_owned_path
from .providers import codex as codex_module
from .providers.claude_cli import expires as cli_expires, fingerprint as cli_fingerprint

ON_PC = "on_pc"
IMPORTED_FROM = "pc"
IMPORT_KIND, PREVIEW_KIND, DISMISS_KIND = "import-bundle", "import-bundle-preview", "import-bundle-dismiss"
LEDGER_KEY = "move-import"
GENERATION_SOURCE = "import-pc"
MIN_SCRYPT_N = mb.SCRYPT_MIN_N           # tests lower it together with the scrypt cost of the bundles they seal
IMPORT_WAIT = 30.0                       # how long `import-bundle` waits for its operation before answering
LEDGER_KEEP = 10
SNAPSHOT_DAYS, READING_DAYS, REFILL_DAYS = 90, 7, 30   # what core.Store keeps
# A main bundle's settings replace this Mac's. These are the words the preview and the result use for each
# one that changes: every key of move_bundle.PORTABLE_SETTINGS this build still has, in the order they are
# listed. A bundle's setting that this build does not have (Claude Desktop's) is dropped (_portable).
SETTING_NAMES = {
    "cli_autoswitch": "automatic terminal switching", "cli_threshold": "the switch point",
    "cli_order": "the switching order", "cli_manual_order": "your own account order",
    "cli_early_switch": "early switching for weekly allowance", "pinned_model": "the pinned model",
    "use_soon": "using expiring quota first", "confirm_when_idle": "asking before a switch when Claude is idle",
    "usage_display": "showing usage as used or left", "sort": "account sorting", "density": "cards or rows",
    "show_other_limits": "showing other limits",
    "alerts_enabled": "alerts", "alert_low_left": "the low-limit alert level", "quiet_hours": "quiet hours",
    "notify_reset_minutes": "the reset notice time", "notifications": "notifications",
    "refresh_paused": "paused usage checks", "log_level": "log detail",
}
_DAY = 86400
LEDGER_RETRY = 60.0

CANCEL_HINT = "Use Cancel move on the PC and export again."
CODE_HINT = "Type this code on the PC to finish the move there."
LEFT_OUT = "This version of AccountDeck does not manage Claude Desktop."
NO_CODE = ("There is no confirmation code, because the code covers every account in the bundle. The PC keeps "
           "its accounts: use Cancel move there.")
REASONS = {
    "identity": "Its sign-in in the bundle belongs to another account.",
    "damaged": "Its sign-in in the bundle is damaged.",
    "unsupported": "Its sign-in comes from an AccountDeck version this one cannot read.",
    "conflict": "Another account saved here holds this sign-in.",
    "not_waiting": "It is not waiting for a hand-over on this Mac.",
    "unsaved": "It could not be saved here.",
    "history": "Its sign-in was saved, but its usage history could not be copied.",
}
ON_PC_ALLOWED = frozenset({"rename", "remove"})       # the only actions that may name an "on_pc" account
OUTCOMES = ("new", "updated", "filled", "kept", "on_pc", "left_out", "failed")
_NOTES = {"new": "Brought over.", "updated": "Already here; it now has the newer sign-in from the PC.",
          "filled": "Its sign-in from the PC is now saved here.", "kept": "Already here; the newer sign-in here is kept.",
          "on_pc": "Still signed in on the PC: listed here with its history, without its sign-in.",
          "left_out": f"Left out. {LEFT_OUT}"}
_NO_HISTORY = " Its usage history could not be copied."


# ---- the bundle file ----

def open_file(path, passphrase, *, paths, expect_kind=None):
    """(header, payload) of the bundle at `path`, or DeckError with a fixed, secret-free message. In safe
    mode only a file under a registered test root is read."""
    if not isinstance(path, str) or not path.strip():
        raise DeckError("Choose the move bundle file.", "invalid")
    if not isinstance(passphrase, str) or not passphrase:
        raise DeckError("Enter the bundle passphrase.", "invalid")
    target = paths.user_path(path.strip())
    if config.safe_mode() and not is_test_owned_path(target):
        raise SafetyViolation("Safe mode: AccountDeck 2 reads only a synthetic move bundle.")
    failure = None
    try:
        data = mb.read_bundle(target)
        return mb.open_bundle(data, passphrase, expect_kind=expect_kind, min_scrypt_n=MIN_SCRYPT_N)
    except mb.BundleError as exc:
        failure = DeckError(str(exc), exc.code)
        failure.reason = exc.reason
    except FileNotFoundError:
        failure = DeckError("The move bundle file was not found.", "not_found")
    except OSError:
        failure = DeckError("The move bundle file could not be read.", "not_found")
    raise failure       # outside the except blocks, so it carries no chained traceback


# ---- planning (shared by the preview and the import) ----

def _plan(engine, header, payload, ledger_entry):
    """[(account, row here or None, action, reason)] in bundle order. action: "new", "update", "on_pc"
    (listed without a sign-in), "fill" (a waiting "on_pc" row gets its sign-in), "refused" (a hand-over
    account no waiting row matches) or "left_out" (an account of a client this build does not manage:
    Claude Desktop; nothing of it is stored)."""
    done = set((ledger_entry or {}).get("accounts") or ())
    started = (ledger_entry or {}).get("started") or {}
    plan = []
    for account in payload["accounts"]:
        if account["provider"] not in contract.PROVIDERS:
            plan.append((account, None, "left_out", LEFT_OUT))
            continue
        row = engine.store.find(account["provider"], account["identity"])
        if header.kind == "handover":
            if row is not None and row["owner"] == ON_PC:
                plan.append((account, row, "fill", None))
            elif row is not None and (account["id"] in done or started.get(account["id"]) == row["id"]):
                # This same hand-over, including a crash after saving a row but before finish.
                plan.append((account, row, "update", None))
            else:
                plan.append((account, row, "refused", REASONS["not_waiting"]))
        elif account["live_on_pc"]:
            plan.append((account, row, "on_pc" if row is None or row["owner"] == ON_PC else "update", None))
        elif row is None:
            plan.append((account, row, "new", None))
        else:
            plan.append((account, row, "fill" if row["owner"] == ON_PC else "update", None))
    return plan


def _portable(settings):
    """The bundle's settings this build has, checked. A setting only Claude Desktop used is dropped."""
    return config.validate_settings({key: value for key, value in settings.items() if key in SETTING_NAMES})


def _settings_here(settings, mapping):
    """The bundle's settings as they apply on this Mac: the manual order lists this Mac's account ids
    (`mapping`: the PC's id -> the id here), without the accounts that were not brought over."""
    settings = copy.deepcopy(settings)
    if "cli_manual_order" in settings:
        settings["cli_manual_order"] = [mapping[key] for key in settings["cli_manual_order"] if key in mapping]
    return settings


def _settings_changed(engine, settings):
    """The names of this Mac's settings that `settings` would change."""
    current = engine.settings()
    return [name for key, name in SETTING_NAMES.items() if key in settings and settings[key] != current[key]]


def _listed(names):
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def preview(engine, path, passphrase):
    """The `import-bundle-preview` payload (see the module docstring). Writes nothing."""
    header, payload = open_file(path, passphrase, paths=engine.paths)
    plan = _plan(engine, header, payload, _ledger(engine).entry(header.bundle_id))
    accounts, counts = [], {"new": 0, "update": 0, "on_pc": 0, "fill": 0, "refused": 0, "left_out": 0}
    for account, row, action, reason in plan:
        counts[action] += 1
        accounts.append({"id": account["id"], "provider": account["provider"], "label": _label(account),
                         "plan": account["plan"], "action": action,
                         "conflict": row["label"] if row is not None and action == "update" else None,
                         "reason": reason})
    refused = counts["refused"] > 0
    # An account not saved here yet gets its id at the import; the PC's id stands in for it, so a manual
    # order that names it counts as changed.
    mapping = {account["id"]: account["id"] if row is None else row["id"] for account, row, _action, _reason in plan}
    settings = [] if refused else _settings_changed(engine, _settings_here(_portable(payload["settings"]), mapping))
    log("move.import_preview", kind=header.kind, accounts=len(accounts), refused=counts["refused"])
    return {"bundle_id": header.bundle_id, "kind": header.kind, "created": header.created,
            "source_platform": payload["source_platform"], "accounts": accounts, "counts": counts,
            "refused": refused, "settings": settings,
            "message": _preview_message(header.kind, counts, refused, settings)}


def _preview_message(kind, counts, refused, settings):
    if refused:
        return (f"{_count(counts['refused'], 'account')} in this hand-over bundle {_verb(counts['refused'], 'is')} "
                "not waiting on this Mac. Import the main move bundle first; nothing will be imported.")
    parts = []
    if kind == "handover":
        total = counts["fill"] + counts["update"]
        parts.append(f"{_count(total, 'account')} will get {'its' if total == 1 else 'their'} sign-in from the PC.")
    else:
        parts.append(f"{_count(counts['new'] + counts['update'] + counts['fill'], 'account')} will move to this Mac.")
        if counts["on_pc"]:
            parts.append(f"{_count(counts['on_pc'], 'account')} still signed in on the PC will be listed here "
                         "without a sign-in.")
    if settings:
        parts.append(f"{_count(len(settings), 'setting')} on this Mac will change to match the PC: {_listed(settings)}.")
    if counts["left_out"]:
        parts.append(_left_out(counts["left_out"], "will be") + " There will be no confirmation code, so the PC "
                     "keeps its accounts.")
    return " ".join(parts)


def _left_out(count, verb):
    """"2 Claude Desktop accounts were left out. This version …": `verb` is "was" or "will be"."""
    verb = {"was": _verb(count, "was"), "will be": "will be"}[verb]
    return f"{_count(count, 'Claude Desktop account')} {verb} left out. {LEFT_OUT}"


# ---- the import ----

class ImportBundle(Operation):
    """`import-bundle`: one step per account, then "finish". The bundle is opened (and the passphrase let
    go) when this is built, so a wrong passphrase or a refused hand-over reaches the action that asked;
    the decrypted payload lives in memory only until run() ends. `result` is the public result
    (_entry_public) once run() finished."""

    def __init__(self, engine, path, passphrase):
        header, payload = open_file(path, passphrase, paths=engine.paths)
        plan = _plan(engine, header, payload, _ledger(engine).entry(header.bundle_id))
        refused = sum(1 for _account, _row, action, _reason in plan if action == "refused")
        if refused:
            raise DeckError(f"{_count(refused, 'account')} in this hand-over bundle {_verb(refused, 'is')} not waiting "
                            "on this Mac. Import the main move bundle first. Nothing was imported.", "invalid")
        steps = [(f"account-{number}", f"{'Leaving out' if action == 'left_out' else 'Bringing over'} {_label(account)}")
                 for number, (account, _row, action, _reason) in enumerate(plan, 1)]
        super().__init__(IMPORT_KIND, steps=steps + [("finish", "Finishing the move")])
        self.cancellable = False     # accounts are saved one by one; a cancel could not say nothing changed
        self.engine, self.header, self._plan = engine, header, plan
        self._settings = _portable(payload["settings"])
        self.result = None

    def run(self):
        began = time.monotonic()
        engine, header = self.engine, self.header
        stored, failures, mapping, changed = set(), [], {}, []
        counts = dict.fromkeys(OUTCOMES, 0)
        code = None
        try:
            self.set_phase("saving", "Bringing over your accounts from the PC…")
            importer = _Importer(engine, self.clock)
            for number, (account, _row, action, _reason) in enumerate(self._plan, 1):
                with self.step(f"account-{number}"):
                    if action == "left_out":       # never stored, so it is never in `stored` and no code is made
                        counts["left_out"] += 1
                        self.note(_NOTES["left_out"], status="warning")
                        continue
                    if header.kind == "handover":
                        _ledger(engine).begin_account(header, account["id"], _row["id"], self.clock())
                    outcome, key, reason, history = importer.bring_over(account)
                    counts[outcome] += 1
                    if outcome == "failed":
                        failures.append({"label": _label(account), "provider": account["provider"], "reason": reason})
                        self.note(f"Not imported. {reason}", status="failed")
                        continue
                    stored.add(account["id"])
                    mapping[account["id"]] = key
                    self.note(_NOTES[outcome] + ("" if history else _NO_HISTORY),
                              status=None if history else "warning")
            with self.step("finish"):
                if not failures:
                    settings = _settings_here(self._settings, mapping)
                    changed = _settings_changed(engine, settings)
                    # Persist (one transaction) before updating the preference cache, so a failed
                    # write cannot look like an unchanged setting on the retry.
                    engine.store.set_settings(settings)
                    engine.update_settings(settings)
                code = None if failures else mb.code_for_imported(header, header.keys, stored)
                entry = _ledger(engine).record(header, at=self.clock(), code=code, counts=counts, failures=failures,
                                              mapping=mapping)
                self.result = _entry_public(entry)
                ready = [key for key in mapping.values() if not is_frozen(engine.store.account(key))]
                engine.scheduler.request_all(ready, self.clock())
        finally:
            self._plan = None        # drops the decrypted payload
            self._settings = None
        self.report["summary"] = _summary(header.kind, counts, failures, code, changed)
        log("move.import_done", kind=header.kind, stored=len(stored), failed=len(failures),
            left_out=counts["left_out"], on_pc=counts["on_pc"], complete=code is not None,
            ms=round((time.monotonic() - began) * 1000))
        if failures:     # the operation ends as failed; `result` (and the ledger) still list what was stored
            raise DeckError(self.report["summary"], "invalid")


class _Refused(Exception):
    """One account cannot be stored; `reason` is a REASONS key."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _Importer:
    """Stores one bundle account: bring_over returns (outcome, account id here, failure reason, whether
    its history was copied). Whatever goes wrong with one account leaves the others unaffected."""

    def __init__(self, engine, clock):
        self.engine, self.store, self.vault, self.clock = engine, engine.store, engine.vault, clock
        self.created = None

    def bring_over(self, account):
        self.created = None
        try:
            outcome, key = self._save(account)
        except SafetyViolation:
            raise
        except _Refused as exc:
            log("move.import_account", "warning", provider=account["provider"], result="failed", reason=exc.reason)
            self._forget()
            return "failed", None, REASONS[exc.reason], True
        except Exception as exc:
            log("move.import_account", "warning", provider=account["provider"], result="failed",
                error_type=type(exc).__name__, code=getattr(exc, "code", None))
            self._forget()
            return "failed", None, REASONS["unsaved"], True
        history = True
        try:
            _copy_history(self.store, key, account, self.clock())
        except Exception as exc:
            history = False
            log("move.import_history", "warning", acct=account_ref(key), error_type=type(exc).__name__)
        log("move.import_account", acct=account_ref(key), provider=account["provider"], result=outcome)
        self.engine.bump()
        if not history:
            return "failed", key, REASONS["history"], False
        return outcome, key, None, history

    def _forget(self):
        """A row this import created for an account that then failed goes again, with anything stored."""
        key, self.created = self.created, None
        if key is None:
            return
        try:
            self.vault.remove(key)
            self.store.remove(key)
        except Exception as exc:
            log("move.import_cleanup", "warning", error_type=type(exc).__name__)

    def _save(self, account):
        provider = account["provider"]
        row = self.store.find(provider, account["identity"])
        if "secret" not in account:                       # still signed in on the PC: a listing only
            if row is None:
                key = self._create(account)
                flags = {"restorable": False, **({"excluded": True} if provider == "claude_cli" else {})}
                self.store.update(key, flags=flags)
                return "on_pc", key
            self._refresh_metadata(row, account)
            return ("on_pc" if row["owner"] == ON_PC else "kept"), row["id"]
        if row is None:
            # A new row stays "on_pc" (never read) until its sign-in is stored, so an interruption never
            # leaves a "v2" row without one.
            key = self._create(account)
            self.store.update(key, flags={"restorable": False})
            row = self.store.account(key)
        else:
            self._refresh_metadata(row, account)
            row = self.store.account(row["id"])
        waiting = row["owner"] == ON_PC
        with _account_lock(self.engine, row["id"]):
            outcome = self._terminal(row, account) if provider == "claude_cli" else self._codex(row, account)
            if waiting:
                self.store.update(row["id"], owner="v2")
        if self.created is not None:
            return "new", row["id"]
        return ("filled" if waiting else outcome), row["id"]

    def _create(self, account):
        key = self.store.save_account(account["provider"], account["identity"], _label(account), account["email"],
                                      account["plan"], account["org"], imported_from=IMPORTED_FROM, owner=ON_PC)
        self.created = key
        return key

    def _refresh_metadata(self, row, account):
        self.store.save_account(row["provider"], row["identity"], row["label"], account["email"] or row["email"] or "",
                                account["plan"] or row["plan"] or "", account["org"] or row["org"] or "")

    # ---- Claude Code: CLIAccounts.save's record and flags ----
    def _terminal(self, row, account):
        record = _terminal_record(account, row, self.clock())
        key, cli = row["id"], self.engine.cli
        with cli.mutex, cli.key_lock(key):
            previous = _vault_record(self.vault, key)
            if isinstance(previous, dict) and previous.get("identity") == row["identity"]:
                if previous.get("credentials") == record.get("credentials") and previous.get("health") == record["health"]:
                    return "kept"
                if self.engine.active_identity.get("claude_cli") == row["identity"]:
                    return "kept"            # the login the terminals use here is the one to keep
                healthier = record["health"] == "ok" and previous.get("health") != "ok"
                if not healthier and (_terminal_time(previous) >= _terminal_time(record) or record["health"] != "ok"):
                    return "kept"
                record["excluded"] = bool(previous.get("excluded", record["excluded"]))
            digest = self.vault.put(key, record)
            healthy = record["health"] == "ok"
            flags = dict(self.store.account(key)["flags"])
            flags.update(restorable=healthy, health=record["health"], excluded=record["excluded"], live_expired=False)
            self.store.update(key, flags=flags, credential_state="fresh" if healthy else "reauth_required", error=None,
                              error_code=None, expires_at=cli_expires(record["credentials"]) if healthy else None)
            self.store.record_generation(key, digest, source=GENERATION_SOURCE, health=record["health"])
        return "updated"

    # ---- Codex: AccountService.save's record (put_guarded for an account saved here already) ----
    def _codex(self, row, account):
        auth = account["secret"]["data"]
        identity = codex_module.identity_key(auth)
        if identity is None:
            raise _Refused("damaged")
        if identity != row["identity"]:
            raise _Refused("identity")
        key, auth = row["id"], copy.deepcopy(auth)
        previous = _vault_record(self.vault, key)
        if previous is not None:
            if previous == auth:
                return "kept"
            status = self.vault.put_guarded(key, auth, "import", codex_module.identity_key, codex_module.freshness)
            if status == "conflict":
                raise _Refused("conflict")
            if status == "kept":
                return "kept"
        else:
            digest = self.vault.put(key, auth)
            self.store.record_generation(key, digest, source=GENERATION_SOURCE)
        self.store.update(key, flags=dict(self.store.account(key)["flags"], restorable=True), error=None, error_code=None)
        return "updated"


def _terminal_record(account, row, now):
    """The bundle's Claude Code record in the vault shape cli_accounts writes, or _Refused. The login's
    own facts travel (verified, refreshed, excluded); this machine's activity (refresh_attempt) does not."""
    record = account["secret"]["data"]
    if record.get("provider", "claude_cli") != "claude_cli" or not isinstance(record.get("identity"), str):
        raise _Refused("damaged")
    if record["identity"] != row["identity"]:
        raise _Refused("identity")
    oauth = record.get("oauthAccount")
    if oauth is not None and (not isinstance(oauth, dict) or oauth.get("accountUuid", row["identity"]) != row["identity"]):
        raise _Refused("identity")
    record = copy.deepcopy(record)
    record.pop("refresh_attempt", None)
    health = record.get("health", "ok")
    if health not in ("ok", "needs_signin"):
        raise _Refused("damaged")
    credentials = record.get("credentials")
    if credentials is None and health == "needs_signin":
        record["fingerprint"] = None
        record.pop("verified", None)
    else:
        try:
            if not isinstance(credentials, dict):
                raise TypeError("credentials")
            print_ = cli_fingerprint(credentials)
            cli_expires(credentials)
        except (DeckError, TypeError, ValueError, AttributeError):
            raise _Refused("damaged") from None
        if record.get("verified") != print_:
            record.pop("verified", None)
        record["fingerprint"] = print_
    record.update(provider="claude_cli", health=health, label=row["label"],
                  excluded=bool(record.get("excluded", False)))
    for name in ("email", "organizationUuid", "plan"):
        if not isinstance(record.get(name), str):
            record[name] = ""
    if not _is_number(record.get("captured")):
        record["captured"] = now
    if not _is_number(record.get("refreshed")):
        record["refreshed"] = record["captured"]
    return record


def _terminal_time(record):
    values = [record.get(name) for name in ("refreshed", "captured")]
    return max((value for value in values if _is_number(value)), default=0.0)


_ledgers = weakref.WeakKeyDictionary()      # per engine, in memory: the ledger cache
_ledgers_lock = threading.Lock()


def _ledger(engine):
    with _ledgers_lock:
        found = _ledgers.get(engine)
        if found is None:
            found = _ledgers[engine] = _LedgerStore(engine.vault, engine.clock)
        return found


class _account_lock:
    """The account's read lock (the scheduler's), so no usage read of it runs while its sign-in changes."""

    def __init__(self, engine, key):
        self.lock, self.timeout = engine.scheduler.account_lock(key), engine.timings.lock_wait

    def __enter__(self):
        if not self.lock.acquire(timeout=self.timeout):
            raise DeckError("This account is being refreshed. Try again in a moment.", "busy")
        return self

    def __exit__(self, *exc):
        self.lock.release()
        return False


# ---- the ledger (encrypted vault record) ----

class _LedgerStore:
    """The `move-import` vault record: {"version": 1, "imports": [entry, …]}, newest last. An entry is
    {"bundle_id", "kind", "created", "at", "complete", "code", "dismissed", "counts", "failures",
    "accounts": {PC account id: account id here}}. Cached in memory; an unreadable record is tried
    again after LEDGER_RETRY seconds, never on every snapshot."""

    def __init__(self, vault, clock):
        self.vault, self.clock = vault, clock
        self.lock = threading.RLock()
        self._doc, self._retry_at = None, 0.0

    def _load(self):
        if self._doc is not None:
            return self._doc
        empty = {"version": 1, "imports": []}
        if self.clock() < self._retry_at:
            return empty
        try:
            doc = self.vault.get(LEDGER_KEY) if self.vault.path(LEDGER_KEY).exists() else empty
        except DeckError:
            log("move.ledger_unreadable", "warning")
            self._retry_at = self.clock() + LEDGER_RETRY
            return empty
        self._doc = doc if isinstance(doc, dict) and isinstance(doc.get("imports"), list) else empty
        return self._doc

    def entries(self):
        with self.lock:
            return copy.deepcopy(self._load()["imports"])

    def entry(self, bundle_id):
        return next((item for item in reversed(self.entries()) if item.get("bundle_id") == bundle_id), None)

    def begin_account(self, header, source_id, target_id, at):
        """Persist a hand-over's matching row before changing it, for crash-safe resumption."""
        with self.lock:
            doc = copy.deepcopy(self._load())
            entry = next((item for item in doc["imports"] if item.get("bundle_id") == header.bundle_id), None)
            if entry is None:
                entry = {"bundle_id": header.bundle_id, "kind": header.kind, "created": header.created,
                         "at": at, "complete": False, "code": None, "dismissed": False,
                         "counts": {}, "failures": [], "accounts": {}}
                doc["imports"].append(entry)
            entry.setdefault("started", {})[source_id] = target_id
            self.vault.put(LEDGER_KEY, doc, generation=False)
            self._doc = doc

    def record(self, header, *, at, code, counts, failures, mapping):
        with self.lock:
            doc = copy.deepcopy(self._load())
            previous = next((item for item in doc["imports"] if item.get("bundle_id") == header.bundle_id), None)
            accounts = {**((previous or {}).get("accounts") or {}), **mapping}
            entry = {"bundle_id": header.bundle_id, "kind": header.kind, "created": header.created, "at": at,
                     "complete": code is not None, "code": code, "dismissed": False, "counts": dict(counts),
                     "failures": [dict(item) for item in failures], "accounts": accounts,
                     "started": dict((previous or {}).get("started") or {})}
            imports = [item for item in doc["imports"] if item.get("bundle_id") != header.bundle_id] + [entry]
            while len(imports) > LEDGER_KEEP:
                imports.remove(next((item for item in imports if item.get("dismissed")), imports[0]))
            doc["imports"] = imports
            self.vault.put(LEDGER_KEY, doc, generation=False)
            self._doc = doc
            return copy.deepcopy(entry)

    def dismiss(self, bundle_id):
        with self.lock:
            doc = copy.deepcopy(self._load())
            found = False
            for item in doc["imports"]:
                if item.get("bundle_id") == bundle_id and not item.get("dismissed"):
                    item["dismissed"], found = True, True
            if found:
                self.vault.put(LEDGER_KEY, doc, generation=False)
                self._doc = doc
            return found


def _entry_public(entry):
    """An import as the state and the action result show it; the PC-to-Mac id mapping stays inside."""
    failures = [dict(item) for item in entry.get("failures") or []]
    complete = bool(entry.get("code")) and not failures
    counts = {key: int((entry.get("counts") or {}).get(key, 0)) for key in OUTCOMES}
    if complete:
        message = (f"Every account from this {'hand-over' if entry.get('kind') == 'handover' else 'move'} bundle is "
                   f"saved on this Mac. {CODE_HINT}")
    elif failures and counts["left_out"]:      # a new export would hold the left-out account again: no "export again"
        message = f"{_count(len(failures), 'account')} could not be imported. {_left_out(counts['left_out'], 'was')} {NO_CODE}"
    elif failures:
        message = (f"{_count(len(failures), 'account')} could not be imported, so there is no confirmation code. "
                   f"{CANCEL_HINT}")
    elif counts["left_out"]:
        saved = sum(counts[key] for key in ("new", "updated", "filled", "kept", "on_pc"))
        message = (f"{_left_out(counts['left_out'], 'was')} "
                   f"{'The other accounts are saved on this Mac. ' if saved else ''}{NO_CODE}")
    else:
        message = "This import has not finished. Import the same bundle again to resume it."
    return {"bundle_id": entry.get("bundle_id"), "kind": entry.get("kind"), "at": entry.get("at"),
            "complete": complete, "code": entry.get("code") if complete else None, "counts": counts,
            "failures": failures, "message": message}


def dismiss(engine, bundle_id):
    """`import-bundle-dismiss`: hides that import's code or failure list."""
    if not isinstance(bundle_id, str) or not bundle_id:
        raise DeckError("Choose the import to dismiss.", "invalid")
    if not _ledger(engine).dismiss(bundle_id):
        raise DeckError("That import is no longer shown.", "not_found")


# ---- what the engine asks ----

def check_action(engine, name, body):
    """Refuses an account action naming an "on_pc" row (only rename and remove may). True when it carried
    the action out itself: renaming an "on_pc" Claude Code row, which has no saved sign-in for
    CLIAccounts.rename to relabel."""
    key = body.get("id") if isinstance(body, dict) else None
    if not isinstance(key, str) or not key:
        return False
    row = engine.store.account(key)
    if row is None:
        return False
    if row["owner"] == ON_PC:
        if name == "rename" and row["provider"] == "claude_cli":
            engine.accounts.rename(key, body.get("label"))
            return True
        if name not in ON_PC_ALLOWED:
            raise DeckError(ON_PC_MESSAGE, "invalid")
    return False


def state(engine, rows=None):
    """The `move_import` block of /api/state: {"imports": [undismissed imports, newest first, as
    _entry_public], "on_pc": count}."""
    rows = engine.store.accounts() if rows is None else rows
    imports = [_entry_public(item) for item in reversed(_ledger(engine).entries()) if not item.get("dismissed")]
    return {"imports": imports, "on_pc": sum(1 for row in rows if row["owner"] == ON_PC)}


def start_import(engine, body):
    """`import-bundle`: submits ImportBundle and, unless body["wait"] is false, waits up to IMPORT_WAIT for
    it. {"op": id, "result": the import (_entry_public: code, counts, failures, message; also after a
    partial import, which ends the operation as failed), or None while it still runs or when it stopped
    before recording anything (the operation's report says why)}."""
    op = ImportBundle(engine, body.get("path"), body.get("passphrase"))
    started = engine.submit(op)
    if body.get("wait", True) is not False:
        op.wait(IMPORT_WAIT)
    return {**started, "result": copy.deepcopy(op.result)}


# ---- history ----

def _copy_history(store, key, account, now):
    """The bundle's readings, snapshots and refills of one account with core.Store's retention, never a
    row twice, and its last good reading when newer than the one here."""
    history = account["history"]
    with store.lock:
        try:
            present = {row[0] for row in store.db.execute("SELECT at FROM snapshots WHERE account=?", (key,))}
            store.db.executemany(
                "INSERT INTO snapshots(account, at, usage) VALUES(?, ?, ?)",
                [(key, item["at"], _encode(item["usage"])) for item in history["snapshots"]
                 if item["usage"] is not None and item["at"] >= now - SNAPSHOT_DAYS * _DAY and item["at"] not in present])
            present = {(row[0], row[1]) for row in store.db.execute(
                "SELECT window_id, at FROM readings WHERE account=?", (key,))}
            store.db.executemany(
                "INSERT INTO readings(account, window_id, at, used, reset) VALUES(?, ?, ?, ?, ?)",
                [(key, item["window_id"], item["at"], item["used"], item["reset"]) for item in history["readings"]
                 if item["at"] >= now - READING_DAYS * _DAY and (item["window_id"], item["at"]) not in present])
            store.db.executemany(
                "INSERT OR IGNORE INTO refills(account, window_id, reset_at, confirmed_at, previous_used) "
                "VALUES(?, ?, ?, ?, ?)",
                [(key, item["window_id"], item["reset_at"], item["confirmed_at"], item["previous_used"])
                 for item in account["refills"] if item["confirmed_at"] >= now - REFILL_DAYS * _DAY])
            store.db.commit()
        except BaseException:
            store.db.rollback()
            raise
    usage, at = history["last_good_usage"], history["last_good_at"]
    if usage is not None and at is not None:
        held = store.account(key)["last_good_at"]
        if held is None or at > held:
            store.update(key, last_good_usage=usage, last_good_at=at)


# ---- small helpers ----

def _vault_record(vault, key):
    """The decrypted record, or None when it is missing or unreadable."""
    try:
        return vault.get(key) if vault.path(key).exists() else None
    except DeckError:
        return None


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _encode(value):
    return json.dumps(value, separators=(",", ":"))


def _label(account):
    return account.get("label") or account.get("email") or account["provider"].replace("_", " ").title()


def _count(number, noun):
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _verb(number, verb):
    return {"was": ("was", "were"), "is": ("is", "are")}[verb][number != 1]


def _summary(kind, counts, failures, code, settings):
    """The operation's summary, which the engine also files as an event: never the code. `settings` names
    this Mac's settings the import changed."""
    stored = sum(counts[key] for key in ("new", "updated", "filled", "kept"))
    if kind == "handover":
        text = f"Took over {_count(stored, 'account')} handed over from the PC."
    else:
        text = f"Imported {_count(stored, 'account')} from the PC."
        if counts["on_pc"]:
            text += (f" {_count(counts['on_pc'], 'account')} still signed in on the PC "
                     f"{_verb(counts['on_pc'], 'is')} listed without a sign-in.")
    if failures and counts["left_out"]:
        return f"{text} {_count(len(failures), 'account')} could not be imported. {_left_out(counts['left_out'], 'was')} {NO_CODE}"
    if failures:
        return f"{text} {_count(len(failures), 'account')} could not be imported. {CANCEL_HINT}"
    if settings:
        text += f" {_count(len(settings), 'setting')} now {'matches' if len(settings) == 1 else 'match'} the PC: {_listed(settings)}."
    if counts["left_out"]:
        return f"{text} {_left_out(counts['left_out'], 'was')} {NO_CODE}"
    return text + (" The confirmation code is shown on the dashboard." if code else "")
