"""The private Claude reset protocol. Unknown or incomplete offers cannot be redeemed."""
import math
import re

from ..core import DeckError
from ..usage import timestamp

STATUS_URL = "https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1"
CLIENT = "claude-cli/2.1.322 (external, cli)"
IDS = re.compile(r"[a-z0-9_-]{1,40}\Z")
UUID = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}\Z")
ULTRA = {"default_claude_max_20x", "claude_max_20x", "default_claude_ultra", "claude_ultra", "ultra"}
LIMITS = {"five_hour", "seven_day", "seven_day_overage_included", "seven_day_opus", "seven_day_sonnet",
          "seven_day_cowork", "seven_day_omelette", "seven_day_oauth_apps", "seven_day_fable"}


def is_ultra(credentials):
    fields = credentials.get("claudeAiOauth", {}) if isinstance(credentials, dict) else {}
    return isinstance(fields, dict) and (fields.get("subscriptionType") == "ultra"
        or isinstance(fields.get("rateLimitTier"), str) and fields["rateLimitTier"] in ULTRA)


def route(url):
    if url in (STATUS_URL, "https://api.anthropic.com/api/oauth/profile"):
        return "GET"
    prefix, suffix = "https://api.anthropic.com/api/organizations/", "/reset_rate_limits"
    if url.startswith(prefix) and url.endswith(suffix) and UUID.fullmatch(url[len(prefix):-len(suffix)]):
        return "POST"
    raise ValueError("Not a Claude reset endpoint")


def integer(value):
    return value if type(value) is int and 0 <= value <= 100000 else None


def date(value):
    parsed = timestamp(value)
    return parsed if type(parsed) in (int, float) and math.isfinite(parsed) else None


def status(block, now):
    if (not isinstance(block, dict) or type(block.get("eligible")) is not bool
            or not isinstance(block.get("grants"), list) or len(block["grants"]) > 100):
        raise DeckError("Claude did not return readable reset offers. Try checking again later.", "network")
    props = block.get("event_props")
    tier = props.get("tier") if isinstance(props, dict) else None
    ultra = isinstance(tier, str) and tier in ULTRA
    reason = "" if block["eligible"] and ultra else "Claude has not made Ultra resets available with this login."
    cooldown = date(block.get("cooldown_until"))
    if block.get("cooldown_until") is not None and cooldown is None:
        reason = "Claude returned an unreadable reset wait. Check again later."
    if cooldown is not None and now < cooldown:
        reason = "Claude requires a wait before another reset."
    offers, seen = [], set()
    for raw in block["grants"]:
        if not isinstance(raw, dict):
            raise DeckError("Claude returned an incomplete reset list.", "network")
        key = raw.get("id")
        count = integer(raw.get("resets_left"))
        clears = raw.get("clears")
        if not isinstance(key, str) or not IDS.fullmatch(key) or key in seen or count is None:
            raise DeckError("Claude returned an incomplete reset list.", "network")
        seen.add(key)
        valid_limits = isinstance(clears, list) and bool(clears) and all(isinstance(x, str) and x in LIMITS for x in clears)
        fable = raw.get("gives_back_weekly") is True and valid_limits and "seven_day_overage_included" in clears
        full = raw.get("gives_back_weekly") is False and valid_limits and "seven_day" in clears
        kind = "fable" if fable and valid_limits else "full" if full else "unknown"
        title = {"fable": "Fable reset", "full": "Full reset", "unknown": "Other reset"}[kind]
        effect = ("Returns weekly usage spent on Fable. Usage spent on other models stays."
                  if kind == "fable" else "Restores the weekly allowances included in this offer."
                  if kind == "full" else "This reset type is not supported yet.")
        if valid_limits and "five_hour" in clears:
            effect += " Also restores the five-hour allowance."
        start, end = date(raw.get("starts_at")), date(raw.get("ends_at"))
        why = reason
        if kind == "unknown":
            why = "This reset type is not supported yet."
        elif not count:
            why = "No uses remaining."
        elif ((raw.get("starts_at") is not None and start is None)
              or (raw.get("ends_at") is not None and end is None)):
            why = "The offer's dates could not be read."
        elif end is not None and end <= now:
            why = "This offer expired."
        elif start is not None and start > now:
            why = "This offer has not started."
        elif raw.get("paused") is not False:
            why = "This offer is paused or unavailable."
        elif raw.get("usable_now") is not True or raw.get("blocking", []) != [] or raw.get("unavailable_reason"):
            why = "Claude is not allowing this reset now."
        elif raw.get("use_requires_limit") is not False and block.get("at_limit") is not True:
            why = "This offer requires a reached usage limit."
        elif key != block.get("next_grant_id"):
            why = "Claude requires another offer to be used first."
        offers.append({"grant": key, "title": title, "kind": kind, "count": count, "expires": end,
                       "effect": effect, "usable": not why, "reason": why,
                       "clears": sorted(set(clears)) if valid_limits else []})
    return {"offers": offers, "reason": reason, "ultra": ultra, "checked": now}
