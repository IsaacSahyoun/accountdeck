"""The terminal auto-switch executor: feeds cli_policy.evaluate with the saved terminal accounts and acts
on its decision. Reads the decision needs still go through the scheduler (and its request governor).

`tick` runs only on the engine's tick thread; `cli_history` is also written by CLISwitch on `ad2-ops`,
so every read-modify-write of it goes through `history()`."""
from contextlib import contextmanager
import copy
import threading

from . import cli_policy, diagnostics
from .core import DeckError, is_frozen

_history_lock = threading.Lock()


@contextmanager
def history(engine):
    """The `cli_history` setting for one read-modify-write; saved on exit only when it changed."""
    with _history_lock:
        value = engine.store.setting("cli_history", {})
        before = copy.deepcopy(value)
        yield value
        if value != before:
            engine.store.set_setting("cli_history", value)


def note_try(engine, key):
    """Notes that an automatic switch to `key` starts now, under `tried` in the history: the time, and which try
    in a row it is. `CLISwitch` calls this before it does anything else and takes the note out once the switch
    worked, and `tick` takes out the note of the account in use. The rule waits `cli_policy.AGAIN` after a
    first try and passes the account over for `cli_policy.RETRY` after a second in a row
    (`cli_policy.evaluate`, `_row`). An older note says nothing.

    A try counts on from the last one while that one is recent. After a first try the second is due 90
    seconds later, so one that comes more than `RETRY` after it is a first try again: the second was never
    made (the user moved the terminals, or the need went away), and counting on made one passing failure
    pass the account over. After a second try the next is due `RETRY` later, or 30 minutes later after a
    refused renewal, so those count on within the hour."""
    now = engine.clock()
    with history(engine) as past:
        notes = past.get("tried") if isinstance(past.get("tried"), dict) else {}
        last = cli_policy.tried(notes.get(key))
        recent = last is not None and abs(now - last[0]) < (cli_policy.RETRY if last[1] == 1 else cli_policy.HOUR)
        count = last[1] + 1 if recent else 1
        past["tried"] = {**notes, key: {"at": now, "count": count}}


FORECAST_MARGIN, FORECAST_CAP = 60.0, 1800.0


def _forecast(engine, row, now):
    """Projected % used per window id of the active account when its next read can land (planned time or
    the governor's wait, whichever is later, plus a margin for the read itself)."""
    planned = engine.scheduler.planned(row["id"])
    wait = engine.scheduler.governor.delay(("claude_cli", row["identity"]), now, urgent=True)
    horizon = min(max((planned or now) - now, wait, 0.0) + FORECAST_MARGIN, FORECAST_CAP)
    result = {}
    for window in (row["last_good_usage"] or {}).get("windows", []):
        if isinstance(window, dict) and isinstance(window.get("id"), str):
            value = cli_policy.forecast(engine.store.readings(row["id"], window["id"], since=now - cli_policy.FORECAST_SPAN - 600),
                                        now, horizon)
            if value is not None:
                result[window["id"]] = value
    return result


def inputs(engine):
    rows = []
    now = engine.clock()
    for row in engine.store.accounts():
        if row["provider"] != "claude_cli":
            continue
        active = row["identity"] == engine.active_identity.get("claude_cli")
        rows.append({"id": row["id"], "label": row["label"], "active": active,
                     "forecast": _forecast(engine, row, now) if active else {},
                     "owner": row["owner"],
                     "excluded": is_frozen(row) or row["flags"].get("excluded", False),
                     "health": row["flags"].get("health", "ok"),
                     "live_expired": row["flags"].get("live_expired", False),
                     "last_good_at": row["last_good_at"],
                     # Until when Claude's refusal of a read pauses the account's reads. The row's `error` does not
                     # say so: a save of the login clears it while the pause lasts (`CLIAccounts.save`).
                     "paused_until": row["paused_until"],
                     "windows": (row["last_good_usage"] or {}).get("windows", [])})
    return rows


def decision(engine, settings=None):
    return cli_policy.evaluate(inputs(engine), settings or engine.prefs.public(),
                               engine.store.setting("cli_history", {}), engine.clock())


def state(engine, switching=False):
    """The state's `providers.claude_cli.autoswitch` block: the decision and the order under each mode
    (one pass over the store, evaluated four times). While an operation of Claude Code runs (`switching`) the
    rule decides nothing and no hold is given: the note of an automatic switch is written when it starts, so
    the hold said that switch "was tried a moment ago and is tried once more" while it ran."""
    settings, rows = engine.prefs.public(), inputs(engine)
    history, now = engine.store.setting("cli_history", {}), engine.clock()
    result = cli_policy.evaluate(rows, settings, history, now)
    return {"enabled": settings["cli_autoswitch"], "threshold": settings["cli_threshold"],
            "order_mode": settings["cli_order"], "early_switch": settings["cli_early_switch"],
            "next_up": result["next_up"], "last": history.get("last"),
            "hold": engine.cli.adapter.readonly_reason or (None if switching else result["hold"]),
            "previews": {mode: cli_policy.evaluate(rows, {**settings, "cli_order": mode}, history, now)["next_up"]
                         for mode in ("soonest_reset", "most_room", "manual")}}


def tick(engine):
    """Acts on the policy's decision. Another client's switch that only waits for its sessions to close does
    not stop it (such a wait lasts until its confirmation runs out, 30 minutes); any other running operation does."""
    if (not engine.owns_login or not engine.identity_known.get("claude_cli")
            or not engine.runner.free_for("claude_cli", beside_waits=True)
            or engine.cli.enrollment or engine.recovery or engine.cli.adapter.readonly_reason):
        return
    leaving = engine.active_identity.get("claude_cli")     # the account this decision is about
    in_use = engine.store.find("claude_cli", leaving)
    with history(engine) as past:
        # The terminals are on this account, however they got there, so a note that a switch to it did not
        # happen is out of date. The app dying after a switch and before its history is written leaves one, and
        # so does a sign-in to that account made in Claude Code. Left there, it had the account passed over
        # once the terminals moved on. This comes before the pause of usage checks: auto-switch does nothing
        # while they are paused, and the terminals are on this account all the same.
        if in_use is not None and isinstance(past.get("tried"), dict):
            past["tried"].pop(in_use["id"], None)
    if engine.prefs.get("refresh_paused"):
        return
    result = decision(engine)
    with history(engine) as past:
        entered = result["exhausted"] and not past.get("exhausted")
        if entered:
            past["exhausted"] = True
        elif not result["exhausted"] and result["action"] == "none" and not result["hold"] and past.get("exhausted"):
            past["exhausted"] = False
    if entered:
        engine.notice(result["reason"], "warning")
        if engine.prefs.get("alerts_enabled"):
            engine.store.add_alert({"kind": "limit_reached", "level": "warning", "account": None, "window": None,
                                    "title": "All terminal accounts are low", "message": result["reason"], "at": engine.clock()})
        diagnostics.log("cli.all_low")
    # Accounts the rule wants read again: candidates too old to switch to, or the one in use after its limit refilled.
    for key in result["refresh"]:
        engine.scheduler.request(key, engine.clock())
    if result["action"] != "switch":
        return
    try:
        target = engine.cli.row(result["target"])
    except DeckError:          # removed since the decision was made; the next tick decides again
        return
    if target["last_good_at"] is None or engine.clock() - target["last_good_at"] > cli_policy.READY:
        engine.scheduler.request(target["id"], engine.clock())
        return     # re-evaluate after the governed read, including every changed window
    from .switch_cli import CLISwitch
    try:
        engine.submit(CLISwitch(engine, target["id"], kind=result["kind"], reason=result["reason"], leaving=leaving))
        diagnostics.log("cli.decision", kind=result["kind"], acct=diagnostics.account_ref(target["id"]))
    except DeckError as exc:
        diagnostics.log("cli.deferred", code=exc.code)
        return
    if result["kind"] == "early":
        # The early switch is not urgent. The rule waits 2 hours after one was started, whether it worked or
        # not (`cli_policy._early`), and moves to no other account meanwhile. The note of `note_try` counts
        # for the account it failed for when another kind of switch wants that account.
        with history(engine) as past:
            past["early_tried"] = engine.clock()
