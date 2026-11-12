"""What AccountDeck measures and decides, on synthetic data: the terminal auto-switch rule as a decision table,
and usage readings, refills and account upkeep for all four clients. Builder B's file (2026-11-12 refactor)."""
import copy
import unittest

from tests import support  # noqa: F401  (turns safe mode on before the app is imported)

from accountdeck import cli_policy

NOW = 1_800_000_000.0
HOUR, DAY = 3600, 86400
SETTINGS = {"cli_autoswitch": True, "cli_threshold": 95, "cli_order": "soonest_reset", "cli_early_switch": True,
            "pinned_model": ""}


def account(key, five, five_reset, weekly, weekly_reset=3 * DAY, *, active=False, age=60, model=None, **fields):
    """One terminal account as `cli_auto.inputs` hands it to the rule. Resets are seconds from NOW; `age` is how
    old the reading is (None: never read); `model` is the % used of a Fable weekly window."""
    windows = [{"id": "five_hour", "kind": "session", "used": five, "reset": NOW + five_reset, "started": True},
               {"id": "seven_day", "kind": "weekly", "used": weekly, "reset": NOW + weekly_reset}]
    if model is not None:
        windows.append({"id": "seven_day_fable", "kind": "model", "model": "Fable", "label": "Fable weekly",
                        "used": model, "reset": NOW + weekly_reset})
    return {"id": key, "label": key.capitalize(), "active": active, "forecast": {}, "owner": "v2", "excluded": False,
            "health": "ok", "live_expired": False, "last_good_at": None if age is None else NOW - age,
            "windows": windows, **fields}


# ---- the decision table ----
# Columns: the reading of Alpha, the account in use. 5-hour % used, when that window resets, weekly % used.
FIVE_USED = (20, 96, 99, 100)
FIVE_RESET = (("in 4 h", 4 * HOUR), ("in 4 min", 240), ("40 s ago", -40))
WEEKLY_USED = (30, 96)
COLUMNS = [(five, name, reset, weekly) for five in FIVE_USED for name, reset in FIVE_RESET for weekly in WEEKLY_USED]

BETA = dict(five=20, five_reset=4 * HOUR, weekly=30)
SOON = dict(BETA, weekly=50, weekly_reset=12 * HOUR)          # Beta's week ends before Alpha's: the early switch
UNDONE = {"left": "beta", "left_at": NOW - 5, "manual_at": NOW - 5,
          "last": {"from": "alpha", "to": "beta", "at": NOW - 60, "kind": "threshold", "reason": "", "undo_until": NOW - 5}}


def beta(**changes):
    spec = {**BETA, **changes}
    return account("beta", spec.pop("five"), spec.pop("five_reset"), spec.pop("weekly"), **spec)


def gamma():
    return account("gamma", 40, 4 * HOUR, 50, 2 * DAY)


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
    "Beta is 20 min old, Gamma at 90%": ([beta(age=1200), account("gamma", 40, 4 * HOUR, 90, 2 * DAY)], {}, {}, {}),
    "Gamma's week ends sooner": ([beta(), gamma()], {}, {}, {}),
    "Gamma's week ends sooner, most room first": ([beta(), gamma()], {}, {"cli_order": "most_room"}, {}),
    "Gamma's week ends sooner, my order Beta first": ([beta(), gamma()], {},
                                                     {"cli_order": "manual", "cli_manual_order": ["beta", "gamma"]}, {}),
    "Undo pressed 5 s ago": ([beta()], UNDONE, {}, {}),
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
    "Alpha's 5-hour is forecast at 97%": ([beta()], {}, {}, {"forecast": {"five_hour": 97.0}}),
    "Alpha's pinned Fable limit is at 96%": ([beta()], {}, {"pinned_model": "fable"}, {"model": 96}),
    "Beta's week ends in 12 h": ([beta(**SOON)], {}, {}, {}),
    "Beta's week ends in 12 h, 85% used": ([beta(**dict(SOON, weekly=85))], {}, {}, {}),
    "Beta's week ends in 12 h, left 10 min ago": ([beta(**SOON)], {"left": "beta", "left_at": NOW - 600}, {}, {}),
    "Beta's week ends in 12 h, manual choice 5 s ago": ([beta(**SOON)], {"manual_at": NOW - 5}, {}, {}),
    "Beta's week ends in 12 h, early switch 1 h ago": ([beta(**SOON)], {"early_at": NOW - HOUR}, {}, {}),
}

HOLDS = {"refills in": "r", "Checking": "c", "Every account is past": "d", "Usage updates after": "x",
         "An early switch happened": "e", "Your manual choice": "m"}
KINDS = {"threshold": "T", "out": "O", "login": "L", "early": "E", "refilled": "R"}


def code(result):
    """One decision as two characters. A switch: its kind (Threshold, Out, Login, Early, Refilled after all were
    low) and the target's initial. A hold: h and its reason (r refills soon, c checking the others, d using
    every account up, x login expired, e early switch too recent, m manual choice), in capitals when the rule
    also asks for a new reading. XX: every account is low. ..: nothing to do."""
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
    five, _name, reset, weekly = column
    alpha = dict(alpha)
    active = account("alpha", five, reset, weekly, active=True, model=alpha.pop("model", None), **alpha)
    return cli_policy.evaluate([active] + copy.deepcopy(others), {**SETTINGS, **settings}, copy.deepcopy(history), NOW)


def table():
    return {row: " ".join(code(decide(row, column)) for column in COLUMNS) for row in ROWS}


# The answers at the starting commit (1c1787b). 5-hour % used:
#                                                      20                96                99                100
# 5-hour reset (in 4 h, in 4 min, 40 s ago), each with weekly 30 then 96:
#                                                      4h    4m    ago   4h    4m    ago   4h    4m    ago   4h    4m    ago
EXPECTED = {
    "Beta has room":                                   ".. Tb .. Tb .. Tb Tb Tb hr Tb Tb Tb Tb Tb hr Tb Tb Tb Ob Ob Ob Ob Ob Ob",
    "Beta's reading is 20 min old":                    ".. hC .. hC .. hC hC hC hr hC hC hC hC hC hr hC hC hC hC hC hC hC hC hC",
    "Beta was never read":                             ".. hd .. hd .. hd hd hd hr hd hd hd XX XX hr XX XX XX XX XX XX XX XX XX",
    "Beta's weekly is at 90%":                         ".. hd .. hd .. hd hd hd hr hd hd hd Tb Tb hr Tb Tb Tb Ob Ob Ob Ob Ob Ob",
    "Beta's weekly is at 98%":                         ".. hd .. hd .. hd hd hd hr hd hd hd XX XX hr XX XX XX XX XX XX XX XX XX",
    "Beta's weekly is unknown":                        ".. hd .. hd .. hd hd hd hr hd hd hd XX XX hr XX XX XX XX XX XX XX XX XX",
    "Beta needs sign-in":                              ".. hd .. hd .. hd hd hd hr hd hd hd XX XX hr XX XX XX XX XX XX XX XX XX",
    "Beta is left out":                                ".. hd .. hd .. hd hd hd hr hd hd hd XX XX hr XX XX XX XX XX XX XX XX XX",
    "Beta belongs to the PC":                          ".. hd .. hd .. hd hd hd hr hd hd hd XX XX hr XX XX XX XX XX XX XX XX XX",
    "Alpha is the only account":                       ".. hd .. hd .. hd hd hd hr hd hd hd XX XX hr XX XX XX XX XX XX XX XX XX",
    "Beta is 20 min old, Gamma at 90%":                ".. hC .. hC .. hC hC hC hr hC hC hC hC hC hr hC hC hC hC hC hC hC hC hC",
    "Gamma's week ends sooner":                        ".. Tg .. Tg .. Tg Tg Tg hr Tg Tg Tg Tg Tg hr Tg Tg Tg Og Og Og Og Og Og",
    "Gamma's week ends sooner, most room first":       ".. Tb .. Tb .. Tb Tb Tb hr Tb Tb Tb Tb Tb hr Tb Tb Tb Ob Ob Ob Ob Ob Ob",
    "Gamma's week ends sooner, my order Beta first":   ".. Tb .. Tb .. Tb Tb Tb hr Tb Tb Tb Tb Tb hr Tb Tb Tb Ob Ob Ob Ob Ob Ob",
    "Undo pressed 5 s ago":                            "hm Tb hm Tb hm Tb Tb Tb hr Tb Tb Tb Tb Tb hr Tb Tb Tb Ob Ob Ob Ob Ob Ob",
    "Manual choice 2 h 5 s ago":                       ".. Tb .. Tb .. Tb Tb Tb hr Tb Tb Tb Tb Tb hr Tb Tb Tb Ob Ob Ob Ob Ob Ob",
    "Early switch 1 h ago":                            "he Tb he Tb he Tb Tb Tb hr Tb Tb Tb Tb Tb hr Tb Tb Tb Ob Ob Ob Ob Ob Ob",
    "Every account was low before":                    ".. Rb .. Rb .. Rb Rb Rb hr Rb Rb Rb Rb Rb hr Rb Rb Rb Rb Rb Rb Rb Rb Rb",
    "Auto-switch is off":                              ".. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. ..",
    "Early switch is off":                             ".. Tb .. Tb .. Tb Tb Tb hr Tb Tb Tb Tb Tb hr Tb Tb Tb Ob Ob Ob Ob Ob Ob",
    "Threshold is 80%":                                ".. Tb .. Tb .. Tb Tb Tb hr Tb Tb Tb Tb Tb hr Tb Tb Tb Ob Ob Ob Ob Ob Ob",
    "Alpha's login expired while idle":                "hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx",
    "Alpha needs sign-in":                             "Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb",
    "Alpha is marked out":                             "Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob",
    "Alpha belongs to the PC":                         ".. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. ..",
    "Alpha's 5-hour is forecast at 97%":               "Tb Tb hr Tb Tb Tb Tb Tb hr Tb Tb Tb Tb Tb hr Tb Tb Tb Ob Ob Ob Ob Ob Ob",
    "Alpha's pinned Fable limit is at 96%":            "Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Ob Ob Ob Ob Ob Ob",
    "Beta's week ends in 12 h":                        "Eb Tb Eb Tb Eb Tb Tb Tb hr Tb Tb Tb Tb Tb hr Tb Tb Tb Ob Ob Ob Ob Ob Ob",
    "Beta's week ends in 12 h, 85% used":              ".. Tb .. Tb .. Tb Tb Tb hr Tb Tb Tb Tb Tb hr Tb Tb Tb Ob Ob Ob Ob Ob Ob",
    "Beta's week ends in 12 h, left 10 min ago":       ".. Tb .. Tb .. Tb Tb Tb hr Tb Tb Tb Tb Tb hr Tb Tb Tb Ob Ob Ob Ob Ob Ob",
    "Beta's week ends in 12 h, manual choice 5 s ago": "hm Tb hm Tb hm Tb Tb Tb hr Tb Tb Tb Tb Tb hr Tb Tb Tb Ob Ob Ob Ob Ob Ob",
    "Beta's week ends in 12 h, early switch 1 h ago":  "he Tb he Tb he Tb Tb Tb hr Tb Tb Tb Tb Tb hr Tb Tb Tb Ob Ob Ob Ob Ob Ob",
}


class AutoSwitchTableTest(unittest.TestCase):
    """Every decision of `cli_policy.evaluate` for 32 situations times 24 readings of the account in use."""
    maxDiff = None

    def test_the_decision_table(self):
        header = " ".join(f"{five:>3}" for five, _n, _r, _w in COLUMNS)
        self.assertEqual(table(), EXPECTED, f"columns (5-hour % used; reset in 4 h, in 4 min, 40 s ago; weekly 30, 96): {header}")


class AutoSwitchWordsTest(unittest.TestCase):
    """The words the rule gives the dashboard, and the order it lists the accounts in."""
    maxDiff = None

    def result(self, row, five=20, reset=4 * HOUR, weekly=30):
        return decide(row, (five, "", reset, weekly))

    def test_reasons_and_holds(self):
        said = {
            "threshold": self.result("Beta has room", five=96)["reason"],
            "weekly": self.result("Beta has room", weekly=96)["reason"],
            "out": self.result("Beta has room", five=100)["reason"],
            "login": self.result("Alpha needs sign-in")["reason"],
            "forecast": self.result("Alpha's 5-hour is forecast at 97%")["reason"],
            "drain": self.result("Beta's weekly is at 90%", five=99)["reason"],
            "early": self.result("Beta's week ends in 12 h")["reason"],
            "refills soon": self.result("Beta has room", five=96, reset=240)["hold"],
            "checking": self.result("Beta's reading is 20 min old", five=96)["hold"],
            "using up": self.result("Beta's weekly is at 90%", five=96)["hold"],
            "login expired": self.result("Alpha's login expired while idle")["hold"],
            "early too recent": self.result("Early switch 1 h ago")["hold"],
            "manual choice": self.result("Undo pressed 5 s ago")["hold"],
        }
        self.assertEqual(said, {
            "threshold": "Alpha reached 95% of its 5-hour limit; Beta is first in your eligible order.",
            "weekly": "Alpha reached 95% of its weekly limit; Beta is first in your eligible order.",
            "out": "Alpha reached a limit; Beta is first in your eligible order.",
            "login": "Alpha needs sign-in; Beta is first in your eligible order.",
            "forecast": "Alpha will pass 95% of its 5-hour limit before the next check; Beta is first in your eligible order.",
            "drain": "Alpha reached 99%; every account is past 95%, so Beta is used up to 99%.",
            "early": "Using Beta's weekly allowance before it resets in 12h 0m (50% left).",
            "refills soon": "Alpha refills in 4 min — staying",
            "checking": "Checking Beta before switching",
            "using up": "Every account is past 95% — using Alpha up to 99%",
            "login expired": "Usage updates after your next message in Claude Code",
            "early too recent": "An early switch happened less than 2 hours ago",
            "manual choice": "Your manual choice is held for 2 hours",
        })

    def test_checking_asks_for_the_old_readings(self):
        self.assertEqual(self.result("Beta is 20 min old, Gamma at 90%", five=96)["refresh"], ["beta"])

    def test_all_low_says_who_refills_first(self):
        result = self.result("Beta's weekly is at 98%", five=99)
        self.assertEqual((result["action"], result["exhausted"]), ("none", True))
        self.assertRegex(result["reason"], r"^All terminal accounts are low\. Alpha refills first at \d{1,2}:\d\d [AP]M\.$")

    def test_the_order_and_why_each_account_is_skipped(self):
        others = [beta(), gamma(), account("delta", 20, 4 * HOUR, 90), account("epsilon", 20, 4 * HOUR, 30, age=1200),
                  account("zeta", 20, 4 * HOUR, 30, health="needs_signin"), account("eta", 20, 4 * HOUR, 30, excluded=True),
                  account("theta", 20, 4 * HOUR, 30, age=None), account("iota", 20, 4 * HOUR, None)]
        active = account("alpha", 20, 4 * HOUR, 30, active=True)

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


if __name__ == "__main__":
    unittest.main()
