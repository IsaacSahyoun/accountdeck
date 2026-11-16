"""Shared races reproduced while checking manual resets, including on the installed build."""
from unittest import mock

from accountdeck.operations import Operation
from tests.test_switch_safety import DeckCase


class SnapshotConsistencyTest(DeckCase):
    def test_accepted_cancellation_stops_a_non_cancellable_phase(self):
        """The shared Operation contract; this does not exercise a client's switch class."""
        effects, cancelled = [], []
        engine = self.engine
        class PhaseOperation(Operation):
            def run(op):
                op.set_phase("stopping", cancellable=False)
                effects.append(True)
                with op.step("save", phase="saving", cancellable=False):
                    pass

            def _moving_to(op, phase):
                super()._moving_to(phase)
                if phase == "stopping" and not cancelled:
                    cancelled.append(engine.runner.cancel(op.id))

        op = PhaseOperation("switch", provider="codex", steps=(("save", "Save"),))
        engine.submit(op)
        self.assertTrue(engine.runner.wait(10))
        self.assertEqual(cancelled, [True])
        self.assertEqual(effects, [])

    def test_removal_during_a_snapshot_keeps_one_account_roster(self):
        key = self.spare("claude_cli")["id"]
        build_row, removed = self.engine._raw_account, []
        def remove_after_capture(*args):
            if not removed:
                removed.append(True)
                self.engine.store.remove(key)
            return build_row(*args)
        self.engine.bump()
        with mock.patch.object(self.engine, "_raw_account", side_effect=remove_after_capture):
            state = self.engine.snapshot()
        rows = {r["id"] for r in state["accounts"] if r["provider"] == "claude_cli" and r["saved"]}
        self.assertEqual({r["id"] for r in state["providers"]["claude_cli"]["autoswitch"]["next_up"]}, rows)

    def test_accepted_cancellation_stops_a_write_step(self):
        written, cancelled = [], []
        engine = self.engine
        class WriteOperation(Operation):
            def run(op):
                with op.step("write", phase="saving", cancellable=False):
                    written.append(True)
                with op.step("verify", phase="verifying", cancellable=False):
                    pass

            def _step(op, name):
                step = super()._step(name)
                if name == "write" and not cancelled:
                    cancelled.append(engine.runner.cancel(op.id))
                return step

        op = WriteOperation("switch", provider="claude_cli", steps=(("write", "Write"), ("verify", "Verify")))
        engine.submit(op)
        self.assertTrue(engine.runner.wait(10))
        self.assertEqual(cancelled, [True])
        self.assertEqual(written, [])
