"""Alert engine (PLAN §6.10): turns successive states into alerts, once per episode.

`AlertEngine.evaluate(prev_state, state, latches, now)` is pure apart from the start/resume hold it
keeps itself: the first observation after start only primes the latches (nothing is raised from
startup, errors or accounts whose reading is ambiguous), and nothing is raised for two minutes after
start or after a resume from sleep. Latches are plain JSON, persisted by the engine in the internal
`alert_latches` setting so a restart does not repeat an alert. Alert text names accounts and
windows only, never a token, an identity or a raw error.
"""
from __future__ import annotations

from . import rules
from .rules import STRIP_MIN_USED, clock_text, countdown_text

HOLD = 120.0                  # quiet period after start and after a resume
REARM = 5                     # the low latch re-arms this many points above the threshold
SOON_USED = 80.0              # reset_soon: windows at least this full
USE_IT_LEFT = 30.0            # use_it: weekly windows with at least this much left ...
USE_IT_WITHIN = 6 * 3600      # ... that reset within this time
KEEP = 8 * 86400              # latches of past resets and cycles are forgotten after this long
PROVIDER_NAMES = {"claude": "Claude", "codex": "Codex", "claude_cli": "Claude Code (terminal)", "muse": "Muse"}
LEVELS = {"low": "warning", "limit_reached": "critical", "reset_confirmed": "good", "reset_soon": "info",
          "suggest_switch": "info", "credential_expired": "warning", "switch_result": "good", "use_it": "info"}
# Every latch maps a key to the time it was set. low and limit are keyed "account|window" and last as
# long as the episode, cred by account; the others carry the reset or cycle they belong to.
GROUPS = ("low", "limit", "refill", "soon", "suggest", "use_it", "cred")
CYCLE_GROUPS = ("refill", "soon", "suggest", "use_it")


def _alert(kind, account, window, title, message, now, level=None):
    return {"kind": kind, "level": level or LEVELS[kind], "account": account, "window": window, "title": title,
            "message": message, "at": now}


def _accounts(state):
    """The saved accounts of a state by id: the unsaved signed-in row is not an account to alert about."""
    return {a["id"]: a for a in (state or {}).get("accounts") or () if a.get("saved", True)}


def _latches(stored):
    stored = stored if isinstance(stored, dict) else {}
    return {group: {str(key): at for key, at in stored[group].items()
                    if isinstance(at, (int, float)) and not isinstance(at, bool)}
            if isinstance(stored.get(group), dict) else {} for group in GROUPS}


class _Run:
    """One evaluation: the latches it updates, the alerts it raises and the thresholds it applies."""

    def __init__(self, latches, now, quiet, tz, low_left, reset_minutes):
        self.latches, self.now, self.quiet, self.tz = latches, now, quiet, tz
        self.low_left, self.reset_minutes = low_left, reset_minutes
        self.alerts = []

    def arm(self, group, key):
        """Latches `key` and says whether it was new. Latches are set while quiet too: that is what
        keeps a condition that already holds at start from being announced later."""
        if key in self.latches[group]:
            return False
        self.latches[group][key] = self.now
        return True

    def raise_alert(self, kind, account, window, title, message, level=None):
        if not self.quiet:
            self.alerts.append(_alert(kind, account["id"], None if window is None else window["id"], title, message,
                                      self.now, level))

    def account(self, account, old, best):
        self.credential(account)
        reading_ok = account["last_good_at"] is not None and account["error"] is None
        for window in account["windows"] + (account.get("other_windows") or []):
            self.refilled(account, window)
            if not (reading_ok and window["primary"]) or window["used"] is None:
                continue
            self.soon(account, window)
            self.use_it(account, window)
            if account["active"]:
                self.low(account, window, old)
                self.limit(account, window, best)
        if account["active"]:
            self.suggest(account, best)

    def credential(self, account):
        """credential_expired once per failure episode; only a fresh successful reading re-arms it, so a
        network error, a rate limit, a block or an outage neither raises nor clears it."""
        latch = self.latches["cred"]
        if account["credential"]["state"] == "reauth_required":
            if self.arm("cred", account["id"]):
                self.raise_alert("credential_expired", account, None, "Sign-in needs renewing",
                                 f"{account['label']}'s sign-in needs renewing.")
        elif account["id"] in latch and (account["last_good_at"] or 0) > latch[account["id"]]:
            del latch[account["id"]]

    def refilled(self, account, window):
        refill = window["refilled"]
        if window["state"] != "refilled" or not refill:
            return
        if self.arm("refill", f"{account['id']}|{window['id']}|{refill['at']}"):
            before = refill["from"]
            if before is None or before >= STRIP_MIN_USED:       # a window that was all but empty is no news
                self.raise_alert("reset_confirmed", account, window,
                                 f"{account['label']}'s {rules.phrase(window)} limit refilled",
                                 f"{account['label']} is ready again.")

    def soon(self, account, window):
        if window["reset_state"] != "scheduled" or window["used"] < SOON_USED:
            return
        secs = window["reset"] - self.now
        if secs <= self.reset_minutes * 60 and self.arm("soon", f"{account['id']}|{window['id']}|{window['reset']}"):
            self.raise_alert("reset_soon", account, window, f"{account['label']}'s {rules.phrase(window)} limit resets soon",
                             f"{account['label']}'s {rules.phrase(window)} limit resets in {countdown_text(secs)}.")

    def use_it(self, account, window):
        """A weekly quota about to expire unused on an account you are not on (a stale reading only
        bounds `left` from above, so it never counts)."""
        if (window["kind"] != "weekly" or account["active"] or not account["switchable"]
                or account["age_tier"] == "stale" or rules.account_out(account) or window["left"] < USE_IT_LEFT
                or window["reset_state"] != "scheduled"):
            return
        secs = window["reset"] - self.now
        if secs <= USE_IT_WITHIN and self.arm("use_it", f"{account['id']}|{window['id']}|{window['reset']}"):
            self.raise_alert("use_it", account, window, f"Use {account['label']}'s weekly quota",
                             f"{account['label']}'s weekly limit resets in {countdown_text(secs)} with "
                             f"{rules.pct_left(window['left'])}% unused.")

    def low(self, account, window, old):
        """The active account's window crossed the threshold downward; it re-arms 5 points above it."""
        key = f"{account['id']}|{window['id']}"
        left = window["left"]
        if left >= self.low_left + REARM:
            self.latches["low"].pop(key, None)
            return
        if left >= self.low_left or window["severity"] == "out":
            return
        if not self.arm("low", key):
            return
        previous = next((w for w in (old or {}).get("windows") or () if w["id"] == window["id"]), None)
        if previous is None or previous["left"] is None or previous["left"] < self.low_left:
            return      # no crossing was observed: a first reading, or the window was low already
        tail = "" if window["reset"] is None or window["reset"] <= self.now \
            else f", resets in {countdown_text(window['reset'] - self.now)}"
        self.raise_alert("low", account, window, f"{account['label']}'s {rules.phrase(window)} limit is running low",
                         f"{account['label']} is running low: {rules.pct_left(left)}% left{tail}.")

    def limit(self, account, window, best):
        """limit_reached once per episode: it ends when the window is no longer out."""
        key = f"{account['id']}|{window['id']}"
        if window["state"] != "out":
            self.latches["limit"].pop(key, None)
            return
        if not self.arm("limit", key):
            return
        until = "" if window["reset"] is None or window["reset"] <= self.now \
            else f" until {clock_text(window['reset'], self.now, self.tz)}"
        other = best.get("label") if best.get("state") == "ok" and best.get("account") != account["id"] else None
        message = f"{account['label']} is out of {rules.phrase(window)} quota{until}."
        if other:
            message += f" Best alternative: {other}."
        self.raise_alert("limit_reached", account, window, f"{account['label']} is out of {rules.phrase(window)} quota",
                         message)

    def suggest(self, account, best):
        """suggest_switch once per (active account, binding window cycle). Snoozing the alert only
        hides it (Store.update_alert); the cycle latch keeps it from being raised again."""
        if not best.get("suggest_switch") or not best.get("account"):
            return
        limiting = next((w for w in account["windows"] if w["binding"]), None)
        key = f"{account['id']}|{limiting['id'] if limiting else ''}|{limiting['reset'] if limiting else ''}"
        if self.arm("suggest", key):
            self.raise_alert("suggest_switch", account, limiting, f"{best['label']} has more room",
                             f"{best['label']} has {rules.pct_left(best['headroom'])}% left; "
                             f"{account['label']} is running low.")

    def forget(self, accounts):
        """Drops the latches of removed accounts and of cycles that ended long ago."""
        self.latches = {group: {key: at for key, at in latch.items() if key.split("|")[0] in accounts
                                and (group not in CYCLE_GROUPS or self.now - at <= KEEP)}
                        for group, latch in self.latches.items()}


class AlertEngine:
    def __init__(self, *, started):
        self.hold_until = started + HOLD
        self.primed = False

    def resumed(self, now):
        """A resume from sleep: hold again, and prime again on the next observation."""
        self.hold_until = max(self.hold_until, now + HOLD)
        self.primed = False

    def evaluate(self, prev_state, state, latches, now, *, low_left=15, reset_minutes=15, tz=None):
        """(new alerts, latches). Alerts are dicts for Store.add_alert; `latches` is the stored
        `alert_latches` value (not modified) and the returned one replaces it. `prev_state` is the
        state of the previous evaluation, which tells a downward crossing from a window that was
        low already."""
        quiet = not self.primed or now < self.hold_until
        self.primed = True
        run = _Run(_latches(latches), now, quiet, tz, low_left, reset_minutes)
        previous = _accounts(prev_state)
        best = ((state or {}).get("now_band") or {}).get("best") or {}
        current = _accounts(state)
        for account in current.values():
            run.account(account, previous.get(account["id"]), best.get(account["provider"]) or {})
        run.forget(current)
        return run.alerts, run.latches


def switch_result(label, provider, verified, now):
    """The switch_result alert for a finished switch (`verified` as in the SwitchReport)."""
    name = PROVIDER_NAMES.get(provider, "The app")
    if verified == "verified":
        return _alert("switch_result", None, None, f"Switched {name} to {label}.", f"{name} is signed in as {label}.", now)
    if verified == "unconfirmed" and provider == "muse":
        return _alert("switch_result", None, None, f"Switched {name} to {label}.",
                      "Muse could not report usage to confirm the sign-in. The next usage check tries again.", now, "info")
    if verified == "unconfirmed":
        return _alert("switch_result", None, None, f"Switched {name} to {label}.",
                      f"{name} opened — still confirming the sign-in in the background.", now, "info")
    return _alert("switch_result", None, None, f"Couldn't switch {name} to {label}.",
                  f"{name} opened a different account. Recovery is available." if provider == "claude"
                  else f"{name} did not sign in as {label}.", now, "critical")
