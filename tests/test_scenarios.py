"""Every demo scenario builds, ticks and validates against the state contract (safe mode, synthetic data)."""
import os
import shutil
import tempfile
import unittest

os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"
os.environ["ACCOUNTDECK_DEMO"] = "1"

from accountdeck import cli_auto, contract, demo, fixtures  # noqa: E402


class ScenarioTest(unittest.TestCase):
    def test_fixture_states_validate(self):
        for scenario in fixtures.SCENARIOS:
            with self.subTest(scenario=scenario):
                self.assertEqual(contract.validate_state(fixtures.example_state(scenario)), [])

    def test_fixture_plans_validate(self):
        for name in fixtures.PLANS:
            with self.subTest(plan=name):
                self.assertEqual(contract.validate_plan(fixtures.example_plan(name)), [])

    def test_demo_scenarios_build_and_validate(self):
        for scenario in demo.SCENARIOS:
            with self.subTest(scenario=scenario):
                root = tempfile.mkdtemp(prefix="ad2-scenario-")
                engine = None
                try:
                    engine = demo.build_demo(root, scenario, start=False, inline=True)
                    engine.tick()
                    state = engine.snapshot()
                    self.assertEqual(contract.validate_state(state), [])
                finally:
                    if engine is not None:
                        engine.stop()
                    shutil.rmtree(root, ignore_errors=True)

    def test_the_example_of_paused_reads_says_the_paused_account_is_passed_over(self):
        """The example states hand the rule the page's own account dicts (`fixtures.terminal_autoswitch`). In
        `rate-limited` Claude has paused Beta's reads. The rule goes by that pause, which those dicts carry, so
        the example says what the engine says on the same data: Beta is passed over."""
        state = fixtures.example_state("rate-limited")
        beta = next(a for a in state["accounts"] if a["provider"] == "claude_cli" and a["label"] == "Beta")
        self.assertGreater(beta["paused_until"], state["now"])
        skips = {row["id"]: row["skip"] for row in state["providers"]["claude_cli"]["autoswitch"]["next_up"]}
        self.assertEqual(skips[beta["id"]], "usage checks paused")

    def test_the_demos_name_claudes_limits_as_the_parser_does(self):
        """The demo data writes its limits out by hand (`fixtures._SHAPES`). A name changed in the parser and
        not there would stay in every demo and in every picture of the look sweep."""
        from accountdeck import usage
        claude = [key for key in fixtures._SHAPES if key == "five_hour" or key.startswith("seven_day")]
        self.assertIn("seven_day_fable", claude)
        for key in claude:
            with self.subTest(key=key):
                self.assertEqual(fixtures._SHAPES[key], usage._describe(key))

    def test_no_demo_moves_its_terminals_by_itself(self):
        """A demo that switches by itself shows another page than the scenario's picture, and that switch races
        whatever stops the demo. `fifty` did at its first check, once its 35 accounts were Claude Code's: the
        early switch moved the terminals to the account whose week ends first. Three more did about 10 seconds
        after their start, once the early switch had no 24-hour limit: the first check only asks for a new
        reading of the account it wants, so the check here is what the rule decides, not only what it starts.
        Two more did minutes later, when their own script changed the readings: `resetting` after its refill
        at 150 seconds, `outage` once its incident ended at 600. So the demo clock is moved on here, 25 minutes
        in steps of 30 seconds, and the early switch has to start nothing and decide nothing the whole way.
        Both are asked: a check that starts an early switch writes down that it tried, and the rule then
        holds, so the decision after that check no longer shows it. A demo's script may still take the
        account in use to the threshold (`typical` does, about 10 minutes in): that is the threshold rule's
        move, not the early switch's, and it is left alone here.
        A job a scenario's picture shows is started when the demo is built, before this check."""
        for scenario in demo.SCENARIOS:
            with self.subTest(scenario=scenario):
                root = tempfile.mkdtemp(prefix="ad2-scenario-")
                engine = None
                try:
                    engine = demo.build_demo(root, scenario, start=False, inline=True)
                    # `terminal` and `resets-soon` are built with a switch at the threshold, and the early switch
                    # waits 2 hours after any automatic switch. A demo runs longer than that. So the switches in
                    # the history are dated 3 hours back here, and no wait hides a move the demo would make later.
                    with cli_auto.history(engine) as past:
                        for key in ("left_at", "early_at", "early_tried"):
                            if key in past:
                                past[key] -= 3 * 3600
                        if past.get("last"):
                            past["last"]["at"] -= 3 * 3600
                    submitted = []       # kept, never run: the job, its client and why auto-switch started it
                    engine.submit = lambda op: submitted.append((op.kind, op.provider, getattr(op, "auto_kind", None)))
                    engine.tick()
                    self.assertEqual(submitted, [])
                    wants = cli_auto.decision(engine)
                    self.assertEqual((wants["action"], wants["target"], wants["reason"]), ("none", None, ""))
                    for later in range(30, 25 * 60 + 1, 30):
                        engine.clock.advance(30)
                        engine.tick()
                        wants = cli_auto.decision(engine)
                        self.assertNotEqual(wants["kind"], "early", f"{later} seconds in: {wants['reason']}")
                        self.assertNotIn("early", [why for _job, _client, why in submitted], f"{later} seconds in")
                finally:
                    if engine is not None:
                        engine.stop()
                    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
