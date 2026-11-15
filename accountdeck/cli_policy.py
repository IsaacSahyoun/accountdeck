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
- a paused account with room is waited for only while the account in use still works and is below the
  ceiling, or has no account that is ready to go to. At the ceiling an account that is ready is used up.
  The ceiling is reached by a reading, not by the forecast: an account left on its forecast can still be
  used up by its reading, and the terminals would come straight back to it. An account in use whose own
  reads Claude has paused, with a reading over 3 minutes old, counts as at the ceiling, with or without
  a forecast: no reading can say 99% before the pause ends, and it would be used until it ran out with
  nothing to see it. Its `old` row passes it over as a target, so the terminals do not come straight
  back. Once its pause is over the scheduler reads it, and with that reading the rule can name it again.
  A reading that is old with no pause does not count: after a start or a wake every reading is old for
  some seconds. Nor does the pause of an account an automatic switch moved the terminals to less than 15
  minutes ago (`STAY`): Claude refusing the first read of each account they came to moved them again
  within minutes, and a switch on a last reading goes to a paused account by design;
  And when the account in use works for no model and every account left is paused, the first of them is
  switched to on the reading it has, however old (`last_reading`, which tells the executor to take no new
  one): staying is known to stop the work, and the switch renews and confirms the login it moves to;
- an account to move to with a limit (its week, or a model's) that was used up at its last reading, and
  has ended since: that limit has refilled. By that number the account was no target until its next reading
  (one with only a model's limit used up was none while the account in use still works), and a pause puts
  that off by up to an hour. It counts with that limit full and as an account with an old reading: it is
  read again before it is used, and used on what it has only where an old reading is (the bullet above). A
  limit counts as refilled 45 seconds after its reset time, which is when the scheduler takes its own
  reading (`scheduler.BOUNDARY_DELAY`);
- an account whose last read failed is passed over the same way (`RefreshScheduler.failing`: a read of it
  failed, and none has worked since). With a reading over 3 minutes old no switch to it can be made. The
  rule went on naming such an account, the executor went on asking for the reading, and the terminals
  stayed on an account that had run out until that reading was 15 minutes old. It is read again when it is
  needed, as an account with an old reading is, and it is not waited for at the ceiling. It is not used on
  the reading it has, as a paused account is when nothing else is left: its reads fail for a cause nobody
  knows, and a switch to it may fail the same way;
- an automatic switch that did not get the terminals to its account is not started again at the next check.
  It is started once more 90 seconds after it was started (`AGAIN`), and the terminals stay where they are
  until then: most failures are over by then (Claude Code was renewing its own login, a lookup failed). The
  wait is the account's: a new reading that puts another account first moves the terminals there. After
  a second try in a row the account is no target for 15 minutes (`RETRY`), and the next account is used
  meanwhile. An account with room is waited for while the account in use still works and is below the
  ceiling, like one with an old reading whose reads are paused;
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
import math

from .core import is_frozen
from .rules import clock_text
from .scheduler import BOUNDARY_DELAY
from .usage import is_fable

HOUR = 3600
READY = 180     # a switch is made on a reading at most this old: `cli_auto.tick` reads an older one again first
AGAIN = 90      # an automatic switch that did not happen is started once more this long after it was started
RETRY = 900     # and after a second try in a row, not before this long after the last one
STAY = 900      # an account an automatic switch moved the terminals to is not left for its own pause before this


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def tried(note):
    """(when, which try in a row) from a note `cli_auto.note_try` wrote, or None for anything else."""
    if not isinstance(note, dict):
        return None
    at, count = number(note.get("at")), note.get("count")
    return (at, count) if at is not None and type(count) is int and count > 0 else None


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


def refilled(window, read_at, now):
    """True when a window's reset time passed after `read_at`, the time of the reading that gave its number. The
    limit has refilled since, and that number is of the window before. The menu bar (`pool.left_at`) and the
    page (`deriveWindow`, "resetting") show such a window as full. A number read after the reset time stands
    as it was read."""
    at = reset(window)
    return at is not None and read_at is not None and read_at < at <= now


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
    order with why it is skipped, "exhausted": every account is low, "last_reading": the switch is made on
    the reading the target has, however old, because no new one can be taken (`_leave`)}."""
    threshold = settings.get("cli_threshold", 95)
    notes = history.get("tried") if isinstance(history.get("tried"), dict) else {}
    rows = _ordered([_row(account, settings, threshold, now, tried(notes.get(account["id"]))) for account in accounts], settings)
    result = {"action": "none", "target": None, "kind": None, "reason": "", "hold": None, "refresh": [],
              "next_up": [{key: row[key] for key in ("id", "label", "skip", "room", "weekly_reset")} for row in rows],
              "exhausted": False, "last_reading": False}
    active = next((row for row in rows if row["account"].get("active")), None)
    if not settings.get("cli_autoswitch", True) or active is None or is_frozen(active["account"]):
        return result
    if active["account"].get("live_expired"):
        result["hold"] = "Usage updates after your next message in Claude Code"
        return result
    need = _need(active, threshold, history, now)
    if need["trigger"]:
        result.update(_leave(need, rows, active, history, settings, now))
    elif need["refilled"]:
        result.update(hold=f"{active['label']}'s {need['refilled'][0]} limit has refilled. Waiting for its new reading.",
                      refresh=[active["id"]])
    elif settings.get("cli_early_switch", True):
        result.update(_early(rows, active, history, now))
    # A switch to this account was started less than `AGAIN` ago and the terminals are not there. Most failures
    # are not the account's and are over in a minute: Claude Code held its login lock, or a lookup of whose
    # login is live failed (every automatic switch asks again itself, `CLISwitch.run`). So the same switch
    # is tried once more before the account is passed over, and the terminals stay until then. Moving on at
    # once sent them to the next account for one failure that had nothing to do with the first. The wait
    # belongs to the account: when a new reading puts another account first, that one is moved to.
    chosen = next((row for row in rows if row["id"] == result["target"]), None)
    if chosen is not None and chosen["again"]:
        result.update(action="none", target=None, kind=None, reason="", last_reading=False,
                      hold=f"A switch to {chosen['label']} was tried a moment ago and is tried once more shortly")
    return result


def _renewed(account, values, room, read_at, now):
    """(the first limit of an account to move to that was used up at its last reading and has ended since, the
    account's limits with every limit that refilled since as full). (None, `values`) for any other account.

    Such a limit has refilled. By that reading's numbers the account had no room and that limit was one of
    those that are spent (`_row`). With its week spent it was no target of any kind, also when the account in
    use had run out and there was nothing else to move to. With only a model's limit spent it was none while
    the account in use still works, and the rule said every account was low. It stayed that until its next
    reading: 45 seconds after the reset as a rule, and the whole pause when Claude had paused its reads. It
    now counts as an account with an old reading: it is read again before it is used, and it is used on what
    it has only where an old reading is (`_leave`).
    A refill only adds room. So an account with room by its last numbers, and one that can be used up by
    them, stay the targets they were and are not judged this way. `room` is None while one of the account's
    limits has no number.

    A limit counts as refilled `BOUNDARY_DELAY` after its reset time, not at it. The scheduler takes its own
    reading then, because Claude may still answer with the old numbers right at a reset. A reading the rule
    asked for earlier put that one off by 3 minutes (the governor spaces the reads of one account), and a
    100% read after the reset time stands as read."""
    if account.get("active") or room is None or room >= 10:
        return None, values
    again = [refilled(w, read_at, now - BOUNDARY_DELAY) for _, _, w in values]
    spent = [label for (label, used, w), full in zip(values, again)
             if full and (used > DRAIN_CEILING - DRAIN_ROOM or w.get("blocked"))]
    if not spent:
        return None, values
    return spent[0], [(label, 0.0, dict(w, used=0.0, blocked=None, reset=None)) if full else (label, used, w)
                      for (label, used, w), full in zip(values, again)]


def _row(account, settings, threshold, now, note=None):
    """One account as the rule sees it: its room below the threshold and, in `skip`, why it cannot be switched to.
    `note` is (when, which try in a row) of an automatic switch to it that did not get the terminals there."""
    values = considered(account, settings)
    known = all(used is not None for _, used, _ in values)
    room = min(threshold - used for _, used, _ in values) if known else None
    read_at = number(account.get("last_good_at"))
    # The limit that was used up at its last reading and has refilled since: the account needs a new reading.
    renewed, values = _renewed(account, values, room, read_at, now)
    if renewed is not None:
        room = min(threshold - used for _, used, _ in values)
    age = max(0, now - read_at) if read_at is not None else None
    # A reading too old to act on is read again first. That is one over 15 minutes old. It is also one the
    # executor takes again before it switches (older than `READY`) while the account's reads are paused. Claude
    # refusing a read (HTTP 429) pauses them for 15 to 60 minutes. Until then the scheduler starts no read of the
    # account and the Refresh button is refused, and a switch that waited kept the terminals on an account
    # that had run out.
    pause = number(account.get("paused_until"))
    paused = pause is not None and pause > now
    # The same for an account whose last read failed (a read of it failed, and none has worked since). After
    # Claude refuses to renew a login, the app does not ask again for 30 minutes, and every read of the
    # account fails until then. The rule named it all the same, the executor asked for the reading a switch
    # needs, and nothing moved until the reading was 15 minutes old. `silent` says that no reading of the
    # account is on its way, for either cause.
    silent = paused or bool(account.get("failing"))
    unread = age is not None and READY < age <= 900 and silent
    old = age is not None and (age > 900 or unread or renewed is not None)
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
        skip, stale = ("refilled since its last reading" if renewed is not None else
                       "Claude paused its checks" if unread and paused else "its last check failed" if unread
                       else f"reading {math.ceil(age / 60)} min old"), True
    # Skipped only for being past the threshold (not health, freshness or choice): usable when all are.
    usable = skip is None or past
    spent = [(label, used, w) for label, used, w in values
             if usable and (used > DRAIN_CEILING - DRAIN_ROOM or w.get("blocked"))]
    # Only a model's limit is spent and the week is not: the account still works for the other models, so it
    # is used when the account in use works for none. `week_only` names that limit.
    week_only = (f"{spent[0][0]} limit is at {spent[0][1]:g}%"
                 if spent and all(w.get("kind") == "model" for _, _, w in spent) else None)
    drainable = usable and not spent
    # `CLISwitch` notes the start of every automatic switch and takes the note out once the switch worked. So a
    # note says the terminals never got to this account, whatever stopped the switch. Without it the next
    # check, 5 seconds later, started the same switch again. After a first try the account stays a target and
    # the rule waits (`evaluate`). After a second try in a row it is no target of any kind for `RETRY`: not
    # with room, not to be used up, not for its week. An account that is no target anyway keeps its own words.
    # A note counts while now is within the limit of its date, on either side: one dated after now (the Mac's
    # clock was set back) then counts for no more than twice the limit. `wait` is the time left until it ends.
    limit = 0 if note is None else AGAIN if note[1] == 1 else RETRY
    wait = note[0] + limit - now if note is not None and abs(now - note[0]) < limit else 0
    # `failed` names what the account is a target as without its note: with room, to be used up ("drain"), or
    # for its week alone, which only an account in use that works for no model is left for (`_leave`).
    failed = None
    if wait > 0 and note[1] > 1:
        failed = "room" if skip is None else "drain" if drainable else "week" if week_only is not None else None
    if failed:
        skip, drainable, week_only = f"switch tried {max(1, math.ceil((now - note[0]) / 60))} min ago", False, None
    return {"id": account["id"], "label": account["label"], "skip": skip, "room": room,
            "weekly_reset": reset(values[0][2]), "account": account, "values": values,
            "drainable": drainable, "week_only": week_only, "stale": stale, "old": old, "paused": paused,
            "silent": silent, "age": age,
            "renewed": renewed, "again": wait > 0 and note[1] == 1, "twice": wait > 0 and note[1] > 1,
            "failed": failed, "wait": wait}


def _grouped(rows, value, width, then, rest):
    """`rows` in groups by `value`, from the smallest value on: a group is every row whose value is at most
    `width` above the smallest of the group. Each group is ordered by `then`. The rows without a value come
    last, ordered by `rest`."""
    known = sorted((row for row in rows if value(row) is not None), key=value)
    ordered, start = [], 0
    for end in range(1, len(known) + 1):
        if end == len(known) or value(known[end]) - value(known[start]) > width:
            ordered += sorted(known[start:end], key=then)
            start = end
    return ordered + sorted((row for row in rows if value(row) is None), key=rest)


def _ordered(rows, settings):
    """The rows in switching order: the week that ends soonest first, the most room first, or the user's own
    order with the rest after it.

    Weeks that end within the hour of each other end together, and of those the account with the most room
    goes first. Under "most room first", room within a point is the same room, and of those the week that ends
    sooner goes first. Compared two by two, that is no order: with three weeks that each end within the hour
    of the next, the first and the last are further apart, and the first account depended on the order the
    store listed them in. So the rows are grouped from the first one (`_grouped`): a week is in a group while
    it ends within the hour of the soonest week of that group. Without such a chain the order is what it
    was, except for two accounts of one name that are equal in both values: those go by their id, where the
    listing decided. A reset time that reads a second later at the edge of a group moves that edge, and with
    a chain behind it the edges of the groups after it too."""
    def name(row):
        return (row["label"].casefold(), row["id"])

    def week(row):
        return row["weekly_reset"] or None

    def room(row):          # the negative, so that the most room is the smallest value
        return None if row["room"] is None else -row["room"]

    def last(value):
        return lambda row: (math.inf if value(row) is None else value(row), name(row))

    mode = settings.get("cli_order", "soonest_reset")
    if mode == "most_room":
        rows = _grouped(rows, room, 1, last(week), name)
    else:
        rows = _grouped(rows, week, HOUR, last(room), name)
    if mode == "manual":
        place = {key: index for index, key in enumerate(settings.get("cli_manual_order", []))}
        rows.sort(key=lambda row: place.get(row["id"], math.inf))
    return rows


def _came(history, active, now):
    """True for `STAY` after an automatic switch brought the terminals to the account in use. Such a switch
    notes that at its end (`last`, which only those write). When the app died after the login was switched
    and before that note, the next check finds the terminals on the account with the note of the try still
    there, and keeps its time (`came`, `cli_auto.tick`). Either one counts. Dated as a note of a try is
    (`_row`): a switch dated after now, when the Mac's clock was set back, counts for no more than twice
    that long."""
    for note in (history.get("last"), history.get("came")):
        at = number(note.get("at")) if isinstance(note, dict) else None         # an old `last` can have no time
        if at is not None and note.get("to") == active["id"] and abs(now - at) < STAY:
            return True
    return False


def _need(active, threshold, history, now):
    """Why the account in use should be left, judged on its windows still running: `trigger` (login, out,
    threshold or None) and `why` in words. A window whose reset time has passed is left out; `refilled` names
    those that ended at or past the threshold, whose new reading is waited for. `yields` says the user's own
    choice outranks this need: the threshold, and a model's limit that ran out while the week has room. `peak`
    is the highest of the readings and their forecasts.

    `ceiling` says the account is no longer one to be used on: its week or a model's limit ran out, it needs
    sign-in, a reading says 99%, or it cannot be read. A forecast alone does not say so. An account left on its
    forecast is still one to be used up by its reading, and with two such accounts whose forecasts both said
    99% the terminals went from one to the other at every check. An account cannot be read while Claude has
    paused its reads (`paused` here): once its reading is over 3 minutes old, that reading cannot reach 99%
    before the pause ends, and waiting for it used the account until it ran out, with nothing to see it. Its
    `old` row passes it over as a target (`_leave`), so the terminals do not come straight back to it. A
    reading that is over 15 minutes old with no pause does not count. Every reading is that old for some
    seconds after a start or a wake, and the terminals would move for no cause whenever another account
    was read first. (A forecast of 99% made from readings before a sleep still moves them at the wake, as it
    did before this rule.) Nor does the pause of an account the terminals came to by an automatic switch less than
    `STAY` ago (`_came`). After a switch the engine asks for a reading of that account, and when Claude
    refused it the terminals left again minutes later, for as long as Claude refused. A switch on a last
    reading moves to a paused account on purpose, and it was left seconds later for the next one that was
    read. `drain` is `why` for the reason of a switch to an account that is used up."""
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
    peak = max((used for _, used in projected if used is not None), default=0)
    reached = max((used for _, used, _ in values if used is not None), default=0)       # read, not forecast
    drain = (why if trigger != "threshold" else f"reached {DRAIN_CEILING}%" if reached >= DRAIN_CEILING else
             f"will pass {DRAIN_CEILING}% before the next check" if peak >= DRAIN_CEILING else
             f"{why} and its usage checks are paused")
    paused = active["old"] and active["paused"]
    return {"trigger": trigger, "why": why, "drain": drain, "peak": peak,
            "yields": trigger == "threshold" or (trigger == "out" and not account.get("out")
                                                 and all(w.get("kind") == "model" for _, w in out)),
            "ceiling": trigger != "threshold" or reached >= DRAIN_CEILING or (paused and not _came(history, active, now)),
            "paused": paused,
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


def _retry(rows):
    """The hold while the accounts to move to are ones a switch was tried to twice in a row. "At the earliest":
    a switch also needs a new reading of the account unless its reads are paused, and after a refused renewal
    of its login the app does not read the account for 30 minutes (`CLIAccounts._refresh`)."""
    wait = duration(min(row["wait"] for row in rows))
    if len(rows) == 1:
        return {"hold": f"A switch to {rows[0]['label']} was tried and is tried again in {wait} at the earliest"}
    names = ", ".join(row["label"] for row in rows[:3]) + (f" and {len(rows) - 3} more" if len(rows) > 3 else "")
    return {"hold": f"Switches to {names} were tried. The next try is in {wait} at the earliest"}


def _listed(rows):
    """The accounts of `rows` by name, for a reason: "Beta", "Beta and Delta", "Beta, Delta, Epsilon and 1 more"."""
    names = [row["label"] for row in rows[:3]] + ([f"{len(rows) - 3} more"] if len(rows) > 3 else [])
    return " and ".join(filter(None, (", ".join(names[:-1]), *names[-1:])))


def _tried(rows):
    """The words of a reason for accounts that are passed over because a switch to each was tried twice."""
    return f"a switch to {_listed(rows)} was tried" if len(rows) == 1 else f"switches to {_listed(rows)} were tried"


def _passed(stale, roomy, threshold):
    """Why no account with room is used, for the reason of a switch to one that is used up. An account with an
    old reading is read again, unless Claude has paused its reads: then no reading is on its way, and the reason
    names the pause, as the hold before this switch did (`_checking`, which lists the accounts its own way)."""
    waits = (f"Claude paused the usage checks of {_listed(stale)}" if all(row["paused"] for row in stale) else
             f"{_listed(stale)} could not be read" if all(row["silent"] for row in stale) else
             f"{_listed(stale)} need{'s' if len(stale) == 1 else ''} a new reading first")
    # An account with room that was tried twice is passed over too, and it is named beside the ones that wait.
    return ", and ".join(([waits] if stale else []) + ([_tried(roomy)] if roomy else [])) or f"every account is past {threshold}%"


def _behind(stale, roomy, others):
    """Why no account with room is used, for the reason of a switch on a last reading to an account behind
    them. Each was tried twice, or its last read failed: one whose reads are paused, and that was not tried
    twice, is used itself. An account whose last read failed is not used on the reading it has. Its reads
    fail for a cause nobody knows, and a switch to it may fail the same way. It is asked again
    (`_checking`), and it does not keep a paused account behind it waiting: that one is used as it would be
    without it."""
    mute, twice = [row for row in stale if not row["twice"]], [row for row in stale if row["twice"]] + roomy
    return ", and ".join(([f"{_listed(mute)} could not be read"] if mute else []) + ([_tried(twice)] if twice else [])) or others


def _said(row, others):
    """How the reason of a switch names an account that is not the first with a new enough reading and room.
    `others` says why no account with room is used (`_passed`)."""
    if row["stale"]:
        return (f"{row['label']}'s {row['renewed']} limit has refilled since its last reading" if row["renewed"] is not None
                else f"{row['label']} had room at its last reading")
    if row["drainable"]:
        return f"{others}, so {row['label']} is used up to {DRAIN_CEILING}%"
    return f"{row['label']}'s week can still be used, though its {row['week_only']}"


def _unasked(row):
    """The end of the reason of a switch made on the reading the account has. With a limit that refilled since
    that reading, the switch does not go by its number, so the words do not say the reading is used."""
    how = ("it is used without a new reading" if row["renewed"] is not None
           else f"its reading from {duration(row['age'])} ago is used")
    return f"Claude has paused {row['label']}'s usage checks, so {how}."


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
    # The same once the account in use is at the ceiling (`_need`) and every one of them is paused. A new
    # reading is seconds away, and the end of a pause 15 to 60 minutes. Waiting for that kept the terminals
    # on the account in use until it had run out, with an account ready to be used up.
    stale = [row for row in rows if row["stale"]]
    if stale and need["yields"] and not (need["ceiling"] and all(row["silent"] for row in stale)):
        return _checking(stale)
    # An account a switch was tried to twice is tried again `RETRY` after that. One with room is waited for
    # while the account in use still works and is below the ceiling, as a paused account with room is: at
    # the ceiling 15 minutes are too long with an account ready to be used up. One past the threshold is
    # not waited for: it was a last resort. A note counts only for an account this decision would use
    # without it: while the account in use still works, one kept for its week alone is no target, and its
    # note holds nothing back.
    failed = [row for row in rows if row["failed"] and (row["failed"] != "week" or not need["yields"])]
    roomy = [row for row in failed if row["failed"] == "room"]
    if roomy and need["yields"] and not need["ceiling"]:
        return _retry(roomy)
    # While its own reads are paused the account in use cannot be seen to reach 99%, so the hold names the pause.
    using = {"hold": f"Claude paused the usage checks of {label}" if need["paused"] else
             f"Every account is past {threshold}% — using {label} up to {DRAIN_CEILING}%"}
    if not need["ceiling"] and need["peak"] < DRAIN_CEILING:
        return using
    # The last resorts, the better one first. Every account is past the threshold, so one that can still be
    # used up is used. And when the account in use works for no model, an account whose week can still be used
    # works for all but the model whose limit ran out, so it is better than staying. While the account in
    # use still works, it is not.
    why, others = need["drain"], _passed(stale, roomy, threshold)
    last = [(row, kind) for row in rows if row["drainable"]]
    if not need["yields"]:
        # These are judged on their week, so the order is the user's order with the week's room as the room.
        # No limit refilled, so the switch keeps the kind of its trigger after "all low" too.
        by_week = _ordered([dict(row, room=threshold - row["values"][0][1]) for row in rows if row["week_only"]], settings)
        last += [(row, need["trigger"]) for row in by_week]
    # An old reading is read again, and it does not keep an account with a new reading waiting behind it.
    for row, how in last:
        if not row["old"]:
            return {"action": "switch", "target": row["id"], "kind": how, "reason": f"{label} {why}; {_said(row, others)}."}
    # Every account left has an old reading: the ones with room first, then the last resorts.
    unread = [(row, kind) for row in stale] + last
    # None of them is read before its pause ends, and the account in use works for no model. Waiting kept the
    # terminals on it for the whole pause. So the first of them is used on the reading it has, however old:
    # staying is known to stop the work, and the switch renews and confirms the login it moves to itself
    # (`switch_cli.perform`). While one of them can be read, that reading is waited for. A second try in a row
    # passes the account over as for any switch (`_row`: a note counts only for an account that is a target).
    # Only an account with room can be passed over here (one to be used up is in `failed` then), so a last
    # resort used behind such accounts says that switches to them were tried, with the ones in `roomy`.
    # An account whose last read failed is asked again and not used on what it has (`_behind`).
    if unread and not need["yields"] and all(row["silent"] for row, _ in unread):
        for row, how in unread:
            if row["paused"] and not row["twice"]:
                return {"action": "switch", "target": row["id"], "kind": how, "last_reading": True,
                        "reason": f"{label} {why}; {_said(row, _behind(stale, roomy, others))}. {_unasked(row)}"}
        if all(row["twice"] for row, _ in unread):
            return _retry([row for row, _ in unread] + failed)
    # Each of `failed` is a target but for its note, so this is not "all low", which starts an alert. With
    # nothing left that can be read before its pause ends, the hold says when the next try comes. The fifth
    # review found it naming that pause at the ceiling, where the try of an account with room is not waited
    # for further up, and the page no longer said when that account is tried again.
    if failed and all(row["paused"] for row, _ in unread):
        return _retry(failed)
    if unread:
        return _checking([row for row, _ in unread])
    if need["trigger"] == "threshold" and need["peak"] < DRAIN_CEILING:
        return using                # it cannot be read and there is nowhere to go: it is used on, as one that can be
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
    # Of the weeks that end within the hour of the soonest, the one with the most room. `_ordered` groups weeks
    # the same way, from the soonest week of every account. The soonest here is of the accounts the early
    # switch can use, so with a chain of weeks its pick can be another account than the order's first.
    soonest = min(row["weekly_reset"] for row in able)
    target = min((row for row in able if row["weekly_reset"] - soonest <= HOUR),
                 key=lambda row: (-row["room"], row["label"].casefold()))
    return {"action": "switch", "target": target["id"], "kind": "early",
            "reason": f"Using {target['label']}'s weekly allowance before it resets in {duration(target['weekly_reset'] - now)} ({100 - target['values'][0][1]:g}% left)."}
