"""The one wait a switch has: until a client's sessions are closed or idle ("switch when idle").

It runs on the confirmation the user gave (procs.ConsentToken), which lasts `Timings.consent_ttl`. When
that runs out the wait ends and says so: nothing may be closed on a confirmation that has expired, and
most other actions wait behind this one."""
import time

from .core import DeckError


def until_idle(token, name, look, pause):
    """Calls `look()` until it returns something other than None, and returns that; `pause()` sleeps
    between looks (it raises when the user cancels). Ends with `stale_plan` when the confirmation `token`
    runs out first. `name` is the client the user is told about."""
    while True:
        found = look()
        if found is not None:
            return found
        if time.time() >= token.expires:
            minutes = max(1, round((token.expires - token.issued) / 60))
            raise DeckError(f"{name} sessions were still open when your confirmation ran out (it lasts {minutes} "
                            f"minute{'' if minutes == 1 else 's'}). Nothing was changed. Close them, then switch again.",
                            "stale_plan")
        pause()


def for_sessions(op, name, look, replan):
    """The wait step of a Codex or Muse switch `op`: until the client has no session open. `look()` returns
    (the sessions, ..., their ProcessSet) and its last answer is returned. A process the user did not
    confirm ends the wait with `needs_reconfirm` and the plan `replan()` builds; an open session without
    the "wait" consent ends it with `busy`."""
    allowed = set(op.token.pairs)

    def idle():
        seen = look()
        sessions, found = seen[0], seen[-1]
        if not set(found.pairs) <= allowed:
            raise DeckError(f"The {name} process set changed. Review the switch again.", "needs_reconfirm", plan=replan())
        if not sessions:
            return seen
        if op.consent != "idle":
            raise DeckError(f"Close your {name} sessions or choose Wait.", "busy")
        op.set_waiting([{"pid": row.pid, "name": row.name, "status": "busy"} for row in sessions])
        return None
    return until_idle(op.token, name, idle, lambda: op.sleep(.25))
