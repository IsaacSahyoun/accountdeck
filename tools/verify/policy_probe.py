"""Proof run for the auto-switch policy. cli_policy.evaluate is pure (no clock, no files), so these
are the app's real decisions for hand-made readings.  Usage: policy_probe.py <code copy>"""
import os, sys
os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"
sys.path.insert(0, sys.argv[1])
from accountdeck import cli_policy

NOW = 1_800_000_000.0
SETTINGS = {"cli_autoswitch": True, "cli_threshold": 95, "cli_order": "soonest_reset", "cli_early_switch": True, "pinned_model": ""}

def account(key, label, five_used, five_reset, weekly_used, *, active=False, age=60):
    return {"id": key, "label": label, "active": active, "forecast": {}, "owner": "v2", "excluded": False, "health": "ok",
            "live_expired": False, "last_good_at": NOW - age,
            "windows": [{"id": "five_hour", "kind": "session", "used": five_used, "reset": five_reset, "started": True},
                        {"id": "seven_day", "kind": "weekly", "used": weekly_used, "reset": NOW + 3 * 86400}]}

def show(title, accounts, history=None):
    r = cli_policy.evaluate(accounts, SETTINGS, history or {}, NOW)
    print(f"{title}\n   -> action={r['action']} target={r['target']} hold={r['hold']!r} reason={r['reason']!r}")

beta = account("beta", "Beta", 20, NOW + 4 * 3600, 30)
print("A. Active account Alpha is at 96% of its 5-hour limit; Beta has room.")
show("   5-hour limit refills in 4 minutes", [account("alpha", "Alpha", 96, NOW + 240, 40, active=True), beta])
show("   5-hour limit refilled 1 second ago (the reading is still the one from before the refill)",
     [account("alpha", "Alpha", 96, NOW - 1, 40, active=True), beta])
show("   5-hour limit refilled 40 seconds ago (the next read of Alpha lands 45 seconds after the refill)",
     [account("alpha", "Alpha", 96, NOW - 40, 40, active=True), beta])
print("B. Auto-switch left Alpha for Beta because Alpha's weekly limit hit 96%. The user pressed Undo, so Alpha is active again.")
history = {"left": "beta", "left_at": NOW - 5, "manual_at": NOW - 5,
           "last": {"from": "alpha", "to": "beta", "at": NOW - 60, "kind": "threshold", "reason": "", "undo_until": NOW - 5}}
show("   the next 5-second check after the Undo", [account("alpha", "Alpha", 30, NOW + 3600, 96, active=True), beta], history)
