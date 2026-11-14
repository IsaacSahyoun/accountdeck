"""What AccountDeck measures and decides, on synthetic data: the terminal auto-switch rule as a decision table,
and usage readings, refills and account upkeep for all four clients. Builder B's file (2026-11-12 refactor)."""
import copy
import http.server
from pathlib import Path
import threading
import time
import unittest
import uuid

from tests import support

from accountdeck import cli_policy, config, contract, demo, fixtures, move_bundle, move_import
from accountdeck.providers import errors
from accountdeck.providers import http as provider_http
from accountdeck.scheduler import RefreshScheduler, Target
from accountdeck.usage import claude_usage, muse_usage

NOW = 1_800_000_000.0
HOUR, DAY = 3600, 86400
SETTINGS = {"cli_autoswitch": True, "cli_threshold": 95, "cli_order": "soonest_reset", "cli_early_switch": True,
            "pinned_model": ""}


def account(key, weekly, weekly_reset=3 * DAY, *, active=False, age=60, model=None, **fields):
    """One terminal account as `cli_auto.inputs` hands it to the rule. The reset is seconds from NOW; `age` is how
    old the reading is (None: never read); `model` is the % used of its Fable weekly limit, named as
    `usage.claude_usage` names it. An account with no `model` has no such limit in its reading."""
    windows = [{"id": "seven_day", "kind": "weekly", "used": weekly, "reset": NOW + weekly_reset}]
    if model is not None:
        windows.append({"id": "seven_day_fable", "kind": "model", "model": "Fable", "label": "Fable weekly",
                        "used": model, "reset": NOW + weekly_reset})
    return {"id": key, "label": key.capitalize(), "active": active, "forecast": {}, "owner": "v2", "excluded": False,
            "health": "ok", "live_expired": False, "last_good_at": None if age is None else NOW - age,
            "windows": windows, **fields}


# ---- the decision table ----
# Columns: the reading of Alpha, the account in use. Weekly % used, and when that week ends.
WEEKLY_USED = (30, 96, 99, 100)
WEEKLY_RESET = (("in 3 d", 3 * DAY), ("in 4 min", 240), ("40 s ago", -40))
COLUMNS = [(weekly, name, reset) for weekly in WEEKLY_USED for name, reset in WEEKLY_RESET]

BETA = dict(weekly=30)
SOON = dict(BETA, weekly=50, weekly_reset=12 * HOUR)          # Beta's week ends before Alpha's: the early switch
SOONER = dict(BETA, weekly=50, weekly_reset=2 * DAY)          # the same, with the end of Beta's week 2 days off
UNDONE = {"left": "beta", "left_at": NOW - 5, "manual_at": NOW - 5, "manual_for": "alpha",
          "last": {"from": "alpha", "to": "beta", "at": NOW - 60, "kind": "threshold", "reason": "", "undo_until": NOW - 5}}
# The user chose an account 30 minutes ago. Its week ran out, and 10 minutes ago auto-switch moved the terminals
# to Alpha. Alpha is not the user's choice.
MOVED = {"left": "delta", "left_at": NOW - 600, "manual_at": NOW - 1800, "manual_for": "delta",
         "last": {"from": "delta", "to": "alpha", "at": NOW - 600, "kind": "out", "reason": "", "undo_until": NOW}}
# The same Undo with the Mac's clock set back a minute before it: the switch it undid has the later time.
CLOCK_SET_BACK = dict(UNDONE, last=dict(UNDONE["last"], at=NOW + 55))
# A history whose last switch has no time (damaged data: no build writes that).
NO_TIME = dict(UNDONE, last=dict(UNDONE["last"], at=None))
# A history the build before this one wrote: it has the time of the choice and not the account.
BEFORE_NAMES = {key: value for key, value in MOVED.items() if key != "manual_for"}


def beta(**changes):
    spec = {**BETA, **changes}
    return account("beta", spec.pop("weekly"), **spec)


def gamma():
    return account("gamma", 50, 2 * DAY)


# Rows: everything else. name -> (the other accounts, history, settings changes, changes to Alpha).
ROWS = {
    "Beta has room": ([beta()], {}, {}, {}),
    "Beta's reading is 20 min old": ([beta(age=1200)], {}, {}, {}),
    "Beta was never read": ([beta(age=None)], {}, {}, {}),
    "Beta's weekly is at 90%": ([beta(weekly=90)], {}, {}, {}),
    "Beta's weekly is at 98%": ([beta(weekly=98)], {}, {}, {}),
    "Beta's weekly is unknown": ([beta(weekly=None)], {}, {}, {}),
    "Beta needs sign-in": ([beta(health="needs_signin")], {}, {}, {}),
    "Beta is left out": ([beta(excluded=True)], {}, {}, {}),
    "Beta belongs to the PC": ([beta(owner="on_pc")], {}, {}, {}),
    "Alpha is the only account": ([], {}, {}, {}),
    "Beta is 20 min old, Gamma at 90%": ([beta(age=1200), account("gamma", 90, 2 * DAY)], {}, {}, {}),
    "Gamma's week ends sooner": ([beta(), gamma()], {}, {}, {}),
    "Gamma's week ends sooner, most room first": ([beta(), gamma()], {}, {"cli_order": "most_room"}, {}),
    "Gamma's week ends sooner, my order Beta first": ([beta(), gamma()], {},
                                                     {"cli_order": "manual", "cli_manual_order": ["beta", "gamma"]}, {}),
    "Undo pressed 5 s ago": ([beta()], UNDONE, {}, {}),
    "Undo pressed 5 s ago, Alpha needs sign-in": ([beta()], UNDONE, {}, {"health": "needs_signin"}),
    "Undo pressed 5 s ago, Alpha is marked out": ([beta()], UNDONE, {}, {"out": True}),
    "Manual choice 2 h 5 s ago": ([beta()], {"manual_at": NOW - 2 * HOUR - 5}, {}, {}),
    "Manual choice 30 min ago, moved by itself since": ([beta()], MOVED, {}, {}),
    "Manual choice 30 min ago, moved by itself since, Alpha's Fable limit is at 100%": ([beta()], MOVED, {}, {"model": 100}),
    "Manual choice 30 min ago, moved by itself since, Beta's week ends in 12 h": ([beta(**SOON)], MOVED, {}, {}),
    "Undo pressed 5 s ago, the clock was set back before it": ([beta()], CLOCK_SET_BACK, {}, {}),
    "Undo pressed 5 s ago, the last switch has no time": ([beta()], NO_TIME, {}, {}),
    "Manual choice 30 min ago, moved by itself since, history of the build before": ([beta()], BEFORE_NAMES, {}, {}),
    "Early switch 1 h ago": ([beta()], {"early_at": NOW - HOUR}, {}, {}),
    "Every account was low before": ([beta()], {"exhausted": True}, {}, {}),
    "Auto-switch is off": ([beta()], {}, {"cli_autoswitch": False}, {}),
    "Early switch is off": ([beta(**SOON)], {}, {"cli_early_switch": False}, {}),
    "Threshold is 80%": ([beta()], {}, {"cli_threshold": 80}, {}),
    "Alpha's login expired while idle": ([beta()], {}, {}, {"live_expired": True}),
    "Alpha needs sign-in": ([beta()], {}, {}, {"health": "needs_signin"}),
    "Alpha is marked out": ([beta()], {}, {}, {"out": True}),
    "Alpha belongs to the PC": ([beta()], {}, {}, {"owner": "on_pc"}),
    "Alpha's weekly is forecast at 97%": ([beta()], {}, {}, {"forecast": {"seven_day": 97.0}}),
    "Alpha's pinned Fable limit is at 96%": ([beta()], {}, {"pinned_model": "fable"}, {"model": 96}),
    "Alpha's Fable limit is at 96%": ([beta()], {}, {}, {"model": 96}),
    "Alpha's Fable limit is at 100%": ([beta()], {}, {}, {"model": 100}),
    "Alpha's Fable limit is forecast at 97%": ([beta()], {}, {}, {"model": 50, "forecast": {"seven_day_fable": 97.0}}),
    "Undo pressed 5 s ago, Alpha's Fable limit is at 100%": ([beta()], UNDONE, {}, {"model": 100}),
    "Beta's Fable limit is at 90%": ([beta(model=90)], {}, {}, {}),
    "Beta's Fable limit is at 100%": ([beta(model=100)], {}, {}, {}),
    "Beta's Fable limit is at 100%, Gamma has room, most room first": ([beta(model=100), gamma()], {}, {"cli_order": "most_room"}, {}),
    "Beta's Fable limit is at 100%, Alpha needs sign-in": ([beta(model=100)], {}, {}, {"health": "needs_signin"}),
    "Beta's Fable limit is at 100%, its weekly at 98%": ([beta(weekly=98, model=100)], {}, {}, {}),
    # Beta is first in the order here (its week ends first), so only the kind of last resort decides
    "Beta's Fable limit is at 100%, Gamma's weekly is at 96%": ([beta(model=100), account("gamma", 96, 4 * DAY)], {}, {}, {}),
    "Beta's weekly is at 90%, its reading 20 min old": ([beta(weekly=90, age=1200)], {}, {}, {}),
    "Beta's weekly is at 90% and 20 min old, Gamma's is at 90%": ([beta(weekly=90, age=1200), account("gamma", 90, 4 * DAY)], {}, {}, {}),
    "Beta's Fable limit is at 100%, its reading 20 min old": ([beta(model=100, age=1200)], {}, {}, {}),
    "Beta's Fable limit is at 100% and 20 min old, Gamma's is at 100%": ([beta(model=100, age=1200), account("gamma", 30, 4 * DAY, model=100)], {}, {}, {}),
    "Beta's weekly is at 90% and 20 min old, Gamma's Fable limit is at 100%": ([beta(weekly=90, age=1200), account("gamma", 30, 4 * DAY, model=100)], {}, {}, {}),
    "Beta is 20 min old, Gamma's Fable limit is at 100%": ([beta(age=1200), account("gamma", 30, 4 * DAY, model=100)], {}, {}, {}),
    "Beta is 20 min old, Gamma at 90%, Alpha needs sign-in": ([beta(age=1200), account("gamma", 90, 2 * DAY)], {}, {}, {"health": "needs_signin"}),
    "Undo pressed 5 s ago, Beta's Fable limit is at 100%": ([beta(model=100)], UNDONE, {}, {}),
    "Every account was low before, Beta's Fable limit is at 100%": ([beta(model=100)], {"exhausted": True}, {}, {}),
    "Beta's week ends in 12 h": ([beta(**SOON)], {}, {}, {}),
    "Beta's week ends in 12 h, 85% used": ([beta(**dict(SOON, weekly=85))], {}, {}, {}),
    "Beta's week ends in 12 h, left 10 min ago": ([beta(**SOON)], {"left": "beta", "left_at": NOW - 600}, {}, {}),
    "Beta's week ends in 12 h, manual choice 5 s ago": ([beta(**SOON)], {"manual_at": NOW - 5}, {}, {}),
    "Beta's week ends in 12 h, early switch 1 h ago": ([beta(**SOON)], {"early_at": NOW - HOUR}, {}, {}),
    # The week that ends soonest is used first, however far off its end is (Isaac, 2026-11-14). Alpha's week
    # ends in 3 days in the first column of each group.
    "Beta's week ends in 2 d": ([beta(**SOONER)], {}, {}, {}),
    "Beta's week ends in 2 d, early switch is off": ([beta(**SOONER)], {}, {"cli_early_switch": False}, {}),
    "Beta's week ends in 2 d, 85% used": ([beta(**dict(SOONER, weekly=85))], {}, {}, {}),
    "Beta's week ends in 2 d, its Fable limit at 90%": ([beta(**dict(SOONER, model=90))], {}, {}, {}),
    "Beta's week ends in 2 d, its reading 20 min old": ([beta(**dict(SOONER, age=1200))], {}, {}, {}),
    "Beta's week ends in 2 d, manual choice 5 s ago": ([beta(**SOONER)], {"manual_at": NOW - 5}, {}, {}),
    "Beta's week ends in 2 d, early switch 1 h ago": ([beta(**SOONER)], {"early_at": NOW - HOUR}, {}, {}),
    "Beta's week ends in 2 d, an early switch did not finish 1 h ago": ([beta(**SOONER)], {"early_tried": NOW - HOUR}, {}, {}),
    "Beta's week ends in 4 d": ([beta(weekly=50, weekly_reset=4 * DAY)], {}, {}, {}),
}

HOLDS = {"has refilled": "w", "Checking": "c", "Every account is past": "d", "Usage updates after": "x",
         "An early switch happened": "e", "The last early switch did not finish": "t", "Your manual choice": "m"}
KINDS = {"threshold": "T", "out": "O", "login": "L", "early": "E", "refilled": "R"}


def code(result):
    """One decision as two characters. A switch: its kind (Threshold, Out, Login, Early, Refilled after all were
    low) and the target's initial. A hold: h and its reason (w has refilled, c checking the
    others, d using every account up, x login expired, e early switch too recent, t an early switch that did
    not finish too recent, m manual choice), in capitals
    when the rule also asks for a new reading. XX: every account is low. ..: nothing to do."""
    if result["action"] == "switch":
        return KINDS[result["kind"]] + result["target"][0]
    if result["exhausted"]:
        return "XX"
    if result["hold"]:
        letter = next((letter for text, letter in HOLDS.items() if text in result["hold"]), "?")
        return "h" + (letter.upper() if result["refresh"] else letter)
    return ".."


def decide(row, column):
    others, history, settings, alpha = ROWS[row]
    weekly, _name, reset = column
    alpha = dict(alpha)
    active = account("alpha", weekly, reset, active=True, model=alpha.pop("model", None), **alpha)
    return cli_policy.evaluate([active] + copy.deepcopy(others), {**SETTINGS, **settings}, copy.deepcopy(history), NOW)


def table():
    return {row: " ".join(code(decide(row, column)) for column in COLUMNS) for row in ROWS}


# The answers of the rule since Claude dropped its 5-hour limit (2026-11-13). Before that the table had a 5-hour
# reading in every column, and a 5-hour limit that refilled within 10 minutes held the switch back. What it
# keeps from the starting commit (1fffff4): a limit whose reset time passed 40 s ago is waited for (hW) and not
# acted on, and a manual choice or Undo in the last 2 hours holds the threshold switch (hm), but not an
# account that ran out or needs sign-in.
# weekly % used:                                        30         96         99         100
# the week ends:                                        3d 4m ago 3d 4m ago 3d 4m ago 3d 4m ago
EXPECTED = {
    "Beta has room":                                   ".. .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Beta's reading is 20 min old":                    ".. .. .. hC hC hW hC hC hW hC hC hW",
    "Beta was never read":                             ".. .. .. hd hd hW XX XX hW XX XX hW",
    "Beta's weekly is at 90%":                         ".. .. .. hd hd hW Tb Tb hW Ob Ob hW",
    "Beta's weekly is at 98%":                         ".. .. .. hd hd hW XX XX hW XX XX hW",
    "Beta's weekly is unknown":                        ".. .. .. hd hd hW XX XX hW XX XX hW",
    "Beta needs sign-in":                              ".. .. .. hd hd hW XX XX hW XX XX hW",
    "Beta is left out":                                ".. .. .. hd hd hW XX XX hW XX XX hW",
    "Beta belongs to the PC":                          ".. .. .. hd hd hW XX XX hW XX XX hW",
    "Alpha is the only account":                       ".. .. .. hd hd hW XX XX hW XX XX hW",
    # Beta has room and an old reading. While Alpha still works, Beta's new reading is waited for (hC). Once
    # Alpha works for no model, Gamma is used at once, and Beta is read again from there.
    "Beta is 20 min old, Gamma at 90%":                ".. .. .. hC hC hW hC hC hW Og Og hW",
    # Gamma's week ends in 2 days and Alpha's in 3, so the early switch moves to Gamma while Alpha has room (Eg),
    # whatever the order is set to. A switch at the threshold goes to Gamma too (Tg, Og): the order would send
    # the terminals to Beta, whose week ends a day later, and the early switch would move them on 5 seconds later.
    "Gamma's week ends sooner":                        "Eg .. .. Tg Tg hW Tg Tg hW Og Og hW",
    "Gamma's week ends sooner, most room first":       "Eg .. .. Tg Tg hW Tg Tg hW Og Og hW",
    "Gamma's week ends sooner, my order Beta first":   "Eg .. .. Tg Tg hW Tg Tg hW Og Og hW",
    "Undo pressed 5 s ago":                            "hm hm hm hm hm hW hm hm hW Ob Ob hW",
    "Undo pressed 5 s ago, Alpha needs sign-in":       "Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb",
    "Undo pressed 5 s ago, Alpha is marked out":       "Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob",
    "Manual choice 2 h 5 s ago":                       ".. .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    # The hold is for the account the user chose, which the history names (`manual_for`). Once auto-switch has
    # moved the terminals off it, the account in use is not their choice, and the rule acts as if no choice
    # was made. The reverse is the Undo rows: Alpha is the account the user went back to, and it is held.
    "Manual choice 30 min ago, moved by itself since": ".. .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Manual choice 30 min ago, moved by itself since, Alpha's Fable limit is at 100%": "Ob Ob hW Ob Ob hW Ob Ob hW Ob Ob hW",
    "Manual choice 30 min ago, moved by itself since, Beta's week ends in 12 h": "Eb .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    # The account decides, not the order of two times: a clock set back, or a time that is missing, changes nothing.
    "Undo pressed 5 s ago, the clock was set back before it": "hm hm hm hm hm hW hm hm hW Ob Ob hW",
    "Undo pressed 5 s ago, the last switch has no time": "hm hm hm hm hm hW hm hm hW Ob Ob hW",
    # Without the account's name the hold counts for whichever account is in use, as it did in that build.
    "Manual choice 30 min ago, moved by itself since, history of the build before": "hm hm hm hm hm hW hm hm hW Ob Ob hW",
    # The pause between early switches is said (he) only while it holds a move back. Beta's week ends with
    # Alpha's here, so there is none.
    "Early switch 1 h ago":                            ".. .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Every account was low before":                    ".. .. .. Rb Rb hW Rb Rb hW Rb Rb hW",
    "Auto-switch is off":                              ".. .. .. .. .. .. .. .. .. .. .. ..",
    "Early switch is off":                             ".. .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Threshold is 80%":                                ".. .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Alpha's login expired while idle":                "hx hx hx hx hx hx hx hx hx hx hx hx",
    "Alpha needs sign-in":                             "Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb",
    "Alpha is marked out":                             "Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob",
    "Alpha belongs to the PC":                         ".. .. .. .. .. .. .. .. .. .. .. ..",
    "Alpha's weekly is forecast at 97%":               "Tb Tb .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Alpha's pinned Fable limit is at 96%":            "Tb Tb hW Tb Tb hW Tb Tb hW Ob Ob hW",
    # The Fable weekly limit with settings as a new install has them. A manual choice holds against it
    # even when it is out, because the account still works for the other models.
    "Alpha's Fable limit is at 96%":                   "Tb Tb hW Tb Tb hW Tb Tb hW Ob Ob hW",
    "Alpha's Fable limit is at 100%":                  "Ob Ob hW Ob Ob hW Ob Ob hW Ob Ob hW",
    "Alpha's Fable limit is forecast at 97%":          "Tb Tb .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Undo pressed 5 s ago, Alpha's Fable limit is at 100%": "hm hm hW hm hm hW hm hm hW Ob Ob hW",
    "Beta's Fable limit is at 90%":                    ".. .. .. hd hd hW Tb Tb hW Ob Ob hW",
    # An account whose Fable limit is out is refused while the account in use still works. Once the account
    # in use works for no model (its week ran out, or it needs sign-in), an account whose week can still be
    # used is better than staying, and it is picked after every account that has room in both limits.
    "Beta's Fable limit is at 100%":                   ".. .. .. hd hd hW XX XX hW Ob Ob hW",
    "Beta's Fable limit is at 100%, Gamma has room, most room first": "Eg .. .. Tg Tg hW Tg Tg hW Og Og hW",
    "Beta's Fable limit is at 100%, Alpha needs sign-in": "Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb",
    "Beta's Fable limit is at 100%, its weekly at 98%": ".. .. .. hd hd hW XX XX hW XX XX hW",
    "Beta's Fable limit is at 100%, Gamma's weekly is at 96%": ".. .. .. hd hd hW Tg Tg hW Og Og hW",
    "Undo pressed 5 s ago, Beta's Fable limit is at 100%": "hm hm hm hm hm hW hm hm hW Ob Ob hW",
    # No limit refilled here, so the switch is not called "refilled" (R) the way a switch after "all low" is.
    "Every account was low before, Beta's Fable limit is at 100%": ".. .. .. hd hd hW XX XX hW Ob Ob hW",
    # An account past the threshold is used on a reading at most 15 minutes old. An older one is read again
    # (hC), and an account with a new reading behind it in the order is used first.
    "Beta's weekly is at 90%, its reading 20 min old": ".. .. .. hd hd hW hC hC hW hC hC hW",
    "Beta's weekly is at 90% and 20 min old, Gamma's is at 90%": ".. .. .. hd hd hW Tg Tg hW Og Og hW",
    "Beta's Fable limit is at 100%, its reading 20 min old": ".. .. .. hd hd hW XX XX hW hC hC hW",
    "Beta's Fable limit is at 100% and 20 min old, Gamma's is at 100%": ".. .. .. hd hd hW XX XX hW Og Og hW",
    "Beta's weekly is at 90% and 20 min old, Gamma's Fable limit is at 100%": ".. .. .. hd hd hW hC hC hW Og Og hW",
    "Beta is 20 min old, Gamma's Fable limit is at 100%": ".. .. .. hC hC hW hC hC hW Og Og hW",
    "Beta is 20 min old, Gamma at 90%, Alpha needs sign-in": "Lg Lg Lg Lg Lg Lg Lg Lg Lg Lg Lg Lg",
    "Beta's week ends in 12 h":                        "Eb .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Beta's week ends in 12 h, 85% used":              ".. .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Beta's week ends in 12 h, left 10 min ago":       ".. .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Beta's week ends in 12 h, manual choice 5 s ago": "hm hm hm hm hm hW hm hm hW Ob Ob hW",
    "Beta's week ends in 12 h, early switch 1 h ago":  "he .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    # The same answers with the end of Beta's week 2 days off. A week that ends after Alpha's is never moved to.
    "Beta's week ends in 2 d":                         "Eb .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Beta's week ends in 2 d, early switch is off":    ".. .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Beta's week ends in 2 d, 85% used":               ".. .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Beta's week ends in 2 d, its Fable limit at 90%": ".. .. .. hd hd hW Tb Tb hW Ob Ob hW",
    "Beta's week ends in 2 d, its reading 20 min old": ".. .. .. hC hC hW hC hC hW hC hC hW",
    "Beta's week ends in 2 d, manual choice 5 s ago":  "hm hm hm hm hm hW hm hm hW Ob Ob hW",
    "Beta's week ends in 2 d, early switch 1 h ago":   "he .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    # A try of the early switch that failed counts like one that worked (ht). The threshold does not wait for it.
    "Beta's week ends in 2 d, an early switch did not finish 1 h ago": "ht .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Beta's week ends in 4 d":                         ".. .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
}


class AutoSwitchTableTest(unittest.TestCase):
    """Every decision of `cli_policy.evaluate` for 68 situations times 12 readings of the account in use."""
    maxDiff = None

    def test_the_decision_table(self):
        header = " ".join(f"{weekly:>3}" for weekly, _name, _reset in COLUMNS)
        self.assertEqual(table(), EXPECTED, f"columns (weekly % used; the week ends in 3 d, in 4 min, 40 s ago): {header}")


class SoonestWeekFirstTest(unittest.TestCase):
    """The early switch keeps the terminals on the usable account whose week ends soonest, so that allowance is
    spent before it expires (Isaac, 2026-11-14: "it switches to the accounts thats nearest to its reset"). Until
    then it moved only to a week that ended within 24 hours. Alpha is in use here, with 20% of a week used that
    ends in 6 days."""

    def decide(self, *others, alpha=(20, 6 * DAY), history=None, **settings):
        active = account("alpha", alpha[0], alpha[1], active=True)
        return cli_policy.evaluate([active, *copy.deepcopy(list(others))], {**SETTINGS, **settings}, history or {}, NOW)

    def moves(self, *others, **more):
        result = self.decide(*others, **more)
        return (result["action"], result["target"], result["kind"], result["hold"])

    def test_it_moves_to_a_week_that_ends_sooner_however_far_off_its_end_is(self):
        for name, ends in (("12 hours", 12 * HOUR), ("25 hours", 25 * HOUR), ("2 days", 2 * DAY), ("5 days", 5 * DAY),
                           ("an hour and a minute before Alpha's", 6 * DAY - HOUR - 60)):
            with self.subTest(ends_in=name):
                self.assertEqual(self.moves(account("beta", 40, ends)), ("switch", "beta", "early", None))

    def test_of_several_it_moves_to_the_week_that_ends_soonest(self):
        others = [account("beta", 40, 4 * DAY), account("gamma", 40, 2 * DAY), account("delta", 40, 5 * DAY)]
        for order in ("soonest_reset", "most_room", "manual"):
            with self.subTest(order=order):
                self.assertEqual(self.moves(*others, cli_order=order, cli_manual_order=["delta", "beta", "gamma"]),
                                 ("switch", "gamma", "early", None))

    def test_it_never_moves_to_a_week_that_ends_later_or_with_the_one_in_use(self):
        """Weeks that end within the hour of each other end together, as they do in the order
        (`cli_policy._ordered`): an hour of allowance is not worth a switch."""
        for name, ends in (("an hour before", 6 * DAY - HOUR), ("a minute before", 6 * DAY - 60), ("with Alpha's", 6 * DAY),
                           ("a minute after", 6 * DAY + 60), ("a day after", 7 * DAY)):
            with self.subTest(ends=name):
                self.assertEqual(self.moves(account("beta", 40, ends)), ("none", None, None, None))

    def test_two_weeks_that_end_together_never_trade_places(self):
        """Claude's reset time for one week differs by a second from reading to reading (`cli_policy.SAME_RESET`).
        Whichever of two such weeks reads a second sooner, the terminals stay where they are."""
        for alpha_ends, beta_ends in ((6 * DAY, 6 * DAY - 1), (6 * DAY - 1, 6 * DAY)):
            for in_use in ("alpha", "beta"):
                with self.subTest(alpha_ends=alpha_ends, beta_ends=beta_ends, in_use=in_use):
                    accounts = [account("alpha", 20, alpha_ends, active=in_use == "alpha"),
                                account("beta", 30, beta_ends, active=in_use == "beta")]
                    self.assertEqual(cli_policy.evaluate(accounts, SETTINGS, {}, NOW)["action"], "none")

    def play(self, accounts, **settings):
        """The switches the rule makes one after another, each check 5 seconds after the last one's switch, with
        the history a switch writes (`switch_cli.CLISwitch.run`), until a check decides nothing. With them, the
        reason of the first."""
        accounts, history, now, made, said = copy.deepcopy(accounts), {}, NOW, [], ""
        for _ in range(4):
            result = cli_policy.evaluate(accounts, {**SETTINGS, **settings}, history, now)
            if result["action"] != "switch":
                break
            made.append((result["kind"], result["target"]))
            said = said or result["reason"]
            leaving = next(a["id"] for a in accounts if a["active"])
            history.update(left=leaving, left_at=now, exhausted=False,
                           last={"from": leaving, "to": result["target"], "at": now, "kind": result["kind"],
                                 "reason": result["reason"], "undo_until": now + 600})
            if result["kind"] == "early":
                history["early_at"] = now
            for one in accounts:
                one["active"] = one["id"] == result["target"]
            now += 5
        return made, said

    def test_at_the_threshold_the_terminals_go_straight_to_the_week_that_ends_soonest(self):
        """One switch, not two. The order the user chose puts Beta first here. A switch to Beta would be followed
        5 seconds later by the early switch to Gamma, and the Undo of the first would be gone."""
        accounts = [account("alpha", 96, 6 * DAY, active=True), account("beta", 30, 4 * DAY), account("gamma", 40, 2 * DAY)]
        for name, order in (("most room first", {"cli_order": "most_room"}),
                            ("my order, Beta first", {"cli_order": "manual", "cli_manual_order": ["beta", "gamma"]}),
                            ("soonest reset first", {})):
            with self.subTest(order=name):
                self.assertEqual(self.play(accounts, **order),
                                 ([("threshold", "gamma")], "Alpha reached 95% of its weekly limit; Gamma's week resets soonest."
                                  if order else "Alpha reached 95% of its weekly limit; Gamma is first in your eligible order."))
        # The order shows it too: the account the terminals go to next is first.
        shown = cli_policy.evaluate(accounts, {**SETTINGS, "cli_order": "most_room"}, {}, NOW)["next_up"]
        self.assertEqual([row["id"] for row in shown], ["gamma", "beta", "alpha"])

    def test_with_the_early_switch_off_the_order_decides_alone(self):
        accounts = [account("alpha", 96, 6 * DAY, active=True), account("beta", 30, 4 * DAY), account("gamma", 40, 2 * DAY)]
        made, said = self.play(accounts, cli_order="most_room", cli_early_switch=False)
        self.assertEqual((made, said), ([("threshold", "beta")], "Alpha reached 95% of its weekly limit; Beta is first in your eligible order."))
        shown = cli_policy.evaluate(accounts, {**SETTINGS, "cli_order": "most_room", "cli_early_switch": False}, {}, NOW)["next_up"]
        self.assertEqual([row["id"] for row in shown], ["beta", "gamma", "alpha"])

    def test_a_switch_at_the_threshold_is_not_followed_by_one_to_a_week_that_ends_minutes_sooner(self):
        """The order calls two weeks that end within the hour a tie and puts the one with more room first. The
        early switch calls them a tie too, so it does not move on to the other."""
        accounts = [account("alpha", 96, 6 * DAY, active=True), account("beta", 30, 2 * DAY + 1800), account("gamma", 50, 2 * DAY)]
        self.assertEqual(self.play(accounts)[0], [("threshold", "beta")])

    def test_at_the_threshold_a_week_that_ends_sooner_is_used_only_with_a_fifth_of_it_left(self):
        """Gamma's week ends first and 85% of it is used: the early switch would not move there, so it does not
        go ahead of the user's order. The order still reaches it."""
        accounts = [account("alpha", 96, 6 * DAY, active=True), account("beta", 30, 4 * DAY), account("gamma", 85, 2 * DAY)]
        self.assertEqual(self.play(accounts, cli_order="most_room")[0], [("threshold", "beta")])
        self.assertEqual(self.play(accounts)[0], [("threshold", "gamma")])

    def test_an_old_reading_of_a_week_that_ends_sooner_is_passed_over_not_waited_for(self):
        """Beta's week ends first and its reading is 20 minutes old. The terminals go to Gamma, and on to Beta
        after the pause between early switches if Beta is read by then. Waiting for Beta was tried and taken
        out (2026-11-14): an account that cannot be read would hold every early switch back, and at the
        threshold it would keep the terminals on an account until that ran out."""
        old_beta, gamma = account("beta", 40, DAY, age=1200), account("gamma", 40, 3 * DAY)

        def decides(used, *others, **settings):
            result = cli_policy.evaluate([account("alpha", used, 6 * DAY, active=True), *copy.deepcopy(list(others))],
                                         {**SETTINGS, **settings}, {}, NOW)
            return (result["action"], result["target"], result["kind"], result["hold"], result["refresh"])

        self.assertEqual(decides(20, old_beta, gamma), ("switch", "gamma", "early", None, []))
        self.assertEqual(decides(20, old_beta), ("none", None, None, None, []))
        # At the threshold too: the account in use is left at once for the one that can be used now.
        self.assertEqual(decides(96, old_beta, gamma), ("switch", "gamma", "threshold", None, []))
        self.assertEqual(decides(96, old_beta, gamma, cli_order="most_room"), ("switch", "gamma", "threshold", None, []))

    def test_the_pause_between_early_switches_is_said_only_while_it_holds_a_move_back(self):
        """After an early switch the terminals are on the week that ends soonest and nothing more would move
        them, so the page has no pause to show."""
        beta_in_use = [dict(account("beta", 40, 2 * DAY), active=True), account("alpha", 20, 6 * DAY)]
        result = cli_policy.evaluate(beta_in_use, SETTINGS, {"early_at": NOW - HOUR}, NOW)
        self.assertEqual((result["action"], result["hold"]), ("none", None))
        self.assertEqual(self.moves(account("beta", 40, 2 * DAY), history={"early_at": NOW - HOUR}),
                         ("none", None, None, "An early switch happened less than 2 hours ago"))

    def test_an_early_switch_that_did_not_finish_is_tried_again_after_two_hours(self):
        """`cli_auto.tick` writes the time it started an early switch. One that failed leaves nothing else, and
        without the pause the same switch would be started at every check."""
        beta_sooner = account("beta", 40, 2 * DAY)
        self.assertEqual(self.moves(beta_sooner, history={"early_tried": NOW - 5}),
                         ("none", None, None, "The last early switch did not finish. It is tried again 2 hours after it"))
        self.assertEqual(self.moves(beta_sooner, history={"early_tried": NOW - 2 * HOUR + 60})[0], "none")
        self.assertEqual(self.moves(beta_sooner, history={"early_tried": NOW - 2 * HOUR}), ("switch", "beta", "early", None))
        # One that worked says so, and neither is said once nothing would move.
        self.assertEqual(self.moves(beta_sooner, history={"early_tried": NOW - 5, "early_at": NOW - 3})[3],
                         "An early switch happened less than 2 hours ago")
        self.assertEqual(self.moves(account("beta", 40, 7 * DAY), history={"early_tried": NOW - 5}), ("none", None, None, None))
        # A switch the account in use needs does not wait for it.
        self.assertEqual(self.moves(beta_sooner, alpha=(96, 6 * DAY), history={"early_tried": NOW - 5})[:3], ("switch", "beta", "threshold"))

    def test_two_hours_after_an_undo_the_move_is_made_again(self):
        """An Undo is the user's choice and is held like any other, for 2 hours. After that the week that ends
        soonest is used first again: to keep an account, the user leaves the other out or turns this setting off."""
        undone = {"left": "beta", "left_at": NOW - 2 * HOUR, "manual_at": NOW - 2 * HOUR, "manual_for": "alpha", "early_at": NOW - 2 * HOUR - 300,
                  "last": {"from": "alpha", "to": "beta", "at": NOW - 2 * HOUR - 300, "kind": "early", "reason": "", "undo_until": NOW - 2 * HOUR}}
        beta_sooner = account("beta", 40, 2 * DAY)
        self.assertEqual(self.moves(beta_sooner, history=dict(undone, manual_at=NOW - 2 * HOUR + 60)),
                         ("none", None, None, "Your manual choice is held for 2 hours"))
        self.assertEqual(self.moves(beta_sooner, history=undone), ("switch", "beta", "early", None))

    def test_after_the_move_it_stays(self):
        """The reverse: Beta is in use now, its week ends first, and nothing moves the terminals back."""
        beta_in_use = [dict(account("beta", 40, 2 * DAY), active=True), account("alpha", 20, 6 * DAY)]
        result = cli_policy.evaluate(beta_in_use, SETTINGS, {"early_at": NOW - 3 * HOUR}, NOW)
        self.assertEqual((result["action"], result["hold"]), ("none", None))

    def test_a_week_that_has_not_started_is_not_moved_to(self):
        """It has no end yet. Its 7 days start with the first message, and the rule does not start that clock."""
        unstarted = dict(account("beta", 0, DAY), windows=[{"id": "seven_day", "kind": "weekly", "used": 0, "reset": None}])
        self.assertEqual(self.moves(unstarted), ("none", None, None, None))

    def test_an_account_that_cannot_be_used_is_not_moved_to(self):
        cases = {"needs sign-in": account("beta", 40, 2 * DAY, health="needs_signin"),
                 "left out": account("beta", 40, 2 * DAY, excluded=True),
                 "less than 20% of its week left": account("beta", 81, 2 * DAY),
                 "its Fable limit nearly used up": account("beta", 40, 2 * DAY, model=90),
                 "a reading 20 minutes old": account("beta", 40, 2 * DAY, age=1200),
                 "never read": account("beta", 40, 2 * DAY, age=None),
                 "its week already ended": account("beta", 40, -60)}
        for name, other in cases.items():
            with self.subTest(name):
                self.assertEqual(self.moves(other), ("none", None, None, None))

    def test_the_setting_turns_it_off_and_the_holds_still_hold(self):
        beta_sooner = account("beta", 40, 2 * DAY)
        self.assertEqual(self.moves(beta_sooner, cli_early_switch=False), ("none", None, None, None))
        self.assertEqual(self.moves(beta_sooner, cli_autoswitch=False), ("none", None, None, None))
        self.assertEqual(self.moves(beta_sooner, history={"manual_at": NOW - 5}), ("none", None, None, "Your manual choice is held for 2 hours"))
        self.assertEqual(self.moves(beta_sooner, history={"early_at": NOW - HOUR}),
                         ("none", None, None, "An early switch happened less than 2 hours ago"))
        self.assertEqual(self.moves(beta_sooner, history={"left": "beta", "left_at": NOW - 600}), ("none", None, None, None))
        self.assertEqual(self.moves(beta_sooner, history={"left": "beta", "left_at": NOW - 2000}), ("switch", "beta", "early", None))

    def test_the_threshold_still_comes_first(self):
        """An account in use that reached the threshold is left for the first account in the user's order, as before."""
        result = self.decide(account("beta", 40, 4 * DAY), account("gamma", 40, 2 * DAY), alpha=(96, 6 * DAY), cli_order="most_room",
                             cli_manual_order=[])
        self.assertEqual((result["action"], result["kind"]), ("switch", "threshold"))

    def test_the_reason_says_how_far_off_the_end_is(self):
        said = {name: self.decide(account("beta", 40, ends))["reason"]
                for name, ends in (("12 hours", 12 * HOUR), ("2 days", 2 * DAY), ("3 days 5 hours", 3 * DAY + 5 * HOUR))}
        self.assertEqual(said, {
            "12 hours": "Using Beta's weekly allowance before it resets in 12h 0m (60% left).",
            "2 days": "Using Beta's weekly allowance before it resets in 2d 0h (60% left).",
            "3 days 5 hours": "Using Beta's weekly allowance before it resets in 3d 5h (60% left).",
        })


class AutoSwitchWordsTest(unittest.TestCase):
    """The words the rule gives the dashboard, and the order it lists the accounts in."""
    maxDiff = None

    def result(self, row, weekly=30, reset=3 * DAY):
        return decide(row, (weekly, "", reset))

    def test_reasons_and_holds(self):
        said = {
            "threshold": self.result("Beta has room", weekly=96)["reason"],
            "out": self.result("Beta has room", weekly=100)["reason"],
            "marked out": self.result("Alpha is marked out")["reason"],
            "Fable threshold": self.result("Alpha's Fable limit is at 96%")["reason"],
            "Fable out": self.result("Alpha's Fable limit is at 100%")["reason"],
            "Fable forecast": self.result("Alpha's Fable limit is forecast at 97%")["reason"],
            "Fable has refilled": self.result("Alpha's Fable limit is at 96%", reset=-40)["hold"],
            "login": self.result("Alpha needs sign-in")["reason"],
            "forecast": self.result("Alpha's weekly is forecast at 97%")["reason"],
            "drain": self.result("Beta's weekly is at 90%", weekly=99)["reason"],
            "its week alone": self.result("Beta's Fable limit is at 100%", weekly=100)["reason"],
            "its week alone, sign-in": self.result("Beta's Fable limit is at 100%, Alpha needs sign-in")["reason"],
            "drain beside an old reading": self.result("Beta is 20 min old, Gamma at 90%", weekly=100)["reason"],
            "drain beside two old readings": cli_policy.evaluate(
                [account("alpha", 100, active=True), beta(age=1200), account("delta", 30, age=1200), account("gamma", 90, 2 * DAY)],
                SETTINGS, {}, NOW)["reason"],
            "drain beside four old readings": cli_policy.evaluate(
                [account("alpha", 100, active=True), beta(age=1200), account("gamma", 90, 2 * DAY)]
                + [account(key, 30, age=1200) for key in ("delta", "epsilon", "zeta")], SETTINGS, {}, NOW)["reason"],
            "early": self.result("Beta's week ends in 12 h")["reason"],
            "has refilled": self.result("Beta has room", weekly=96, reset=-40)["hold"],
            "checking": self.result("Beta's reading is 20 min old", weekly=96)["hold"],
            "using up": self.result("Beta's weekly is at 90%", weekly=96)["hold"],
            "login expired": self.result("Alpha's login expired while idle")["hold"],
            "early too recent": self.result("Beta's week ends in 12 h, early switch 1 h ago")["hold"],
            "manual choice": self.result("Undo pressed 5 s ago")["hold"],
        }
        self.assertEqual(said, {
            "threshold": "Alpha reached 95% of its weekly limit; Beta is first in your eligible order.",
            "out": "Alpha reached its weekly limit; Beta is first in your eligible order.",
            "marked out": "Alpha reached a limit; Beta is first in your eligible order.",
            "Fable threshold": "Alpha reached 95% of its Fable weekly limit; Beta is first in your eligible order.",
            "Fable out": "Alpha reached its Fable weekly limit; Beta is first in your eligible order.",
            "Fable forecast": "Alpha will pass 95% of its Fable weekly limit before the next check; Beta is first in your eligible order.",
            "Fable has refilled": "Alpha's Fable weekly limit has refilled. Waiting for its new reading.",
            "login": "Alpha needs sign-in; Beta is first in your eligible order.",
            "forecast": "Alpha will pass 95% of its weekly limit before the next check; Beta is first in your eligible order.",
            "drain": "Alpha reached 99%; every account is past 95%, so Beta is used up to 99%.",
            "its week alone": "Alpha reached its weekly limit; Beta's week can still be used, though its Fable weekly limit is at 100%.",
            "its week alone, sign-in": "Alpha needs sign-in; Beta's week can still be used, though its Fable weekly limit is at 100%.",
            "drain beside an old reading": "Alpha reached its weekly limit; Beta needs a new reading first, so Gamma is used up to 99%.",
            "drain beside two old readings": "Alpha reached its weekly limit; Beta and Delta need a new reading first, so Gamma is used up to 99%.",
            # every account that waits is counted (found by the fourth review: the fourth name was dropped unsaid)
            "drain beside four old readings": "Alpha reached its weekly limit; Beta, Delta, Epsilon and 1 more need a new reading first, so Gamma is used up to 99%.",
            "early": "Using Beta's weekly allowance before it resets in 12h 0m (50% left).",
            "has refilled": "Alpha's weekly limit has refilled. Waiting for its new reading.",
            "checking": "Checking Beta before switching",
            "using up": "Every account is past 95% — using Alpha up to 99%",
            "login expired": "Usage updates after your next message in Claude Code",
            "early too recent": "An early switch happened less than 2 hours ago",
            "manual choice": "Your manual choice is held for 2 hours",
        })

    def test_checking_asks_for_the_old_readings(self):
        self.assertEqual(self.result("Beta is 20 min old, Gamma at 90%", weekly=96)["refresh"], ["beta"])
        # the last resorts too: an account past the threshold, and one used for its week alone
        for row, weekly in (("Beta's weekly is at 90%, its reading 20 min old", 99),
                            ("Beta's Fable limit is at 100%, its reading 20 min old", 100)):
            with self.subTest(row):
                result = self.result(row, weekly=weekly)
                self.assertEqual((result["action"], result["hold"], result["refresh"]), ("none", "Checking Beta before switching", ["beta"]))

    def test_a_refilled_limit_asks_for_a_new_reading_of_the_account_in_use(self):
        self.assertEqual(self.result("Beta has room", weekly=100, reset=-1)["refresh"], ["alpha"])

    def test_all_low_says_who_refills_first(self):
        result = self.result("Beta's weekly is at 98%", weekly=99, reset=2 * DAY)
        self.assertEqual((result["action"], result["exhausted"]), ("none", True))
        self.assertRegex(result["reason"], r"^All terminal accounts are low\. Alpha refills first, [A-Z][a-z]{2} \d{1,2}:\d\d [AP]M\.$")
        soon = self.result("Beta's weekly is at 98%", weekly=99, reset=5 * HOUR)["reason"]
        self.assertRegex(soon, r"^All terminal accounts are low\. Alpha refills first, \d{1,2}:\d\d [AP]M\.$")

    def test_the_order_and_why_each_account_is_skipped(self):
        others = [beta(), gamma(), account("delta", 90), account("epsilon", 30, age=1200),
                  account("zeta", 30, health="needs_signin"), account("eta", 30, excluded=True),
                  account("theta", 30, age=None), account("iota", None)]
        active = account("alpha", 30, active=True)

        def order(**settings):
            result = cli_policy.evaluate([active] + copy.deepcopy(others), {**SETTINGS, **settings}, {}, NOW)
            return [(row["id"], row["skip"], row["room"]) for row in result["next_up"]]

        self.assertEqual(order(), [
            ("gamma", None, 45), ("alpha", "in use by your terminals", 65), ("beta", None, 65),
            ("epsilon", "reading 20 min old", 65), ("eta", "left out", 65), ("theta", "reading unavailable", 65),
            ("zeta", "needs sign-in", 65), ("delta", "weekly at 90%", 5), ("iota", "weekly reading unknown", None)])
        # Gamma's week ends a day before the others'. With the early switch on the terminals go there next under
        # every order, so it is first in each. With it off the order alone decides.
        most_room = ["alpha", "beta", "epsilon", "eta", "theta", "zeta", "gamma", "delta", "iota"]
        self.assertEqual([key for key, _skip, _room in order(cli_order="most_room")], ["gamma"] + [key for key in most_room if key != "gamma"])
        self.assertEqual([key for key, _skip, _room in order(cli_order="most_room", cli_early_switch=False)], most_room)
        self.assertEqual([key for key, _skip, _room in order(cli_order="manual", cli_manual_order=["iota", "beta"])][:3],
                         ["gamma", "iota", "beta"])
        self.assertEqual([key for key, _skip, _room in order(cli_order="manual", cli_manual_order=["iota", "beta"],
                                                             cli_early_switch=False)][:3], ["iota", "beta", "gamma"])
        self.assertEqual(order(cli_early_switch=False), order())
        self.assertEqual(set(cli_policy.evaluate([active], SETTINGS, {}, NOW)["next_up"][0]),
                         {"id", "label", "skip", "room", "weekly_reset"})


class FableLimitTest(unittest.TestCase):
    """Auto-switch acts on the Fable weekly limit with settings as a new install has them (Isaac's
    decision, 2026-11-13). Settings and the notch on the bar had said so all along, and the rule had acted on
    it only for a `pinned_model` no control sets."""
    WEEK = "2027-01-20T00:00:00Z"

    def terminal(self, key, answer, *, active=False):
        return {"id": key, "label": key.capitalize(), "provider": "claude_cli", "active": active, "health": "ok",
                "last_good_at": NOW - 60, "windows": claude_usage(answer, now=NOW)["windows"]}

    def answers(self, weekly, fable):
        """Claude's answer in both shapes it has had, with these percents used."""
        named = {"seven_day": {"utilization": weekly, "resets_at": self.WEEK},
                 "seven_day_fable": {"utilization": fable, "resets_at": self.WEEK}}
        listed = {"limits": [{"kind": "weekly_all", "percent": weekly, "resets_at": self.WEEK},
                             {"kind": "weekly_scoped", "percent": fable, "resets_at": self.WEEK,
                              "scope": {"model": {"display_name": "Fable 5.1"}}}]}
        return (("named keys", named), ("limits list", listed))

    def test_it_leaves_an_account_whose_fable_limit_passed_the_threshold_in_both_shapes_of_claudes_answer(self):
        for (shape, over), (_, roomy) in zip(self.answers(40, 96), self.answers(30, 30)):
            with self.subTest(shape=shape):
                alpha, beta = self.terminal("alpha", over, active=True), self.terminal("beta", roomy)
                self.assertEqual([name for name, _used, _window in cli_policy.considered(alpha, SETTINGS)],
                                 ["weekly", "Fable weekly"])
                result = cli_policy.evaluate([alpha, beta], SETTINGS, {}, NOW)
                self.assertEqual((result["action"], result["target"], result["kind"]), ("switch", "beta", "threshold"))
                self.assertEqual(result["reason"], "Alpha reached 95% of its Fable weekly limit; Beta is first in your eligible order.")

    def test_it_never_moves_to_an_account_whose_fable_limit_is_out(self):
        for (shape, low), (_, out), (_, roomy) in zip(self.answers(96, 10), self.answers(20, 100), self.answers(60, 30)):
            with self.subTest(shape=shape):
                result = cli_policy.evaluate([self.terminal("alpha", low, active=True), self.terminal("beta", out),
                                              self.terminal("gamma", roomy)], {**SETTINGS, "cli_order": "most_room"}, {}, NOW)
                self.assertEqual((result["action"], result["target"]), ("switch", "gamma"))
                self.assertEqual([(row["id"], row["skip"], row["room"]) for row in result["next_up"]],
                                 [("gamma", None, 35), ("alpha", "in use by your terminals", -1),
                                  ("beta", "Fable weekly at 100%", -5)])

    def test_an_account_that_works_for_no_model_is_left_for_one_whose_week_still_can_be_used(self):
        """Found by the review of this change. The rule refused every account whose Fable limit was out, so
        an account in use whose week ran out, or that needed sign-in, kept the terminals while another account
        had most of its week left. The rule that looked at the week alone moved them."""
        for (shape, dead), (_, fable_out), (_, fine) in zip(self.answers(100, 10), self.answers(20, 100), self.answers(30, 10)):
            with self.subTest(shape=shape, why="its week ran out"):
                result = cli_policy.evaluate([self.terminal("alpha", dead, active=True), self.terminal("beta", fable_out)],
                                             SETTINGS, {}, NOW)
                self.assertEqual((result["action"], result["target"], result["kind"]), ("switch", "beta", "out"))
            with self.subTest(shape=shape, why="it needs sign-in"):
                alpha = dict(self.terminal("alpha", fine, active=True), health="needs_signin")
                result = cli_policy.evaluate([alpha, self.terminal("beta", fable_out)], SETTINGS, {}, NOW)
                self.assertEqual((result["action"], result["target"], result["kind"]), ("switch", "beta", "login"))
            with self.subTest(shape=shape, why="the account that works for no model is never moved back to"):
                result = cli_policy.evaluate([self.terminal("alpha", dead), self.terminal("beta", fable_out, active=True)],
                                             SETTINGS, {}, NOW)
                self.assertEqual((result["action"], result["exhausted"]), ("none", True))

    def test_an_account_is_used_for_its_week_alone_only_when_its_reading_says_so(self):
        """ "I cannot tell" is never "it is fine". The account in use works for no model, and still the rule
        does not move to an account whose Fable number is missing, whose week is past using up, that is
        left out, that needs sign-in or that was never read."""
        dead = account("alpha", 100, active=True)

        def answer(**changes):
            other = beta(model=100, **changes)
            result = cli_policy.evaluate([dead, other], SETTINGS, {}, NOW)
            return result["action"], result["exhausted"]

        self.assertEqual(answer(), ("switch", False), "the case the others are compared with")
        unknown = beta(model=50)
        unknown["windows"][1]["used"] = None
        result = cli_policy.evaluate([dead, unknown], SETTINGS, {}, NOW)
        self.assertEqual((result["action"], result["exhausted"], result["next_up"][-1]["skip"]), ("none", True, "reading incomplete"))
        for name, changes in (("its week is at 98%", dict(weekly=98)), ("left out", dict(excluded=True)),
                              ("needs sign-in", dict(health="needs_signin")), ("never read", dict(age=None)),
                              ("its week is unknown", dict(weekly=None))):
            with self.subTest(name):
                self.assertEqual(answer(**changes), ("none", True))
        hour_old = cli_policy.evaluate([dead, beta(model=100, age=3600)], SETTINGS, {}, NOW)
        self.assertEqual((hour_old["action"], hour_old["exhausted"], hour_old["hold"], hour_old["refresh"]),
                         ("none", False, "Checking Beta before switching", ["beta"]), "an old reading is read again first")
        blocked = beta(model=100)
        blocked["windows"][0]["blocked"] = "limit"
        result = cli_policy.evaluate([dead, blocked], SETTINGS, {}, NOW)
        self.assertEqual((result["action"], result["exhausted"]), ("none", True), "a week Claude has blocked")

    def test_accounts_used_for_their_week_alone_are_ordered_by_their_week(self):
        """Found by the second review of this change. An account's room is the least room of its limits, so for
        these accounts it was the room of the limit that is used up. "Most room first" then picked the account
        with 1 point of its week left before the one with 92, and switched again soon after."""
        nearly_out, roomy = beta(weekly=96, model=98), account("gamma", 5, 3 * DAY, model=100)

        def first(**settings):
            result = cli_policy.evaluate([account("alpha", 100, active=True), copy.deepcopy(nearly_out), copy.deepcopy(roomy)],
                                         {**SETTINGS, **settings}, {}, NOW)
            return result["action"], result["target"]
        self.assertEqual(first(cli_order="most_room"), ("switch", "gamma"))
        self.assertEqual(first(cli_order="soonest_reset"), ("switch", "gamma"), "their weeks end together, so the room in the week decides")
        self.assertEqual(first(cli_order="manual", cli_manual_order=["beta", "gamma"]), ("switch", "beta"), "the user's own order stands")
        sooner = cli_policy.evaluate([account("alpha", 100, active=True), copy.deepcopy(nearly_out), account("gamma", 5, 5 * DAY, model=100)],
                                     SETTINGS, {}, NOW)
        self.assertEqual((sooner["action"], sooner["target"]), ("switch", "beta"), "the week that ends first, as for every account")

    def test_an_old_reading_does_not_keep_an_account_with_a_new_one_waiting(self):
        """Found by the second review of this change. Beta is first in the user's order and its reading is an
        hour old, so it has to be read again. While its reads failed, the rule asked for Beta at every check
        and never moved to Gamma, which had a reading 30 seconds old. The same held for two accounts past the
        threshold, in the installed build too."""
        mine = {**SETTINGS, "cli_order": "manual", "cli_manual_order": ["beta", "gamma"]}
        for name, others in (("used for their week alone", dict(model=100)), ("past the threshold", dict(weekly=90))):
            with self.subTest(name):
                old, new = beta(age=3600, **others), dict(beta(age=30, **others), id="gamma", label="Gamma")
                result = cli_policy.evaluate([account("alpha", 100, active=True), old, new], mine, {}, NOW)
                self.assertEqual([row["id"] for row in result["next_up"]][:2], ["beta", "gamma"])
                self.assertEqual((result["action"], result["target"], result["kind"]), ("switch", "gamma", "out"))
                both_old = cli_policy.evaluate([account("alpha", 100, active=True), old, dict(new, last_good_at=NOW - 3600)], mine, {}, NOW)
                self.assertEqual((both_old["action"], both_old["hold"], both_old["refresh"]),
                                 ("none", "Checking Beta, Gamma before switching", ["beta", "gamma"]))

    def test_an_account_that_works_for_no_model_does_not_wait_for_a_reading(self):
        """Found by the second review of this change. Beta has room and a reading an hour old, so it is read
        again. While the account in use still works, that is worth waiting for. Once it works for no model,
        the rule used to wait all the same, for as long as Beta's reads failed, with Gamma ready to use."""
        for name, ready in (("an account used for its week alone", dict(model=100)), ("an account past the threshold", dict(weekly=90))):
            with self.subTest(name):
                others = [beta(age=3600), dict(beta(**ready), id="gamma", label="Gamma")]
                dead = cli_policy.evaluate([account("alpha", 100, active=True)] + copy.deepcopy(others), SETTINGS, {}, NOW)
                self.assertEqual((dead["action"], dead["target"], dead["kind"]), ("switch", "gamma", "out"))
                works = cli_policy.evaluate([account("alpha", 99, active=True)] + copy.deepcopy(others), SETTINGS, {}, NOW)
                self.assertEqual((works["action"], works["hold"], works["refresh"]), ("none", "Checking Beta before switching", ["beta"]))
        alone = cli_policy.evaluate([account("alpha", 100, active=True), beta(age=3600)], SETTINGS, {}, NOW)
        self.assertEqual((alone["action"], alone["hold"], alone["refresh"]), ("none", "Checking Beta before switching", ["beta"]))
        # and from Gamma the rule still waits for Beta, the better account, and moves there once it is read
        gamma_in_use = dict(beta(model=100), id="gamma", label="Gamma", active=True)
        there = cli_policy.evaluate([account("alpha", 100), beta(age=3600), gamma_in_use], SETTINGS, {}, NOW)
        self.assertEqual((there["action"], there["hold"], there["refresh"]), ("none", "Checking Beta before switching", ["beta"]))
        read = cli_policy.evaluate([account("alpha", 100), beta(), gamma_in_use], SETTINGS, {}, NOW)
        self.assertEqual((read["action"], read["target"]), ("switch", "beta"))

    def test_a_setting_that_pins_fable_finds_the_limit_under_both_of_its_names(self):
        """Found by the second review of this change. With the list's entry in place of the named key's, the
        limit's model reads "Fable 5.1", and a `pinned_model` of "Fable" no longer matched it."""
        from accountdeck import rules

        def gating(model, pinned):
            return rules.is_gating({"primary": True, "used": 10.0, "severity": "plenty", "model": model}, pinned)
        self.assertEqual([gating(model, pinned) for model in ("Fable", "Fable 5.1") for pinned in ("Fable", "fable", "Fable 5.1")], [True] * 6)
        self.assertEqual([gating("Sonnet", "Fable"), gating("Fable 5.1", "Sonnet"), gating("Fable 5.1", ""), gating("Sonnet", "sonnet")],
                         [False, False, False, True])

    def test_a_setting_that_pins_fable_marks_the_limit_on_the_page_under_both_of_its_names(self):
        """The same, the way a user sees it: `intel.build_account` marks the limit a row is judged on (`binding`),
        and `rules.headroom` is what the recommender ranks by. A Fable limit under 50% used counts for both
        only when Fable is the pinned model: with no pinned model nothing here is past 50%, and no limit is marked.
        Asked for by the third review of this change."""
        from accountdeck import intel, rules
        for shape, answer in self.answers(20, 40):
            for pinned, marked, left in (("Fable", ["model"], 60.0), ("", [], 80.0)):
                with self.subTest(shape=shape, pinned_model=pinned):
                    saved = {"id": "alpha", "provider": "claude_cli", "usage": claude_usage(answer, now=NOW), "last_good_at": NOW - 60}
                    built = intel.build_account(saved, now=NOW, pinned=pinned)
                    self.assertEqual([w["kind"] for w in built["windows"] if w.get("binding")], marked)
                    self.assertEqual(rules.headroom(built, pinned), left)

    def test_a_reading_counts_as_old_after_15_minutes(self):
        """Found by the third review of this change. Every old reading in these tests was 20 or 60 minutes old,
        so no test held the 15 minutes that the rule's header and the README name."""
        kinds = (("with room", 96, dict(weekly=30)), ("past the threshold", 99, dict(weekly=90)),
                 ("used for its week alone", 100, dict(weekly=30, model=100)))
        for name, in_use, other in kinds:
            for age, want in ((900, ("switch", [])), (901, ("none", ["beta"]))):
                with self.subTest(f"an account {name}", age=age):
                    result = cli_policy.evaluate([account("alpha", in_use, active=True), account("beta", age=age, **other)],
                                                 SETTINGS, {}, NOW)
                    self.assertEqual((result["action"], result["refresh"]), want)

    def test_an_account_that_still_works_is_not_left_for_one_whose_fable_limit_is_out(self):
        """The other side of the same rule. At 99% of its week the account in use still answers, for every
        model. Moving to an account that has lost Fable would take that model away early."""
        for weekly, fable in ((99, 10), (30, 100), (30, 99)):
            with self.subTest(weekly=weekly, fable=fable):
                result = cli_policy.evaluate([account("alpha", weekly, active=True, model=fable), beta(model=100)], SETTINGS, {}, NOW)
                self.assertEqual((result["action"], result["exhausted"]), ("none", True))

    def test_the_limit_is_named_fable_whatever_claudes_answer_calls_the_model(self):
        """Isaac, 2026-11-14: it is a Fable limit, not Fable 5.1's. Older builds gave the named key's
        limit the name "Fable 5.1" and kept the version a list entry put in the model's name. Claude's own
        name for the model, and the id the limit's samples are saved under, stay as they were."""
        for shape, answer in self.answers(40, 20):
            with self.subTest(shape=shape):
                fable = claude_usage(answer, now=NOW)["windows"][1]
                self.assertEqual((fable["label"], fable["short"]), ("Fable weekly", "Fable"))
                self.assertEqual((fable["id"], fable["model"], fable["scope"]),
                                 ("seven_day_fable", "Fable", "Fable") if shape == "named keys"
                                 else ("seven_day_fable_5_1", "Fable 5.1", "Fable 5.1"))
        sonnet = claude_usage({"seven_day_sonnet": {"utilization": 20, "resets_at": self.WEEK}}, now=NOW)["windows"][0]
        self.assertEqual((sonnet["label"], sonnet["short"]), ("Sonnet weekly", "Sonnet"), "another model keeps its name")
        # A later version in the list's name is the same limit, so the named key's number is not a second one.
        named, listed = self.answers(40, 100)[0][1], self.answers(40, 20)[1][1]
        listed["limits"][1]["scope"]["model"]["display_name"] = "Fable 5.2"
        self.assertEqual([(w["id"], w["label"], w["used"]) for w in claude_usage({**named, **listed}, now=NOW)["windows"]],
                         [("seven_day", "Weekly", 40), ("seven_day_fable_5_2", "Fable weekly", 20)])

    def test_a_reading_an_older_build_saved_shows_the_limit_under_its_name_now(self):
        """Until an account is read again, the saved reading is all the app shows of it, and an account that
        needs sign-in is not read again. The saved data stays as it is."""
        from accountdeck.usage import saved_reading
        for name, saved, ids in (("the named key", support.shipped_reading(NOW, 10.0, 40.0, 96.0), ["seven_day", "seven_day_fable"]),
                                 ("both names", support.shipped_both_names(NOW, 40.0, 100.0, 20.0), ["seven_day", "seven_day_fable_5_1"])):
            with self.subTest(name):
                before = copy.deepcopy(saved)
                self.assertIn("Fable 5.1 weekly", [w["label"] for w in saved["windows"]], "the older build's name")
                shown = saved_reading("claude_cli", saved)
                self.assertEqual([(w["id"], w["label"], w["short"]) for w in shown["windows"]],
                                 list(zip(ids, ("Weekly", "Fable weekly"), ("7d", "Fable"))))
                self.assertEqual(saved, before, "the saved reading itself is not changed")
                self.assertIs(saved_reading("claude_cli", shown), shown, "a reading under the name now is returned as it is")
                account = {"id": "alpha", "label": "Alpha", "provider": "claude_cli", "active": True, "health": "ok",
                           "last_good_at": NOW - 60, "windows": shown["windows"]}
                self.assertEqual([name for name, _used, _window in cli_policy.considered(account, SETTINGS)],
                                 ["weekly", "Fable weekly"])
        codex = support.shipped_reading(NOW, 10.0, 40.0, 96.0)
        self.assertIs(saved_reading("codex", codex), codex, "only Claude's readings")

    def test_the_menu_bar_calls_the_room_fable(self):
        from accountdeck import pool
        totals = pool.summarize({"accounts": [self.terminal(key, answer, active=key == "named keys")
                                              for key, answer in self.answers(40, 50)]}, NOW)
        lines = pool.tooltip_lines(totals, NOW)
        self.assertEqual(lines[2], "Fable 1.0 of 2 accounts (50%)")
        self.assertEqual([line for line in lines if line.startswith("Named keys")],
                         ["Named keys (in use): weekly 60% · Fable 50% left"])

    def both_names(self, weekly, old_number, current):
        """One answer that carries the Fable limit under its named key and in its list, with numbers that differ."""
        return {**self.answers(weekly, old_number)[0][1], **self.answers(weekly, current)[1][1]}

    def test_an_answer_that_names_the_fable_limit_twice_gives_one_limit(self):
        """The list replaces the named key, as it does for the week. "Fable 5.1" in the list and the bare
        `seven_day_fable` key are one limit (`usage.model_label`), and the review of this change found them
        kept as two: the number from the named key could move the terminals or refuse a target. The list's
        entry keeps the id it always had, so its saved samples stay one series."""
        reading = claude_usage(self.both_names(40, 100, 20), now=NOW)
        self.assertEqual([(w["id"], w["kind"], w["label"], w["used"]) for w in reading["windows"]],
                         [("seven_day", "weekly", "Weekly", 40), ("seven_day_fable_5_1", "model", "Fable weekly", 20)])
        for shape, answer in self.answers(40, 20):
            with self.subTest(shape=shape, said="once"):
                self.assertEqual([w["id"] for w in claude_usage(answer, now=NOW)["windows"]],
                                 ["seven_day", "seven_day_fable" if shape == "named keys" else "seven_day_fable_5_1"])
        _, roomy = self.answers(30, 30)[0]
        stays = cli_policy.evaluate([self.terminal("alpha", self.both_names(40, 100, 20), active=True), self.terminal("beta", roomy)],
                                    SETTINGS, {}, NOW)
        self.assertEqual((stays["action"], stays["hold"]), ("none", None))
        _, over = self.answers(96, 10)[0]
        moves = cli_policy.evaluate([self.terminal("alpha", over, active=True), self.terminal("beta", self.both_names(20, 100, 20))],
                                    SETTINGS, {}, NOW)
        self.assertEqual((moves["action"], moves["target"]), ("switch", "beta"))

    def test_two_entries_of_one_model_in_claudes_list_are_two_limits(self):
        """Found by the third review of this change. Only a named key's limit is a second name of a list
        entry. Two list entries for one model with numbers of their own are kept as two, as before. The first
        of them has the id a named key would have, and was taken for one and left out."""
        later = "2027-01-27T00:00:00Z"
        twice = [{"kind": "weekly_scoped", "percent": 100, "resets_at": self.WEEK, "scope": {"model": {"display_name": "Fable"}}},
                 {"kind": "weekly_scoped", "percent": 20, "resets_at": later, "scope": {"model": {"display_name": "Fable"}}}]
        week = {"seven_day": {"utilization": 40, "resets_at": self.WEEK}}
        for name, answer in (("the list alone", {**week, "limits": twice}),
                             ("with the named key too", {**week, "seven_day_fable": {"utilization": 55, "resets_at": self.WEEK}, "limits": twice})):
            with self.subTest(name):
                reading = claude_usage(answer, now=NOW)
                self.assertEqual([(w["id"], w["used"]) for w in reading["windows"]],
                                 [("seven_day", 40), ("seven_day_fable", 100), ("seven_day_fable_2", 20)])
                alpha = self.terminal("alpha", answer, active=True)
                self.assertEqual([used for _name, used, _window in cli_policy.considered(alpha, SETTINGS)], [40, 100, 20])
        # The same once the reading is saved, whatever the two entries call the model (found by the fourth
        # review: "Fable" beside "fable" or "Fable 5.1" was taken for a named key beside its list entry).
        from accountdeck.usage import saved_reading
        for names in (("Fable", "Fable"), ("Fable", "fable"), ("Fable", "Fable 5.1")):
            with self.subTest("saved", names=names):
                listed = [dict(entry, scope={"model": {"display_name": name}}) for entry, name in zip(twice, names)]
                saved = copy.deepcopy(claude_usage({**week, "limits": listed}, now=NOW))
                self.assertEqual([w["used"] for w in saved["windows"]], [40, 100, 20])
                self.assertIs(saved_reading("claude_cli", saved), saved)

    def test_a_saved_reading_that_names_the_fable_limit_twice_gives_one_limit(self):
        """Data an older build wrote. It saved the listed limit beside the named key's. The named key's is left
        out when the reading is read, and no id changes."""
        from accountdeck.usage import saved_reading
        saved = support.shipped_both_names(NOW, 40.0, 100.0, 20.0)
        before = copy.deepcopy(saved)
        def kept(reading):
            return [(w["id"], w["used"]) for w in saved_reading("claude_cli", reading)["windows"]]
        self.assertEqual(kept(saved), [("seven_day", 40.0), ("seven_day_fable_5_1", 20.0)])
        self.assertEqual(saved, before, "the saved reading itself is not changed")
        week, fable, listed = saved["windows"][1:]
        raw = {entry["key"]: entry for entry in saved["raw_windows"]}
        for name, windows, keys in (("the list's entry alone", [week, listed], ("limits[0].weekly_all", "limits[1].weekly_scoped.fable_5_1")),
                                    ("the named key alone", [week, fable], ("seven_day", "seven_day_fable"))):
            with self.subTest(name):
                alone = {"checked": NOW, "windows": windows, "raw_windows": [raw[key] for key in keys]}
                self.assertEqual(kept(alone), [(w["id"], w["used"]) for w in windows])
        sonnet = dict(fable, id="seven_day_sonnet", model="Sonnet", scope="Sonnet", label="Sonnet weekly")
        other = {"checked": NOW, "windows": [week, sonnet, listed],
                 "raw_windows": [raw["seven_day"], dict(raw["seven_day_fable"], key="seven_day_sonnet"), raw["limits[1].weekly_scoped.fable_5_1"]]}
        self.assertEqual(kept(other), [(w["id"], w["used"]) for w in other["windows"]], "another model's named key is another limit")
        unsaid = {"checked": NOW, "windows": [week, fable, listed]}
        self.assertEqual(kept(unsaid), [(w["id"], w["used"]) for w in unsaid["windows"]],
                         "a reading that does not say where a number came from keeps it")
        self.assertIs(saved_reading("codex", saved), saved, "only Claude's readings")

    def test_the_menu_bar_counts_the_fable_limit_in_both_shapes(self):
        from accountdeck import pool
        totals = pool.summarize({"accounts": [self.terminal(key, answer, active=key == "named keys")
                                              for key, answer in self.answers(40, 50)]}, NOW)
        self.assertEqual((totals.capacity, totals.fable_capacity, pool.amount(totals.fable)), (2, 2, "1.0"))

    def test_an_account_with_no_fable_reading_is_judged_on_its_week(self):
        """The limit appears in Claude's answer after the first Fable message. Until then there is nothing to act on."""
        week_only = {"seven_day": {"utilization": 30, "resets_at": self.WEEK}}
        _, over = self.answers(96, 10)[0]
        result = cli_policy.evaluate([self.terminal("alpha", over, active=True), self.terminal("beta", week_only)], SETTINGS, {}, NOW)
        self.assertEqual((result["action"], result["target"]), ("switch", "beta"))
        stays = cli_policy.evaluate([self.terminal("beta", week_only, active=True), self.terminal("alpha", over)], SETTINGS, {}, NOW)
        self.assertEqual((stays["action"], stays["hold"]), ("none", None))

    def test_another_models_limit_is_not_acted_on(self):
        sonnet_out = {"seven_day": {"utilization": 30, "resets_at": self.WEEK},
                      "seven_day_sonnet": {"utilization": 100, "resets_at": self.WEEK}}
        _, roomy = self.answers(30, 30)[0]
        alpha = self.terminal("alpha", sonnet_out, active=True)
        self.assertEqual([name for name, _used, _window in cli_policy.considered(alpha, SETTINGS)], ["weekly"])
        result = cli_policy.evaluate([alpha, self.terminal("beta", roomy)], SETTINGS, {}, NOW)
        self.assertEqual((result["action"], result["hold"]), ("none", None))
        pinned = cli_policy.considered(alpha, {**SETTINGS, "pinned_model": "sonnet"})
        self.assertEqual([name for name, _used, _window in pinned], ["weekly", "Sonnet weekly"], "a pinned model still counts")

    def test_a_pinned_models_limit_moves_the_terminals_and_refuses_a_target(self):
        """`pinned_model` has no control, and a settings bundle can still bring it. The tests that covered it
        pinned Fable, which now counts without the pin (found by the review of this change)."""
        def sonnet(key, used, *, active=False):
            answer = {"seven_day": {"utilization": 30, "resets_at": self.WEEK},
                      "seven_day_sonnet": {"utilization": used, "resets_at": self.WEEK}}
            return self.terminal(key, answer, active=active)

        def decide(used, other=30, history=None, pinned="sonnet"):
            result = cli_policy.evaluate([sonnet("alpha", used, active=True), sonnet("beta", other)],
                                         {**SETTINGS, "pinned_model": pinned}, history or {}, NOW)
            return result["action"], result["kind"], result["hold"], result["exhausted"]

        self.assertEqual(decide(96), ("switch", "threshold", None, False))
        self.assertEqual(decide(100), ("switch", "out", None, False))
        self.assertEqual(decide(96, other=100), ("none", None, "Every account is past 95% — using Alpha up to 99%", False))
        self.assertEqual(decide(100, other=100), ("none", None, None, True), "its own limit is out too: nowhere to go")
        self.assertEqual(decide(100, history={"manual_at": NOW - 5}), ("none", None, "Your manual choice is held for 2 hours", False))
        self.assertEqual(decide(100, other=100, pinned=""), ("none", None, None, False), "not pinned: not acted on")

    def test_a_setting_that_pins_fable_counts_the_limit_once(self):
        """Data an older version wrote: `pinned_model` could come in with a settings bundle."""
        _, over = self.answers(40, 96)[0]
        alpha = self.terminal("alpha", over, active=True)
        for pinned in ("Fable", "fable"):
            with self.subTest(pinned_model=pinned):
                self.assertEqual([name for name, _used, _window in cli_policy.considered(alpha, {**SETTINGS, "pinned_model": pinned})],
                                 ["weekly", "Fable weekly"])

    def test_a_manual_choice_holds_against_a_fable_limit_that_is_out_but_not_against_the_week(self):
        """With its Fable limit out an account still works for the other models, so the user's own choice
        of it stands for the 2 hours. An account whose week is out works for nothing."""
        chosen = {"manual_at": NOW - 5}
        _, roomy = self.answers(30, 30)[0]
        for name, (weekly, fable), want in (("the Fable limit is out", (40, 100), ("none", None, "Your manual choice is held for 2 hours")),
                                            ("the week is out", (100, 100), ("switch", "beta", None))):
            with self.subTest(name):
                _, answer = self.answers(weekly, fable)[0]
                result = cli_policy.evaluate([self.terminal("alpha", answer, active=True), self.terminal("beta", roomy)],
                                             SETTINGS, dict(chosen), NOW)
                self.assertEqual((result["action"], result["target"], result["hold"]), want)
        later = cli_policy.evaluate([self.terminal("alpha", self.answers(40, 100)[0][1], active=True), self.terminal("beta", roomy)],
                                    SETTINGS, {"manual_at": NOW - 2 * HOUR - 5}, NOW)
        self.assertEqual((later["action"], later["target"], later["kind"]), ("switch", "beta", "out"), "the hold ends after 2 hours")


class ClaudeHasNoFiveHourLimitTest(unittest.TestCase):
    """Claude dropped its 5-hour limit. The rule, the totals and the menu bar used to need a 5-hour number: with
    none in Claude's answer every other account was "reading incomplete", so auto-switch never switched and the
    menu bar counted no account."""
    WEEK = "2027-01-20T00:00:00Z"

    def terminal(self, key, weekly, *, active=False, five=None):
        answer = {"seven_day": {"utilization": weekly, "resets_at": self.WEEK}}
        if five is not None:
            answer["five_hour"] = {"utilization": five, "resets_at": "2027-01-15T10:00:00Z"}
        return {"id": key, "label": key.capitalize(), "provider": "claude_cli", "active": active, "health": "ok",
                "last_good_at": NOW - 60, "windows": claude_usage(answer, now=NOW)["windows"]}

    def test_an_answer_with_no_5_hour_number_still_switches(self):
        result = cli_policy.evaluate([self.terminal("alpha", 97, active=True), self.terminal("beta", 20),
                                      self.terminal("gamma", 35)], SETTINGS, {}, NOW)
        self.assertEqual([(row["id"], row["skip"], row["room"]) for row in result["next_up"]],
                         [("beta", None, 75), ("gamma", None, 60), ("alpha", "in use by your terminals", -2)])
        self.assertEqual((result["action"], result["target"]), ("switch", "beta"))
        self.assertEqual(result["reason"], "Alpha reached 95% of its weekly limit; Beta is first in your eligible order.")

    def test_a_5_hour_number_claude_still_sends_is_no_limit(self):
        old = {"five_hour": {"utilization": 99, "resets_at": "2027-01-15T10:00:00Z"},
               "seven_day": {"utilization": 20, "resets_at": self.WEEK}}
        new = {"limits": [{"kind": "session", "percent": 99, "resets_at": "2027-01-15T10:00:00Z"},
                          {"kind": "weekly_all", "percent": 20, "resets_at": self.WEEK}]}
        for shape, answer in (("named keys", old), ("limits list", new)):
            with self.subTest(shape=shape):
                reading = claude_usage(answer, now=NOW)
                self.assertEqual([(w["kind"], w["used"]) for w in reading["windows"]], [("weekly", 20)])
                self.assertEqual(sorted(raw["used"] for raw in reading["raw_windows"]), [20, 99])
        result = cli_policy.evaluate([self.terminal("alpha", 20, active=True, five=99), self.terminal("beta", 20, five=99)],
                                     SETTINGS, {}, NOW)
        self.assertEqual((result["action"], result["hold"], [row["skip"] for row in result["next_up"]]),
                         ("none", None, ["in use by your terminals", None]))

    def test_the_menu_bar_counts_an_account_by_its_weekly_number(self):
        from accountdeck import pool
        from accountdeck.platform import menubar
        state = {"accounts": [self.terminal("alpha", 97, active=True), self.terminal("beta", 20), self.terminal("gamma", 35)]}
        totals = pool.summarize(state, NOW)
        self.assertEqual((totals.capacity, totals.unread, pool.amount(totals.weekly)), (3, (), "1.5"))
        self.assertEqual(menubar.title_runs(state, NOW), (("wk ", "label"), ("1.5", "value")))
        self.assertEqual(menubar._terminals_line(totals), "Terminals: weekly 1.5 of 3 accounts")
        self.assertEqual(pool.tooltip_lines(totals, NOW)[:3], ["Claude Code terminals: room left in accounts' worth",
                                                               "Weekly 1.5 of 3 accounts (49%)",
                                                               "Next refill: Alpha +1.0 (weekly) in 4d 16h"])
        self.assertIn("Beta: weekly 80% left", pool.tooltip_lines(totals, NOW))

    def test_an_account_whose_week_has_not_started_can_be_the_best_pick(self):
        """Its only limit shows no number yet. That is a whole week left, not a reading nobody has."""
        from accountdeck import rules
        state = fixtures.example_state("not-started")
        beta = next(a for a in state["accounts"] if a["label"] == "Beta")
        self.assertEqual([(w["kind"], w["state"], w["left"]) for w in beta["windows"]], [("weekly", "not_started", None)])
        self.assertEqual((rules.headroom(beta, ""), rules.eligible(beta, "")), (100.0, True))
        best = state["now_band"]["best"]["claude_cli"]
        self.assertEqual((best["label"], best["reason"]), ("Beta", "Most headroom (100% left) and a fresh reading"))


class ReadingsTheShippedVersionSavedTest(unittest.TestCase):
    """The 2.0.0 build saved a Claude account's reading with a 5-hour limit in it. The new code opens that data
    as it is, and until the next read that saved reading is all it knows of the account. Found by the fourth
    review: the rule and the menu bar ignored the saved 5-hour limit while the page drew it and the best pick
    counted the account as out."""

    def test_a_saved_5_hour_limit_is_no_limit_of_a_claude_account(self):
        from accountdeck import rules
        engine = support.demo_engine(self, "typical")
        now = engine.clock()
        row = next(r for r in engine.store.accounts() if r["provider"] == "claude_cli"
                   and r["identity"] != engine.active_identity["claude_cli"])
        # before the first state is built: the engine keeps the state it built
        engine.store.observe(row["id"], support.shipped_reading(now, 100.0, 20.0), at=now)
        self.assertEqual([w["kind"] for w in engine.store.account(row["id"])["last_good_usage"]["windows"]], ["weekly"])
        account = shown(engine, row["id"])
        self.assertEqual([(w["kind"], w["used"]) for w in account["windows"]], [("weekly", 20.0)])
        self.assertEqual((account["state"], rules.eligible(account, "")), ("ok", True), account["headline"])

    def test_codex_and_muse_keep_a_saved_5_hour_limit(self):
        engine = support.demo_engine(self, "typical")
        for provider in ("codex", "muse"):
            with self.subTest(provider=provider):
                row = next(r for r in engine.store.accounts() if r["provider"] == provider)
                self.assertIn("session", [w["kind"] for w in engine.store.account(row["id"])["last_good_usage"]["windows"]])
                self.assertIn("session", [w["kind"] for w in shown(engine, row["id"])["windows"]])


# ---- readings, for all three clients ----
CLIENTS = ("claude_cli", "codex", "muse")
# For each client: the limit whose refill the test records, and one that keeps running. Claude has weekly limits only.
WINDOWS = {"claude_cli": ("seven_day", "seven_day_fable"),
           "codex": ("codex:primary", "codex:secondary"), "muse": ("muse:window", "muse:weekly")}


def answer(engine, provider, windows):
    """From now on `provider`'s synthetic adapter answers a usage read with these windows
    ([{"key", "used", "reset"}]), in the provider's own format."""
    payload = demo._payload(provider, windows)
    adapter = engine.providers[provider]
    if provider == "codex":
        adapter._probe = lambda work, identity, refresh: (copy.deepcopy(payload), None)
    elif provider == "muse":
        adapter._run_usage = lambda config_home, *, file_mode, isolated: copy.deepcopy(payload)
    else:
        adapter.usage = lambda credentials: claude_usage(copy.deepcopy(payload), now=engine.clock())


def signed_in(engine, provider):
    return next(row for row in engine.store.accounts()
                if row["provider"] == provider and row["identity"] == engine.active_identity[provider])


def shown(engine, key):
    return next(a for a in engine.snapshot()["accounts"] if a["id"] == key)


def sign_out(engine, provider):
    """What signing out of the client leaves: its login file is gone."""
    paths = engine.paths
    {"claude_cli": Path(paths.claude_cli_credentials), "codex": Path(paths.codex_home) / "auth.json",
     "muse": Path(paths.muse_config) / "auth.json"}[provider].unlink()
    engine.sense_active()


class RefillTest(unittest.TestCase):
    """A reading taken after a limit's reset time records the refill, shows the limit as refilled and raises the
    "limit refilled" alert, whichever client the account belongs to."""

    def refill_is_recorded(self, provider):
        engine = support.demo_engine(self, "typical")
        now, row = engine.clock(), signed_in(engine, provider)
        refilled, running = WINDOWS[provider]
        before = [{"key": refilled, "used": 92.0, "reset": now - 120}, {"key": running, "used": 40.0, "reset": now + 3 * DAY}]
        after = [{"key": refilled, "used": 3.0, "reset": now + 5 * DAY}, {"key": running, "used": 41.0, "reset": now + 3 * DAY}]
        engine.store.observe(row["id"], demo._usage(provider, demo._payload(provider, before), now - 600), at=now - 600)
        engine.alert_engine.hold_until = 0
        engine._evaluate_alerts(now)                              # the first pass only notes what already holds
        had, alerted = len(engine.store.refills(0)), {a["id"] for a in engine.store.alerts()}
        answer(engine, provider, after)
        self.assertEqual(engine._refresh(row["id"]), "ok")
        new = engine.store.refills(0)[had:]
        self.assertEqual([(r["account"], r["window"], r["previous_used"]) for r in new], [(row["id"], refilled, 92.0)])
        window = next(w for w in shown(engine, row["id"])["windows"] if w["id"] == refilled)
        self.assertEqual((window["state"], window["used"]), ("refilled", 3.0))
        engine._evaluate_alerts(now)
        self.assertEqual([(a["kind"], a["account"], a["window"]) for a in engine.store.alerts() if a["id"] not in alerted],
                         [("reset_confirmed", row["id"], refilled)])
        self.assertEqual(engine._refresh(row["id"]), "ok")       # the same refill is not recorded twice
        self.assertEqual(len(engine.store.refills(0)), had + 1)

    def test_claude_code(self):
        self.refill_is_recorded("claude_cli")

    def test_codex(self):
        self.refill_is_recorded("codex")

    def test_muse(self):
        self.refill_is_recorded("muse")


class MuseAnswerAgeTest(unittest.TestCase):
    """Consider 8. Muse dates its own answer (asOfMs). A cached answer is as old as Muse says, not as new as
    the read that fetched it."""

    def test_the_reading_is_dated_by_muse(self):
        def checked(as_of):
            return muse_usage({"allowance": {"window": {"usedPercent": 8.0, "resetsAtMs": (NOW + HOUR) * 1000}},
                               "asOfMs": as_of}, now=NOW)["checked"]

        self.assertEqual(checked((NOW - 1200) * 1000), NOW - 1200)                # a cached answer, 20 min old
        self.assertEqual([checked(value) for value in ((NOW + 60) * 1000, 0, -5, None, "soon", True)], [NOW] * 6)
        self.assertEqual(muse_usage({"allowance": {}}, now=NOW)["checked"], NOW)   # Muse 1.4.13 sends no asOfMs

    def test_a_cached_answer_is_stored_with_its_own_age(self):
        engine = support.demo_engine(self, "typical")
        row, now = signed_in(engine, "muse"), engine.clock()
        payload = demo._payload("muse", [{"key": "muse:window", "used": 8.0, "reset": now + HOUR},
                                         {"key": "muse:weekly", "used": 40.0, "reset": now + 3 * DAY}])
        payload["asOfMs"] = int((now - 1200) * 1000)
        engine.providers["muse"]._run_usage = lambda config_home, *, file_mode, isolated: copy.deepcopy(payload)
        self.assertEqual(engine._refresh(row["id"]), "ok")
        self.assertAlmostEqual(engine.store.account(row["id"])["last_good_at"], now - 1200, delta=1)
        self.assertAlmostEqual(shown(engine, row["id"])["last_good_at"], now - 1200, delta=1)
        self.assertEqual([r["used"] for r in engine.store.readings(row["id"], "muse:window")
                          if abs(r["at"] - (now - 1200)) < 1], [8.0])


class UpkeepTest(unittest.TestCase):
    """Renaming and removing a saved account, for all three clients, signed in or not."""

    def rename_then_remove(self, provider, *, signed_out):
        engine = support.demo_engine(self, "typical")
        rows = [a for a in engine.snapshot()["accounts"] if a["provider"] == provider and a["saved"]]
        victim = next((a for a in rows if not a["active"]), rows[0])      # Codex has only the signed-in one
        if signed_out:
            sign_out(engine, provider)
        engine.action("rename", {"id": victim["id"], "label": "Renamed"})
        self.assertEqual(shown(engine, victim["id"])["label"], "Renamed")
        engine.action("remove", {"id": victim["id"]})
        self.assertTrue(engine.runner.wait(30))
        report = engine.snapshot()["report"]
        self.assertEqual((report["outcome"], report["error"]), ("success", None))
        left = [a["id"] for a in engine.snapshot()["accounts"] if a["provider"] == provider and a["saved"]]
        self.assertEqual(sorted(left), sorted(a["id"] for a in rows if a["id"] != victim["id"]))

    def test_while_signed_in(self):
        for provider in CLIENTS:
            with self.subTest(provider=provider):
                self.rename_then_remove(provider, signed_out=False)

    def test_while_signed_out(self):
        for provider in CLIENTS:
            with self.subTest(provider=provider):
                self.rename_then_remove(provider, signed_out=True)

    def test_the_terminal_account_in_use_cannot_be_removed(self):
        engine = support.demo_engine(self, "typical")
        row = signed_in(engine, "claude_cli")
        engine.action("remove", {"id": row["id"]})
        self.assertTrue(engine.runner.wait(30))
        self.assertEqual(engine.snapshot()["report"]["error"]["message"], "The account in use by your terminals cannot be removed. Switch to another account first.")
        self.assertIsNotNone(engine.store.account(row["id"]))

    def test_the_signed_in_account_is_marked_as_used_once_an_hour(self):
        engine = support.demo_engine(self, "typical")
        row = signed_in(engine, "codex")
        first = row["flags"]["last_active_at"]
        engine.clock.advance(600)
        engine.sense_active()
        self.assertEqual(signed_in(engine, "codex")["flags"]["last_active_at"], first)
        engine.clock.advance(3600)
        engine.sense_active()
        self.assertGreater(signed_in(engine, "codex")["flags"]["last_active_at"], first + 3600)


class FlagsTest(unittest.TestCase):
    """Noted 6. Marking the signed-in account as used rewrites its flags. A usage read writes flags too, from
    another thread, under the account's lock."""

    def setUp(self):
        self.engine = support.demo_engine(self, "typical")
        self.row = signed_in(self.engine, "codex")
        self.later = self.engine.clock() + 2 * HOUR

    def flags(self):
        return self.engine.store.account(self.row["id"])["flags"]

    def touch(self):
        self.engine.accounts.touch_active("codex", self.row["identity"], self.later)

    def test_a_flag_a_read_wrote_a_moment_ago_is_kept(self):
        store, find = self.engine.store, self.engine.store.find

        def find_then_a_read_finishes(provider, identity):
            account = find(provider, identity)
            store.update(account["id"], flags={**account["flags"], "refresh_failures": 3})
            return account

        store.find = find_then_a_read_finishes
        self.touch()
        self.assertEqual((self.flags().get("refresh_failures"), self.flags()["last_active_at"]), (3, self.later))

    def test_it_waits_for_a_read_in_progress_without_blocking(self):
        before, holding, done = self.flags()["last_active_at"], threading.Event(), threading.Event()

        def a_read():
            with self.engine.scheduler.account_lock(self.row["id"]):
                holding.set()
                done.wait(10)

        thread = threading.Thread(target=a_read, daemon=True)
        thread.start()
        self.assertTrue(holding.wait(5))
        started = time.monotonic()
        self.touch()
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.flags()["last_active_at"], before)       # left for the next tick
        done.set()
        thread.join(5)
        self.touch()
        self.assertEqual(self.flags()["last_active_at"], self.later)


class SchedulerTest(unittest.TestCase):
    def test_one_helper_read_per_client_at_a_time(self):
        running, most, done, lock = [], [], [], threading.Lock()

        def run(key):
            with lock:
                running.append(key)
                most.append(len(running))
            time.sleep(0.05)
            with lock:
                running.remove(key)
                done.append(key)
            return "ok"

        scheduler = RefreshScheduler(run)
        self.addCleanup(scheduler.shutdown)
        targets = [Target(f"codex-{n}", "codex", f"id-{n}") for n in range(3)]
        deadline = time.monotonic() + 10
        while len(done) < 3 and time.monotonic() < deadline:
            scheduler.tick(targets, 1000.0, lambda: 1000.0)
            time.sleep(0.02)
        self.assertEqual(sorted(done), ["codex-0", "codex-1", "codex-2"])
        self.assertEqual(max(most), 1)

    def test_waiting_helper_reads_leave_workers_for_the_other_clients(self):
        """Consider 5. Codex and Muse reads run one at a time per client. The ones that wait their turn must not
        each hold one of the three workers, or nothing else is read until the first one ends."""
        release, ran = threading.Event(), []

        def run(key):
            if key.startswith(("codex", "muse")):
                release.wait(10)
            ran.append(key)
            return "ok"

        for client in ("codex", "muse"):
            with self.subTest(client=client):
                release.clear()
                scheduler = RefreshScheduler(run)
                self.addCleanup(scheduler.shutdown)
                self.addCleanup(release.set)
                slow = [Target(f"{client}-{n}", client, f"id-{n}") for n in range(3)]
                scheduler.tick(slow, 1000.0, lambda: 1000.0)
                time.sleep(0.2)                               # the first helper read is running, the other two are due
                scheduler.tick(slow + [Target(f"claude-with-{client}", "claude_cli", "person")], 1000.0, lambda: 1000.0)
                deadline = time.monotonic() + 3
                while f"claude-with-{client}" not in ran and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertIn(f"claude-with-{client}", ran, f"three {client} reads held all three workers")
                release.set()


class RetryAfterTest(unittest.TestCase):
    def test_what_a_retry_after_header_asks_for(self):
        seconds = errors.retry_after_seconds
        self.assertEqual((seconds("120"), seconds(" 0 "), seconds(None), seconds("soon"), seconds("-5")),
                         (120.0, 0.0, None, None, None))
        self.assertEqual(seconds("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412480.0 - 90), 90.0)
        self.assertEqual((errors.rate_limit_wait(None), errors.rate_limit_wait(120.0)), (900, 1020.0))
        self.assertEqual(errors.ProviderError("rate_limited").retry_after, 900)

    def refused(self, retry_after):
        """The error `http.request` raises when a loopback server answers 429 with this Retry-After header."""
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(429)
                self.send_header("Retry-After", retry_after)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        host = f"127.0.0.1:{server.server_address[1]}"
        provider_http.register_test_host(host)
        try:
            with self.assertRaises(errors.ProviderError) as caught:
                provider_http.request("GET", f"http://{host}/usage", timeout=5)
        finally:
            provider_http.unregister_test_host(host)
            server.shutdown()
            server.server_close()
        return caught.exception

    def test_no_answer_pauses_an_account_for_more_than_an_hour(self):
        """Noted 12. `Retry-After: 99999999` used to pause the account for 3.2 years."""
        self.assertEqual((self.refused("120").kind, self.refused("120").retry_after), ("rate_limited", 1020.0))
        self.assertEqual(self.refused("99999999").retry_after, 3600)
        self.assertEqual(self.refused("Fri, 01 Jan 2100 00:00:00 GMT").retry_after, 3600)
        self.assertEqual(errors.ProviderError("rate_limited", retry_after=4000).retry_after, 3600)
        self.assertEqual(errors.ProviderError("blocked").retry_after, 1800)


# ---- the PC move import ----
PASSPHRASE = "synthetic passphrase for tests"
SCRYPT_N = 2 ** 10


def write_bundle(case, engine, settings):
    """A main move bundle under the demo's own folder: one terminal account still signed in on the PC."""
    payload = {"format": 1, "source_platform": "windows", "settings": settings, "accounts": [{
        "id": "pc-1", "provider": "claude_cli", "identity": str(uuid.uuid4()), "label": "From the PC",
        "email": "pc@example.test", "plan": "max", "org": "", "created": NOW, "live_on_pc": True, "refills": [],
        "history": {"last_good_usage": None, "last_good_at": None, "readings": [], "snapshots": []}}]}
    case.addCleanup(setattr, move_import, "MIN_SCRYPT_N", move_import.MIN_SCRYPT_N)
    move_import.MIN_SCRYPT_N = SCRYPT_N
    path = Path(engine.paths.temp_root) / "move.adeck"
    path.write_bytes(move_bundle.seal(payload, PASSPHRASE, "main", scrypt_n=SCRYPT_N, min_scrypt_n=SCRYPT_N))
    return {"path": str(path), "passphrase": PASSPHRASE}


class MoveImportTest(unittest.TestCase):
    def test_an_import_lists_the_pc_account_and_brings_its_settings(self):
        engine = support.demo_engine(self, "typical")
        engine.update_settings({"cli_autoswitch": False})
        body = write_bundle(self, engine, {"cli_autoswitch": True, "cli_threshold": 90, "sort": "stable"})
        before = len(engine.store.accounts())
        preview = engine.action("import-bundle-preview", body)["preview"]
        self.assertEqual((preview["kind"], preview["refused"], preview["counts"]["on_pc"]), ("main", False, 1))
        self.assertEqual([(a["label"], a["action"]) for a in preview["accounts"]], [("From the PC", "on_pc")])
        self.assertEqual(len(engine.store.accounts()), before, "a preview writes nothing")
        self.assertEqual(engine.settings()["cli_autoswitch"], False)
        result = engine.action("import-bundle", body)["result"]
        self.assertTrue(result["complete"])
        self.assertRegex(result["code"], r"^[A-Z2-7]{4}-[A-Z2-7]{4}$")
        row = next(r for r in engine.store.accounts() if r["label"] == "From the PC")
        self.assertEqual((row["provider"], row["owner"]), ("claude_cli", "on_pc"))
        settings = engine.settings()
        self.assertEqual((settings["cli_autoswitch"], settings["cli_threshold"]), (True, 90))

    def test_an_import_names_the_settings_it_changes(self):
        """Noted 13. The PC's settings replace this Mac's. The preview and the result say which ones change."""
        engine = support.demo_engine(self, "typical")
        engine.update_settings({"cli_autoswitch": False})
        body = write_bundle(self, engine, {"cli_autoswitch": True, "cli_threshold": 90, "sort": "stable"})
        preview = engine.action("import-bundle-preview", body)["preview"]
        self.assertEqual(preview["settings"], ["automatic terminal switching", "the switch point"])
        self.assertTrue(preview["message"].endswith(
            " 2 settings on this Mac will change to match the PC: automatic terminal switching and the switch point."),
            preview["message"])
        engine.action("import-bundle", body)
        self.assertTrue(engine.runner.wait(30))
        self.assertIn(" 2 settings now match the PC: automatic terminal switching and the switch point.",
                      engine.snapshot()["report"]["summary"])
        again = engine.action("import-bundle-preview", body)["preview"]       # they match now: nothing to name
        self.assertEqual(again["settings"], [])
        self.assertNotIn("setting", again["message"])

    def test_every_setting_that_travels_has_a_name(self):
        """Every setting a bundle can carry that this build has. `cookie_route` was Claude Desktop's: a bundle
        may still carry it, and the import drops it."""
        self.assertEqual(set(move_import.SETTING_NAMES), set(move_bundle.PORTABLE_SETTINGS) - {"cookie_route"})
        self.assertNotIn("cookie_route", config.SETTINGS_SCHEMA)


# ---- the state validator ----
def window(account, wid):
    return next(w for w in account["windows"] + account["other_windows"] if w["id"] == wid)


# Ways of breaking one account of an example state: name -> (the account's place, the change). The state is
# "fifty" unless a third item names another: its Codex accounts are many enough that one broken row breaks no
# rule about the client as a whole. There 36 is a Codex account past half of its 5-hour limit, 42 one with room
# on both limits, 45 one past half of both, and 47 the recommended one. In "typical" 1 is a Claude Code account
# with an entry under "other limits"; a broken Claude Code account also breaks the automatic order that lists it.
BROKEN = {
    # the fields of one account
    "no label": (36, lambda a: a.pop("label")),
    "a number for a label": (36, lambda a: a.update(label=5)),
    "an unknown client": (36, lambda a: a.update(provider="gemini")),
    "a client this version no longer has": (36, lambda a: a.update(provider="claude")),
    "a word for a flag": (36, lambda a: a.update(active="yes")),
    "a terminal account without its health": (1, lambda a: a.pop("health"), "typical"),
    "another computer's account marked switchable": (36, lambda a: a.update(owner="on_pc")),
    "an unknown source": (36, lambda a: a.update(imported_from="mac")),
    "a score above 1": (36, lambda a: a.update(score=2)),
    "an identity in the payload": (36, lambda a: a["credential"].update(identity="someone")),
    "an unknown credential state": (36, lambda a: a["credential"].update(state="maybe")),
    "a credential state only Claude Desktop had": (36, lambda a: a["credential"].update(state="web_only")),
    "an error without its fields": (36, lambda a: a.update(error={"code": "network"})),
    "a trend out of order": (36, lambda a: a.update(trend=[[20.0, 5.0], [10.0, 6.0]])),
    # the reading and its windows
    "an unsaved id on a saved account": (36, lambda a: a.update(id="unsaved-codex")),
    "the wrong age": (36, lambda a: a.update(age_tier="stale")),
    "stale without an old reading": (36, lambda a: a.update(stale=True)),
    "windows without a reading": (36, lambda a: a.update(last_good_at=None)),
    # the account as a whole
    "the same window twice": (36, lambda a: a["windows"].append(copy.deepcopy(a["windows"][0]))),
    "an other window with nothing to show": (1, lambda a: a["other_windows"][0].update(
        used=0.0, left=100.0, reset=None, reset_state="none", pace=None), "typical"),
    "no limiting window marked": (36, lambda a: window(a, "codex:primary").update(binding=False)),
    "two limiting windows marked": (45, lambda a: window(a, "codex:secondary").update(binding=True)),
    "the wrong limiting window": (45, lambda a: (window(a, "codex:primary").update(binding=False),
                                                 window(a, "codex:secondary").update(binding=True))),
    "a limiting window where none limits": (42, lambda a: window(a, "codex:primary").update(binding=True)),
    "the wrong state": (36, lambda a: a.update(state="limited")),
    "the wrong headline": (36, lambda a: a["headline"].update(kind="ready")),
    "a headline about another window": (36, lambda a: a["headline"].update(window="codex:secondary")),
    "recommended without rank 1": (47, lambda a: a.update(rank=2)),
}

# What `contract.validate_state` says about each.
SAID = {
    'no label': ["accounts[36]: missing 'label'"],
    'a number for a label': ['accounts[36].label: must be a non-empty string, got 5'],
    'an unknown client': ["accounts[36].provider: 'gemini' is not one of codex, claude_cli, muse"],
    'a client this version no longer has': ["accounts[36].provider: 'claude' is not one of codex, claude_cli, muse"],
    'a word for a flag': ["accounts[36].active: must be true or false, got 'yes'"],
    'a terminal account without its health': ["accounts[1]: missing 'health'", 'providers.claude_cli.autoswitch.next_up: must list every terminal account once'],
    "another computer's account marked switchable": ['accounts[36].switchable: must be false for an account another computer owns'],
    'an unknown source': ["accounts[36].imported_from: must be null, 'v1' or 'pc'"],
    'a score above 1': ['accounts[36].score: must be a number in [0, 1], got 2'],
    'an identity in the payload': ['accounts[36].credential.identity: identities, org ids and secrets never leave the server'],
    'an unknown credential state': ["accounts[36].credential.state: 'maybe' is not one of live, fresh, expired, reauth_required, access_expired, unknown"],
    'a credential state only Claude Desktop had': ["accounts[36].credential.state: 'web_only' is not one of live, fresh, expired, reauth_required, access_expired, unknown"],
    'an error without its fields': ["accounts[36].error: missing 'message'", "accounts[36].error: missing 'since'", "accounts[36].error: missing 'outage'"],
    'a trend out of order': ['accounts[36].trend: points must be in time order and not in the future'],
    'an unsaved id on a saved account': ["accounts[36]: only 'unsaved-codex' / 'unsaved-muse' may be unsaved, and they are active and not switchable"],
    'the wrong age': ["accounts[36].age_tier: must be 'fresh' for a reading 367s old"],
    'stale without an old reading': ["accounts[36].stale: must be true exactly when age_tier is 'stale'"],
    'windows without a reading': ['accounts[36].age_tier: must be None for a reading never taken', 'accounts[36].windows: an account without a reading has no windows'],
    'the same window twice': ['accounts[36].windows: must be sorted session, weekly, then models by label', "accounts[36]: duplicate window id 'codex:primary'", 'accounts[36]: exactly one gating window must be binding', "accounts[36].headline.kind: must be 'ready'", 'accounts[36].headline.window: must be None'],
    'an other window with nothing to show': ['accounts[1].other_windows[0]: other windows appear only when used > 0 or a reset exists', 'providers.claude_cli.autoswitch.next_up: must list every terminal account once'],
    'no limiting window marked': ['accounts[36]: exactly one gating window must be binding', "accounts[36].headline.kind: must be 'ready'", 'accounts[36].headline.window: must be None'],
    'two limiting windows marked': ['accounts[45]: exactly one gating window must be binding', "accounts[45].headline.kind: must be 'ready'", 'accounts[45].headline.window: must be None'],
    'the wrong limiting window': ["accounts[45]: binding must be 'codex:primary' (out beats everything, then the lowest left)", "accounts[45].headline.kind: must be 'watch'", "accounts[45].headline.window: must be 'codex:secondary'"],
    'a limiting window where none limits': ['accounts[42]: no gating window, so no window may be binding'],
    'the wrong state': ["accounts[36].state: must be 'ok'"],
    'the wrong headline': ["accounts[36].headline.kind: must be 'watch'"],
    'a headline about another window': ["accounts[36].headline.window: must be 'codex:primary'"],
    'recommended without rank 1': ['accounts[47].rank: the recommended account has rank 1'],
}


class ValidatorTest(unittest.TestCase):
    """The state validator names each broken rule of an account, and nothing else."""
    maxDiff = None

    def test_what_it_says_about_a_broken_account(self):
        said = {}
        for name, (index, change, *scenario) in BROKEN.items():
            state = fixtures.example_state(*scenario or ["fifty"])
            change(state["accounts"][index])
            said[name] = contract.validate_state(state)
        self.assertEqual(said, SAID)


# ---- the example states ----
def picks(state):
    """What one example state recommends per client: the pick, its state and reason, then the first ranked accounts."""
    found = []
    for provider, best in state["now_band"]["best"].items():
        if best["state"] == "none":
            continue
        ranked = sorted((a for a in state["accounts"] if a["provider"] == provider and a["rank"]), key=lambda a: a["rank"])
        found.append(f"{provider} {best['account']} {best['state']} {best['reason']} | " + " ".join(a["id"] for a in ranked[:4]))
    return found


# The picks since Claude dropped its 5-hour limit (2026-11-13): the weekly limit is an account's room.
PICKS = {
    'terminal': ['claude_cli cli_gamma ok Most headroom (80% left) and a fresh reading | cli_gamma cli_beta cli_delta cli_alpha', 'muse acc_m2 ok Most headroom (88% left) and a fresh reading | acc_m2 acc_m1'],
    'typical': ['claude_cli cli_gamma ok Most headroom (80% left) and a fresh reading | cli_gamma cli_beta cli_alpha cli_delta', 'muse acc_m2 ok Most headroom (88% left) and a fresh reading | acc_m2 acc_m1'],
    'resets-soon': ['claude_cli cli_epsilon ok Most headroom (70% left) and a fresh reading | cli_epsilon cli_gamma cli_beta cli_delta'],
    'refilled': ["claude_cli cli_alpha ok You're on the best account | cli_alpha cli_gamma cli_beta"],
    'not-started': ['claude_cli cli_beta ok Most headroom (100% left) and a fresh reading | cli_beta cli_gamma cli_alpha'],
    'codex-blocked': ['codex None all_exhausted No account has room right now. Soonest back: Codex main in 2h — 4:13 PM | ', 'claude_cli cli_beta ok Most headroom (80% left) and a fresh reading | cli_beta cli_alpha'],
    'needs-signin': ["codex acc_6 ok You're on the best account | acc_6", 'claude_cli cli_delta ok Most headroom (88% left) and a fresh reading | cli_delta cli_alpha'],
    'blocked-cloudflare': ['claude_cli cli_delta ok Most headroom (90% left) and a fresh reading | cli_delta cli_alpha'],
    'stale': ['claude_cli cli_gamma unknown Usage last checked 42m ago | cli_gamma cli_delta'],
    'fable-binding': ['claude_cli cli_beta ok Most Fable headroom (65% left) | cli_beta cli_gamma cli_alpha'],
    'resetting': ['claude_cli cli_gamma ok Only account with headroom | cli_gamma'],
    'rate-limited': ['claude_cli cli_gamma ok Most headroom (80% left) and a fresh reading | cli_gamma cli_alpha cli_delta'],
    'outage': ['claude_cli cli_beta ok Most headroom (60% left) and a fresh reading | cli_beta cli_alpha'],
    'switching': ['codex acc_7 ok Most headroom (75% left) and a fresh reading | acc_7 acc_6', 'claude_cli cli_gamma ok Most headroom (80% left) and a fresh reading | cli_gamma cli_beta cli_alpha cli_delta'],
    'stopping': ['codex acc_7 ok Most headroom (75% left) and a fresh reading | acc_7 acc_6', 'claude_cli cli_gamma ok Most headroom (80% left) and a fresh reading | cli_gamma cli_beta cli_alpha cli_delta'],
    'report': ["codex acc_7 ok You're on the best account | acc_7 acc_6", 'claude_cli cli_gamma ok Most headroom (80% left) and a fresh reading | cli_gamma cli_beta cli_alpha cli_delta'],
    'needs-reconfirm': ['codex acc_7 ok Most headroom (75% left) and a fresh reading | acc_7 acc_6', 'claude_cli cli_gamma ok Most headroom (80% left) and a fresh reading | cli_gamma cli_beta cli_alpha cli_delta'],
    'empty': [],
    'loading': ['claude_cli None unknown Waiting for the first readings | '],
    'fifty': ['codex acc_48 ok Most headroom (89% left) and a fresh reading | acc_48 acc_42 acc_49 acc_43', 'claude_cli cli_15 ok Most headroom (100% left) and a fresh reading | cli_15 cli_31 cli_08 cli_16'],
    'read-only': ['claude_cli cli_gamma ok Most headroom (80% left) and a fresh reading | cli_gamma cli_beta cli_alpha cli_delta'],
    'demo-banner': ['claude_cli cli_gamma ok Most headroom (80% left) and a fresh reading | cli_gamma cli_beta cli_alpha cli_delta'],
    'first-run': [],
}


class ExampleStatesTest(unittest.TestCase):
    maxDiff = None

    def test_what_each_example_state_recommends(self):
        self.assertEqual({scenario: picks(fixtures.example_state(scenario)) for scenario in fixtures.SCENARIOS}, PICKS)

if __name__ == "__main__":
    unittest.main()
