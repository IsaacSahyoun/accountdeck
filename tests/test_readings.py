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

from accountdeck import cli_policy, contract, demo, fixtures, move_bundle, move_import
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
    old the reading is (None: never read); `model` is the % used of its Fable 5.1 weekly limit, named as
    `usage.claude_usage` names it. An account with no `model` has no such limit in its reading."""
    windows = [{"id": "seven_day", "kind": "weekly", "used": weekly, "reset": NOW + weekly_reset}]
    if model is not None:
        windows.append({"id": "seven_day_fable", "kind": "model", "model": "Fable", "label": "Fable 5.1 weekly",
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
UNDONE = {"left": "beta", "left_at": NOW - 5, "manual_at": NOW - 5,
          "last": {"from": "alpha", "to": "beta", "at": NOW - 60, "kind": "threshold", "reason": "", "undo_until": NOW - 5}}


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
    "Beta's week ends in 12 h": ([beta(**SOON)], {}, {}, {}),
    "Beta's week ends in 12 h, 85% used": ([beta(**dict(SOON, weekly=85))], {}, {}, {}),
    "Beta's week ends in 12 h, left 10 min ago": ([beta(**SOON)], {"left": "beta", "left_at": NOW - 600}, {}, {}),
    "Beta's week ends in 12 h, manual choice 5 s ago": ([beta(**SOON)], {"manual_at": NOW - 5}, {}, {}),
    "Beta's week ends in 12 h, early switch 1 h ago": ([beta(**SOON)], {"early_at": NOW - HOUR}, {}, {}),
}

HOLDS = {"has refilled": "w", "Checking": "c", "Every account is past": "d", "Usage updates after": "x",
         "An early switch happened": "e", "Your manual choice": "m"}
KINDS = {"threshold": "T", "out": "O", "login": "L", "early": "E", "refilled": "R"}


def code(result):
    """One decision as two characters. A switch: its kind (Threshold, Out, Login, Early, Refilled after all were
    low) and the target's initial. A hold: h and its reason (w has refilled, c checking the
    others, d using every account up, x login expired, e early switch too recent, m manual choice), in capitals
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
    "Beta is 20 min old, Gamma at 90%":                ".. .. .. hC hC hW hC hC hW hC hC hW",
    "Gamma's week ends sooner":                        ".. .. .. Tg Tg hW Tg Tg hW Og Og hW",
    "Gamma's week ends sooner, most room first":       ".. .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Gamma's week ends sooner, my order Beta first":   ".. .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Undo pressed 5 s ago":                            "hm hm hm hm hm hW hm hm hW Ob Ob hW",
    "Undo pressed 5 s ago, Alpha needs sign-in":       "Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb",
    "Undo pressed 5 s ago, Alpha is marked out":       "Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob",
    "Manual choice 2 h 5 s ago":                       ".. .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Early switch 1 h ago":                            "he he he Tb Tb hW Tb Tb hW Ob Ob hW",
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
    # The Fable 5.1 weekly limit with settings as a new install has them. A manual choice holds against it
    # even when it is out, because the account still works for the other models.
    "Alpha's Fable limit is at 96%":                   "Tb Tb hW Tb Tb hW Tb Tb hW Ob Ob hW",
    "Alpha's Fable limit is at 100%":                  "Ob Ob hW Ob Ob hW Ob Ob hW Ob Ob hW",
    "Alpha's Fable limit is forecast at 97%":          "Tb Tb .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Undo pressed 5 s ago, Alpha's Fable limit is at 100%": "hm hm hW hm hm hW hm hm hW Ob Ob hW",
    "Beta's Fable limit is at 90%":                    ".. .. .. hd hd hW Tb Tb hW Ob Ob hW",
    "Beta's Fable limit is at 100%":                   ".. .. .. hd hd hW XX XX hW XX XX hW",
    "Beta's Fable limit is at 100%, Gamma has room, most room first": ".. .. .. Tg Tg hW Tg Tg hW Og Og hW",
    "Beta's week ends in 12 h":                        "Eb .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Beta's week ends in 12 h, 85% used":              ".. .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Beta's week ends in 12 h, left 10 min ago":       ".. .. .. Tb Tb hW Tb Tb hW Ob Ob hW",
    "Beta's week ends in 12 h, manual choice 5 s ago": "hm hm hm hm hm hW hm hm hW Ob Ob hW",
    "Beta's week ends in 12 h, early switch 1 h ago":  "he he he Tb Tb hW Tb Tb hW Ob Ob hW",
}


class AutoSwitchTableTest(unittest.TestCase):
    """Every decision of `cli_policy.evaluate` for 41 situations times 12 readings of the account in use."""
    maxDiff = None

    def test_the_decision_table(self):
        header = " ".join(f"{weekly:>3}" for weekly, _name, _reset in COLUMNS)
        self.assertEqual(table(), EXPECTED, f"columns (weekly % used; the week ends in 3 d, in 4 min, 40 s ago): {header}")


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
            "early": self.result("Beta's week ends in 12 h")["reason"],
            "has refilled": self.result("Beta has room", weekly=96, reset=-40)["hold"],
            "checking": self.result("Beta's reading is 20 min old", weekly=96)["hold"],
            "using up": self.result("Beta's weekly is at 90%", weekly=96)["hold"],
            "login expired": self.result("Alpha's login expired while idle")["hold"],
            "early too recent": self.result("Early switch 1 h ago")["hold"],
            "manual choice": self.result("Undo pressed 5 s ago")["hold"],
        }
        self.assertEqual(said, {
            "threshold": "Alpha reached 95% of its weekly limit; Beta is first in your eligible order.",
            "out": "Alpha reached its weekly limit; Beta is first in your eligible order.",
            "marked out": "Alpha reached a limit; Beta is first in your eligible order.",
            "Fable threshold": "Alpha reached 95% of its Fable 5.1 weekly limit; Beta is first in your eligible order.",
            "Fable out": "Alpha reached its Fable 5.1 weekly limit; Beta is first in your eligible order.",
            "Fable forecast": "Alpha will pass 95% of its Fable 5.1 weekly limit before the next check; Beta is first in your eligible order.",
            "Fable has refilled": "Alpha's Fable 5.1 weekly limit has refilled. Waiting for its new reading.",
            "login": "Alpha needs sign-in; Beta is first in your eligible order.",
            "forecast": "Alpha will pass 95% of its weekly limit before the next check; Beta is first in your eligible order.",
            "drain": "Alpha reached 99%; every account is past 95%, so Beta is used up to 99%.",
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
        self.assertEqual([key for key, _skip, _room in order(cli_order="most_room")],
                         ["alpha", "beta", "epsilon", "eta", "theta", "zeta", "gamma", "delta", "iota"])
        self.assertEqual([key for key, _skip, _room in order(cli_order="manual", cli_manual_order=["iota", "beta"])][:3],
                         ["iota", "beta", "gamma"])
        self.assertEqual(set(cli_policy.evaluate([active], SETTINGS, {}, NOW)["next_up"][0]),
                         {"id", "label", "skip", "room", "weekly_reset"})


class FableLimitTest(unittest.TestCase):
    """Auto-switch acts on the Fable 5.1 weekly limit with settings as a new install has them (Isaac's
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
                                 ["weekly", "Fable 5.1 weekly"])
                result = cli_policy.evaluate([alpha, beta], SETTINGS, {}, NOW)
                self.assertEqual((result["action"], result["target"], result["kind"]), ("switch", "beta", "threshold"))
                self.assertEqual(result["reason"], "Alpha reached 95% of its Fable 5.1 weekly limit; Beta is first in your eligible order.")

    def test_it_never_moves_to_an_account_whose_fable_limit_is_out(self):
        for (shape, low), (_, out), (_, roomy) in zip(self.answers(96, 10), self.answers(20, 100), self.answers(60, 30)):
            with self.subTest(shape=shape):
                result = cli_policy.evaluate([self.terminal("alpha", low, active=True), self.terminal("beta", out),
                                              self.terminal("gamma", roomy)], {**SETTINGS, "cli_order": "most_room"}, {}, NOW)
                self.assertEqual((result["action"], result["target"]), ("switch", "gamma"))
                self.assertEqual([(row["id"], row["skip"], row["room"]) for row in result["next_up"]],
                                 [("gamma", None, 35), ("alpha", "in use by your terminals", -1),
                                  ("beta", "Fable 5.1 weekly at 100%", -5)])

    def test_an_account_with_no_fable_reading_is_judged_on_its_week(self):
        """The limit appears in Claude's answer after the first Fable 5.1 message. Until then there is nothing to act on."""
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

    def test_a_setting_that_pins_fable_counts_the_limit_once(self):
        """Data an older version wrote: `pinned_model` could come in with a settings bundle."""
        _, over = self.answers(40, 96)[0]
        alpha = self.terminal("alpha", over, active=True)
        for pinned in ("Fable", "fable"):
            with self.subTest(pinned_model=pinned):
                self.assertEqual([name for name, _used, _window in cli_policy.considered(alpha, {**SETTINGS, "pinned_model": pinned})],
                                 ["weekly", "Fable 5.1 weekly"])

    def test_a_manual_choice_holds_against_a_fable_limit_that_is_out_but_not_against_the_week(self):
        """With its Fable 5.1 limit out an account still works for the other models, so the user's own choice
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
        best = state["now_band"]["best"]["claude"]
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
        rows = {provider: next(r for r in engine.store.accounts() if r["provider"] == provider
                               and r["identity"] != engine.active_identity[provider]) for provider in ("claude_cli", "claude")}
        for row in rows.values():       # before the first state is built: the engine keeps the state it built
            engine.store.observe(row["id"], support.shipped_reading(now, 100.0, 20.0), at=now)
        for provider, row in rows.items():
            with self.subTest(provider=provider):
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


# ---- readings, for all four clients ----
CLIENTS = ("claude_cli", "claude", "codex", "muse")
# For each client: the limit whose refill the test records, and one that keeps running. Claude has weekly limits only.
WINDOWS = {"claude_cli": ("seven_day", "seven_day_fable"), "claude": ("seven_day", "seven_day_fable"),
           "codex": ("codex:primary", "codex:secondary"), "muse": ("muse:window", "muse:weekly")}


def answer(engine, provider, windows):
    """From now on `provider`'s synthetic adapter answers a usage read with these windows
    ([{"key", "used", "reset"}]), in the provider's own format."""
    payload = demo._payload(provider, windows)
    adapter = engine.providers[provider]
    if provider == "claude":
        adapter._payload = lambda profile: copy.deepcopy(payload)
    elif provider == "codex":
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
    {"claude_cli": Path(paths.claude_cli_credentials), "claude": Path(paths.claude_profile) / "config.json",
     "codex": Path(paths.codex_home) / "auth.json", "muse": Path(paths.muse_config) / "auth.json"}[provider].unlink()
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

    def test_claude_desktop(self):
        self.refill_is_recorded("claude")

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
    """Renaming and removing a saved account, for all four clients, signed in or not."""

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
                scheduler.tick(slow + [Target(f"claude-with-{client}", "claude", "person")], 1000.0, lambda: 1000.0)
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
        self.assertEqual(set(move_import.SETTING_NAMES), set(move_bundle.PORTABLE_SETTINGS))


# ---- the state validator ----
def window(account, wid):
    return next(w for w in account["windows"] + account["other_windows"] if w["id"] == wid)


# Ways of breaking one account of the "typical" example state: name -> (the account's place, the change).
BROKEN = {
    # the fields of one account
    "no label": (1, lambda a: a.pop("label")),
    "a number for a label": (1, lambda a: a.update(label=5)),
    "an unknown client": (1, lambda a: a.update(provider="gemini")),
    "a word for a flag": (1, lambda a: a.update(active="yes")),
    "a terminal account without its health": (7, lambda a: a.pop("health")),
    "another computer's account marked switchable": (2, lambda a: a.update(owner="on_pc")),
    "an unknown source": (1, lambda a: a.update(imported_from="mac")),
    "a score above 1": (1, lambda a: a.update(score=2)),
    "an identity in the payload": (1, lambda a: a["credential"].update(identity="someone")),
    "an unknown credential state": (1, lambda a: a["credential"].update(state="maybe")),
    "an error without its fields": (1, lambda a: a.update(error={"code": "network"})),
    "a trend out of order": (1, lambda a: a.update(trend=[[20.0, 5.0], [10.0, 6.0]])),
    # the reading and its windows
    "an unsaved id on a saved account": (1, lambda a: a.update(id="unsaved-claude")),
    "the wrong age": (1, lambda a: a.update(age_tier="stale")),
    "stale without an old reading": (1, lambda a: a.update(stale=True)),
    "windows without a reading": (1, lambda a: a.update(last_good_at=None)),
    # the account as a whole
    "the same window twice": (1, lambda a: a["windows"].append(copy.deepcopy(a["windows"][0]))),
    "an other window with nothing to show": (1, lambda a: a["other_windows"][0].update(
        used=0.0, left=100.0, reset=None, reset_state="none", pace=None)),
    "no limiting window marked": (1, lambda a: window(a, "seven_day").update(binding=False)),
    "two limiting windows marked": (0, lambda a: window(a, "seven_day_fable").update(binding=True)),
    "the wrong limiting window": (0, lambda a: (window(a, "seven_day").update(binding=False), window(a, "seven_day_fable").update(binding=True))),
    "a limiting window where none limits": (2, lambda a: window(a, "seven_day").update(binding=True)),
    "the wrong state": (1, lambda a: a.update(state="limited")),
    "the wrong headline": (1, lambda a: a["headline"].update(kind="ready")),
    "a headline about another window": (1, lambda a: a["headline"].update(window="seven_day_oauth_apps")),
    "recommended without rank 1": (3, lambda a: a.update(rank=2)),
}

# What `contract.validate_state` says about each.
SAID = {
    'no label': ["accounts[1]: missing 'label'"],
    'a number for a label': ['accounts[1].label: must be a non-empty string, got 5'],
    'an unknown client': ["accounts[1].provider: 'gemini' is not one of claude, codex, claude_cli, muse"],
    'a word for a flag': ["accounts[1].active: must be true or false, got 'yes'"],
    'a terminal account without its health': ["accounts[7]: missing 'health'", 'providers.claude_cli.autoswitch.next_up: must list every terminal account once'],
    "another computer's account marked switchable": ['accounts[2].switchable: must be false for an account another computer owns'],
    'an unknown source': ["accounts[1].imported_from: must be null, 'v1' or 'pc'"],
    'a score above 1': ['accounts[1].score: must be a number in [0, 1], got 2'],
    'an identity in the payload': ['accounts[1].credential.identity: identities, org ids and secrets never leave the server'],
    'an unknown credential state': ["accounts[1].credential.state: 'maybe' is not one of live, fresh, expired, web_only, reauth_required, access_expired, unknown"],
    'an error without its fields': ["accounts[1].error: missing 'message'", "accounts[1].error: missing 'since'", "accounts[1].error: missing 'outage'"],
    'a trend out of order': ['accounts[1].trend: points must be in time order and not in the future'],
    'an unsaved id on a saved account': ["accounts[1]: only 'unsaved-claude' / 'unsaved-codex' may be unsaved, and they are active and not switchable", "now_band.best.claude: unknown account 'acc_2'"],
    'the wrong age': ["accounts[1].age_tier: must be 'fresh' for a reading 600s old"],
    'stale without an old reading': ["accounts[1].stale: must be true exactly when age_tier is 'stale'"],
    'windows without a reading': ['accounts[1].age_tier: must be None for a reading never taken', 'accounts[1].windows: an account without a reading has no windows'],
    'the same window twice': ["accounts[1]: duplicate window id 'seven_day'", 'accounts[1]: exactly one gating window must be binding', "accounts[1].headline.kind: must be 'ready'", 'accounts[1].headline.window: must be None'],
    'an other window with nothing to show': ['accounts[1].other_windows[0]: other windows appear only when used > 0 or a reset exists'],
    'no limiting window marked': ['accounts[1]: exactly one gating window must be binding', "accounts[1].headline.kind: must be 'ready'", 'accounts[1].headline.window: must be None'],
    'two limiting windows marked': ['accounts[0]: exactly one gating window must be binding', "accounts[0].headline.kind: must be 'ready'", 'accounts[0].headline.window: must be None', 'providers.claude.active: must name the active claude account (None)'],
    'the wrong limiting window': ["accounts[0]: binding must be 'seven_day' (out beats everything, then the lowest left)", "accounts[0].headline.kind: must be 'ready'", 'accounts[0].headline.window: must be None', 'providers.claude.active: must name the active claude account (None)'],
    'a limiting window where none limits': ['accounts[2]: no gating window, so no window may be binding'],
    'the wrong state': ["accounts[1].state: must be 'ok'"],
    'the wrong headline': ["accounts[1].headline.kind: must be 'watch'"],
    'a headline about another window': ["accounts[1].headline.window: must be 'seven_day'"],
    'recommended without rank 1': ['accounts[3].rank: the recommended account has rank 1'],
}


class ValidatorTest(unittest.TestCase):
    """The state validator names each broken rule of an account, and nothing else."""
    maxDiff = None

    def test_what_it_says_about_a_broken_account(self):
        said = {}
        for name, (index, change) in BROKEN.items():
            state = fixtures.example_state("typical")
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
    'terminal': ['claude acc_4 ok Most headroom (91% left) and a fresh reading | acc_4 acc_5 acc_3 acc_2', 'claude_cli cli_gamma ok Most headroom (80% left) and a fresh reading | cli_gamma cli_beta cli_delta cli_alpha', 'muse acc_m2 ok Most headroom (88% left) and a fresh reading | acc_m2 acc_m1'],
    'typical': ['claude acc_4 ok Most headroom (91% left) and a fresh reading | acc_4 acc_5 acc_3 acc_2', 'claude_cli cli_gamma ok Most headroom (80% left) and a fresh reading | cli_gamma cli_beta cli_alpha cli_delta', 'muse acc_m2 ok Most headroom (88% left) and a fresh reading | acc_m2 acc_m1'],
    'resets-soon': ['claude acc_5 ok Most headroom (70% left) and a fresh reading | acc_5 acc_3 acc_2 acc_4', 'claude_cli cli_gamma ok Most headroom (80% left) and a fresh reading | cli_gamma cli_beta cli_delta cli_alpha'],
    'refilled': ["claude acc_1 ok You're on the best account | acc_1 acc_3 acc_2"],
    'not-started': ['claude acc_2 ok Most headroom (100% left) and a fresh reading | acc_2 acc_3 acc_1'],
    'codex-blocked': ['claude acc_2 ok Most headroom (80% left) and a fresh reading | acc_2 acc_1', 'codex None all_exhausted No account has room right now. Soonest back: Codex main in 2h — 4:13 PM | '],
    'needs-signin': ['claude acc_4 ok Most headroom (88% left) and a fresh reading | acc_4 acc_1'],
    'blocked-cloudflare': ['claude acc_4 ok Most headroom (90% left) and a fresh reading | acc_4 acc_1'],
    'stale': ['claude acc_3 unknown Usage last checked 42m ago | acc_3 acc_4'],
    'fable-binding': ['claude acc_2 ok Most Fable headroom (65% left) | acc_2 acc_3 acc_1'],
    'resetting': ['claude acc_3 ok Only account with headroom | acc_3'],
    'rate-limited': ['claude acc_4 ok Most headroom (91% left) and a fresh reading | acc_4 acc_5 acc_3'],
    'outage': ['claude acc_2 ok Most headroom (60% left) and a fresh reading | acc_2 acc_1'],
    'switching': ['claude acc_4 ok Most headroom (91% left) and a fresh reading | acc_4 acc_5 acc_3 acc_2'],
    'stopping': ['claude acc_4 ok Most headroom (91% left) and a fresh reading | acc_4 acc_5 acc_3 acc_2'],
    'report': ["claude acc_4 ok You're on the best account | acc_4 acc_5 acc_3 acc_2"],
    'needs-reconfirm': ['claude acc_4 ok Most headroom (91% left) and a fresh reading | acc_4 acc_5 acc_3 acc_2'],
    'empty': [],
    'loading': ['claude None unknown Waiting for the first readings | '],
    'fifty': ['claude acc_15 ok Most headroom (100% left) and a fresh reading | acc_15 acc_31 acc_8 acc_16', 'codex acc_48 ok Most headroom (89% left) and a fresh reading | acc_48 acc_42 acc_49 acc_43'],
    'read-only': ['claude acc_4 ok Most headroom (91% left) and a fresh reading | acc_4 acc_5 acc_3 acc_2'],
    'demo-banner': ['claude acc_4 ok Most headroom (91% left) and a fresh reading | acc_4 acc_5 acc_3 acc_2'],
    'first-run': [],
}


class ExampleStatesTest(unittest.TestCase):
    maxDiff = None

    def test_what_each_example_state_recommends(self):
        self.assertEqual({scenario: picks(fixtures.example_state(scenario)) for scenario in fixtures.SCENARIOS}, PICKS)

if __name__ == "__main__":
    unittest.main()
