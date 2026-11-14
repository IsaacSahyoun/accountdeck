"""Example /api/state payloads and SwitchPlans: every scenario the dashboard renders,
built from a few raw readings through the same rules (`accountdeck.rules`) the engine and the validator
apply, so the fixtures, the validator and the browser model cannot drift apart. The demo seeds its
synthetic accounts from these scenarios; the dashboard's tests render the written fixtures.

    python -m accountdeck.fixtures typical            # print one /api/state payload
    python -m accountdeck.fixtures --plan busy        # print one SwitchPlan
    python -m accountdeck.fixtures --write-fixtures   # regenerate tests/fixtures/state-*.json, plan-*.json
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys

from . import __version__, intel, rules
from .cli_policy import evaluate
from .contract import PROVIDERS, platform_block, validate_plan, validate_state
from .rules import PRIMARY_KINDS, REFILL_KEEP, clock_text

BASE_NOW = 1790000000.0
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
_UTC = dt.timezone.utc

SCENARIOS = {
    "terminal": "Terminal accounts, automatic order, enrollment and Undo",
    "typical": "Every client with saved accounts: out / watch / plenty, one Codex session open, recommendation",
    "resets-soon": "Four windows inside the 15 min / 1 h / 6 h / 24 h buckets; hero at 12m 30s; Back-in and Use-it chips",
    "refilled": "A confirmed refill 4 min old; hero kind refilled",
    "not-started": "Limits that have not started: Beta's week and Codex's 5-hour window",
    "codex-blocked": "Codex blocked and ordinaryUsageAllowed=false, reset credits, every Codex account exhausted",
    "needs-signin": "Claude Code: one account reauth_required, one expired (last known 3 h); Codex: one reauth_required",
    "blocked-cloudflare": "error.code=blocked on two inactive accounts, state unchanged, last-known readings",
    "stale": "Readings 42 min / 3 h old, age_tier aging/stale, best state unknown",
    "fable-binding": "Fable weekly binding at 92 % used with the account-wide weekly at 40 %",
    "resetting": "A reset passed 2 min ago (percent hidden) and expected_refill on a stale account",
    "rate-limited": "paused_until set, checks paused copy",
    "outage": "Claude outage with incident text, recommendation kept",
    "switching": "A Codex switch pending in waiting_idle with waiting_on",
    "stopping": "A Codex switch pending in stopping, not cancellable",
    "report": "The report of a Codex switch with verified unconfirmed",
    "needs-reconfirm": "A Codex switch with pending.phase needs_reconfirm and the fresh plan attached",
    "empty": "No accounts",
    "loading": "ready=false: first readings still in flight",
    "fifty": "50 accounts (35 Claude Code / 15 Codex), rows density",
    "read-only": "Another AccountDeck 2 holds the sign-ins: read_only=true with its banner",
    "demo-banner": "Demo and safe-mode banners",
    "first-run": "First start on this Mac: the terminal login saved by itself, Codex signed in but not saved",
}
PLANS = {
    "idle": "Codex switch with nothing open: Switch now is the default",
    "busy": "Codex switch while Codex sessions are open: waiting for them is the default",
    "blocked": "A blocker replaces the buttons",
    "add": "Add a Codex account (purpose add)",
    "muse-sessions": "Muse switch while a Muse session is open: wait for it to close",
}
DEFAULT_SETTINGS = {
    "cli_autoswitch": True, "cli_threshold": 95, "cli_order": "soonest_reset", "cli_manual_order": [], "cli_early_switch": True,
    "usage_display": "used", "confirm_when_idle": True, "sort": "stable", "density": "auto", "pinned_model": "",
    "use_soon": False, "show_other_limits": False, "alert_low_left": 15, "alerts_enabled": True,
    "quiet_hours": [22, 8], "notify_reset_minutes": 15, "notifications": True, "tray": True, "start_on_login": False,
    "cwd": "/Users/me", "log_level": "info", "refresh_paused": False,
}


# Window id -> (kind, label, short, model, minutes), as usage.py normalises them.
_SHAPES = {
    "five_hour": ("session", "5-hour", "5h", None, 300),
    "seven_day": ("weekly", "Weekly", "7d", None, 10080),
    "seven_day_fable": ("model", "Fable weekly", "Fable", "Fable", 10080),
    "seven_day_oauth_apps": ("other", "Connected apps weekly", "Apps", None, 10080),
    "seven_day_cowork": ("other", "Cowork weekly", "Cowork", None, 10080),
    "codex:primary": ("session", "5-hour", "5h", None, 300),
    "codex:secondary": ("weekly", "Weekly", "7d", None, 10080),
    "muse:window": ("session", "5-hour", "5h", None, 300),
    "muse:weekly": ("weekly", "Weekly", "7d", None, 10080),
}
_CODEX_IDS = {"five_hour": "codex:primary", "seven_day": "codex:secondary"}
_MUSE_IDS = {"five_hour": "muse:window", "seven_day": "muse:weekly"}
_CODENAMES = ("iguana_necktie", "nimbus_quill")
_SAME = object()


def _w(wid, used, reset=None, *, blocked=None, refilled=None, samples=6):
    """A raw window reading; ``reset`` and ``refilled=(at, confirmed, previous_used)`` are offsets from now."""
    return {"id": wid, "used": used, "reset": reset, "blocked": blocked, "refilled": refilled, "samples": samples}


def _terminal(aid, label, windows=(), **spec):
    """A Claude Code account, as the terminal table lists it."""
    return {"id": aid, "provider": "claude_cli", "label": label, "email": f"{label.lower()}@example.test",
            "plan": "max", "windows": list(windows), **spec}


def _codex(aid, label, windows=(), **spec):
    """A Codex account; scenarios name its windows five_hour/seven_day, usage.py ids them codex:primary/secondary."""
    email = f"{label.lower().replace(' ', '.')}@example.test"
    return {"id": aid, "provider": "codex", "label": label, "email": email, "plan": "pro",
            "windows": [{**w, "id": _CODEX_IDS[w["id"]]} for w in windows], **spec}


def _muse(aid, label, windows=(), **spec):
    """A Muse Code account; scenarios name its windows five_hour/seven_day, usage.py ids them muse:window/weekly."""
    email = f"{label.lower().replace(' ', '.')}@example.test"
    return {"id": aid, "provider": "muse", "label": label, "email": email, "plan": "Max",
            "windows": [{**w, "id": _MUSE_IDS[w["id"]]} for w in windows], **spec}


def muse_accounts():
    return [_muse("acc_m1", "Muse main", [_w("five_hour", 8, 6400), _w("seven_day", 81, 205000)], active=True,
                  checked=-90, credential={"state": "live"}),
            _muse("acc_m2", "Muse side", [_w("five_hour", 0), _w("seven_day", 12, 480000)], checked=-700,
                  credential={"state": "fresh", "expires_in": 2400})]


def terminal_accounts(scenario):
    switched = scenario in ("terminal", "resets-soon")
    rows = []
    for key, label, weekly, reset, excluded, health in (
            ("alpha", "Alpha", 95 if switched else 70, 4 * 3600, False, "ok"),
            ("beta", "Beta", 60, 9 * 3600, False, "ok"),
            ("delta", "Delta", 90, 5 * 3600, False, "ok"),
            ("gamma", "Gamma", 20, 3 * 86400, True, "ok"),
            ("epsilon", "Epsilon", 35, 2 * 86400, False, "needs_signin")):
        rows.append(_terminal("cli_" + key, label, [_w("seven_day", weekly, reset)],
                              active=key == ("beta" if switched else "alpha"), excluded=excluded, health=health,
                              credential={"state": "reauth_required" if health == "needs_signin" else "fresh"}))
    rows[1]["other"] = [_w("seven_day_oauth_apps", 12, 259200)]      # one row with an entry under "other limits"
    if scenario == "first-run":          # the terminal login AccountDeck saved by itself on first start
        rows[0]["label"] = "alpha"
        return rows[:1]
    return rows


def _codex_main(windows=None, **spec):
    """The Codex account in use in most scenarios."""
    windows = [_w("five_hour", 30, 10800), _w("seven_day", 36, 172800)] if windows is None else windows
    return _codex("acc_6", "Codex main", windows, **{"active": True, "checked": -60,
                                                     "credential": {"state": "live", "expires_in": 518400}, **spec})


def _codex_spare(**spec):
    """A second Codex account: the one the switch scenarios switch to."""
    return _codex("acc_7", "Codex spare", [_w("five_hour", 10, 4000), _w("seven_day", 25, 300000)],
                  **{"checked": -400, **spec})


def _at(now, offset):
    return None if offset is None else now + offset


def _window(raw, now, at, stale):
    kind, label, short, model, minutes = _SHAPES[raw["id"]]
    used = None if raw["used"] is None else float(raw["used"])
    reset = _at(now, raw["reset"])
    started = not (used == 0 and reset is None)
    reset_state = rules.reset_state(reset, now, at)
    refilled = None
    if raw["refilled"] is not None:
        refilled = {"at": now + raw["refilled"][0], "confirmed": now + raw["refilled"][1], "from": float(raw["refilled"][2])}
        if now - refilled["confirmed"] > REFILL_KEEP:
            refilled = None
    shown = used if started and reset_state != "passed" else None
    base = rules.band(None if shown is None else max(0.0, 100.0 - shown), raw["blocked"])
    pace = None if base in ("out", "unknown") else rules.pace(shown, reset, minutes, at, raw["samples"])
    severity = rules.severity(shown, raw["blocked"], pace)
    return {"id": raw["id"], "kind": kind, "label": label, "short": short, "model": model, "minutes": minutes,
            "used": shown, "left": None if shown is None else max(0.0, 100.0 - shown), "known": shown is not None,
            "started": started, "reset": reset, "reset_state": reset_state,
            "state": rules.window_state(started, reset_state, severity, refilled, stale, now),
            "severity": severity, "binding": False, "pace": pace, "blocked": raw["blocked"],
            "primary": kind in PRIMARY_KINDS, "refilled": refilled}


def _trend(raws, now, at):
    """A few weekly readings from the current reset epoch (the account's `trend`)."""
    weekly = next((r for r in raws if _SHAPES[r["id"]][0] == "weekly" and r["used"] is not None
                   and r["reset"] is not None), None)
    if weekly is None or at is None:
        return []
    start = now + weekly["reset"] - 604800
    points = [[at - days * 86400, round(weekly["used"] * share, 1)] for days, share in ((3, 0.55), (2, 0.7), (1, 0.85))]
    return [p for p in points if p[0] >= start] + [[at, float(weekly["used"])]]


def _account(spec, now, pinned, tz, order):
    provider, active = spec["provider"], spec.get("active", False)
    checked = _at(now, spec.get("checked", -300))
    reading = spec.get("reading", _SAME)
    at = checked if reading is _SAME else _at(now, reading)
    tier = rules.age_tier(None if at is None else now - at, active)
    raws = spec["windows"] if at is not None else []
    others = spec.get("other", []) if at is not None else []
    windows = sorted((_window(r, now, at, tier == "stale") for r in raws), key=rules.window_order)
    credential = {"state": "fresh", "expires_in": None, "last_outcome": "2xx" if at is not None else None,
                  "dormant_days": None, "session_expires_in_days": None, "revoked_hint": False,
                  **spec.get("credential", {})}
    error = spec.get("error")
    if error is not None:
        error = {"code": error["code"], "message": error["message"], "since": now + error["since"],
                 "outage": error.get("outage", False)}
    paused_until = _at(now, spec.get("paused_until"))
    raw_windows = [{"key": r["id"], "used": None if r["used"] is None else float(r["used"]), "reset": _at(now, r["reset"])}
                   for r in raws + others]
    if provider == "claude_cli" and at is not None:
        raw_windows += [{"key": key, "used": 0.0, "reset": None} for key in _CODENAMES]
    account = {
        "id": spec["id"], "provider": provider, "label": spec["label"], "email": spec.get("email"),
        "plan": spec.get("plan"), "active": active, "switchable": spec.get("switchable", True),
        "saved": spec.get("saved", True), "imported_from": spec.get("imported_from"), "owner": spec.get("owner", "v2"),
        "state": None, "headline": None, "credential": credential, "checked": checked, "last_good_at": at,
        "stale": tier == "stale", "age_tier": tier, "refreshing": spec.get("refreshing", False), "error": error,
        "next_check": paused_until or (None if checked is None else checked + (300 if active else 900)),
        "paused_until": paused_until, "score": None, "recommended": False, "rank": None, "sort_order": order,
        "windows": windows,
        "other_windows": sorted((_window(r, now, at, tier == "stale") for r in others), key=rules.window_order),
        "raw_windows": raw_windows, "credits": spec.get("credits", []), "tokens": spec.get("tokens"),
        "trend": _trend(raws, now, at),
    }
    if spec.get("reset_credits"):
        account["reset_credits"] = [{"count": c, "expires": _at(now, e), "title": t} for c, e, t in spec["reset_credits"]]
    binding = rules.pick_binding(windows, pinned)
    if binding is not None:
        binding["binding"] = True
    account["state"] = rules.account_state(account)
    account["headline"] = rules.headline(account, binding, now, tz)
    return account


def _status(provider, now, level="ok", *, incident=None, since=None, source="statuspage", checked=-120):
    summary = {"ok": "All systems operational", "degraded": "Partially degraded service",
               "outage": "Major outage", "unknown": "Status not checked yet"}[level]
    url = {"claude_cli": "https://status.claude.com", "codex": "https://status.openai.com", "muse": None}[provider]
    return {"level": level, "summary": summary, "source": source, "checked": _at(now, checked),
            "since": _at(now, since), "url": url, "incident": incident}


def _codex_running(now, consumers=True):
    rows = [{"pid": 9001, "kind": "tui", "started": now - 3600, "parent": "Terminal"}] if consumers else []
    return {"consumers": rows, "daemon": bool(consumers), "desktop": False, "checked": now - 5}


def _muse_running(now, consumers=False):
    rows = [{"pid": 9301, "kind": "tui", "started": now - 1800, "parent": "Terminal"}] if consumers else []
    return {"consumers": rows, "checked": now - 5}


def _banner(kind, title, message, actions=(), provider=None, link=None):
    return {"kind": kind, "title": title, "message": message, "actions": list(actions), "provider": provider, "link": link}


_SWITCH_STEPS = (("wait", "Waiting for Codex sessions"), ("save", "Saving the current sign-in"),
                 ("restore", "Restoring the selected sign-in"), ("verify", "Checking the sign-in"))    # file_switch.FileSwitch


def _steps(now, progress):
    """The steps of a Codex switch; ``progress`` maps step id -> (status, detail, started offset, ended offset)."""
    steps = []
    for sid, text in _SWITCH_STEPS:
        status, detail, started, ended = progress.get(sid, ("pending", "", None, None))
        steps.append({"id": sid, "label": text, "status": status, "detail": detail,
                      "started": _at(now, started), "ended": _at(now, ended)})
    return steps


def _pending(now, phase, message, cancellable, progress, *, consent="idle", waiting_on=(), plan=None):
    pending = {"id": "op_7", "kind": "switch", "provider": "codex", "account": "acc_7", "label": "Codex spare",
               "phase": phase, "message": message, "cancellable": cancellable, "started": now - 40,
               "eta_seconds": None if phase == "waiting_idle" else 25, "consent": consent,
               "waiting_on": list(waiting_on), "steps": _steps(now, progress)}
    if plan is not None:
        pending["plan"] = plan
    return pending


def _report(now, verify_reason):
    return {"op": "op_7", "kind": "switch", "provider": "codex", "account": "acc_7", "label": "Codex spare",
            "finished": now - 120, "outcome": "success", "verified": "unconfirmed", "verified_at": None,
            "verify_reason": verify_reason, "summary": "Codex is ready as Codex spare.", "manifest": "op_7",
            "error": None}


def _events(now):
    return [{"id": 88, "at": now - 10, "kind": "refresh", "message": "Refreshed 8 accounts."},
            {"id": 87, "at": now - 900, "kind": "switch", "message": "Switched Codex to Codex main."}]


def _scene_typical(spec, now):
    spec["alerts"] = [{"id": "al_3", "kind": "reset_confirmed", "level": "good", "account": "acc_m2", "window": "muse:window",
                       "title": "Muse side's 5-hour limit refilled", "message": "Muse side is ready again.", "at": now - 800,
                       "snoozed_until": None, "dismissed": False}]
    spec["notices"] = [{"id": "n_12", "level": "info", "message": "Alpha is ready.", "at": now - 900, "sticky": False}]


# A demo holds still: its picture is the state it starts in. In the scenarios that use this, the week of the
# account in use is not the one that ends first, so with the early switch on the demo would move its terminals
# moments after it starts (`cli_policy._early`).
_HOLDS_STILL = {"cli_early_switch": False}


def _scene_resets_soon(spec, now):
    spec["settings"] = dict(_HOLDS_STILL)
    spec["accounts"] = [
        _terminal("cli_alpha", "Alpha", [_w("seven_day", 100, 750)], checked=-90),
        _terminal("cli_beta", "Beta", [_w("seven_day", 55, 345600)], active=True, checked=-400,
                  credential={"state": "live", "expires_in": 4000}),
        _terminal("cli_gamma", "Gamma", [_w("seven_day", 45, 18000)], checked=-500),
        _terminal("cli_delta", "Delta", [_w("seven_day", 60, 72000)], checked=-300),
        _terminal("cli_epsilon", "Epsilon", [_w("seven_day", 30, 518400)], checked=-700),
        _codex_main([_w("five_hour", 20, 1200), _w("seven_day", 50, 172800)]),
    ]


def _scene_refilled(spec, now):
    spec["settings"] = dict(_HOLDS_STILL)
    spec["accounts"] = [
        _terminal("cli_alpha", "Alpha", [_w("seven_day", 2, 604200, refilled=(-600, -240, 100))],
                  active=True, checked=-240, credential={"state": "live", "expires_in": 6000}),
        _terminal("cli_beta", "Beta", [_w("seven_day", 20, 259200)], checked=-500),
        _terminal("cli_gamma", "Gamma", [_w("seven_day", 15, 432000)], checked=-700),
        _codex_main([_w("five_hour", 5, 16000), _w("seven_day", 12, 500000)]),
    ]
    spec["alerts"] = [{"id": "al_9", "kind": "reset_confirmed", "level": "good", "account": "cli_alpha", "window": "seven_day",
                       "title": "Alpha's weekly limit refilled", "message": "Alpha is ready again.", "at": now - 240,
                       "snoozed_until": None, "dismissed": False}]


def _scene_not_started(spec, now):
    spec["accounts"] = [
        _terminal("cli_alpha", "Alpha", [_w("seven_day", 35, 259200)], active=True, checked=-100,
                  credential={"state": "live", "expires_in": 7000}),
        _terminal("cli_beta", "Beta", [_w("seven_day", 0)], checked=-400),
        _terminal("cli_gamma", "Gamma", [_w("seven_day", 10, 518400)], checked=-600),
        _codex_main([_w("five_hour", 0), _w("seven_day", 20, 432000)]),
    ]


def _scene_codex_blocked(spec, now):
    spec["accounts"] = [
        _terminal("cli_alpha", "Alpha", [_w("seven_day", 30, 300000)], active=True, checked=-100,
                  credential={"state": "live", "expires_in": 7000}),
        _terminal("cli_beta", "Beta", [_w("seven_day", 20, 400000)], checked=-500),
        _codex_main([_w("five_hour", 100, 7200, blocked="not_allowed"), _w("seven_day", 64, 259200)],
                    credits=[{"label": "Codex credits", "balance": "0", "unlimited": False, "hasCredits": False},
                             {"label": "Limit reset credits", "details": {"availableCount": 1}}],
                    reset_credits=[(1, 1728000, "Limit reset")]),
        _codex("acc_7", "Codex spare", [_w("five_hour", 20, 3000, blocked="workspace_member_credits_depleted"),
                                        _w("seven_day", 35, 200000, blocked="workspace_member_credits_depleted")],
               checked=-700),
    ]


def _scene_needs_signin(spec, now):
    renew = {"code": "reauth_required", "message": "Sign-in needs renewing.", "since": -2400}
    spec["accounts"] = [
        _terminal("cli_alpha", "Alpha", [_w("seven_day", 40, 300000)], active=True, checked=-90,
                  credential={"state": "live", "expires_in": 6000}),
        _terminal("cli_beta", "Beta", [_w("seven_day", 75, 200000)], checked=-300, reading=-7200, health="needs_signin",
                  credential={"state": "reauth_required", "last_outcome": "4xx"}, error=renew),
        _terminal("cli_gamma", "Gamma", [_w("seven_day", 50, 250000)], checked=-10800,
                  credential={"state": "expired", "expires_in": -14400}),
        _terminal("cli_delta", "Delta", [_w("seven_day", 12, 400000)], checked=-400),
        _codex_main([_w("five_hour", 10, 15000), _w("seven_day", 20, 300000)], reading=-1500,
                    credential={"state": "access_expired", "expires_in": -300, "last_outcome": "4xx"}),
        _codex_spare(checked=-300, reading=-7200, credential={"state": "reauth_required", "last_outcome": "4xx"},
                     error=renew),
    ]


def _scene_blocked_cloudflare(spec, now):
    blocked = {"code": "blocked", "message": "Check refused by Cloudflare · sign-in not known to be bad"}
    spec["accounts"] = [
        _terminal("cli_alpha", "Alpha", [_w("seven_day", 55, 250000)], active=True, checked=-100,
                  credential={"state": "live", "expires_in": 6000}),
        _terminal("cli_beta", "Beta", [_w("seven_day", 35, 300000)], checked=-60, reading=-1500,
                  credential={"state": "expired", "last_outcome": "4xx"}, error={**blocked, "since": -1500}),
        _terminal("cli_gamma", "Gamma", [_w("seven_day", 61, 90000)], checked=-60, reading=-5400,
                  credential={"state": "expired", "last_outcome": "4xx"}, error={**blocked, "since": -5400}),
        _terminal("cli_delta", "Delta", [_w("seven_day", 10, 500000)], checked=-200),
        _codex_main(),
    ]


def _scene_stale(spec, now):
    spec["accounts"] = [
        _terminal("cli_alpha", "Alpha", [_w("seven_day", 62, 200000)], active=True, checked=-2520,
                  credential={"state": "live", "expires_in": 3000}),
        _terminal("cli_beta", "Beta", [_w("seven_day", 80, 150000)], checked=-10800,
                  credential={"state": "expired", "expires_in": -7200}),
        _terminal("cli_gamma", "Gamma", [_w("seven_day", 20, 400000)], checked=-300),
        _terminal("cli_delta", "Delta", [_w("seven_day", 33, 300000)], checked=-900),
        _codex_main(),
    ]


def _scene_fable_binding(spec, now):
    spec["settings"] = {"pinned_model": "Fable", **_HOLDS_STILL}
    spec["accounts"] = [
        _terminal("cli_alpha", "Alpha", [_w("seven_day", 40, 170000), _w("seven_day_fable", 92, 170000)], active=True,
                  checked=-100, credential={"state": "live", "expires_in": 6000}),
        _terminal("cli_beta", "Beta", [_w("seven_day", 30, 300000), _w("seven_day_fable", 35, 300000)], checked=-400),
        _terminal("cli_gamma", "Gamma", [_w("seven_day", 50, 100000), _w("seven_day_fable", 70, 100000)], checked=-600),
        _codex_main(),
    ]


def _scene_resetting(spec, now):
    spec["accounts"] = [
        _terminal("cli_alpha", "Alpha", [_w("seven_day", 100, -120)], active=True, checked=-200,
                  credential={"state": "live", "expires_in": 5000}),
        _terminal("cli_beta", "Beta", [_w("seven_day", 80, -1800)], checked=-7200,
                  credential={"state": "expired", "expires_in": -3600}),
        _terminal("cli_gamma", "Gamma", [_w("seven_day", 25, 350000)], checked=-400),
        _codex_main(),
    ]


def _scene_rate_limited(spec, now):
    paused = 1920
    index = next(i for i, account in enumerate(spec["accounts"]) if account["id"] == "cli_beta")
    spec["accounts"][index] = _terminal(
        "cli_beta", "Beta", [_w("seven_day", 45, 300000)], checked=-60, reading=-900, paused_until=paused,
        credential={"state": "expired", "expires_in": -3600, "last_outcome": "429"},
        error={"code": "rate_limited", "since": -60,
               "message": f"Usage checks paused until {clock_text(now + paused, now, _UTC)}"})


def _scene_outage(spec, now):
    paused = {"code": "outage", "message": "Reading paused · provider incident", "since": -1500, "outage": True}
    spec["status"] = {"claude_cli": _status("claude_cli", now, "outage", since=-1800, checked=-60, incident={
        "title": "Elevated errors on claude.ai", "impact": "major", "status": "investigating", "started": now - 1800})}
    spec["accounts"] = [
        _terminal("cli_alpha", "Alpha", [_w("seven_day", 70, 200000)], checked=-60, reading=-1500, error=paused),
        _terminal("cli_beta", "Beta", [_w("seven_day", 40, 300000)], checked=-60, reading=-1500, error=paused),
        _terminal("cli_delta", "Delta", [_w("seven_day", 12, 400000)], active=True, checked=-60, reading=-1500,
                  credential={"state": "live", "expires_in": 2000}, error=paused),
        _codex_main(),
    ]
    spec["banners"] = [_banner("outage", "Claude Code (terminal) is having an incident",
                               "Elevated errors on claude.ai. Your last good readings are kept. Switching still works; "
                               "verification may take longer.", provider="claude_cli", link="https://status.claude.com")]


def _scene_switching(spec, now):
    spec["accounts"].append(_codex_spare())
    spec["pending"] = _pending(now, "waiting_idle", "Waiting for Codex sessions to close…", True,
                               {"wait": ("active", "", -38, None)},
                               waiting_on=[{"pid": 9001, "name": "codex", "status": "busy"}])


def _scene_stopping(spec, now):
    spec["accounts"].append(_codex_spare())
    spec["pending"] = _pending(now, "stopping", "Stopping the Codex daemon…", False, {"wait": ("active", "", -38, None)})
    spec["codex_running"] = {**_codex_running(now, consumers=False), "daemon": True}


def _scene_report(spec, now):
    spec["accounts"] = [a for a in spec["accounts"] if a["provider"] != "codex"] + [
        _codex_main(active=False, checked=-600, credential={"state": "fresh", "expires_in": 518400}),
        _codex_spare(active=True, checked=-100, credential={"state": "live", "expires_in": 500000})]
    spec["report"] = _report(now, "Codex did not answer the usage check yet. The local sign-in matches.")
    spec["last_manifest"] = "op_7"


def _scene_needs_reconfirm(spec, now):
    spec["accounts"].append(_codex_spare())
    plan = example_plan("busy", now)
    plan["running"]["codex_consumers"].append({"pid": 9120, "kind": "tui", "started": now - 15, "parent": "Terminal"})
    plan["process_set"].append([9120, 133812345690000000])
    plan["signature"] = _signature(plan["process_set"])
    spec["pending"] = _pending(now, "needs_reconfirm", "The Codex process set changed. Review the switch again.", True,
                               {"wait": ("warning", "A new Codex session started", -38, -15)}, plan=plan)


def _scene_empty(spec, now):
    spec.update(accounts=[], events=[], codex_running=_codex_running(now, consumers=False))


def _scene_loading(spec, now):
    spec.update(ready=False, events=[], status={p: _status(p, now, "unknown", source="none", checked=None) for p in PROVIDERS})
    spec["accounts"] = [
        _terminal("cli_alpha", "Alpha", active=True, checked=None, refreshing=True,
                  credential={"state": "unknown", "last_outcome": None}),
        _terminal("cli_beta", "Beta", checked=None, credential={"state": "unknown", "last_outcome": None}),
        _codex_main([], checked=None, refreshing=True, credential={"state": "unknown", "last_outcome": None}),
    ]


def _scene_fifty(spec, now):
    spec["settings"] = {"density": "rows", **_HOLDS_STILL}
    spec["accounts"] = _fifty_accounts()
    spec["events"] = [{"id": 412, "at": now - 30, "kind": "refresh", "message": "Refreshed 50 accounts."}]


def _scene_read_only(spec, now):
    spec["read_only"] = True
    spec["banners"] = [_banner("read_only", "Another AccountDeck 2 is managing your sign-ins",
                               "Quit that copy to make changes here. This one only shows usage.")]


def _scene_demo_banner(spec, now):
    spec.update(mode="demo", safe_mode=True)
    spec["banners"] = [_banner("demo", "Demo mode", "Demo mode — synthetic accounts; no real apps are touched."),
                       _banner("safe_mode", "Safe mode", "Safe mode is on: AccountDeck 2 will not touch real apps or files.")]


def _scene_first_run(spec, now):
    spec["accounts"] = terminal_accounts("first-run") + [
        _codex("unsaved-codex", "gamma", [_w("five_hour", 20, 9000), _w("seven_day", 46, 280000)], active=True,
               switchable=False, saved=False, checked=-20, email="gamma@example.test",
               credential={"state": "live", "expires_in": 6800})]
    spec["notices"] = [{"id": "n_1", "level": "good", "message": "Saved your current terminal login as alpha@example.test.",
                        "at": now - 30, "sticky": True}]
    spec["events"] = []
    spec["codex_running"] = _codex_running(now, consumers=False)


# What each scenario changes in the typical state ("terminal" is "typical" with a terminal switch just made).
_SCENES = {
    "typical": _scene_typical,
    "resets-soon": _scene_resets_soon,
    "refilled": _scene_refilled,
    "not-started": _scene_not_started,
    "codex-blocked": _scene_codex_blocked,
    "needs-signin": _scene_needs_signin,
    "blocked-cloudflare": _scene_blocked_cloudflare,
    "stale": _scene_stale,
    "fable-binding": _scene_fable_binding,
    "resetting": _scene_resetting,
    "rate-limited": _scene_rate_limited,
    "outage": _scene_outage,
    "switching": _scene_switching,
    "stopping": _scene_stopping,
    "report": _scene_report,
    "needs-reconfirm": _scene_needs_reconfirm,
    "empty": _scene_empty,
    "loading": _scene_loading,
    "fifty": _scene_fifty,
    "read-only": _scene_read_only,
    "demo-banner": _scene_demo_banner,
    "first-run": _scene_first_run,
}


def _scenario(name, now):
    """The raw pieces of one example state: the typical accounts and events as its scenario changes them. The
    typical accounts are the terminal ones, Codex main and, where the scenario shows them, the Muse ones."""
    codex_credits = [{"label": "Codex credits", "balance": "0", "unlimited": False, "hasCredits": False}]
    spec = {"accounts": terminal_accounts(name) + [_codex_main(credits=codex_credits)], "events": _events(now)}
    if name in ("typical", "terminal"):
        spec["accounts"] += muse_accounts()
    _SCENES["typical" if name == "terminal" else name](spec, now)
    return spec


def _fifty_accounts():
    accounts = []
    for n in range(1, 36):
        stale = n % 9 == 0
        age = 3900 + 60 * n if stale else 60 + (n * 97) % 1100
        week = (n * 13 + 7) % 101
        week_reset = 3600 + (n * 104729) % 590000
        windows = [_w("seven_day", week, None if week == 0 else week_reset)]
        if n % 5 == 0:
            windows.append(_w("seven_day_fable", min(100, week + 20), week_reset))
        state = "reauth_required" if n == 17 else "expired" if n % 11 == 5 else "live" if n == 1 else "fresh"
        error = {"code": "blocked", "message": "Check refused by Cloudflare · sign-in not known to be bad",
                 "since": -age} if n == 23 else None
        accounts.append(_terminal(f"cli_{n:02d}", f"Claude {n:02d}", windows, active=n == 1, checked=-age, error=error,
                                  email=f"claude{n:02d}@example.test", credential={"state": state},
                                  health="needs_signin" if n == 17 else "ok"))
    for n in range(36, 51):
        age = 60 + (n * 211) % 1500
        five = (0, 9, 22, 35, 58, 73, 90, 100)[n % 8]
        week = (n * 17 + 3) % 101
        five_raw = _w("five_hour", five, None if five == 0 else min(18000 - age - 60, 900 + (n * 977) % 15000),
                      blocked="rate_limit_reached" if five == 100 else None)
        week_raw = _w("seven_day", None if n == 40 else week, 7200 + (n * 7919) % 580000)
        accounts.append(_codex(f"acc_{n}", f"Codex {n - 35:02d}", [five_raw, week_raw], active=n == 36, checked=-age,
                               email=f"codex{n - 35:02d}@example.test",
                               credential={"state": "live" if n == 36 else "fresh"}))
    return accounts


def example_state(scenario, now=BASE_NOW):
    """The /api/state payload for one of ``SCENARIOS`` at ``now`` (clock copy in UTC)."""
    if scenario not in SCENARIOS:
        raise KeyError(f"Unknown scenario {scenario!r}; choose one of {', '.join(SCENARIOS)}")
    tz = _UTC
    spec = _scenario(scenario, now)
    settings = copy.deepcopy({**DEFAULT_SETTINGS, **spec.get("settings", {})})    # callers may edit the payload
    pinned = settings["pinned_model"]
    accounts = [_account(a, now, pinned, tz, index) for index, a in enumerate(spec["accounts"])]
    for account, source in zip(accounts, spec["accounts"], strict=True):
        if account["provider"] == "claude_cli":
            account.update(excluded=source.get("excluded", False), health=source.get("health", "ok"), live_expired=False)
    status = {p: spec.get("status", {}).get(p) or _status(p, now) for p in PROVIDERS}
    best = {}
    for provider in PROVIDERS:
        best[provider], ranked, _pick = intel.recommend(provider, accounts, settings, None, now, tz=tz)
        for rank, account in enumerate(ranked, 1):
            account["rank"], account["score"] = rank, round(rules.headroom(account, pinned) / 100, 2)
        if best[provider]["state"] in ("ok", "unknown") and best[provider]["account"] is not None:
            next(a for a in accounts if a["id"] == best[provider]["account"])["recommended"] = True
    strip = rules.strip(accounts, now)
    pending = spec.get("pending")
    switching = {p: pending is not None and pending["provider"] == p for p in PROVIDERS}
    active = {p: next((a["id"] for a in accounts if a["provider"] == p and a["active"]), None) for p in PROVIDERS}
    return {
        "app": "accountdeck2", "version": __version__, "rev": 4812, "now": now,
        "mode": spec.get("mode", "live"), "safe_mode": spec.get("safe_mode", False), "ready": spec.get("ready", True),
        "read_only": spec.get("read_only", False), "settings": settings,
        "providers": {
            "codex": {"status": status["codex"], "active": active["codex"],
                      "running": spec.get("codex_running") or _codex_running(now), "switching": switching["codex"],
                      "paused": settings["refresh_paused"] or switching["codex"]},
            "claude_cli": {"status": status["claude_cli"], "active": active["claude_cli"], "running": {"checked": now},
                           "switching": switching["claude_cli"], "paused": settings["refresh_paused"],
                           "autoswitch": terminal_autoswitch(accounts, settings, now,
                               {"last": {"from": "cli_alpha", "to": "cli_beta", "at": now - 120,
                                "kind": "threshold", "reason": "Alpha reached 95% of its weekly limit; switched terminals to Beta.",
                                "undo_until": now + 480}} if scenario in ("terminal", "resets-soon") else None),
                           "enrollment": {"phase": "idle", "message": ""}, "readonly_reason": None},
            "muse": {"status": status["muse"], "active": active["muse"],
                     "running": spec.get("muse_running") or _muse_running(now), "switching": switching["muse"],
                     "paused": settings["refresh_paused"] or switching["muse"]},
        },
        "accounts": accounts,
        "now_band": {"next_reset": rules.next_reset(accounts, strip, now), "best": best, "attention": rules.attention(accounts, now, tz)},
        "resetting_soon": strip,
        "pending": pending, "report": spec.get("report"), "login": None,
        "recovery": {"pending": False, "count": 0},
        "notices": spec.get("notices", []), "alerts": spec.get("alerts", []), "banners": spec.get("banners", []),
        "setup": {"message": "Your settings stay shared.", "groups": []},
        "sessions": [], "cwd": settings["cwd"], "events": spec.get("events", []),
        "last_manifest": spec.get("last_manifest"), "platform": platform_block(),
    }


# ---- SwitchPlan fixtures ----

def terminal_autoswitch(accounts, settings, now, history=None):
    rows = [a for a in accounts if a["provider"] == "claude_cli"]
    result = evaluate(rows, settings, history or {}, now)
    return {"enabled": settings["cli_autoswitch"], "threshold": settings["cli_threshold"],
            "order_mode": settings["cli_order"], "early_switch": settings["cli_early_switch"],
            "next_up": result["next_up"], "last": (history or {}).get("last"), "hold": result["hold"],
            "previews": {mode: evaluate(rows, {**settings, "cli_order": mode}, history or {}, now)["next_up"]
                         for mode in ("soonest_reset", "most_room", "manual")}}




def _signature(pairs):
    return "sha256:" + hashlib.sha256(json.dumps(sorted(pairs)).encode()).hexdigest()


def example_plan(name, now=BASE_NOW):
    """The SwitchPlan fixture ``plan-<name>`` (one of ``PLANS``) at ``now``: what switch_codex.build_plan and
    switch_muse's planner make."""
    if name not in PLANS:
        raise KeyError(f"Unknown plan {name!r}; choose one of {', '.join(PLANS)}")
    state = example_state("typical", now)
    accounts = {a["id"]: a for a in state["accounts"]}
    main = accounts["acc_6"]
    target = _account(_codex_spare(), now, "", _UTC, len(accounts))
    consumers = [{"pid": 9001, "kind": "tui", "started": now - 3600, "parent": None},
                 {"pid": 9100, "kind": "app_server_child", "started": now - 600, "parent": None}]
    plan = {
        "plan_id": "pl_" + hashlib.sha256(name.encode()).hexdigest()[:32], "expires": now + 120,
        "provider": "codex", "purpose": "switch", "target": rules.plan_target(target), "current": rules.plan_current(main),
        "running": {"codex_consumers": consumers},
        "process_set": [[9001, 133812300000000000], [9100, 133812340000000000]],
        "signature": "", "blockers": [], "warnings": [],
        "will": ["Wait for Codex sessions to close.", "Save the outgoing sign-in.",
                 "Replace only auth.json and verify the selected account."],
        "preserved": ["Configuration, MCP servers, plugins and session history"],
        "confirmation_required": True, "idle": False, "remember_available": False, "default_button": "idle",
        "saves_outgoing": False,
    }
    if name == "idle":
        plan["running"]["codex_consumers"] = []
        plan["process_set"] = []
        plan.update(idle=True, remember_available=True, default_button="now")
    elif name == "blocked":
        plan["blockers"] = [{"code": "recovery", "message": "Finish the pending recovery first."}]
    elif name == "add":
        plan.update(purpose="add", target=None,
                    will=["Use an isolated sign-in folder. The current Codex sign-in stays in place."])
    elif name == "muse-sessions":
        target, main = accounts["acc_m2"], accounts["acc_m1"]
        plan.update(
            provider="muse", target=rules.plan_target(target), current=rules.plan_current(main),
            running={"codex_consumers": [{"pid": 9301, "kind": "tui", "started": now - 1800, "parent": "Terminal"}]},
            process_set=[[9301, 133812310000000000]],
            will=["Wait for Muse sessions to close.", "Save the outgoing sign-in.",
                  "Replace only Muse's auth.json and check the selected account."],
            preserved=["Muse settings, trusted folders and session history"])
    plan["signature"] = _signature(plan["process_set"])
    return plan


# ---- fixtures and CLI ----

def write_fixtures(directory=FIXTURE_DIR):
    """Writes state-<scenario>.json and plan-<name>.json into ``directory``; returns the paths."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for prefix, names, build in (("state", SCENARIOS, example_state), ("plan", PLANS, example_plan)):
        for name in names:
            path = target / f"{prefix}-{name}.json"
            path.write_text(json.dumps(build(name), indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
            written.append(path)
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m accountdeck.fixtures",
                                     description="Print an example /api/state payload or regenerate the contract fixtures.")
    parser.add_argument("scenario", nargs="?", default="typical", choices=list(SCENARIOS))
    parser.add_argument("--plan", choices=list(PLANS), help="print a SwitchPlan fixture instead")
    parser.add_argument("--now", type=float, default=BASE_NOW, help="Unix time of the payload (default BASE_NOW)")
    parser.add_argument("--write-fixtures", nargs="?", const=str(FIXTURE_DIR), metavar="DIR",
                        help=f"write every fixture (default {FIXTURE_DIR})")
    args = parser.parse_args(argv)
    if args.write_fixtures is not None:
        written = write_fixtures(args.write_fixtures)
        print(f"Wrote {len(written)} fixtures to {Path(args.write_fixtures)}")
        return 0
    payload = example_plan(args.plan, args.now) if args.plan else example_state(args.scenario, args.now)
    problems = validate_plan(payload) if args.plan else validate_state(payload)
    print(json.dumps(payload, indent=2))
    for problem in problems:
        print(f"contract violation: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
