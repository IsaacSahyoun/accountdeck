"""The terminal auto-switch policy the owner approved (§7). Pure: no I/O or clock reads.

`evaluate` decides, for the active terminal account, whether to switch now and to which account:
- threshold: any considered window (5-hour, weekly, the pinned model's weekly) at or past `cli_threshold`,
  or projected to pass it before the next read can land (the burn-rate forecast);
- out / login: a limit reached or a sign-in that needs renewing switches whatever the threshold;
- eligible targets have a reading at most 15 minutes old and at least 10 points of room below the
  threshold in every considered window; left-out, frozen and needs-sign-in accounts never qualify;
  when none qualifies but some are only too old, those are read again first (`refresh`);
- once no account qualifies, every account is used up to DRAIN_CEILING (99 %): the active one stays
  until it reaches it, then a target with DRAIN_ROOM points left below it is used;
- holds: an expired idle terminal token, and a 5-hour refill within 10 minutes when only the 5-hour
  window is past. An Anthropic incident never holds (the owner's choice, 2026-10-09): a switch only
  needs the target's own fresh reading;
- the optional early switch spends weekly allowance that expires within 24 hours, at most every 2
  hours and never within 2 hours of a manual choice.
"""
from datetime import datetime
from functools import cmp_to_key
import math

from .core import is_frozen

HOUR = 3600


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def considered(account, settings):
    windows = account.get("windows") or {}
    if isinstance(windows, list):
        five = next((w for w in windows if w.get("kind") == "session"), {})
        weekly = next((w for w in windows if w.get("kind") == "weekly"), {})
        models = {w.get("model") or w.get("label", "").removesuffix(" weekly"): w for w in windows if w.get("kind") == "model"}
    else:
        five, weekly = windows.get("five_hour", {}), windows.get("weekly", {})
        models = windows.get("models", {})
    values = [("5-hour", 0 if five.get("started") is False else number(five.get("used")), five),
              ("weekly", number(weekly.get("used")), weekly)]
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
    return number(window.get("resets_at", window.get("reset")))


def duration(seconds):
    minutes = max(0, math.ceil(seconds / 60))
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes} min"


# Once every account is past the threshold, each is used up to the ceiling rather than idling
# there: the active account stays until it reaches it, and a target needs this much room below it.
DRAIN_CEILING, DRAIN_ROOM = 99, 2


def evaluate(accounts, settings, history, now):
    threshold = settings.get("cli_threshold", 95)
    mode = settings.get("cli_order", "soonest_reset")
    rows = []
    for account in accounts:
        values = considered(account, settings)
        known = all(used is not None for _, used, _ in values)
        room = min(threshold - used for _, used, _ in values) if known else None
        weekly = values[1][2]
        age = account.get("reading_age")
        if age is None and number(account.get("last_good_at")) is not None:
            age = max(0, now - account["last_good_at"])
        skip, past, stale = None, False, False
        if account.get("active"):
            skip = "in use by your terminals"
        elif is_frozen(account) or account.get("excluded"):
            skip = "left out"
        elif account.get("health", "ok") != "ok":
            skip = "needs sign-in"
        elif number(age) is None:
            skip = "reading unavailable"
        elif not known:
            skip = "weekly reading unknown" if values[1][1] is None else "reading incomplete"
        elif room < 10:
            label, used, _ = max(values, key=lambda v: v[1])
            skip, past = f"{label} at {used:g}%", True
        elif age > 900:
            # Room at its last reading, too old to act on: read it again when it is needed.
            skip, stale = f"reading {math.ceil(age / 60)} min old", True
        # Skipped only for being past the threshold (not health, freshness or choice): usable when all are.
        drainable = (skip is None or past) and all(used <= DRAIN_CEILING - DRAIN_ROOM and not w.get("blocked")
                                                    for _, used, w in values)
        rows.append({"id": account["id"], "label": account["label"], "skip": skip, "room": room,
                     "weekly_reset": reset(weekly), "account": account, "values": values, "drainable": drainable,
                     "stale": stale})

    def cmp(a, b, order="soonest_reset"):
        ar, br = a["weekly_reset"] or math.inf, b["weekly_reset"] or math.inf
        am, bm = a["room"] if a["room"] is not None else -math.inf, b["room"] if b["room"] is not None else -math.inf
        if order == "most_room":
            primary = (br - ar) if abs(am - bm) <= 1 else am - bm
        else:
            primary = am - bm if abs(ar - br) <= HOUR else br - ar
        if primary and not math.isnan(primary):
            return -1 if primary > 0 else 1
        return (a["label"].casefold() > b["label"].casefold()) - (a["label"].casefold() < b["label"].casefold())

    if mode == "manual":
        order = {key: i for i, key in enumerate(settings.get("cli_manual_order", []))}
        rows.sort(key=cmp_to_key(cmp))
        rows.sort(key=lambda a: order.get(a["id"], math.inf))
    else:
        rows.sort(key=cmp_to_key(lambda a, b: cmp(a, b, mode)))
    result = {"action": "none", "target": None, "kind": None, "reason": "", "hold": None, "refresh": [],
              "next_up": [{k: v for k, v in row.items() if k not in ("account", "values", "drainable", "stale")}
                          for row in rows],
              "exhausted": False}
    active = next((row for row in rows if row["account"].get("active")), None)
    if not settings.get("cli_autoswitch", True) or active is None or is_frozen(active["account"]):
        return result
    values = active["values"]
    over = [(label, used, w) for label, used, w in values if used is not None and used >= threshold]
    # A limit filling fast can pass the threshold, or run out, before the next read is allowed: act on the
    # forecast for that moment rather than the last reading.
    ahead = active["account"].get("forecast") or {}
    projected = [(label, max(used, ahead[w["id"]]) if used is not None and w.get("id") in ahead else used, w)
                 for label, used, w in values]
    soon = not over and [(label, used, w) for label, used, w in projected if used is not None and used >= threshold]
    if soon:
        over = soon
    out = active["account"].get("out") or any(w.get("blocked") or (used is not None and used >= 100) for _, used, w in values)
    login = active["account"].get("health") == "needs_signin"
    if active["account"].get("live_expired"):
        result["hold"] = "Usage updates after your next message in Claude Code"
        return result
    trigger = "login" if login else "out" if out else "threshold" if over else None
    eligible = [row for row in rows if row["skip"] is None]
    if trigger:
        five_reset = reset(values[0][2])
        if not out and not login and over and all(label == "5-hour" for label, _, _ in over) and five_reset is not None and 0 <= five_reset - now <= 600:
            result["hold"] = f"{active['label']} refills in {duration(five_reset - now)} — staying"
            return result
        stale = [row for row in rows if row["stale"]]
        if not eligible and stale:
            result.update(hold=f"Checking {', '.join(row['label'] for row in stale[:3])} before switching",
                          refresh=[row["id"] for row in stale])
            return result
        if eligible:
            target = eligible[0]
            if login:
                why = "needs sign-in"
            elif out:
                why = "reached a limit"
            elif soon:
                why = f"will pass {threshold}% of its {over[0][0]} limit before the next check"
            else:
                why = f"reached {threshold}% of its {over[0][0]} limit"
            result.update(action="switch", target=target["id"], kind="refilled" if history.get("exhausted") else trigger,
                          reason=f"{active['label']} {why}; {target['label']} is first in your eligible order.")
        else:
            peak = max((used for _, used, _ in projected if used is not None), default=0)
            if not (out or login) and peak < DRAIN_CEILING:
                result["hold"] = f"Every account is past {threshold}% — using {active['label']} up to {DRAIN_CEILING}%"
                return result
            drain = [row for row in rows if row["drainable"]]
            if drain:
                target = drain[0]
                why = "needs sign-in" if login else "reached a limit" if out else f"reached {DRAIN_CEILING}%"
                result.update(action="switch", target=target["id"], kind="refilled" if history.get("exhausted") else trigger,
                              reason=f"{active['label']} {why}; every account is past {threshold}%, so {target['label']} is used up to {DRAIN_CEILING}%.")
            else:
                resets = []
                for row in rows:
                    if is_frozen(row["account"]) or row["account"].get("excluded") or row["account"].get("health", "ok") != "ok":
                        continue
                    blocked = [w for _, used, w in row["values"] if used is not None and used > threshold - 10]
                    if blocked and all(reset(w) is not None and reset(w) > now for w in blocked):
                        # Every limiting window has to refill before this account has room.
                        resets.append((max(reset(w) for w in blocked), row))
                first_reset, first = min(resets, key=lambda pair: pair[0]) if resets else (None, None)
                when = datetime.fromtimestamp(first_reset or first["weekly_reset"]).strftime("%I:%M %p").lstrip("0") if first else ""
                tail = f" {first['label']} refills first at {when}." if first else ""
                result.update(exhausted=True, reason="All terminal accounts are low." + tail)
        return result
    if not settings.get("cli_early_switch", True):
        return result
    last = history.get("last") or {}
    early_at = history.get("early_at", last.get("at", -1e20) if last.get("kind") == "early" else -1e20)
    if now - early_at < 2 * HOUR:
        result["hold"] = "An early switch happened less than 2 hours ago"
        return result
    if now - history.get("manual_at", -1e20) < 2 * HOUR:
        result["hold"] = "Your manual choice is held for 2 hours"
        return result
    active_reset = active["weekly_reset"]
    early = [row for row in eligible if row["weekly_reset"] is not None and active_reset is not None
             and 0 < row["weekly_reset"] - now <= 24 * HOUR and row["weekly_reset"] < active_reset
             and row["values"][1][1] <= 80
             and not (row["id"] == history.get("left") and now - history.get("left_at", -1e20) < 1800)]
    if early:
        target = min(early, key=lambda row: (row["weekly_reset"], row["label"].casefold()))
        result.update(action="switch", target=target["id"], kind="early",
                      reason=f"Using {target['label']}'s weekly allowance before it resets in {duration(target['weekly_reset'] - now)} ({100 - target['values'][1][1]:g}% left).")
    return result
