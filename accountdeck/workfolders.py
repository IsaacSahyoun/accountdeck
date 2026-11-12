"""The cleanup at start of the private folders helpers ran in (`<data>/work`).

A helper (a Codex app-server, `muse usage`, `muse login`) is handed a login in plain text in a folder of its
own, and the folder is deleted when the helper is done. When AccountDeck dies first the folder stays, and a
login the helper renewed, or a sign-in the user finished, may exist nowhere else. Claude Code has no folder
here: CLIAccounts picks up its unfinished sign-in and renewals at start (restore_enrollment, adopt_stashes).
"""
import json
from pathlib import Path

from . import diagnostics
from .accounts import GUARDS
from .core import is_frozen
from .platform import facts_mac
from .procs import helper_running
from .providers import codex, muse

# folder name prefix -> (client, where its login is inside the folder); None for a folder that holds no login
KINDS = {"probe-": ("codex", "auth.json"), "login-": ("codex", "auth.json"),
         "muse-probe-": ("muse", "muse/auth.json"), "muse-login-": ("muse", "muse/auth.json"),
         "cookies-": None}
# Whether a copy is a whole login. A usage check that must not renew is handed one with a blank refresh token,
# and the saved copy already holds everything such a copy has.
WHOLE = {"codex": lambda doc: codex.fingerprint(doc) is not None,
         "muse": lambda doc: muse.is_file_login(doc) and muse.meta_block(doc).get("refresh_token") != ""}


def sweep(engine) -> int:
    """Once at start, before any read: deletes the helper folders an earlier run left behind and returns how
    many. A login found in one is offered to the vault under the account it belongs to, and the vault's own
    rule decides whether it is the newer copy. One the vault does not take, or that belongs to no saved
    account, is kept encrypted for review (Vault.quarantine). A folder whose helper still runs is left for
    the next start, and so is one whose login could not be stored."""
    work = Path(engine.paths.data_dir) / "work"
    try:
        folders = sorted(entry for entry in work.iterdir() if entry.name.startswith(tuple(KINDS)))
    except FileNotFoundError:
        return 0
    removed = 0
    for folder in folders:
        kind = next(kind for prefix, kind in KINDS.items() if folder.name.startswith(prefix))
        if folder.is_symlink() or not folder.is_dir() or helper_running(folder):
            diagnostics.log("work.left", "warning")
            continue
        try:
            if kind is not None:
                _keep(engine, kind[0], folder / kind[1])
            facts_mac.remove_work_directory(folder, work)
            removed += 1
        except Exception as exc:
            diagnostics.log_exception("work.sweep_failed", exc)
    if removed:
        diagnostics.log("work.swept", "warning", count=removed)
    return removed


def _keep(engine, client, path):
    """Stores the login at `path` when it is worth keeping. Raises when it could not be stored."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return
    try:
        doc = json.loads(raw)
    except ValueError:
        doc = None
    key = client + "-leftover"
    identity = GUARDS[client][0](doc) if isinstance(doc, dict) else None
    row = engine.store.find(client, identity) if identity else None
    if row is not None and not is_frozen(row):
        if not WHOLE[client](doc):
            return
        key = row["id"]
        with engine.accounts._locked(key):
            if _offer(engine, client, key, doc) == "accepted":
                diagnostics.log("work.login_saved", "warning", provider=client, account=diagnostics.account_ref(key))
                return
    engine.vault.quarantine(key, raw)
    diagnostics.log("work.kept_for_review", "warning", provider=client)


def _offer(engine, client, key, doc):
    """The vault's answer to a found copy for saved account `key`, by the rule a usage check's copy is stored under."""
    if client == "muse":
        return engine.accounts._put_muse(key, doc, "probe")
    return engine.vault.put_guarded(key, doc, "probe", *GUARDS[client])
