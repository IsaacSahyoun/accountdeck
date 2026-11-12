"""One shape for putting an interrupted switch right, shared by all four clients.

A switch saves a record (core.FileTransaction) before it changes a login and closes it when it is done. An
open record means the switch stopped part way: it failed, or the app died. Each client settles its own
record (`settle` in switch_cli, `put_back` here for Codex and Muse, `Recover` in switch_claude) in the same
four steps:

1. Look at who is signed in now, by whose login it is. Bytes are never compared: a client renews its login
   whenever it runs.
2. Save that login under its own account, so a renewal is never lost.
3. Write the previous account's saved login whole, never one part of it.
4. Close the record.

`after_failure` runs those steps as soon as a switch fails, and `press` is the Recover action around them.
A record that cannot be read or applied is set aside: kept as it is in the vault, out of the pending list,
with a notice that says so.
"""
import base64
from dataclasses import dataclass
import json
from pathlib import Path

from . import diagnostics
from .core import OPEN_STATUSES, DeckError, FileTransaction, atomic_write, json_bytes, recover_transactions

WAIT_CODES = ("busy", "read_only")        # the user can lift these and press Recover again; nothing changed
NAMES = {"claude_cli": "Claude Code", "claude": "Claude", "codex": "Codex", "muse": "Muse"}
KEPT = "The record stays encrypted in AccountDeck's data folder for 30 days."
UNREADABLE = ("AccountDeck could not read the record of an interrupted switch, so it set the record aside and "
              f"cleared it. {KEPT} Nothing in your apps was changed: open each one and check it shows the "
              "account you expect.")
SET_ASIDE = "The interrupted switch could not be undone. Its record was set aside; see the notice."
NOTHING_TO_UNDO = "The interrupted switch had changed nothing."
UNAPPLIED = ("AccountDeck could not undo an interrupted {name} switch, so it set that switch's record aside and "
             f"cleared it. {KEPT} Open {{name}} and check it shows the account you expect.")


def set_aside(engine, key, message):
    """Takes a record nobody can settle out of the pending list, keeps it, and tells the user."""
    try:
        engine.vault.set_aside(key)
    except FileNotFoundError:
        return
    diagnostics.log("recovery.set_aside", "warning")
    engine.notice(message, "warning")


def press(engine, provider, settle) -> list:
    """One press of Recover for `provider`: `settle(tx)` puts one of its open records right and returns a
    sentence for the user; the sentences are returned. A refusal the user can lift (the client still runs)
    is raised with the record left open. Any other failure sets the record aside, as does a record that
    cannot be read. A record that is no longer open is left alone, however old the list is."""
    sentences = []
    for key in list(engine.recovery):
        try:
            tx = FileTransaction.load(engine.vault, engine.roots(), key)
        except DeckError as exc:
            if getattr(exc, "reason", None):          # the vault key itself is unavailable: nothing can be judged
                raise
            set_aside(engine, key, UNREADABLE)
            continue
        if tx.doc.get("provider", "claude") != provider:
            continue
        if tx.doc.get("status") not in OPEN_STATUSES:     # it finished since the list was read: nothing to settle
            continue
        try:
            sentences.append(settle(tx))
        except Exception as exc:
            if isinstance(exc, DeckError) and exc.code in WAIT_CODES:
                raise
            diagnostics.log_exception("recovery.unapplied", exc)
            set_aside(engine, key, UNAPPLIED.format(name=NAMES.get(provider, "app")))
    engine.recovery = recover_transactions(engine.vault)
    return sentences


def after_failure(tx, undo):
    """A switch failed after its record `tx` was saved: `undo(tx)` settles it now. When that cannot be done
    yet (the client runs again, the provider is unreachable) the record stays open for Recover."""
    if tx is None or tx.doc["status"] == "complete":
        return None
    try:
        return undo(tx)
    except Exception as exc:
        diagnostics.log_exception("recovery.undo_failed", exc)
        tx.close("conflict")
        return None


def summary(sentences) -> str:
    """One line for the report of a Recover press, from what `press` returned."""
    return " ".join(dict.fromkeys(sentences)) or SET_ASIDE


@dataclass(frozen=True)
class FileLogin:
    """What `put_back` needs from a client that keeps its whole login in one file (Codex, Muse)."""
    provider: str
    path: Path
    running: object         # () -> true while the client runs: it rewrites the file
    busy: str               # what the user is told then
    live: object            # () -> the signed-in document, or None when signed out; DeckError when unreadable
    keep: object            # (document) -> None: saves a live document under its own account
    identity_of: object     # (document) -> the account identity, or None


def put_back(ctx, tx, client) -> str:
    """Settles the unfinished switch `tx` of a one-file client: whatever is signed in now is saved first (or
    kept encrypted when it cannot be read), then the outgoing account is written from the vault, which holds
    its newest copy. The client must not be running. An undo that never finished is not replayed: the switch
    it was undoing stays open until that undo is done, and settling the switch writes the right login
    whichever record comes first."""
    if client.running():
        raise DeckError(client.busy, "busy")
    if not tx.doc.get("changes") or tx.doc.get("purpose") == "recover":
        tx.close()
        return NOTHING_TO_UNDO
    try:
        live = client.live()
    except DeckError:
        live = None
        _keep_unreadable(ctx, client)
    if live is not None:
        client.keep(live)
    before = tx.doc["changes"][0]["before"]
    data = None if before is None else base64.b64decode(before, validate=True)
    outgoing = tx.doc.get("outgoing") or _identity(client, data)
    row = ctx.store.find(client.provider, outgoing) if outgoing else None
    if row is not None:
        try:
            data = json_bytes(ctx.vault.get(row["id"]))
        except DeckError:       # an unreadable saved copy: the file the client had before the switch is next best
            diagnostics.log("recovery.vault_unreadable", "warning", provider=client.provider)
    fix = FileTransaction(ctx.vault, ctx.engine.roots())
    fix.doc.update(provider=client.provider, purpose="recover")
    fix.save()                  # an undo is under way; it is never replayed, so it records no bytes
    path = fix.check(client.path)
    if data is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, data)
    fix.commit()
    tx.close()
    diagnostics.log("recovery.put_back", provider=client.provider, outgoing_saved=row is not None,
                    live_saved=live is not None)
    return f"The previous {NAMES[client.provider]} sign-in was restored."


def _keep_unreadable(ctx, client):
    """The live file cannot be parsed: its bytes go to the vault before the outgoing login replaces them."""
    try:
        ctx.vault.quarantine(client.provider + "-recover", client.path.read_bytes())
    except OSError:             # it cannot even be opened, so there is nothing to keep
        diagnostics.log("recovery.live_unreadable", "warning", provider=client.provider)


def _identity(client, data):
    try:
        return client.identity_of(json.loads(data)) if data else None
    except ValueError:
        return None
