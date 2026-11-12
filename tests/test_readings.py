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

from accountdeck import cli_policy, demo, fixtures, move_bundle, move_import
from accountdeck.providers import errors
from accountdeck.providers import http as provider_http
from accountdeck.scheduler import RefreshScheduler, Target
from accountdeck.usage import claude_usage, muse_usage

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
    "Alpha's 5-hour is forecast at 97%": ([beta()], {}, {}, {"forecast": {"five_hour": 97.0}}),
    "Alpha's pinned Fable limit is at 96%": ([beta()], {}, {"pinned_model": "fable"}, {"model": 96}),
    "Beta's week ends in 12 h": ([beta(**SOON)], {}, {}, {}),
    "Beta's week ends in 12 h, 85% used": ([beta(**dict(SOON, weekly=85))], {}, {}, {}),
    "Beta's week ends in 12 h, left 10 min ago": ([beta(**SOON)], {"left": "beta", "left_at": NOW - 600}, {}, {}),
    "Beta's week ends in 12 h, manual choice 5 s ago": ([beta(**SOON)], {"manual_at": NOW - 5}, {}, {}),
    "Beta's week ends in 12 h, early switch 1 h ago": ([beta(**SOON)], {"early_at": NOW - HOUR}, {}, {}),
}

HOLDS = {"refills in": "r", "has refilled": "w", "Checking": "c", "Every account is past": "d", "Usage updates after": "x",
         "An early switch happened": "e", "Your manual choice": "m"}
KINDS = {"threshold": "T", "out": "O", "login": "L", "early": "E", "refilled": "R"}


def code(result):
    """One decision as two characters. A switch: its kind (Threshold, Out, Login, Early, Refilled after all were
    low) and the target's initial. A hold: h and its reason (r refills soon, w has refilled, c checking the
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
    five, _name, reset, weekly = column
    alpha = dict(alpha)
    active = account("alpha", five, reset, weekly, active=True, model=alpha.pop("model", None), **alpha)
    return cli_policy.evaluate([active] + copy.deepcopy(others), {**SETTINGS, **settings}, copy.deepcopy(history), NOW)


def table():
    return {row: " ".join(code(decide(row, column)) for column in COLUMNS) for row in ROWS}


# The answers at the starting commit (1c1787b), apart from two fixed bugs. A 5-hour window that ended 40 s ago used
# to count with its old number (Tb, Ob at 96% and up). Now it is waited for (hW) and the weekly limit decides.
# A manual choice or Undo in the last 2 hours used to hold only the early switch. Now it holds the threshold
# switch too (hm where the two manual rows had Tb), but not an account that ran out or needs sign-in.
# 5-hour % used:
#                                                      20                96                99                100
# 5-hour reset (in 4 h, in 4 min, 40 s ago), each with weekly 30 then 96:
#                                                      4h    4m    ago   4h    4m    ago   4h    4m    ago   4h    4m    ago
EXPECTED = {
    "Beta has room":                                   ".. Tb .. Tb .. Tb Tb Tb hr Tb hW Tb Tb Tb hr Tb hW Tb Ob Ob Ob Ob hW Tb",
    "Beta's reading is 20 min old":                    ".. hC .. hC .. hC hC hC hr hC hW hC hC hC hr hC hW hC hC hC hC hC hW hC",
    "Beta was never read":                             ".. hd .. hd .. hd hd hd hr hd hW hd XX XX hr XX hW hd XX XX XX XX hW hd",
    "Beta's weekly is at 90%":                         ".. hd .. hd .. hd hd hd hr hd hW hd Tb Tb hr Tb hW hd Ob Ob Ob Ob hW hd",
    "Beta's weekly is at 98%":                         ".. hd .. hd .. hd hd hd hr hd hW hd XX XX hr XX hW hd XX XX XX XX hW hd",
    "Beta's weekly is unknown":                        ".. hd .. hd .. hd hd hd hr hd hW hd XX XX hr XX hW hd XX XX XX XX hW hd",
    "Beta needs sign-in":                              ".. hd .. hd .. hd hd hd hr hd hW hd XX XX hr XX hW hd XX XX XX XX hW hd",
    "Beta is left out":                                ".. hd .. hd .. hd hd hd hr hd hW hd XX XX hr XX hW hd XX XX XX XX hW hd",
    "Beta belongs to the PC":                          ".. hd .. hd .. hd hd hd hr hd hW hd XX XX hr XX hW hd XX XX XX XX hW hd",
    "Alpha is the only account":                       ".. hd .. hd .. hd hd hd hr hd hW hd XX XX hr XX hW hd XX XX XX XX hW hd",
    "Beta is 20 min old, Gamma at 90%":                ".. hC .. hC .. hC hC hC hr hC hW hC hC hC hr hC hW hC hC hC hC hC hW hC",
    "Gamma's week ends sooner":                        ".. Tg .. Tg .. Tg Tg Tg hr Tg hW Tg Tg Tg hr Tg hW Tg Og Og Og Og hW Tg",
    "Gamma's week ends sooner, most room first":       ".. Tb .. Tb .. Tb Tb Tb hr Tb hW Tb Tb Tb hr Tb hW Tb Ob Ob Ob Ob hW Tb",
    "Gamma's week ends sooner, my order Beta first":   ".. Tb .. Tb .. Tb Tb Tb hr Tb hW Tb Tb Tb hr Tb hW Tb Ob Ob Ob Ob hW Tb",
    "Undo pressed 5 s ago":                            "hm hm hm hm hm hm hm hm hr hm hW hm hm hm hr hm hW hm Ob Ob Ob Ob hW hm",
    "Undo pressed 5 s ago, Alpha needs sign-in":       "Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb",
    "Undo pressed 5 s ago, Alpha is marked out":       "Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob",
    "Manual choice 2 h 5 s ago":                       ".. Tb .. Tb .. Tb Tb Tb hr Tb hW Tb Tb Tb hr Tb hW Tb Ob Ob Ob Ob hW Tb",
    "Early switch 1 h ago":                            "he Tb he Tb he Tb Tb Tb hr Tb hW Tb Tb Tb hr Tb hW Tb Ob Ob Ob Ob hW Tb",
    "Every account was low before":                    ".. Rb .. Rb .. Rb Rb Rb hr Rb hW Rb Rb Rb hr Rb hW Rb Rb Rb Rb Rb hW Rb",
    "Auto-switch is off":                              ".. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. ..",
    "Early switch is off":                             ".. Tb .. Tb .. Tb Tb Tb hr Tb hW Tb Tb Tb hr Tb hW Tb Ob Ob Ob Ob hW Tb",
    "Threshold is 80%":                                ".. Tb .. Tb .. Tb Tb Tb hr Tb hW Tb Tb Tb hr Tb hW Tb Ob Ob Ob Ob hW Tb",
    "Alpha's login expired while idle":                "hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx hx",
    "Alpha needs sign-in":                             "Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb Lb",
    "Alpha is marked out":                             "Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob Ob",
    "Alpha belongs to the PC":                         ".. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. .. ..",
    "Alpha's 5-hour is forecast at 97%":               "Tb Tb hr Tb .. Tb Tb Tb hr Tb hW Tb Tb Tb hr Tb hW Tb Ob Ob Ob Ob hW Tb",
    "Alpha's pinned Fable limit is at 96%":            "Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Tb Ob Ob Ob Ob Tb Tb",
    "Beta's week ends in 12 h":                        "Eb Tb Eb Tb Eb Tb Tb Tb hr Tb hW Tb Tb Tb hr Tb hW Tb Ob Ob Ob Ob hW Tb",
    "Beta's week ends in 12 h, 85% used":              ".. Tb .. Tb .. Tb Tb Tb hr Tb hW Tb Tb Tb hr Tb hW Tb Ob Ob Ob Ob hW Tb",
    "Beta's week ends in 12 h, left 10 min ago":       ".. Tb .. Tb .. Tb Tb Tb hr Tb hW Tb Tb Tb hr Tb hW Tb Ob Ob Ob Ob hW Tb",
    "Beta's week ends in 12 h, manual choice 5 s ago": "hm hm hm hm hm hm hm hm hr hm hW hm hm hm hr hm hW hm Ob Ob Ob Ob hW hm",
    "Beta's week ends in 12 h, early switch 1 h ago":  "he Tb he Tb he Tb Tb Tb hr Tb hW Tb Tb Tb hr Tb hW Tb Ob Ob Ob Ob hW Tb",
}


class AutoSwitchTableTest(unittest.TestCase):
    """Every decision of `cli_policy.evaluate` for 34 situations times 24 readings of the account in use."""
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
            "has refilled": self.result("Beta has room", five=96, reset=-40)["hold"],
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
            "has refilled": "Alpha's 5-hour limit has refilled. Waiting for its new reading.",
            "checking": "Checking Beta before switching",
            "using up": "Every account is past 95% — using Alpha up to 99%",
            "login expired": "Usage updates after your next message in Claude Code",
            "early too recent": "An early switch happened less than 2 hours ago",
            "manual choice": "Your manual choice is held for 2 hours",
        })

    def test_checking_asks_for_the_old_readings(self):
        self.assertEqual(self.result("Beta is 20 min old, Gamma at 90%", five=96)["refresh"], ["beta"])

    def test_a_refilled_limit_asks_for_a_new_reading_of_the_account_in_use(self):
        self.assertEqual(self.result("Beta has room", five=100, reset=-1)["refresh"], ["alpha"])

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


# ---- readings, for all four clients ----
CLIENTS = ("claude_cli", "claude", "codex", "muse")
WINDOWS = {"claude_cli": ("five_hour", "seven_day"), "claude": ("five_hour", "seven_day"),
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
        session, weekly = WINDOWS[provider]
        before = [{"key": session, "used": 92.0, "reset": now - 120}, {"key": weekly, "used": 40.0, "reset": now + 3 * DAY}]
        after = [{"key": session, "used": 3.0, "reset": now + 5 * HOUR}, {"key": weekly, "used": 41.0, "reset": now + 3 * DAY}]
        engine.store.observe(row["id"], demo._usage(provider, demo._payload(provider, before), now - 600), at=now - 600)
        engine.alert_engine.hold_until = 0
        engine._evaluate_alerts(now)                              # the first pass only notes what already holds
        had, alerted = len(engine.store.refills(0)), {a["id"] for a in engine.store.alerts()}
        answer(engine, provider, after)
        self.assertEqual(engine._refresh(row["id"]), "ok")
        new = engine.store.refills(0)[had:]
        self.assertEqual([(r["account"], r["window"], r["previous_used"]) for r in new], [(row["id"], session, 92.0)])
        window = next(w for w in shown(engine, row["id"])["windows"] if w["id"] == session)
        self.assertEqual((window["state"], window["used"]), ("refilled", 3.0))
        engine._evaluate_alerts(now)
        self.assertEqual([(a["kind"], a["account"], a["window"]) for a in engine.store.alerts() if a["id"] not in alerted],
                         [("reset_confirmed", row["id"], session)])
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
        self.assertEqual(engine.snapshot()["report"]["error"]["message"], "The account in use by your terminals cannot be removed.")
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


# The answers at the starting commit (1c1787b).
PICKS = {
    'terminal': ['claude acc_4 ok Most headroom (91% left) and a fresh reading | acc_4 acc_5 acc_3 acc_2', 'claude_cli cli_gamma ok Most headroom (80% left) and a fresh reading | cli_gamma cli_beta cli_delta cli_alpha', 'muse acc_m2 ok Most headroom (88% left) and a fresh reading | acc_m2 acc_m1'],
    'typical': ['claude acc_4 ok Most headroom (91% left) and a fresh reading | acc_4 acc_5 acc_3 acc_2', 'claude_cli cli_gamma ok Most headroom (80% left) and a fresh reading | cli_gamma cli_beta cli_alpha cli_delta', 'muse acc_m2 ok Most headroom (88% left) and a fresh reading | acc_m2 acc_m1'],
    'resets-soon': ['claude acc_5 ok Most headroom (70% left) and a fresh reading | acc_5 acc_3 acc_4 acc_2', 'claude_cli cli_gamma ok Most headroom (80% left) and a fresh reading | cli_gamma cli_beta cli_delta cli_alpha'],
    'refilled': ['claude acc_3 ok Most headroom (85% left) and a fresh reading | acc_3 acc_2 acc_1'],
    'not-started': ['claude acc_3 ok Most headroom (90% left) and a fresh reading | acc_3 acc_1 acc_2'],
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
    'fifty': ['claude acc_31 ok Most headroom (94% left) and a fresh reading | acc_31 acc_32 acc_1 acc_33', 'codex acc_48 ok Most headroom (89% left) and a fresh reading | acc_48 acc_42 acc_49 acc_43'],
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
