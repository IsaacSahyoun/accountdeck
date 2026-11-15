"""Proof run for the auto-switch policy. cli_policy.evaluate is pure (no clock, no files), so these
are the app's real decisions for hand-made readings.  Usage: policy_probe.py <code copy>"""
import os, sys
os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"
sys.path.insert(0, sys.argv[1])
from accountdeck import cli_policy, usage

NOW = 1_800_000_000.0
SETTINGS = {"cli_autoswitch": True, "cli_threshold": 95, "cli_order": "soonest_reset", "cli_early_switch": True, "pinned_model": ""}

def account(key, label, weekly_used, weekly_reset, *, active=False, age=60, windows=None):
    return {"id": key, "label": label, "active": active, "forecast": {}, "owner": "v2", "excluded": False, "health": "ok",
            "live_expired": False, "last_good_at": NOW - age,
            "windows": windows or [{"id": "seven_day", "kind": "weekly", "used": weekly_used, "reset": weekly_reset}]}

def answered(key, label, answer, *, active=False):
    """An account whose limits are what the app makes of one answer from Claude."""
    return account(key, label, None, None, active=active, windows=usage.claude_usage(answer, now=NOW)["windows"])

def show(title, accounts, history=None, **settings):
    r = cli_policy.evaluate(accounts, {**SETTINGS, **settings}, history or {}, NOW)
    # `last_reading` is in the decision since the change for a paused account; an older copy has no such key.
    how = " on its last reading, with no new one taken" if r.get("last_reading") else ""
    print(f"{title}\n   -> action={r['action']} target={r['target']}{how} hold={r['hold']!r} reason={r['reason']!r}")

WEEK = NOW + 3 * 86400
beta = account("beta", "Beta", 30, WEEK)
print("A. Active account Alpha is at 96% of its weekly limit; Beta has room.")
show("   the week ends in 3 days", [account("alpha", "Alpha", 96, WEEK, active=True), beta])
show("   the week ended 1 second ago (the reading is still the one from before the refill)",
     [account("alpha", "Alpha", 96, NOW - 1, active=True), beta])
show("   the week ended 40 seconds ago (the next read of Alpha lands 45 seconds after the refill)",
     [account("alpha", "Alpha", 96, NOW - 40, active=True), beta])
print("B. Auto-switch left Alpha for Beta because Alpha's weekly limit hit 96%. The user pressed Undo, so Alpha is active again.")
history = {"left": "beta", "left_at": NOW - 5, "manual_at": NOW - 5,
           "last": {"from": "alpha", "to": "beta", "at": NOW - 60, "kind": "threshold", "reason": "", "undo_until": NOW - 5}}
show("   the next 5-second check after the Undo", [account("alpha", "Alpha", 96, WEEK, active=True), beta], history)
print("C. Claude has no 5-hour limit. What the app makes of Claude's own answers:")
week = {"utilization": 97, "resets_at": "2027-01-20T00:00:00Z"}
show("   an answer with a weekly number only, Alpha at 97%",
     [answered("alpha", "Alpha", {"seven_day": week}, active=True), answered("beta", "Beta", {"seven_day": dict(week, utilization=20)})])
show("   an answer that still carries a 5-hour number of 99%, weekly at 20%",
     [answered("alpha", "Alpha", {"five_hour": dict(week, utilization=99), "seven_day": dict(week, utilization=20)}, active=True),
      answered("beta", "Beta", {"five_hour": dict(week, utilization=99), "seven_day": dict(week, utilization=20)})])
print("D. The Fable weekly limit. What the app makes of Claude's own answers, with settings as a new install has them:")
def fable(key, label, weekly, model, *, active=False):
    return answered(key, label, {"seven_day": dict(week, utilization=weekly), "seven_day_fable": dict(week, utilization=model)}, active=active)
show("   Alpha: weekly 40%, Fable weekly 96%. Beta has room in both",
     [fable("alpha", "Alpha", 40, 96, active=True), fable("beta", "Beta", 30, 30)])
show("   Alpha: weekly 96%. Beta's Fable limit is out, Gamma has room in both",
     [fable("alpha", "Alpha", 96, 10, active=True), fable("beta", "Beta", 20, 100), fable("gamma", "Gamma", 60, 30)])
show("   Alpha's Fable limit is out and the user chose Alpha 5 seconds ago",
     [fable("alpha", "Alpha", 40, 100, active=True), fable("beta", "Beta", 30, 30)], {"manual_at": NOW - 5})
show("   Alpha's week is out. Beta's Fable limit is out and its week is not: Beta still works for the other models",
     [fable("alpha", "Alpha", 100, 10, active=True), fable("beta", "Beta", 20, 100)])
show("   Alpha's week is at 99%, so it still works. Beta's Fable limit is out",
     [fable("alpha", "Alpha", 99, 10, active=True), fable("beta", "Beta", 20, 100)])
show("   Alpha's Sonnet limit is out: another model's limit is not acted on",
     [answered("alpha", "Alpha", {"seven_day": dict(week, utilization=40), "seven_day_sonnet": dict(week, utilization=100)}, active=True),
      fable("beta", "Beta", 30, 30)])
print("E. The early switch: the week that ends soonest is used first. Alpha is in use, 20% of its week used, and its week ends in 6 days:")
def ends_in(key, label, used, days, **more):
    return account(key, label, used, NOW + days * 86400, **more)
alpha = ends_in("alpha", "Alpha", 20, 6, active=True)
show("   Beta's week ends in 12 hours", [alpha, ends_in("beta", "Beta", 40, 0.5)])
show("   Beta's week ends in 2 days, Gamma's in 4 days", [alpha, ends_in("beta", "Beta", 40, 2), ends_in("gamma", "Gamma", 40, 4)])
show("   Beta's week ends in 7 days, after Alpha's", [alpha, ends_in("beta", "Beta", 40, 7)])
show("   Beta's week ends 30 minutes before Alpha's: weeks within the hour end together", [alpha, ends_in("beta", "Beta", 40, 6 - 1 / 48)])
show("   Beta's week ends in 2 days and 85% of it is used", [alpha, ends_in("beta", "Beta", 85, 2)])
show("   Beta's week ends in 2 days and the user chose Alpha 5 seconds ago", [alpha, ends_in("beta", "Beta", 40, 2)], {"manual_at": NOW - 5})
show("   Beta's week ends in 2 days and an early switch happened 1 hour ago", [alpha, ends_in("beta", "Beta", 40, 2)], {"early_at": NOW - 3600})
show("   Beta's week ends in 2 days and an early switch failed 1 hour ago", [alpha, ends_in("beta", "Beta", 40, 2)], {"early_tried": NOW - 3600})
show("   Beta's week ends in 2 days, Gamma's 30 minutes later with more room: a tie, and the one with more room is used",
     [alpha, ends_in("beta", "Beta", 40, 2), ends_in("gamma", "Gamma", 20, 2 + 1 / 48)])
show("   Beta's week ends in 2 days and its reading is 20 minutes old, Gamma's ends in 4 days: Beta is passed over",
     [alpha, dict(ends_in("beta", "Beta", 40, 2), last_good_at=NOW - 1200), ends_in("gamma", "Gamma", 40, 4)])
print("F. A switch at the threshold follows the order, and the early switch waits 2 hours after it. The order is \"most room first\", Beta has the most room and Gamma's week ends first:")
full = ends_in("alpha", "Alpha", 96, 6, active=True)
show("   Alpha reaches 96%", [full, ends_in("beta", "Beta", 10, 4), ends_in("gamma", "Gamma", 40, 2)], cli_order="most_room")
moved = [ends_in("alpha", "Alpha", 96, 6), ends_in("beta", "Beta", 10, 4, active=True), ends_in("gamma", "Gamma", 40, 2)]
def after(seconds):
    return {"left": "alpha", "left_at": NOW - seconds, "last": {"from": "alpha", "to": "beta", "at": NOW - seconds, "kind": "threshold",
                                                                "reason": "", "undo_until": NOW - seconds + 600}}
show("   5 seconds after that switch, Beta in use", moved, after(5), cli_order="most_room")
show("   2 hours after that switch", moved, after(7200), cli_order="most_room")
print("G. Beta is first in the order, its reading is 4 minutes old and Claude has paused its reads for 10 more minutes. Gamma is ready:")
out = ends_in("alpha", "Alpha", 100, 6, active=True)
unread = dict(ends_in("beta", "Beta", 30, 2), last_good_at=NOW - 240, paused_until=NOW + 600)
show("   Alpha's week ran out: Gamma is used, and Beta is not waited for", [out, unread, ends_in("gamma", "Gamma", 10, 4)])
show("   Alpha is at 96%: the same", [full, unread, ends_in("gamma", "Gamma", 10, 4)])
show("   Alpha's week ran out and Beta is the only other account: Beta is used on the reading it has", [out, unread])
show("   Alpha is at 96% and Beta is the only other account: the hold names the pause", [full, unread])
nearly = ends_in("gamma", "Gamma", 92, 4)
show("   Alpha is at 98%, Beta is paused and Gamma is at 92%: Alpha is used on",
     [ends_in("alpha", "Alpha", 98, 6, active=True), unread, nearly])
show("   Alpha is at 99%: Gamma is used up, and Beta's pause is not waited for",
     [ends_in("alpha", "Alpha", 99, 6, active=True), unread, nearly])
show("   Alpha is at 96% and its forecast says 99%: the pause is waited for, since Alpha could be gone back to",
     [dict(full, forecast={"seven_day": 99.5}), unread, ends_in("gamma", "Gamma", 96, 4)])
print("H. A switch to Beta, the first in the order, was started and the terminals did not get there. Gamma is ready:")
ready = ends_in("beta", "Beta", 30, 2)
def tried(seconds, count):
    return {"tried": {"beta": {"at": NOW - seconds, "count": count}}}
show("   Alpha's week ran out, the first try was 5 seconds ago: the terminals stay and the switch is tried once more", [out, ready, ends_in("gamma", "Gamma", 10, 4)], tried(5, 1))
show("   90 seconds after the first try: Beta is tried again", [out, ready, ends_in("gamma", "Gamma", 10, 4)], tried(90, 1))
show("   5 seconds after a second try in a row: Gamma is used", [out, ready, ends_in("gamma", "Gamma", 10, 4)], tried(5, 2))
show("   15 minutes after it: Beta is tried again", [out, ready, ends_in("gamma", "Gamma", 10, 4)], tried(900, 2))
show("   Beta is the only other account, 6 minutes after the second try: the hold gives the earliest time of the next try", [out, ready], tried(360, 2))
show("   Alpha is at 96% and Beta is the only other account: the same hold", [full, ready], tried(360, 2))
