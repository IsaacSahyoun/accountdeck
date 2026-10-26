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


if __name__ == "__main__":
    unittest.main()
