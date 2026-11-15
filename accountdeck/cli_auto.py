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
    """Notes that an automatic switch to `key` starts now: one time per account, under `tried` in the history.
    `CLISwitch` calls it before it does anything else and takes the note out once the switch worked. The rule
    passes an account with a note over for `cli_policy.RETRY` (`cli_policy._row`), and an older note says nothing."""
    with history(engine) as past:
        tried = past.get("tried") if isinstance(past.get("tried"), dict) else {}
        past["tried"] = {**tried, key: engine.clock()}


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


def state(engine):
    """The state's `providers.claude_cli.autoswitch` block: the decision and the order under each mode
    (one pass over the store, evaluated four times)."""
    settings, rows = engine.prefs.public(), inputs(engine)
    history, now = engine.store.setting("cli_history", {}), engine.clock()
    result = cli_policy.evaluate(rows, settings, history, now)
    return {"enabled": settings["cli_autoswitch"], "threshold": settings["cli_threshold"],
            "order_mode": settings["cli_order"], "early_switch": settings["cli_early_switch"],
            "next_up": result["next_up"], "last": history.get("last"),
            "hold": engine.cli.adapter.readonly_reason or result["hold"],
            "previews": {mode: cli_policy.evaluate(rows, {**settings, "cli_order": mode}, history, now)["next_up"]
                         for mode in ("soonest_reset", "most_room", "manual")}}


def tick(engine):
    """Acts on the policy's decision. Another client's switch that only waits for its sessions to close does
    not stop it (such a wait lasts until its confirmation runs out, 30 minutes); any other running operation does."""
    if (not engine.owns_login or not engine.identity_known.get("claude_cli")
            or not engine.runner.free_for("claude_cli", beside_waits=True)
            or engine.cli.enrollment or engine.recovery or engine.cli.adapter.readonly_reason
            or engine.prefs.get("refresh_paused")):
        return
    leaving = engine.active_identity.get("claude_cli")     # the account this decision is about
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
        # not (`cli_policy._early`). The account it failed for is also passed over for 15 minutes, as after
        # any automatic switch that did not happen (`note_try`).
        with history(engine) as past:
            past["early_tried"] = engine.clock()
