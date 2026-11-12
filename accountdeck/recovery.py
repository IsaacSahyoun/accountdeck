"""One shape for putting an interrupted switch right, shared by all four clients.

A switch saves a record (core.FileTransaction) before it changes a login and closes it when it is done. An
open record means the switch stopped part way: it failed, or the app died. Each client settles its own
record in the same four steps:

1. Look at who is signed in now, by whose login it is. Bytes are never compared: a client renews its login
   whenever it runs.
2. Save that login under its own account, so a renewal is never lost.
3. Write the previous account's saved login whole, never one part of it.
4. Close the record.

`after_failure` runs those steps as soon as a switch fails, and `press` is the Recover action around them.
A record that cannot be read or applied is set aside: kept as it is in the vault, out of the pending list,
with a notice that says so.
"""
from . import diagnostics
from .core import DeckError, FileTransaction, recover_transactions

WAIT_CODES = ("busy", "read_only")        # the user can lift these and press Recover again; nothing changed
NAMES = {"claude_cli": "Claude Code", "claude": "Claude", "codex": "Codex", "muse": "Muse"}
KEPT = "The record stays encrypted in AccountDeck's data folder for 30 days."
UNREADABLE = ("AccountDeck could not read the record of an interrupted switch, so it set the record aside and "
              f"cleared it. {KEPT} Nothing in your apps was changed: open each one and check it shows the "
              "account you expect.")
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
    cannot be read."""
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
