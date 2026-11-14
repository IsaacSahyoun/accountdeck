"""Only one job writes logins at a time, and Recover only touches switches that are still unfinished.
Found by the orchestrator while reading the merged refactor on 2026-11-12. Synthetic data only."""
import threading
import time
import unittest

from tests import support

from accountdeck import switch_cli, switching
from accountdeck.core import DeckError, FileTransaction
from accountdeck.operations import Operation, OperationRunner


class WaitThenWrite(Operation):
    """A switch of another client: waits for its sessions, then writes."""

    def __init__(self):
        super().__init__("switch", provider="codex", label="stand-in", consent="idle",
                         steps=(("wait", "Waiting for sessions"), ("restore", "Writing the login")))
        self.sessions_closed, self.writing, self.finish = threading.Event(), threading.Event(), threading.Event()

    def run(self):
        with self.step("wait", phase="waiting_idle", message="Waiting for sessions to close…"):
            self.sessions_closed.wait(10)
        with self.step("restore", phase="restoring", message="Writing…", cancellable=False):
            self.writing.set()
            self.finish.wait(10)


class Beside(Operation):
    """A terminal switch, which may run beside another client's wait. It holds until the test sets `finish`.
    `let_go_by_the_test` is False when it gave up waiting instead: whatever the test did after that was not
    done beside it."""
    beside_waits = True
    let_go_by_the_test = None

    def __init__(self):
        super().__init__("switch", provider="claude_cli", label="stand-in", consent="now",
                         steps=(("switch", "Switching the terminal login"),))
        self.running, self.finish = threading.Event(), threading.Event()

    def run(self):
        with self.step("switch", phase="restoring", message="Switching…", cancellable=False):
            self.running.set()
            self.let_go_by_the_test = self.finish.wait(10)


def until(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


class OneWriterTest(unittest.TestCase):
    def setUp(self):
        self.runner = OperationRunner()
        self.main, self.beside = WaitThenWrite(), Beside()
        for op in (self.main, self.beside):
            self.addCleanup(op.finish.set)
        self.addCleanup(self.main.sessions_closed.set)
        self.runner.submit(self.main)
        self.assertTrue(until(lambda: self.main.phase == "waiting_idle"))

    def test_a_waiting_job_does_not_start_writing_until_the_job_beside_it_has_ended(self):
        self.runner.submit(self.beside)
        self.assertTrue(self.beside.running.wait(5))
        self.main.sessions_closed.set()           # the sessions closed while the terminal switch is mid-way
        self.assertFalse(self.main.writing.wait(0.5), "two jobs were writing logins at the same time")
        self.assertEqual(self.main.phase, "waiting_idle")
        self.beside.finish.set()
        self.assertTrue(self.main.writing.wait(5), "the waiting job never went on after the other ended")
        self.main.finish.set()
        self.assertTrue(self.runner.wait(5))

    def test_nothing_starts_beside_a_job_that_is_about_to_write(self):
        self.main.sessions_closed.set()
        self.assertTrue(self.main.writing.wait(5))
        with self.assertRaises(DeckError) as refused:
            self.runner.submit(self.beside)
        self.assertEqual(refused.exception.code, "busy")


class RecoverLeavesFinishedSwitchesAloneTest(unittest.TestCase):
    def test_a_stale_recover_list_never_undoes_a_switch_that_finished(self):
        engine = support.demo_engine(self, "terminal")
        rows = support.terminal_rows(engine)
        target = next(a for a in rows if not a["active"] and a.get("health", "ok") == "ok" and not a.get("excluded"))
        identity = engine.cli.row(target["id"])["identity"]
        before = {path.name for path in engine.vault.root.glob("transaction-*")}
        switch_cli.perform(engine.cli, target["id"])
        self.assertEqual(support.terminal_on_disk(engine), {"name": identity, "token": identity})
        records = [path.name.split(".")[0] for path in engine.vault.root.glob("transaction-*") if path.name not in before]
        finished = [key for key in records
                    if FileTransaction.load(engine.vault, engine.roots(), key).doc["status"] == "complete"]
        self.assertTrue(finished, "the finished switch left no record to test with")
        # The list was read while that switch was still running (another job finished at that moment).
        engine.recovery = list(finished)
        switching.dispatch(engine, "recover", {})
        engine.runner.wait(30)
        self.assertEqual(support.terminal_on_disk(engine), {"name": identity, "token": identity},
                         "Recover undid a switch that had finished")
        self.assertEqual(engine._scan_recovery(), [])

    def test_two_jobs_finishing_together_leave_the_newest_list_of_unfinished_switches(self):
        engine = support.demo_engine(self, "terminal")
        first_is_looking, let_first_finish, calls = threading.Event(), threading.Event(), []

        def scan():
            calls.append(True)
            if len(calls) == 1:               # the first job to finish looks while the other one's record is open
                first_is_looking.set()
                let_first_finish.wait(10)
                return ["transaction-seen-while-still-running"]
            return []                         # the second looks later, after that record was closed

        engine._scan_recovery = scan
        self.addCleanup(let_first_finish.set)
        finished = Beside()
        first = threading.Thread(target=engine._finished, args=(finished, None))
        second = threading.Thread(target=engine._finished, args=(finished, None))
        first.start()
        self.assertTrue(first_is_looking.wait(5))
        second.start()
        second.join(0.3)                      # room for the later look to land first, if nothing orders them
        let_first_finish.set()
        first.join(5)
        second.join(5)
        self.assertEqual(engine.recovery, [], "an older look at the records replaced a newer one")


if __name__ == "__main__":
    unittest.main()
