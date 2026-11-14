"""One shape for putting an interrupted switch right, shared by all three clients.

A switch saves a record (core.FileTransaction) before it changes a login and closes it when it is done. An
open record means the switch stopped part way: it failed, or the app died. Each client settles its own
record (`settle` in switch_cli, `put_back` here for Codex and Muse) in the same four steps:

1. Look at who is signed in now, by whose login it is. Bytes are never compared: a client renews its login
   whenever it runs.
2. Save that login under its own account, so a renewal is never lost.
3. Write the previous account's saved login whole, never one part of it.
4. Close the record.

`after_failure` runs those steps as soon as a switch fails, and `press` is the Recover action around them.
A record that cannot be read or applied is set aside: kept as it is in the vault, out of the pending list,
with a notice that says so. A record is never set aside while its client's login is part one account and
part another (`StillNeeded`): settling that record again is the only thing that makes the login whole.

Builds before 2.1 also switched Claude Desktop. A record one of them left open is not this build's to settle:
`pending` keeps it out of the list and sets it aside, and no code here opens a file it names.
"""
import base64
from dataclasses import dataclass
import json
from pathlib import Path

from . import diagnostics
from .core import OPEN_STATUSES, DeckError, FileTransaction, atomic_write, json_bytes, recover_transactions

WAIT_CODES = ("busy", "read_only")        # the user can lift these and press Recover again; nothing changed
NAMES = {"claude_cli": "Claude Code", "codex": "Codex", "muse": "Muse"}
KEPT = "The record stays encrypted in AccountDeck's data folder for 30 days."
UNREADABLE = ("AccountDeck could not read the record of an interrupted switch, so it set the record aside and "
              f"cleared it. {KEPT} Nothing in your apps was changed: open each one and check it shows the "
              "account you expect.")
SET_ASIDE = "An interrupted switch could not be undone. Its record was set aside; see the notice."
NOTHING_WAITING = "No interrupted switch was waiting."
NOTHING_TO_UNDO = "The interrupted switch had changed nothing."
UNAPPLIED = ("AccountDeck could not undo an interrupted {name} switch, so it set that switch's record aside and "
             f"cleared it. {KEPT} Open {{name}} and check it shows the account you expect.")
UNMANAGED = ("This version of AccountDeck does not manage Claude Desktop. An earlier version left a Claude Desktop "
             f"switch unfinished, and its record was set aside. {KEPT} Open Claude Desktop and check it shows the "
             "account you expect. If it does not, sign out of Claude Desktop and sign in again.")


class StillNeeded(DeckError):
    """A `settle` failed and the client's login is not whole, or nobody can tell. The record stays open and
    the user is told why, so another press of Recover can finish the job."""

    def __init__(self, exc, state, hint=""):
        cause = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        super().__init__(f"Recover could not finish: {cause.rstrip('.')}. {state} The interrupted switch is still "
                         f"waiting, so press Recover again.{' ' + hint if hint else ''}", "recovery")


def client_of(doc):
    """The client an open record belongs to, or None when this build does not manage it. A record that names
    no client is Claude Desktop's: the builds that switched it are the only ones that wrote such a record."""
    provider = doc.get("provider")
    return provider if provider in NAMES else None


def pending(engine):
    """The keys of the open records a press of Recover settles. A record that cannot be read is one of them:
    Recover sets it aside. A record of a client this build does not manage is not. Nothing here can put
    that client's login right, so the copy that owns the sign-ins sets the record aside, kept as it is, and
    says so once. Another copy leaves it where it is. A set-aside that fails leaves the record open and out
    of the list, and the next start tries again."""
    keys = []
    for key in recover_transactions(engine.vault):
        try:
            managed = client_of(engine.vault.get(key)) is not None
        except DeckError:
            managed = True
        if managed:
            keys.append(key)
        elif engine.owns_login:
            try:
                set_aside(engine, key, UNMANAGED)
            except OSError as exc:
                diagnostics.log_exception("recovery.unmanaged_kept", exc)
    return keys


def set_aside(engine, key, message):
    """Takes a record nobody can settle out of the pending list, keeps it, and tells the user."""
    try:
        engine.vault.set_aside(key)
    except FileNotFoundError:
        return
    diagnostics.log("recovery.set_aside", "warning")
    engine.notice(message, "warning")


@dataclass
class Pressed:
    """What one press of Recover did: a sentence for each record it settled, and how many it set aside."""
    sentences: list
    aside: int = 0


def press(engine, provider, settle) -> Pressed:
    """One press of Recover for `provider`: `settle(tx)` puts one of its open records right and returns a
    sentence for the user; what was done is returned. A refusal the user can lift (the client still runs)
    and a `StillNeeded` are raised with the record left open. Any other failure sets the record aside, as
    does a record that cannot be read. The records are looked up now, not taken from `engine.recovery`, and
    only one that is still open is settled: a list read while a switch was running never undoes that switch."""
    done = Pressed([])
    try:
        for key in recover_transactions(engine.vault):
            try:
                tx = FileTransaction.load(engine.vault, engine.roots(), key)
            except DeckError as exc:
                if getattr(exc, "reason", None):      # the vault key itself is unavailable: nothing can be judged
                    raise
                set_aside(engine, key, UNREADABLE)
                done.aside += 1
                continue
            if client_of(tx.doc) != provider or tx.doc.get("status") not in OPEN_STATUSES:
                continue
            try:
                done.sentences.append(settle(tx))
            except Exception as exc:
                if isinstance(exc, StillNeeded):
                    _left(engine, provider, True)       # the next press goes to another client's record first
                    raise
                if isinstance(exc, DeckError) and exc.code in WAIT_CODES:
                    raise
                diagnostics.log_exception("recovery.unapplied", exc)
                set_aside(engine, key, UNAPPLIED.format(name=NAMES.get(provider, "app")))
                done.aside += 1
    finally:
        engine.look_for_unfinished()
    _left(engine, provider, False)
    return done


def _left(engine, provider, still_needed):
    """Keeps `engine.recovery_left`: the clients whose last press had to leave a record open, the one tried
    longest ago first. switching._recovery_provider sends a press to them last, and in that order."""
    if provider in engine.recovery_left:
        engine.recovery_left.remove(provider)
    if still_needed:
        engine.recovery_left.append(provider)


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


def summary(done) -> str:
    """One line for the report of a Recover press, from what `press` returned."""
    said = list(dict.fromkeys(done.sentences)) + ([SET_ASIDE] if done.aside else [])
    return " ".join(said) or NOTHING_WAITING


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
    """The live file cannot be parsed: its bytes go to the vault before the outgoing login replaces them. It
    may hold the only copy of a login the client renewed, so when the bytes cannot be kept the undo stops
    and the file stays as it is. A file the user owns but may not read is made readable first."""
    path = client.path
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return
    except OSError:
        try:
            path.chmod(0o600)
            raw = path.read_bytes()
        except OSError:
            diagnostics.log("recovery.live_unreadable", "warning", provider=client.provider)
            raise DeckError(f"{NAMES[client.provider]}'s login file cannot be read, so it was left as it is.",
                            "recovery") from None
    ctx.vault.quarantine(client.provider + "-recover", raw)


def _identity(client, data):
    try:
        return client.identity_of(json.loads(data)) if data else None
    except ValueError:
        return None
