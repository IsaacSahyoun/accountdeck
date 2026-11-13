"""Normalise provider usage payloads without guessing missing limits.

Unknown stays ``None`` (never 0), every numeric entry survives in ``raw_windows`` for the
"Raw usage" view, and credits are reported, never consumed.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import time

SESSION_MINUTES = 300
WEEKLY_MINUTES = 10080
PRIMARY_KINDS = frozenset({"session", "weekly", "model"})
KIND_ORDER = {"session": 0, "weekly": 1, "model": 2, "other": 3}
NON_WINDOW_KEYS = frozenset({"extra_usage", "spend", "limits"})
_LEGACY_MODEL = re.compile(r"seven_day_(opus|sonnet|haiku|fable)")
_OTHER_KEYS = {
    "seven_day_oauth_apps": ("Connected apps weekly", "Apps"),
    "seven_day_cowork": ("Cowork weekly", "Cowork"),
    "cowork": ("Cowork weekly", "Cowork"),
    "seven_day_routines": ("Routines weekly", "Routines"),
}


def number(value):
    return value if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) else None


def timestamp(value):
    """Unix seconds as a float from seconds, milliseconds or an ISO-8601 string (UTC unless it
    names an offset); ``None`` otherwise."""
    if number(value) is not None:
        return value / 1000 if value > 100000000000 else float(value)
    if isinstance(value, str):
        try:
            moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        # Provider times are UTC: a string without an offset is read as UTC, never as local time.
        return (moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)).timestamp()
    return None


def title(value):
    return str(value).replace("_", " ").replace("-", " ").strip().capitalize()


def scalars(value, depth=0):
    if depth > 4:
        return None
    if isinstance(value, dict):
        return {str(k): scalars(v, depth + 1) for k, v in value.items()
                if isinstance(v, (dict, list, int, float, str, bool)) or v is None}
    if isinstance(value, list):
        return [scalars(x, depth + 1) for x in value[:30]]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def classify_key(key, value):
    """``(kind, label, short, model, primary)`` for a top-level Claude usage key, or ``None``
    when the entry carries no numeric percentage (null codenames, credit blocks, lists)."""
    if key in NON_WINDOW_KEYS or not isinstance(value, dict) or _percent(value) is None:
        return None
    kind, label, short, model, _minutes = _describe(key)
    return kind, label, short, model, kind in PRIMARY_KINDS


CLAUDE_CLIENTS = frozenset({"claude", "claude_cli"})


def saved_reading(provider, reading):
    """A reading as the store saved it, made current. The 2.0.0 build saved a Claude account's 5-hour limit as
    a window, and until the next read that saved reading is all the app knows of the account. It is no limit
    any more (`claude_usage`), so it is left out here, where every saved reading is read."""
    if provider not in CLAUDE_CLIENTS or not isinstance(reading, dict) or not isinstance(reading.get("windows"), list):
        return reading
    kept = [w for w in reading["windows"] if not (isinstance(w, dict) and w.get("kind") == "session")]
    return reading if len(kept) == len(reading["windows"]) else dict(reading, windows=kept)


def claude_usage(data, *, now=None):
    """Claude's limits are weekly. It dropped its 5-hour limit, so a number an answer still carries for one
    stays in ``raw_windows`` and becomes no window: nothing shows it, counts it or switches on it. A reading
    saved before that goes through `saved_reading`."""
    windows, raw, structured = [], [], {}
    for key, value in data.items():
        described = classify_key(key, value)
        if described is None:
            continue
        kind, label, short, model, _primary = described
        used, reset = _percent(value), timestamp(value.get("resets_at"))
        raw.append({"key": key, "used": used, "reset": reset})
        if kind == "session":
            continue
        windows.append(_window(key, kind, label, short, model, _describe(key)[4], used, reset,
                               scope=model if kind == "model" else key if kind == "other" else None))
    for index, limit in enumerate(data.get("limits") or []):
        if not isinstance(limit, dict):
            continue
        used = _float(limit.get("percent", limit.get("utilization")))
        if used is None:
            continue
        reset = timestamp(limit.get("resets_at"))
        wid, raw_key, (kind, label, short, model, minutes) = _limit_shape(index, limit)
        raw.append({"key": raw_key, "used": used, "reset": reset})
        if kind == "session":
            continue
        if wid in structured:
            if (used, reset) in structured[wid]:
                continue
            structured[wid].append((used, reset))
            wid = _unique(wid, windows)
        else:
            # The structured entry replaces its legacy alias, even when the asynchronously
            # updated percentages differ slightly.
            windows = [w for w in windows if w["id"] != wid]
            structured[wid] = [(used, reset)]
        severity = limit.get("severity")
        active = limit.get("is_active")
        windows.append(_window(wid, kind, label, short, model, minutes, used, reset,
                               group=_text(limit.get("group")), scope=model,
                               severity_hint=severity if isinstance(severity, str) else None,
                               is_active=active if isinstance(active, bool) else None))
    return {"checked": time.time() if now is None else now, "windows": _shown(windows),
            "raw_windows": raw, "credits": _claude_credits(data), "reset_credits": [], "tokens": None}


def codex_usage(data, tokens=None, *, now=None):
    windows, raw, credits = [], [], []
    buckets = data.get("rateLimitsByLimitId") if isinstance(data.get("rateLimitsByLimitId"), dict) else {}
    default = data.get("rateLimits") if isinstance(data.get("rateLimits"), dict) else None
    if not buckets and default is not None:
        buckets = {default.get("limitId") or "codex": default}
    main = default.get("limitId") if default is not None else None
    if main not in buckets:
        main = "codex" if "codex" in buckets else None
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        group = _text(bucket.get("limitName")) or key
        name = _text(bucket.get("limitName")) or ("Codex" if key == "codex" else title(key))
        own = []
        for slot in ("primary", "secondary"):
            value = bucket.get(slot)
            if not isinstance(value, dict):
                continue
            used, reset = _float(value.get("usedPercent")), timestamp(value.get("resetsAt"))
            minutes = number(value.get("windowDurationMins"))
            minutes = minutes if minutes is not None and minutes > 0 else None
            wid = f"{key}:{slot}"      # v1 ids, so history and alert latches carry over
            raw.append({"key": wid, "used": used, "reset": reset})
            span, short = _span_label(minutes, slot), _span_short(minutes, slot)
            if key == main:
                # The default bucket always gates: primary → session, secondary → weekly,
                # unless the reported duration says otherwise.
                weekly = minutes == WEEKLY_MINUTES or (minutes != SESSION_MINUTES and slot == "secondary")
                kind, label, model = ("weekly" if weekly else "session"), span, None
            else:
                kind = "model" if used is not None else "other"
                label, short = f"{name} {span[0].lower()}{span[1:]}", f"{name} {short}"
                model = name if kind == "model" else None
            own.append(_window(wid, kind, label, short, model, minutes, used, reset,
                               group=group, scope=None if key == main else group))
        reached = _text(bucket.get("rateLimitReachedType"))
        # ordinaryUsageAllowed=false is authoritative for the default bucket even without a reached type.
        blocked = reached or ("not_allowed" if key == main and data.get("ordinaryUsageAllowed") is False else None)
        if blocked:
            full = [w for w in own if w["used"] is not None and w["used"] >= 100]
            for window in full or own:
                window["blocked"] = blocked
        windows.extend(own)
        credit = bucket.get("credits")
        if isinstance(credit, dict):
            credits.append({"label": f"{name} credits", "balance": credit.get("balance"),
                            "unlimited": credit.get("unlimited"), "hasCredits": credit.get("hasCredits")})
        individual = bucket.get("individualLimit")
        if isinstance(individual, dict):
            # Extra allowances are preserved as details, never flattened into a false percentage.
            credits.append({"label": f"{name} allowance", "details": scalars(individual)})
    resets = data.get("rateLimitResetCredits")
    if resets is not None:
        credits.append({"label": "Limit reset credits", "details": scalars(resets) if isinstance(resets, dict) else resets})
    allowed = data.get("ordinaryUsageAllowed")
    return {"checked": time.time() if now is None else now, "windows": _shown(windows), "raw_windows": raw,
            "credits": credits, "reset_credits": reset_credits(resets),
            "ordinaryUsageAllowed": allowed if isinstance(allowed, bool) else None,
            "tokens": scalars((tokens or {}).get("summary") or {}) or None}


def muse_usage(data, *, now=None):
    """`muse usage --json` (schemaVersion 1): `allowance.window` is Muse's rolling 5-hour-class block and
    `allowance.weekly` its weekly one, each {usedPercent, resetsAtMs, windowDurationMins?}; usedPercent may
    pass 100. The window's length comes from windowDurationMins (weekly: 7 days when absent). Muse dates its
    own answer in `asOfMs`: a cached answer is as old as Muse says, so the reading is dated then, not `now`."""
    allowance = data.get("allowance") if isinstance(data.get("allowance"), dict) else {}
    windows, raw = [], []
    for key, slot, kind in (("window", "primary", "session"), ("weekly", "secondary", "weekly")):
        value = allowance.get(key)
        if not isinstance(value, dict):
            continue
        used, reset = _float(value.get("usedPercent")), timestamp(value.get("resetsAtMs"))
        minutes = number(value.get("windowDurationMins"))
        # Muse states no length for its weekly allowance; "weekly" is the length.
        minutes = minutes if minutes is not None and minutes > 0 else WEEKLY_MINUTES if key == "weekly" else None
        wid = f"muse:{key}"
        raw.append({"key": wid, "used": used, "reset": reset})
        windows.append(_window(wid, kind, _span_label(minutes, slot), _span_short(minutes, slot), None, minutes,
                               used, reset))
    reason = _text(data.get("reason")) or _text(allowance.get("reason"))
    if reason:
        # Muse names why it is holding back (usage_limited, budget_limited): only a full window is blocked.
        for window in windows:
            if window["used"] is not None and window["used"] >= 100:
                window["blocked"] = reason
    now = time.time() if now is None else now
    as_of = timestamp(data.get("asOfMs"))
    return {"checked": as_of if as_of is not None and 0 < as_of < now else now, "windows": _shown(windows),
            "raw_windows": raw, "credits": [], "reset_credits": [], "tokens": None}


def muse_plan(data):
    """A short plan name from `muse usage --json`'s tier ("Muse Code Max Usage" → "Max"); "" when absent."""
    allowance = data.get("allowance") if isinstance(data, dict) and isinstance(data.get("allowance"), dict) else {}
    tier = _text(allowance.get("tier")) or ""
    short = re.sub(r"^muse code\s+", "", tier, flags=re.IGNORECASE)
    short = re.sub(r"\s+usage$", "", short, flags=re.IGNORECASE).strip()
    return (short or tier)[:40]


def reset_credits(resets):
    """Available Codex limit-reset credits grouped by expiry: ``[{"count","expires","title"}]``.
    Credits counted by ``availableCount`` but not listed become one row without an expiry."""
    if not isinstance(resets, dict):
        return []
    listed = [c for c in resets.get("credits") or []
              if isinstance(c, dict) and c.get("status") in (None, "available")]
    groups = {}
    for credit in listed:
        key = (timestamp(credit.get("expiresAt")), _text(credit.get("title")))
        groups[key] = groups.get(key, 0) + 1
    rows = [{"count": count, "expires": expires, "title": label} for (expires, label), count in groups.items()]
    rows.sort(key=lambda row: (row["expires"] is None, row["expires"] or 0))
    available = number(resets.get("availableCount"))
    if available is not None and int(available) > len(listed):
        rows.append({"count": int(available) - len(listed), "expires": None, "title": None})
    return rows


def _window(wid, kind, label, short, model, minutes, used, reset, *, group=None, scope=None,
            severity_hint=None, is_active=None):
    return {"id": wid, "kind": kind, "label": label, "short": short, "model": model,
            "used": used, "known": used is not None, "reset": reset, "minutes": minutes,
            # A window at 0 % without a reset time has not started (it starts with the next message).
            "started": not (used == 0 and reset is None), "blocked": None,
            "group": group, "scope": scope, "primary": kind in PRIMARY_KINDS,
            "severity_hint": severity_hint, "is_active": is_active}


def _shown(windows):
    """Primary windows always; others only when used > 0 or a reset exists (D2)."""
    kept = [w for w in windows if w["primary"] or (w["used"] or 0) > 0 or w["reset"] is not None]
    return sorted(kept, key=lambda w: (KIND_ORDER[w["kind"]], w["label"].casefold()))


def model_label(model):
    """How a per-model limit is named: the bare "Fable" limit is Fable 5.1's."""
    return "Fable 5.1" if model == "Fable" else model


def _describe(key):
    if key == "five_hour":
        return "session", "5-hour", "5h", None, SESSION_MINUTES
    if key == "seven_day":
        return "weekly", "Weekly", "7d", None, WEEKLY_MINUTES
    legacy = _LEGACY_MODEL.fullmatch(key)
    if legacy:
        model = legacy.group(1).capitalize()
        return "model", f"{model_label(model)} weekly", model_label(model), model, WEEKLY_MINUTES
    if key in _OTHER_KEYS:
        label, short = _OTHER_KEYS[key]
        return "other", label, short, None, WEEKLY_MINUTES
    return "other", title(key), "Other", None, None


def _limit_shape(index, limit):
    """(window id, raw key, (kind, label, short, model, minutes)) for one ``limits[]`` entry."""
    kind = _text(limit.get("kind")) or _text(limit.get("type"))
    scope = limit.get("scope") if isinstance(limit.get("scope"), dict) else {}
    model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
    model_name = _text(model.get("display_name")) or _text(model.get("id"))
    surface = _text(scope.get("surface"))
    base = f"limits[{index}].{kind or 'unknown'}"
    if kind == "session":
        return "five_hour", base, _describe("five_hour")
    if kind == "weekly_all":
        return "seven_day", base, _describe("seven_day")
    if kind == "weekly_scoped" and model_name:
        slug = _slug(model_name)
        label = model_label(model_name)
        return f"seven_day_{slug}", f"{base}.{slug}", ("model", f"{label} weekly", label, model_name, WEEKLY_MINUTES)
    if kind == "weekly_scoped" and surface:
        key = f"seven_day_{_slug(surface)}"
        shape = _describe(key) if key in _OTHER_KEYS else ("other", f"{title(surface)} weekly", title(surface), None, WEEKLY_MINUTES)
        return key, f"{base}.{_slug(surface)}", shape
    label = _text(limit.get("display_name")) or _text(limit.get("name")) or title(kind or f"limit {index + 1}")
    return _slug(_text(limit.get("id")) or kind or f"limit_{index + 1}"), base, ("other", label, "Other", None, None)


def _span_label(minutes, slot):
    if minutes is None:
        return "5-hour" if slot == "primary" else "Weekly"
    if minutes == SESSION_MINUTES:
        return "5-hour"
    if minutes == WEEKLY_MINUTES:
        return "Weekly"
    if minutes == 1440:
        return "Daily"
    if minutes % 1440 == 0:
        return f"{minutes // 1440:g}-day"
    if minutes % 60 == 0:
        return f"{minutes // 60:g}-hour"
    return f"{minutes:g}-minute"


def _span_short(minutes, slot):
    if minutes is None:
        return "5h" if slot == "primary" else "7d"
    if minutes == WEEKLY_MINUTES:
        return "7d"
    if minutes % 1440 == 0:
        return f"{minutes // 1440:g}d"
    if minutes % 60 == 0:
        return f"{minutes // 60:g}h"
    return f"{minutes:g}m"


def _claude_credits(data):
    credits = []
    spend, extra = data.get("spend"), data.get("extra_usage")
    if isinstance(spend, dict):
        amounts = [spend.get(k) if isinstance(spend.get(k), dict) else None for k in ("used", "limit", "balance")]
        money = next((a for a in amounts if a is not None), {})
        credits.append({"label": "Usage credits", "enabled": spend.get("enabled"),
                        "used": _minor(amounts[0]), "limit": _minor(amounts[1]), "balance": _minor(amounts[2]),
                        "unit": "minor currency units", "exponent": _exponent(money.get("exponent")),
                        "currency": _text(money.get("currency")) or "USD", "percent": number(spend.get("percent"))})
    elif isinstance(extra, dict):
        credits.append({"label": "Usage credits", "enabled": extra.get("is_enabled"),
                        "used": number(extra.get("used_credits")), "limit": number(extra.get("monthly_limit")),
                        "balance": number(extra.get("balance")), "unit": "minor currency units",
                        "exponent": _exponent(extra.get("decimal_places")),
                        "currency": _text(extra.get("currency")) or "USD", "percent": number(extra.get("utilization")),
                        "reset": timestamp(extra.get("resets_at"))})
    if isinstance(spend, dict):
        for key, label in (("cap", "Usage credit cap"), ("auto_reload", "Automatic credit reload")):
            if spend.get(key) is not None:
                credits.append({"label": label, "details": scalars(spend[key])})
    if isinstance(extra, dict):
        for key, label in (("daily", "Daily credit allowance"), ("weekly", "Weekly credit allowance")):
            if extra.get(key) is not None:
                credits.append({"label": label, "details": scalars(extra[key])})
    return credits


def _percent(value):
    return _float(value.get("utilization", value.get("percent")))


def _float(value):
    value = number(value)
    return None if value is None else float(value)


def _minor(amount):
    return number(amount.get("amount_minor")) if isinstance(amount, dict) else None


def _exponent(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 6 else 2


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _slug(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_") or "unknown"


def _unique(wid, windows):
    taken = {w["id"] for w in windows}
    suffix = 2
    while f"{wid}_{suffix}" in taken:
        suffix += 1
    return f"{wid}_{suffix}"
