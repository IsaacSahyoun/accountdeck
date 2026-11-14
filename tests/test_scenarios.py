"""Every demo scenario builds, ticks and validates against the state contract (safe mode, synthetic data)."""
import os
import shutil
import tempfile
import unittest

os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"
os.environ["ACCOUNTDECK_DEMO"] = "1"

from accountdeck import contract, demo, fixtures  # noqa: E402


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

    def test_no_demo_starts_a_job_at_its_first_check(self):
        """A demo that switches by itself at its first check shows another page than the scenario's picture, and
        that switch races whatever stops the demo. `fifty` did, once its 35 accounts were Claude Code's: the early
        switch moved the terminals to the account whose week ends first. A job a scenario's picture shows is
        started when the demo is built, before this check."""
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
                finally:
                    if engine is not None:
                        engine.stop()
                    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
