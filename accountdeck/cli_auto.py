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


def note_try(engine, key, leaving=None):
    """Notes that an automatic switch to `key` starts now, under `tried` in the history: the time, which try in
    a row it is, and the account it is leaving (`leaving` is that account's identity, as `CLISwitch` has it).
    `CLISwitch` calls this before it does anything else and takes the note out once the switch worked, and
    `tick` takes out the note of the account in use and keeps its time (`came`). The rule waits `cli_policy.AGAIN` after a first try and
    passes the account over for `cli_policy.RETRY` after a second in a row (`cli_policy.evaluate`, `_row`). An
    older note says nothing.

    A try counts on from the last one while both are the same need: this try leaves the account the last one
    was leaving, and the last one is recent. With the terminals on another account, a failure of the switch
    that account needs is a first failure, and counting on made one passing failure pass the wanted account
    over. The note says nothing of where the terminals have been in between: away and back within the
    window, and the next try counts on. After a first try the second is due 90 seconds later, so one that comes
    more than `RETRY` after it is a first try again: the need went away meanwhile. After a second try the
    next is due `RETRY` later, or 30 minutes later after a refused renewal, so those count on within the
    hour. A first try dated after now (the Mac's clock was set back) is not counted on from: the rule stops
    waiting for it once it is over `AGAIN` off (`cli_policy._row`), so the next try can come at once, and it
    is a first try again."""
    now = engine.clock()
    row = engine.store.find("claude_cli", leaving) if leaving is not None else None
    origin = row["id"] if row is not None else None
    with history(engine) as past:
        notes = past.get("tried") if isinstance(past.get("tried"), dict) else {}
        last = cli_policy.tried(notes.get(key))
        recent = last is not None and abs(now - last[0]) < (cli_policy.RETRY if last[1] == 1 else cli_policy.HOUR)
        ahead = last is not None and last[1] == 1 and now < last[0]
        count = last[1] + 1 if recent and not ahead and notes[key].get("from") == origin else 1
        past["tried"] = {**notes, key: {"at": now, "count": count, "from": origin}}


FORECAST_MARGIN, FORECAST_CAP = 60.0, 1800.0


def _forecast(engine, row, now):
    """Projected % used per window id of the active account when its next read can land (planned time or
    the governor's wait, whichever is later, plus a margin for the read itself).

    The rate comes from the readings since the terminals came to the account (`Engine.in_use_since`). An
    account they just came to has none, and is judged on its readings until it has two of its own that are
    2 minutes apart. Its readings from before say how fast it filled when it was last used, and with two
    accounts that both filled fast, and no account with room, the terminals went from one to the other at
    every check: each was left on its forecast and could still be used up by its reading.
    Time the app did not run since the last reading is not counted (`Engine.idle_since`). The Mac slept,
    so the account was not used from here, and a rate carried across the sleep moved the terminals at the
    wake. With the app running and no reading coming, the time counts: that is what the forecast is for."""
    planned = engine.scheduler.planned(row["id"])
    wait = engine.scheduler.governor.delay(("claude_cli", row["identity"]), now, urgent=True)
    horizon = min(max((planned or now) - now, wait, 0.0) + FORECAST_MARGIN, FORECAST_CAP)
    came = engine.in_use_since.get("claude_cli")
    since = max(now - cli_policy.FORECAST_SPAN - 600, now if came is None else came)
    result = {}
    for window in (row["last_good_usage"] or {}).get("windows", []):
        if isinstance(window, dict) and isinstance(window.get("id"), str):
            readings = engine.store.readings(row["id"], window["id"], since=since)
            idle = engine.idle_since(readings[-1]["at"]) if readings else 0.0
            value = cli_policy.forecast(readings, now, horizon, idle)
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
                     # A read of it failed, and none has worked since: no new reading can be had for now.
                     "failing": engine.scheduler.failing(row["id"]),
                     "windows": (row["last_good_usage"] or {}).get("windows", [])})
    return rows


def decision(engine, settings=None):
    return cli_policy.evaluate(inputs(engine), settings or engine.prefs.public(),
                               engine.store.setting("cli_history", {}), engine.clock())


def state(engine):
    """The state's `providers.claude_cli.autoswitch` block: the decision and the order under each mode
    (one pass over the store, evaluated four times).

    While an operation of Claude Code runs, the rule decides nothing (`tick`), and what a running switch has
    written is no past try yet: its note when it starts, and the early switch's own mark right after. So the
    block carries no hold then, and it is worked out without the notes of the running operations' accounts.
    With them the page was told that the running switch "was tried a moment ago", during a second try that
    its account is skipped, and during an early switch that the early switch waits 2 hours.

    The running operations are read after the history, never before it. A switch that started while this
    state was being built has written its note by then, and it is in the list."""
    settings, rows = engine.prefs.public(), inputs(engine)
    history, now = engine.store.setting("cli_history", {}), engine.clock()
    running = {op.account for op in engine.runner.unfinished("claude_cli")}
    if isinstance(history.get("tried"), dict):
        history = {**history, "tried": {key: note for key, note in history["tried"].items() if key not in running}}
    result = cli_policy.evaluate(rows, settings, history, now)
    return {"enabled": settings["cli_autoswitch"], "threshold": settings["cli_threshold"],
            "order_mode": settings["cli_order"], "early_switch": settings["cli_early_switch"],
            "next_up": result["next_up"], "last": history.get("last"),
            "hold": engine.cli.adapter.readonly_reason or (None if running else result["hold"]),
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
            note = cli_policy.tried(past["tried"].pop(in_use["id"], None))
            if note is not None:
                # When the app died, this is the note of the switch that brought the terminals here, and what
                # that switch writes at its end (`last`) is missing. The same after a switch that failed past
                # its write and left the login on its target (`switch_cli.settle`). The rule uses an account
                # the terminals just came to for 15 minutes before its paused reads count
                # (`cli_policy._came`), and without the time of that switch it left the account again at
                # once. After a sign-in in Claude Code the time is that of the try that failed, and the same
                # stay does no harm.
                past["came"] = {"to": in_use["id"], "at": note[0]}
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
    # A switch is made on a reading at most `READY` old, and an older one is read again first. The rule says
    # when a switch goes by the reading the account has (`last_reading`, `cli_policy._leave`): Claude has paused
    # the account's reads, so the scheduler starts none before the pause ends, and the account in use works
    # for no model.
    if not result["last_reading"] and (target["last_good_at"] is None or engine.clock() - target["last_good_at"] > cli_policy.READY):
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
