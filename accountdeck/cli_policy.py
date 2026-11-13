"""The terminal auto-switch policy the owner approved. Pure: no I/O or clock reads.

`evaluate` decides, for the active terminal account, whether to switch now and to which account:
- threshold: any considered window (weekly, the pinned model's weekly) at or past `cli_threshold`,
  or projected to pass it before the next read can land (the burn-rate forecast);
- out / login: a limit reached or a sign-in that needs renewing switches whatever the threshold;
- eligible targets have a reading at most 15 minutes old and at least 10 points of room below the
  threshold in every considered window; left-out, frozen and needs-sign-in accounts never qualify;
  when none qualifies but some are only too old, those are read again first (`refresh`);
- once no account qualifies, every account is used up to DRAIN_CEILING (99 %): the active one stays
  until it reaches it, then a target with DRAIN_ROOM points left below it is used;
- holds: an expired idle terminal token. An Anthropic incident never holds (the owner's choice,
  2026-10-09): a switch only needs the target's own fresh reading;
- a window of the account in use whose reset time has passed has refilled, so its last number is not
  acted on: the windows still running decide, and when none of them asks for a switch the rule holds
  and asks for a new reading of that account (`refresh`);
- a manual choice, Undo included, is held for 2 hours against the threshold and the early switch. It is
  not held against an account that ran out or needs sign-in;
- the optional early switch spends weekly allowance that expires within 24 hours, at most every 2 hours.
"""
from functools import cmp_to_key
import math

from .core import is_frozen
from .rules import clock_text

HOUR = 3600


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def considered(account, settings):
    """The limits the rule acts on, the weekly one first: (name, % used or None, window). Claude has no 5-hour
    limit (`usage.claude_usage`)."""
    windows = account.get("windows") or []
    weekly = next((w for w in windows if w.get("kind") == "weekly"), {})
    models = {w.get("model") or w.get("label", "").removesuffix(" weekly"): w for w in windows if w.get("kind") == "model"}
    values = [("weekly", number(weekly.get("used")), weekly)]
    pinned = settings.get("pinned_model", "").casefold()
    for label, window in models.items():
        if pinned and label.casefold() == pinned:
            values.append((label + " weekly", number(window.get("used")), window))
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
    minutes = max(0, math.ceil(seconds / 60))
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes} min"


# Once every account is past the threshold, each is used up to the ceiling rather than idling
# there: the active account stays until it reaches it, and a target needs this much room below it.
DRAIN_CEILING, DRAIN_ROOM = 99, 2
MANUAL_HOLD = "Your manual choice is held for 2 hours"


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
        result.update(_leave(need, rows, active, history, threshold, now))
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
    elif age > 900:
        # Room at its last reading, too old to act on: read it again when it is needed.
        skip, stale = f"reading {math.ceil(age / 60)} min old", True
    # Skipped only for being past the threshold (not health, freshness or choice): usable when all are.
    drainable = (skip is None or past) and all(used <= DRAIN_CEILING - DRAIN_ROOM and not w.get("blocked")
                                                for _, used, w in values)
    return {"id": account["id"], "label": account["label"], "skip": skip, "room": room,
            "weekly_reset": reset(values[0][2]), "account": account, "values": values, "drainable": drainable,
            "stale": stale}


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
    those that ended at or past the threshold, whose new reading is waited for."""
    account = active["account"]
    values = [value for value in active["values"] if not ended(value[2], now)]
    # A limit filling fast can pass the threshold, or run out, before the next read is allowed: act on the
    # forecast for that moment rather than the last reading.
    ahead = account.get("forecast") or {}
    projected = [(label, max(used, ahead[w["id"]]) if used is not None and w.get("id") in ahead else used)
                 for label, used, w in values]
    over = [label for label, used, _ in values if used is not None and used >= threshold]
    soon = [label for label, used in projected if used is not None and used >= threshold]
    if account.get("health") == "needs_signin":
        trigger, why = "login", "needs sign-in"
    elif account.get("out") or any(w.get("blocked") or (used is not None and used >= 100) for _, used, w in values):
        trigger, why = "out", "reached a limit"
    elif over:
        trigger, why = "threshold", f"reached {threshold}% of its {over[0]} limit"
    elif soon:
        trigger, why = "threshold", f"will pass {threshold}% of its {soon[0]} limit before the next check"
    else:
        trigger, why = None, ""
    return {"trigger": trigger, "why": why,
            "peak": max((used for _, used in projected if used is not None), default=0),
            "refilled": [label for label, used, w in active["values"]
                         if ended(w, now) and (w.get("blocked") or (used is not None and used >= threshold))]}


def _held(history, now):
    """True for 2 hours after the user's own choice, Undo included."""
    return now - history.get("manual_at", -1e20) < 2 * HOUR


def _leave(need, rows, active, history, threshold, now):
    """What to do once the account in use asks to be left: wait, read the others again, switch, or say that every
    account is low. Returns the fields of the result that change."""
    label = active["label"]
    kind = "refilled" if history.get("exhausted") else need["trigger"]
    # The user's own choice outranks the threshold. It does not outrank an account that ran out or needs
    # sign-in: staying there would stop their work.
    if need["trigger"] == "threshold" and _held(history, now):
        return {"hold": MANUAL_HOLD}
    eligible = [row for row in rows if row["skip"] is None]
    if eligible:
        return {"action": "switch", "target": eligible[0]["id"], "kind": kind,
                "reason": f"{label} {need['why']}; {eligible[0]['label']} is first in your eligible order."}
    stale = [row for row in rows if row["stale"]]
    if stale:
        return {"hold": f"Checking {', '.join(row['label'] for row in stale[:3])} before switching",
                "refresh": [row["id"] for row in stale]}
    if need["trigger"] == "threshold" and need["peak"] < DRAIN_CEILING:
        return {"hold": f"Every account is past {threshold}% — using {label} up to {DRAIN_CEILING}%"}
    drain = [row for row in rows if row["drainable"]]
    if drain:
        why = f"reached {DRAIN_CEILING}%" if need["trigger"] == "threshold" else need["why"]
        return {"action": "switch", "target": drain[0]["id"], "kind": kind,
                "reason": f"{label} {why}; every account is past {threshold}%, so {drain[0]['label']} is used up to {DRAIN_CEILING}%."}
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
    """The optional early switch: move to an account whose week ends within 24 hours and before the active one's,
    to spend allowance that would expire. Returns the fields of the result that change."""
    last = history.get("last") or {}
    early_at = history.get("early_at", last.get("at", -1e20) if last.get("kind") == "early" else -1e20)
    if now - early_at < 2 * HOUR:
        return {"hold": "An early switch happened less than 2 hours ago"}
    if _held(history, now):
        return {"hold": MANUAL_HOLD}
    mine = active["weekly_reset"]
    just_left = history.get("left") if now - history.get("left_at", -1e20) < 1800 else None
    early = [row for row in rows if row["skip"] is None and row["weekly_reset"] is not None and mine is not None
             and 0 < row["weekly_reset"] - now <= 24 * HOUR and row["weekly_reset"] < mine
             and row["values"][0][1] <= 80 and row["id"] != just_left]
    if not early:
        return {}
    target = min(early, key=lambda row: (row["weekly_reset"], row["label"].casefold()))
    return {"action": "switch", "target": target["id"], "kind": "early",
            "reason": f"Using {target['label']}'s weekly allowance before it resets in {duration(target['weekly_reset'] - now)} ({100 - target['values'][0][1]:g}% left)."}
