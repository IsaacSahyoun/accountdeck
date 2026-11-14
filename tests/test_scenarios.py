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
        in steps of 30 seconds, and the early switch has to decide nothing the whole way. A demo's script may
        still take the account in use to the threshold (`typical` does, about 10 minutes in): that is the
        threshold rule's move, not the early switch's, and it is left alone here.
        A job a scenario's picture shows is started when the demo is built, before this check."""
        for scenario in demo.SCENARIOS:
            with self.subTest(scenario=scenario):
                root = tempfile.mkdtemp(prefix="ad2-scenario-")
                engine = None
                try:
                    engine = demo.build_demo(root, scenario, start=False, inline=True)
                    submitted = []
                    engine.submit = lambda op: submitted.append((op.kind, op.provider))    # kept, never run
                    engine.tick()
                    self.assertEqual(submitted, [])
                    wants = cli_auto.decision(engine)
                    self.assertEqual((wants["action"], wants["target"], wants["reason"]), ("none", None, ""))
                    for later in range(30, 25 * 60 + 1, 30):
                        engine.clock.advance(30)
                        engine.tick()
                        wants = cli_auto.decision(engine)
                        self.assertNotEqual(wants["kind"], "early", f"{later} seconds in: {wants['reason']}")
                finally:
                    if engine is not None:
                        engine.stop()
                    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
