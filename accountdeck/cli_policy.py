"""The terminal auto-switch policy the owner approved. Pure: no I/O or clock reads.

`evaluate` decides, for the active terminal account, whether to switch now and to which account:
- threshold: any considered limit (weekly, Fable weekly, the pinned model's weekly) at or past
  `cli_threshold`, or projected to pass it before the next read can land (the burn-rate forecast);
- out / login: a limit reached or a sign-in that needs renewing switches whatever the threshold;
- eligible targets have a reading at most 15 minutes old and at least 10 points of room below the
  threshold in every considered window; left-out, frozen and needs-sign-in accounts never qualify;
  when none qualifies but some are only too old, those are read again first (`refresh`);
- a switch is made on a reading at most 3 minutes old (`READY`), and the executor reads an older one again
  first. So an account whose reading is older than that, and whose reads are paused (Claude refused one), is
  not eligible while the pause lasts: nothing reads it until then, and waiting for it kept the terminals on
  an account that had run out;
- once no account qualifies, every account is used up to DRAIN_CEILING (99 %): the active one stays
  until it reaches it, then a target with DRAIN_ROOM points left below it is used;
- an account in use that works for no model (its week ran out, or it needs sign-in) is left, after every
  account above, for one whose week can still be used while a model's limit is used up. An account in
  use that still works is never left for such an account;
- an account past the threshold is used only on a reading at most 15 minutes old, like an eligible one.
  An older one is read again, and an account with a new reading behind it in the order is used first.
  An account in use that works for no model does not wait for a reading while another account is ready;
- holds: an expired idle terminal token. An Anthropic incident never holds (the owner's choice,
  2026-10-09): a switch only needs the target's own fresh reading;
- a window of the account in use whose reset time has passed has refilled, so its last number is not
  acted on: the windows still running decide, and when none of them asks for a switch the rule holds
  and asks for a new reading of that account (`refresh`);
- a manual choice, Undo included, is held for 2 hours against the threshold, the early switch and a model's
  limit that ran out (the account still works for the other models). It is not held against an account
  whose week ran out or that needs sign-in, and once auto-switch has moved the terminals off the chosen
  account for that, nothing is held;
- the optional early switch keeps the terminals on the usable account whose week ends soonest, so allowance
  is spent before it expires, however far off that end is. Weeks that end within the hour of each other end
  together, and of those the one with the most room is used first. It waits 2 hours after any automatic
  switch, and after one of its own that did not finish. Where a switch at the threshold goes is the order's
  to say, as before.
"""
from functools import cmp_to_key
import math

from .core import is_frozen
from .rules import clock_text
from .usage import is_fable

HOUR = 3600
READY = 180     # a switch is made on a reading at most this old: `cli_auto.tick` reads an older one again first


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def considered(account, settings):
    """The limits the rule acts on, the weekly one first: (name, % used or None, window). They are the weekly
    limit, the Fable weekly limit (`usage.is_fable`) and the pinned model's, each only when the account's
    reading has it: a model's limit is in Claude's answer after the first message to that model. Another
    model's limit is not acted on. Claude has no 5-hour limit (`usage.claude_usage`). `engine.targets` (how
    often an account is read), `switch_cli.plan` (the sheet's warning) and the page's notch
    (`AD.accounts.watched`) follow this list."""
    windows = account.get("windows") or []
    weekly = next((w for w in windows if w.get("kind") == "weekly"), {})
    values = [("weekly", number(weekly.get("used")), weekly)]
    pinned = settings.get("pinned_model", "").casefold()
    for window in windows:
        if window.get("kind") != "model":
            continue
        model = str(window.get("model") or window.get("label", "").removesuffix(" weekly"))
        if is_fable(window) or (pinned and model.casefold() == pinned):
            values.append((window.get("label") or f"{model} weekly", number(window.get("used")), window))
    return values


FORECAST_SPAN, FORECAST_MIN_SPAN = 1200, 120    # the burn rate comes from the last 20 min of readings
SAME_RESET = 300     # reset times of one window drift by a second between readings (21:29:59 vs 21:30:00)


def same_window(a, b):
    """True when two readings' reset times name the same window; a new window resets hours later."""
    a, b = number(a), number(b)
    return a == b if a is None or b is None else abs(a - b) <= SAME_RESET


def forecast(readings, now, horizon):
    """Projected % used at `now + horizon` from one window's recent readings (oldest first, {"at", "used",
    "reset"}), or None without a rising trend or when the window refills first."""
    if not readings:
        return None
    last = readings[-1]
    recent = [r for r in readings if r["at"] >= last["at"] - FORECAST_SPAN and same_window(r.get("reset"), last.get("reset"))]
    first = recent[0]
    span = last["at"] - first["at"]
    if span < FORECAST_MIN_SPAN or last["used"] <= first["used"]:
        return None
    if number(last.get("reset")) is not None and last["reset"] <= now + horizon:
        return None
    return last["used"] + (last["used"] - first["used"]) / span * (now + horizon - last["at"])


def reset(window):
    return number(window.get("reset"))


def ended(window, now):
    """True once a window's reset time has passed. The limit it measured has refilled, so its last number
    says nothing about the new window, whenever that number was read."""
    at = reset(window)
    return at is not None and at <= now


def duration(seconds):
    """"45 min", "12h 0m" or "2d 5h"."""
    minutes = max(0, math.ceil(seconds / 60))
    if minutes >= 1440:
        return f"{minutes // 1440}d {minutes % 1440 // 60}h"
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes} min"


# Once every account is past the threshold, each is used up to the ceiling rather than idling
# there: the active account stays until it reaches it, and a target needs this much room below it.
DRAIN_CEILING, DRAIN_ROOM = 99, 2
MANUAL_HOLD = "Your manual choice is held for 2 hours"
EARLY_PAUSE = "The early switch waits 2 hours after an automatic switch"


def evaluate(accounts, settings, history, now):
    """The decision for the terminal account in use: {"action": "switch" or "none", "target", "kind", "reason",
    "hold": why it is waiting, "refresh": accounts to read again first, "next_up": every account in switching
    order with why it is skipped, "exhausted": every account is low}."""
    threshold = settings.get("cli_threshold", 95)
    rows = _ordered([_row(account, settings, threshold, now) for account in accounts], settings)
    result = {"action": "none", "target": None, "kind": None, "reason": "", "hold": None, "refresh": [],
              "next_up": [{key: row[key] for key in ("id", "label", "skip", "room", "weekly_reset")} for row in rows],
              "exhausted": False}
    active = next((row for row in rows if row["account"].get("active")), None)
    if not settings.get("cli_autoswitch", True) or active is None or is_frozen(active["account"]):
        return result
    if active["account"].get("live_expired"):
        result["hold"] = "Usage updates after your next message in Claude Code"
        return result
    need = _need(active, threshold, now)
    if need["trigger"]:
        result.update(_leave(need, rows, active, history, settings, now))
    elif need["refilled"]:
        result.update(hold=f"{active['label']}'s {need['refilled'][0]} limit has refilled. Waiting for its new reading.",
                      refresh=[active["id"]])
    elif settings.get("cli_early_switch", True):
        result.update(_early(rows, active, history, now))
    return result


def _row(account, settings, threshold, now):
    """One account as the rule sees it: its room below the threshold and, in `skip`, why it cannot be switched to."""
    values = considered(account, settings)
    known = all(used is not None for _, used, _ in values)
    room = min(threshold - used for _, used, _ in values) if known else None
    age = max(0, now - account["last_good_at"]) if number(account.get("last_good_at")) is not None else None
    # A reading too old to act on is read again first. That is one over 15 minutes old. It is also one the
    # executor takes again before it switches (older than `READY`) while the account's reads are paused. Claude
    # refusing a read (HTTP 429) pauses them for 15 to 60 minutes. Until then the scheduler starts no read of the
    # account and the Refresh button is refused, and a switch that waited kept the terminals on an account
    # that had run out.
    pause = number(account.get("paused_until"))
    paused = pause is not None and pause > now
    unread = age is not None and READY < age <= 900 and paused
    old = age is not None and (age > 900 or unread)
    skip, past, stale = None, False, False
    if account.get("active"):
        skip = "in use by your terminals"
    elif is_frozen(account) or account.get("excluded"):
        skip = "left out"
    elif account.get("health", "ok") != "ok":
        skip = "needs sign-in"
    elif age is None:
        skip = "reading unavailable"
    elif not known:
        skip = "weekly reading unknown" if values[0][1] is None else "reading incomplete"
    elif room < 10:
        label, used, _ = max(values, key=lambda v: v[1])
        skip, past = f"{label} at {used:g}%", True
    elif old:
        # Room at its last reading: read it again when it is needed.
        # Not the words of the setting that pauses every check ("Usage checks paused"): this pause is Claude's.
        skip, stale = "Claude paused its checks" if unread else f"reading {math.ceil(age / 60)} min old", True
    # Skipped only for being past the threshold (not health, freshness or choice): usable when all are.
    usable = skip is None or past
    spent = [(label, used, w) for label, used, w in values
             if usable and (used > DRAIN_CEILING - DRAIN_ROOM or w.get("blocked"))]
    # Only a model's limit is spent and the week is not: the account still works for the other models, so it
    # is used when the account in use works for none. `week_only` names that limit.
    week_only = (f"{spent[0][0]} limit is at {spent[0][1]:g}%"
                 if spent and all(w.get("kind") == "model" for _, _, w in spent) else None)
    return {"id": account["id"], "label": account["label"], "skip": skip, "room": room,
            "weekly_reset": reset(values[0][2]), "account": account, "values": values,
            "drainable": usable and not spent, "week_only": week_only, "stale": stale, "old": old, "paused": paused}


def _ordered(rows, settings):
    """The rows in switching order: the week that ends soonest first, the most room first, or the user's own
    order with the rest after it."""
    def compare(a, b, most_room=False):
        ar, br = a["weekly_reset"] or math.inf, b["weekly_reset"] or math.inf
        am, bm = a["room"] if a["room"] is not None else -math.inf, b["room"] if b["room"] is not None else -math.inf
        if most_room:
            primary = (br - ar) if abs(am - bm) <= 1 else am - bm
        else:
            primary = am - bm if abs(ar - br) <= HOUR else br - ar
        if primary and not math.isnan(primary):
            return -1 if primary > 0 else 1
        return (a["label"].casefold() > b["label"].casefold()) - (a["label"].casefold() < b["label"].casefold())

    mode = settings.get("cli_order", "soonest_reset")
    rows = sorted(rows, key=cmp_to_key(lambda a, b: compare(a, b, mode == "most_room")))
    if mode == "manual":
        place = {key: index for index, key in enumerate(settings.get("cli_manual_order", []))}
        rows.sort(key=lambda row: place.get(row["id"], math.inf))
    return rows


def _need(active, threshold, now):
    """Why the account in use should be left, judged on its windows still running: `trigger` (login, out,
    threshold or None) and `why` in words. A window whose reset time has passed is left out; `refilled` names
    those that ended at or past the threshold, whose new reading is waited for. `yields` says the user's own
    choice outranks this need: the threshold, and a model's limit that ran out while the week has room."""
    account = active["account"]
    values = [value for value in active["values"] if not ended(value[2], now)]
    # A limit filling fast can pass the threshold, or run out, before the next read is allowed: act on the
    # forecast for that moment rather than the last reading.
    ahead = account.get("forecast") or {}
    projected = [(label, max(used, ahead[w["id"]]) if used is not None and w.get("id") in ahead else used)
                 for label, used, w in values]
    over = [label for label, used, _ in values if used is not None and used >= threshold]
    soon = [label for label, used in projected if used is not None and used >= threshold]
    out = [(label, w) for label, used, w in values if w.get("blocked") or (used is not None and used >= 100)]
    if account.get("health") == "needs_signin":
        trigger, why = "login", "needs sign-in"
    elif account.get("out") or out:
        trigger, why = "out", f"reached its {out[0][0]} limit" if out else "reached a limit"
    elif over:
        trigger, why = "threshold", f"reached {threshold}% of its {over[0]} limit"
    elif soon:
        trigger, why = "threshold", f"will pass {threshold}% of its {soon[0]} limit before the next check"
    else:
        trigger, why = None, ""
    return {"trigger": trigger, "why": why,
            "yields": trigger == "threshold" or (trigger == "out" and not account.get("out")
                                                 and all(w.get("kind") == "model" for _, w in out)),
            "peak": max((used for _, used in projected if used is not None), default=0),
            "refilled": [label for label, used, w in active["values"]
                         if ended(w, now) and (w.get("blocked") or (used is not None and used >= threshold))]}


def _held(history, active, now):
    """True for 2 hours after the user's own choice, Undo included, while the account they chose
    (`manual_for`) is the one in use. Once auto-switch has moved the terminals off it (its week ran out, or
    it needed sign-in), the account in use is not the user's choice. A history from before `manual_for`
    has only the time, and holds whichever account is in use, as it did then."""
    return (now - history.get("manual_at", -1e20) < 2 * HOUR
            and history.get("manual_for", active["id"]) == active["id"])


def _checking(rows):
    """The hold while accounts whose readings are too old to act on are read again. An account whose reads Claude
    paused is not read before the pause ends, so when every one of them is paused the hold says that."""
    names = ", ".join(row["label"] for row in rows[:3])
    waits = all(row["paused"] for row in rows)
    return {"hold": f"Claude paused the usage checks of {names}" if waits else f"Checking {names} before switching",
            "refresh": [row["id"] for row in rows]}


def _leave(need, rows, active, history, settings, now):
    """What to do once the account in use asks to be left: wait, read the others again, switch, or say that every
    account is low. Returns the fields of the result that change."""
    label, threshold = active["label"], settings.get("cli_threshold", 95)
    kind = "refilled" if history.get("exhausted") else need["trigger"]
    # The user's own choice outranks the threshold and a model's limit that ran out. It does not outrank an
    # account whose week ran out or that needs sign-in: staying there would stop their work.
    if need["yields"] and _held(history, active, now):
        return {"hold": MANUAL_HOLD}
    eligible = [row for row in rows if row["skip"] is None]
    if eligible:
        return {"action": "switch", "target": eligible[0]["id"], "kind": kind,
                "reason": f"{label} {need['why']}; {eligible[0]['label']} is first in your eligible order."}
    # An account with room and an old reading is read again. While the account in use still works, that is
    # waited for. Once it works for no model, an account that is ready now is used first (further down).
    stale = [row for row in rows if row["stale"]]
    if stale and need["yields"]:
        return _checking(stale)
    if need["trigger"] == "threshold" and need["peak"] < DRAIN_CEILING:
        return {"hold": f"Every account is past {threshold}% — using {label} up to {DRAIN_CEILING}%"}
    # The last resorts, the better one first. Every account is past the threshold, so one that can still be
    # used up is used. And when the account in use works for no model, an account whose week can still be used
    # works for all but the model whose limit ran out, so it is better than staying. While the account in
    # use still works, it is not.
    why = f"reached {DRAIN_CEILING}%" if need["trigger"] == "threshold" else need["why"]
    names = [row["label"] for row in stale[:3]] + ([f"{len(stale) - 3} more"] if len(stale) > 3 else [])
    others = (f"every account is past {threshold}%" if not stale else
              f"{names[0]} needs a new reading first" if len(names) == 1 else
              f"{', '.join(names[:-1])} and {names[-1]} need a new reading first")
    last = [(row, kind, f"{others}, so {row['label']} is used up to {DRAIN_CEILING}%") for row in rows if row["drainable"]]
    if not need["yields"]:
        # These are judged on their week, so the order is the user's order with the week's room as the room.
        # No limit refilled, so the switch keeps the kind of its trigger after "all low" too.
        by_week = _ordered([dict(row, room=threshold - row["values"][0][1]) for row in rows if row["week_only"]], settings)
        last += [(row, need["trigger"], f"{row['label']}'s week can still be used, though its {row['week_only']}")
                 for row in by_week]
    # An old reading is read again, and it does not keep an account with a new reading waiting behind it.
    for row, how, words in last:
        if not row["old"]:
            return {"action": "switch", "target": row["id"], "kind": how, "reason": f"{label} {why}; {words}."}
    if stale or last:
        return _checking(stale + [row for row, _, _ in last])
    return {"exhausted": True, "reason": "All terminal accounts are low." + _refills_first(rows, threshold, now)}


def _refills_first(rows, threshold, now):
    """" Beta refills first, Thu 9:00 AM.": the usable account whose limiting windows all refill soonest, or "".
    A week ends days away, so the time carries its day unless it is within 24 hours."""
    resets = []
    for row in rows:
        account = row["account"]
        if is_frozen(account) or account.get("excluded") or account.get("health", "ok") != "ok":
            continue
        limiting = [w for _, used, w in row["values"] if used is not None and used > threshold - 10]
        if limiting and all(reset(w) is not None and reset(w) > now for w in limiting):
            # Every limiting window has to refill before this account has room.
            resets.append((max(reset(w) for w in limiting), row["label"]))
    if not resets:
        return ""
    at, label = min(resets, key=lambda pair: pair[0])
    return f" {label} refills first, {clock_text(at, now)}."


def _early(rows, active, history, now):
    """The optional early switch: move to the usable account whose week ends soonest, when that is more than an
    hour before the week of the account in use ends. Allowance left at the end of a week is lost, so the week
    nearest its end is spent first, however far off that end is. Weeks within the hour end together, as in
    `_ordered`: an hour of allowance is not worth a switch. To leave an account, the other week has to end
    sooner by that hour and by `SAME_RESET` more: a reset time differs by a second from reading to reading, and
    two weeks an hour apart would be a tie at one reading and apart at the next. The target's week has started
    and not ended, and at least a fifth of it is unused. An account whose reading is too old to act on is
    passed over, not waited for: one that cannot be read would hold every early switch back. Returns the
    fields of the result that change."""
    mine = active["weekly_reset"]
    just_left = history.get("left") if now - history.get("left_at", -1e20) < 1800 else None
    able = [row for row in rows if row["skip"] is None and row["weekly_reset"] is not None and mine is not None
            and now < row["weekly_reset"] < mine - HOUR - SAME_RESET and row["values"][0][1] <= 80
            and row["id"] != just_left]
    # It waits 2 hours after any automatic switch (`last`, which only those write). A switch the account in
    # use needed is then not followed minutes later by a second change of login, which would also take the
    # Undo of the first away. It waits as long after one of its own that was started (`early_tried`, written
    # by `cli_auto.tick`): one that failed is not started again 5 seconds later, with a call to renew the
    # login and an alert each time. The wait is said only while it holds a move back.
    times = ((history.get("last") or {}).get("at"), history.get("early_at"), history.get("early_tried"))
    switched_at = max((at for at in times if number(at) is not None), default=-1e20)      # an old `last` can have no time
    if able and now - switched_at < 2 * HOUR:
        return {"hold": EARLY_PAUSE}
    if _held(history, active, now):
        return {"hold": MANUAL_HOLD}
    if not able:
        return {}
    # Of the weeks that end within the hour of the soonest, the one with the most room. `_ordered` breaks a tie
    # of two weeks the same way. Its tie is pairwise, so with three weeks that each end within the hour of the
    # next its first account can be another one.
    soonest = min(row["weekly_reset"] for row in able)
    target = min((row for row in able if row["weekly_reset"] - soonest <= HOUR),
                 key=lambda row: (-row["room"], row["label"].casefold()))
    return {"action": "switch", "target": target["id"], "kind": "early",
            "reason": f"Using {target['label']}'s weekly allowance before it resets in {duration(target['weekly_reset'] - now)} ({100 - target['values'][0][1]:g}% left)."}
