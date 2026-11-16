"""Manual Ultra reset jobs. Pending attempts are durable and are never replayed."""
import copy
from collections import Counter
import hashlib
import math
import sqlite3
import threading
import uuid

from . import readings
from .core import DeckError, is_frozen
from .operations import Operation
from .providers.claude_cli import expires, oauth
from .providers.claude_resets import IDS, UUID, LIMITS, integer, is_ultra, status
from .providers.errors import ProviderError
from .usage import classify_key, model_label, number

CONFIRM_SECONDS = 120
UNSAVED_RESULT = "Resets for this account remain blocked by the unsaved local result."


def limit_key(window):
    if window.get("kind") == "model":
        return "seven_day_" + model_label(window.get("model") or "").casefold()
    if window.get("kind") == "weekly":
        return "seven_day"
    return named_key(window["scope"]) if window.get("scope") else historical_key(window["id"])


def named_key(key):
    """Provider names have explicit aliases; numeric suffixes belong to stored display ids."""
    return "seven_day_cowork" if key == "cowork" else key


def reset_windows(usage):
    """Include reported optional zeroes that the dashboard deliberately hides."""
    windows = list(usage.get("windows", [])) + usage.get("unstarted_windows", [])
    windows = [w for w in windows if w.get("kind") != "session" and limit_key(w) != "five_hour"]
    known = {limit_key(w) for w in windows}
    for raw in raw_limits(usage):
        key, used = raw["key"], raw["used"]
        if key not in LIMITS or key == "five_hour" or key in known or used is None:
            continue
        kind, _, _, model, _ = classify_key(key, {"utilization": used})
        windows.append({"id": raw["id"], "kind": kind, "model": model, "scope": key, "used": used})
        known.add(key)
    return windows


def required_limits(offer):
    # The internal counter need not be exposed; the retired session window stays hidden.
    required = set(offer["clears"]) - {"seven_day_overage_included", "five_hour"}
    if offer["kind"] == "fable":
        required.add("seven_day")
    return required


def historical_key(wid):
    """One reset identity for provider aliases and stored display suffixes."""
    if wid.startswith("seven_day_fable"):
        return "seven_day_fable"
    base, _, suffix = wid.rpartition("_")
    if suffix.isdigit() and (base in LIMITS or base == "cowork"):
        wid = base
    return named_key(wid)


def raw_limits(usage):
    """Keep distinct raw counters; a numeric list entry replaces only its own named source."""
    named, listed = [], []
    for raw in usage.get("raw_windows", []):
        path = raw.get("key", "")
        is_list = path.startswith("limits[")
        identity = raw.get("limit_id") if is_list else path
        kind, model = raw.get("kind"), raw.get("model")
        if is_list and not identity:
            shape = path.partition("].")[2]
            if shape == "weekly_all":
                identity, kind = "seven_day", "weekly"
            elif shape.startswith("weekly_scoped."):
                identity = "seven_day_" + shape[len("weekly_scoped."):]
            elif shape == "session":
                identity, kind = "five_hour", "session"
            else:
                continue
        if kind is None:
            described = classify_key(identity, {"utilization": 0})
            kind, model = (described[0], described[3]) if described else ("other", None)
            if is_list and identity.startswith("seven_day_fable"):
                kind, model = "model", "Fable"
        key = "seven_day_" + model_label(model).casefold() if kind == "model" else named_key(identity)
        entry = {"id": identity, "key": key, "used": number(raw.get("used")), "model": kind == "model"}
        (listed if is_list else named).append(entry)
    replacements = {entry["id"] for entry in listed if entry["used"] is not None}
    models = {entry["key"] for entry in listed if entry["model"] and entry["used"] is not None}
    return [entry for entry in named if entry["used"] is None or
            (entry["id"] not in replacements and not (entry["model"] and entry["key"] in models))] + listed


def ambiguous_limits(usage):
    """Unknown or duplicate affected counters cannot prove a reset."""
    limits = raw_limits(usage)
    unknown = {entry["key"] for entry in limits if entry["used"] is None}
    duplicates = {key for key, count in Counter(entry["key"] for entry in limits).items() if count > 1}
    return (unknown | duplicates) - {"five_hour"}


def budget_error(delay, held):
    pause = "Usage checks will pause to make room. " if held else ""
    return DeckError(f"Claude's hourly check budget is full. {pause}"
                     f"Check again in {max(1, math.ceil(delay / 60))} min.", "rate_limited", delay)


class ClaudeResets:
    def __init__(self, engine):
        self.engine = engine
        self.plans = {}
        self.check_after = {}
        self.unused = {}
        self.lock = threading.Lock()

    def available(self, row):
        if row["provider"] != "claude_cli" or is_frozen(row):
            return False
        try:
            saved = self.engine.vault.get(row["id"])
            return isinstance(saved, dict) and is_ultra(saved.get("credentials"))
        except (DeckError, OSError):
            return False

    def _account(self, key):
        e = self.engine
        e._own_the_login()
        row = e.store.account(key)
        if row is None or row["provider"] != "claude_cli":
            raise DeckError("Choose a saved Claude Ultra account.", "invalid")
        e.cli.require_local(key)
        if e.cli.enrollment or e.recovery:
            raise DeckError("Finish the current sign-in or recovery first.", "busy")
        saved = e.vault.get(key)
        if not isinstance(saved, dict):
            raise DeckError("The saved login could not be read.", "login_needed")
        credentials = saved.get("credentials")
        if saved.get("identity") != row["identity"] or saved.get("organizationUuid") != row["org"]:
            raise DeckError("The saved account has changed. Check it again.", "identity")
        if not isinstance(row["org"], str) or not UUID.fullmatch(row["org"]):
            raise DeckError("This account's organization could not be verified.", "identity")
        if not is_ultra(credentials):
            raise DeckError("Manual resets are available for Claude Ultra accounts only.", "unsupported")
        if saved.get("health") != "ok" or expires(credentials) <= e.clock() + 60:
            raise DeckError("This login needs to be renewed in Claude Code before using a reset.", "login_needed")
        if "user:profile" not in oauth(credentials).get("scopes", []):
            raise DeckError("This login cannot read reset offers. Sign in again in Claude Code.", "login_needed")
        return row, saved

    def _journal_key(self, row):
        identity = row["identity"] + "/" + row["org"]
        return "claude-reset-" + hashlib.sha256(identity.encode()).hexdigest()

    def _journal(self, row):
        key = self._journal_key(row)
        if not self.engine.vault.path(key).exists():
            return {"revision": 0, "attempt": None}
        value = self.engine.vault.get(key)
        if not isinstance(value, dict) or integer(value.get("revision")) is None or "attempt" not in value:
            raise DeckError("The previous reset record could not be read. No reset was sent.", "recovery")
        attempt = value["attempt"]
        if attempt is not None and (not isinstance(attempt, dict)
                or not isinstance(attempt.get("grant"), str) or not IDS.fullmatch(attempt["grant"])
                or not isinstance(attempt.get("request"), str) or not UUID.fullmatch(attempt["request"])
                or integer(attempt.get("count")) in (None, 0)
                or attempt.get("state") not in ("pending", "accepted", "settled")
                or attempt.get("kind") not in ("fable", "full")
                or not isinstance(attempt.get("clears"), list)
                or not all(isinstance(k, str) and k in LIMITS for k in attempt["clears"])
                or not isinstance(attempt.get("before"), list) or not attempt["before"]
                or not all(isinstance(w, dict) and isinstance(w.get("id"), str)
                           and isinstance(w.get("key"), str) and number(w.get("used")) is not None
                           for w in attempt["before"])):
            raise DeckError("The previous reset record is incomplete. No reset was sent.", "recovery")
        return value

    def _save(self, row, journal):
        self.engine.vault.put(self._journal_key(row), journal, generation=False)

    def _finish_unused(self, row, journal):
        """Retry only a known local closure; this evidence deliberately does not survive a restart."""
        key = self._journal_key(row)
        with self.lock:
            request = self.unused.get(key)
        if request is None:
            return False
        attempt = journal["attempt"]
        if attempt and attempt["request"] == request and attempt["state"] != "settled":
            closed = copy.deepcopy(journal)
            closed["attempt"]["state"] = "settled"
            try:
                self._save(row, closed)
            except (OSError, DeckError):
                return True
            journal.update(closed)
        with self.lock:
            self.unused.pop(key, None)
        return False

    def _pace(self, row, count, *, leave=0, retry=0):
        e = self.engine
        now = e.clock()
        key = ("claude_cli", row["identity"])
        delay = (row["paused_until"] or 0) - now
        if e.prefs.get("refresh_paused"):
            raise DeckError("Usage checks are paused. Resume them before checking resets.", "invalid")
        if delay > 0:
            raise DeckError("Claude's usage checks are waiting. Check resets after the wait ends.", "rate_limited", delay)
        delay = e.scheduler.governor.reserve(key, now, count, leave=leave, hold=CONFIRM_SECONDS)
        if delay and retry:
            delay = e.scheduler.governor.reserve(key, now, count, leave=leave + retry, hold=CONFIRM_SECONDS)
        if retry:
            with self.lock:
                self.check_after = {k: v for k, v in self.check_after.items() if v > now}
                if delay:
                    self.check_after[row["identity"]] = now + delay
                else:
                    self.check_after.pop(row["identity"], None)
        if delay:
            raise budget_error(delay, bool(leave))

    def _call(self, row, function, *args):
        """Keep the provider's result even if saving its throttle fails."""
        e = self.engine
        try:
            return function(*args)
        except DeckError as exc:
            if exc.code == "rate_limited":
                try:
                    e.store.update(row["id"], paused_until=e.clock() + (exc.retry_after or 900))
                except (DeckError, OSError, sqlite3.Error):
                    pass
            raise

    def _read(self, row, saved):
        adapter, credentials = self.engine.cli.adapter, saved["credentials"]
        who = self._call(row, adapter.reset_profile, credentials)
        if who.get("accountUuid") != row["identity"] or who.get("organizationUuid") != row["org"]:
            raise DeckError("Claude reported a different account or organization. No reset was sent.", "identity")
        return status(self._call(row, adapter.reset_status, credentials), self.engine.clock())

    def _baseline(self, row, saved, offer):
        usage = self._call(row, self.engine.cli.adapter.usage, saved["credentials"])
        return self._baseline_values(row, usage, offer)

    def _baseline_values(self, row, usage, offer):
        before = [{"id": w["id"], "key": limit_key(w), "used": w.get("used")}
                  for w in reset_windows(usage) if limit_key(w) in offer["clears"]
                  or offer["kind"] == "fable" and w.get("kind") == "weekly"]
        required = self._required(row, offer)
        prior = [limit_key(w) for w in reset_windows(row.get("last_good_usage") or {})
                 if limit_key(w) in offer["clears"] or offer["kind"] == "fable" and w.get("kind") == "weekly"]
        required.update(prior)
        keys = [w["key"] for w in before]
        if (not before or not required.issubset(keys) or len(keys) != len(set(keys))
                or len(prior) != len(set(prior)) or any(number(w["used"]) is None for w in before)
                or (required | set(offer["clears"])) & (ambiguous_limits(usage)
                    | ambiguous_limits(row.get("last_good_usage") or {}))):
            raise DeckError("Claude has not provided complete, unambiguous usage for this reset. No reset was sent.", "network")
        return before

    def _required(self, row, offer):
        required = required_limits(offer)
        required.update(key for window in self.engine.store.reading_windows(row["id"])
                        if (key := limit_key(window)) != "five_hour" and key in offer["clears"])
        return required

    def _refresh_usage(self, row, saved, attempt):
        e = self.engine
        usage = self._call(row, e.cli.adapter.usage, saved["credentials"])
        windows, known = set(), reset_windows(usage)
        keys = [w["key"] for w in attempt["before"]]
        verified = e.store.reset_verified(row["id"], attempt["request"])
        # `before` already captured the affected history at admission; later readings cannot add to it.
        required = required_limits(attempt)
        if not verified and (len(keys) != len(set(keys)) or not required.issubset(keys)
                             or (set(keys) | required | set(attempt["clears"])) & ambiguous_limits(usage)):
            return False
        empty_can_grow = attempt["state"] == "accepted" and any(w["used"] > 0 for w in attempt["before"])
        for before in attempt["before"]:
            matches = [w for w in known if limit_key(w) == before["key"]]
            used = number(matches[0].get("used")) if len(matches) == 1 else None
            if not verified and (used is None or not (used < before["used"] or used == before["used"] == 0
                                                      or before["used"] == 0 and empty_can_grow)):
                return False
            windows.add(before["id"])
            windows.update(w["id"] for w in matches)
        windows.update(window["id"] for window in e.store.reading_windows(row["id"]) if limit_key(window) in keys)
        readings.record(e.store, row["id"], usage, reset=(attempt["request"], windows, row))
        e.scheduler.request(row["id"], e.clock())
        return True

    def _settle(self, row, saved, journal, data):
        attempt = journal["attempt"]
        if not attempt or attempt["state"] == "settled":
            return False
        match = next((g for g in data["offers"] if g["grant"] == attempt["grant"]), None)
        if (not self.engine.store.reset_verified(row["id"], attempt["request"])
                and attempt["state"] != "accepted" and (match is None or match["count"] >= attempt["count"])):
            return False
        if not self._refresh_usage(row, saved, attempt):
            return False
        journal["attempt"]["state"] = "settled"
        journal["revision"] += 1
        self._save(row, journal)
        return True

    def check(self, key):
        e = self.engine
        if e.runner.busy:
            raise DeckError("Finish the current action before checking resets.", "busy")
        with e.scheduler.account_lock(key), e.cli.gate(key):
            row, saved = self._account(key)
            journal = self._journal(row)
            if self._finish_unused(row, journal):
                return {"account": {"id": key, "label": row["label"], "email": row["email"]}, "offers": [],
                        "checked": None, "plan_id": None, "unconfirmed": True,
                        "message": UNSAVED_RESULT + " Check again to retry saving."}
            pending = journal["attempt"] is not None and journal["attempt"]["state"] != "settled"
            if not pending:
                with self.lock:
                    delay = self.check_after.get(row["identity"], 0) - e.clock()
                if delay > 0:
                    raise budget_error(delay, True)
            self._pace(row, 3 if pending else 2)
            data = self._read(row, saved)
            if self._settle(row, saved, journal, data):
                row = e.store.account(key)
            pending = journal["attempt"] is not None and journal["attempt"]["state"] != "settled"
            offers = [{k: g[k] for k in ("title", "count", "expires", "effect", "usable", "reason")}
                      for g in data["offers"]]
            if pending:
                for offer in offers:
                    offer.update(usable=False, reason="")
            else:
                for grant, offer in zip(data["offers"], offers):
                    if offer["usable"]:
                        try:
                            self._baseline_values(row, row.get("last_good_usage") or {}, grant)
                        except DeckError:
                            e.scheduler.request(row["id"], e.clock())
                            offer.update(usable=False, reason="Claude's usage is missing or unclear for a limit this reset affects.")
            chosen = next((g for g, offer in zip(data["offers"], offers) if offer["usable"]), None)
            plan_id = None
            if chosen and not pending:
                self._pace(row, 0, leave=7, retry=2)
                plan_id = "reset_" + uuid.uuid4().hex
                now = e.clock()
                with self.lock:
                    self.plans = {k: v for k, v in self.plans.items() if v["expires"] > now}
                    if len(self.plans) >= 100:
                        self.plans.pop(next(iter(self.plans)))
                    self.plans[plan_id] = {"key": key, "identity": row["identity"], "org": row["org"],
                        "credential": hashlib.sha256(oauth(saved["credentials"])["accessToken"].encode()).hexdigest(),
                        "offer": chosen, "revision": journal["revision"], "expires": now + CONFIRM_SECONDS}
            e.bump()
            return {"account": {"id": key, "label": row["label"], "email": row["email"]}, "offers": offers,
                    "checked": data["checked"], "plan_id": plan_id, "unconfirmed": pending,
                    "message": "The previous reset's result is still unconfirmed. Check again later." if pending else data["reason"]}

    def submit(self, key, plan_id):
        with self.lock:
            plan = self.plans.get(plan_id) if isinstance(plan_id, str) else None
            if plan is None or plan["key"] != key:
                raise DeckError("Check the reset offers again before confirming.", "stale_plan")
            del self.plans[plan_id]
        op = None
        try:
            row, _ = self._account(key)
            op = ResetOperation(self, plan, row["label"])
            return self.engine.submit(op)
        except DeckError as exc:
            if exc.code == "busy" and (op is None or op.id is None) and self.engine.clock() < plan["expires"]:
                with self.lock:
                    self.plans[plan_id] = plan
            raise


class ResetOperation(Operation):
    not_cancellable_message = "The reset request may already have reached Claude. Its result must be checked."

    def __init__(self, service, plan, label):
        super().__init__("claude-reset", provider="claude_cli", account=plan["key"], label=label,
                         steps=(("check", "Checking the account and reset"), ("send", "Using the reset"),
                                ("verify", "Checking the result")))
        self.service, self.plan = service, copy.deepcopy(plan)
        self.message = "Using a Claude reset"

    def run(self):
        s, p = self.service, self.plan
        e = s.engine
        with e.scheduler.account_lock(self.account), e.cli.gate(self.account):
            with self.step("check", phase="planning"):
                row, saved = s._account(self.account)
                fingerprint = hashlib.sha256(oauth(saved["credentials"])["accessToken"].encode()).hexdigest()
                journal = s._journal(row)
                pending = journal["attempt"]
                if (e.clock() >= p["expires"] or fingerprint != p["credential"] or row["identity"] != p["identity"]
                        or row["org"] != p["org"] or journal["revision"] != p["revision"]
                        or pending is not None and pending["state"] != "settled"):
                    raise DeckError("The reset confirmation changed or expired. Check the offers again.", "stale_plan")
                s._pace(row, 7)
                data = s._read(row, saved)
                chosen = next((g for g in data["offers"] if g["grant"] == p["offer"]["grant"]), None)
                if chosen != p["offer"] or not chosen["usable"] or e.clock() >= p["expires"]:
                    raise DeckError("This reset offer changed. Check the offers again.", "stale_plan")
                before = s._baseline(row, saved, chosen)
            with self.step("send", phase="saving", cancellable=False):
                journal = {"revision": journal["revision"] + 1, "attempt": {"grant": chosen["grant"],
                    "request": str(uuid.uuid4()), "count": chosen["count"], "kind": chosen["kind"],
                    "clears": chosen["clears"], "before": before, "state": "pending"}}
                s._save(row, journal)
                if e.clock() >= p["expires"] or chosen["expires"] is not None and e.clock() >= chosen["expires"]:
                    if not self._record_not_used(row, journal):
                        return
                    raise DeckError("The reset confirmation expired. Check the offers again.", "stale_plan")
                try:
                    answer = s._call(row, e.cli.adapter.redeem_reset, saved["credentials"], row["org"],
                                     chosen["grant"], journal["attempt"]["request"])
                except ProviderError as exc:
                    if exc.http in (400, 401, 403, 404, 410, 422, 429):
                        if not self._record_not_used(row, journal):
                            return
                        self.report.update(verified="failed", summary="Claude refused the reset request. Check the offers again later.")
                    else:
                        self._uncertain()
                    return
                except Exception:
                    self._uncertain()
                    return
                result = answer.get("result") if isinstance(answer, dict) else None
                if result == "reset":
                    journal["attempt"]["state"] = "accepted"
                    try:
                        s._save(row, journal)
                    except Exception:
                        self._uncertain(accepted=True)
                        return
                elif result in ("not_limited", "cooldown", "ineligible", "unavailable"):
                    if not self._record_not_used(row, journal):
                        return
                    self.report.update(verified="failed", summary="Claude did not use the reset. Check the offers again.")
                    return
                elif result != "already_used":
                    self._uncertain()
                    return
            with self.step("verify", phase="verifying", cancellable=False):
                try:
                    data = s._read(row, saved)
                    settled = s._settle(row, saved, journal, data)
                except Exception:
                    settled = False
                if not settled:
                    self._uncertain(accepted=result == "reset")
                    return
                self.report.update(verified="verified", verified_at=e.clock(),
                    summary=f"{chosen['title']} used. {row['label']}'s usage was refreshed." if result == "reset"
                    else "That reset was already used. Usage was refreshed.")

    def _record_not_used(self, row, journal):
        journal["attempt"]["state"] = "settled"
        for _ in range(2):
            try:
                self.service._save(row, journal)
                return True
            except (OSError, DeckError):
                pass
        with self.service.lock:
            self.service.unused[self.service._journal_key(row)] = journal["attempt"]["request"]
        self.report.update(verified="failed", summary="No reset was used, but AccountDeck could not save the result.",
                           verify_reason=UNSAVED_RESULT + " Open Resets for this account to retry saving.")
        return False

    def _uncertain(self, *, accepted=False):
        self.report.update(verified="unconfirmed", summary="Claude accepted the reset. Usage is not confirmed yet."
                           if accepted else "The reset's result is unconfirmed.",
                           verify_reason="Open Resets and check again. AccountDeck will not send this reset again.")
