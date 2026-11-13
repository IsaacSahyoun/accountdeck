"""The proof tools in tools/verify/ and the review audit decide whether a change counts as proven. These tests
hold them to what they claim, on scratch folders and fake data only: a demo's cleanup removes its own folder
and no other run's, the take-the-fix-out check does not count a broken run as proof, the review audit passes
only with three whole answers, and the Chrome sweep fails on a page that leaves an account out."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

from accountdeck import fixtures
from tests.test_dashboard import DashboardTestCase

ROOT = Path(__file__).resolve().parent.parent
VERIFY = ROOT / "tools" / "verify"


def tool(path):
    """A proof tool as a module. The tools find each other by putting their folder on `sys.path`; that is
    undone here, so loading one changes nothing for the other tests."""
    before = list(sys.path)
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(f"proof_tool_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = before
    return module


def scratch(case, prefix="accountdeck-test-tools-"):
    folder = Path(tempfile.mkdtemp(prefix=prefix))
    case.addCleanup(shutil.rmtree, folder, True)
    return folder


class DemoCleanupTest(unittest.TestCase):
    def test_stopping_a_demo_leaves_the_folder_another_run_made_while_it_started(self):
        # "Another run" is played by the demo's own launcher: it makes a demo folder in the shared temp
        # folder after this demo's start has begun and before the demo prints its address.
        other = Path(tempfile.gettempdir()) / f"accountdeck2-demo-other-run-{uuid.uuid4().hex[:10]}"
        self.addCleanup(shutil.rmtree, other, True)
        launcher = scratch(self) / "python"
        launcher.write_text(f'#!/bin/sh\nmkdir "{other}" && echo data > "{other}/vault"\nexec "{sys.executable}" "$@"\n')
        launcher.chmod(0o755)
        demo = tool(VERIFY / "demo_run.py").Demo(str(launcher), str(ROOT), "typical")
        self.addCleanup(shutil.rmtree, demo.home, True)         # when the stop below is broken, nothing is left here
        try:
            self.assertEqual(demo.call("/api/state")[0], 401, "the demo is up and wants a session")
            own = [str(path) for path in Path(demo.home).iterdir()]
            self.assertEqual(len(own), 1, "the demo keeps its data in the temp folder it was given")
        finally:
            demo.stop()
        self.assertEqual((other / "vault").read_text(), "data\n", "the stop removed another run's demo folder")
        self.assertFalse(Path(demo.home).exists(), "the demo's own temp folder is removed")
        self.assertEqual((demo.stopped, demo.ended, demo.left), (200, 0, []))


    def test_a_demo_that_never_prints_its_address_is_ended_and_its_folder_removed(self):
        launcher = scratch(self) / "python"
        launcher.write_text("#!/bin/sh\nexec sleep 30\n")
        launcher.chmod(0o755)
        demo_run, made = tool(VERIFY / "demo_run.py"), []
        real = demo_run.tempfile.mkdtemp

        def note(**how):
            made.append(real(**how))
            return made[-1]
        started = time.monotonic()
        with mock.patch.object(demo_run.Demo, "START_LIMIT", 0.3), mock.patch.object(demo_run.tempfile, "mkdtemp", note):
            with self.assertRaisesRegex(RuntimeError, "no demo address"):
                demo_run.Demo(str(launcher), str(ROOT), "typical")
        self.assertLess(time.monotonic() - started, 15, "it waited for the silent demo to end by itself")
        self.assertEqual([os.path.isdir(folder) for folder in made], [False], "its temp folder is removed")

    def test_a_sweep_that_is_killed_stops_the_demo_it_started(self):
        # the burst tool, with far more rounds than it gets to: it is killed while its demo is up
        sweep = subprocess.Popen([sys.executable, "-E", "-s", str(VERIFY / "static_burst.py"), sys.executable, str(ROOT), "2", "5000"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(sweep.wait)
        self.addCleanup(sweep.kill)
        deadline, demos = time.monotonic() + 30, []
        while not demos and time.monotonic() < deadline:
            listed = subprocess.run(["pgrep", "-P", str(sweep.pid)], capture_output=True, text=True).stdout.split()
            demos = [int(pid) for pid in listed]
            time.sleep(0.05)
        self.assertEqual(len(demos), 1, "the sweep started its demo")
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(demos[0])], capture_output=True))
        sweep.terminate()
        self.assertEqual(sweep.wait(timeout=40), 143)
        self.assertNotEqual(subprocess.run(["kill", "-0", str(demos[0])], capture_output=True).returncode, 0,
                            "the demo the sweep started is still running")


class TakeFixOutTest(unittest.TestCase):
    """A tiny package with one fix and its tests, and what the check says about each way a case can go."""

    def setUp(self):
        self.copy = scratch(self)
        (self.copy / "app").mkdir()
        (self.copy / "tests").mkdir()
        (self.copy / "app" / "__init__.py").write_text("")
        (self.copy / "tests" / "__init__.py").write_text("")
        (self.copy / "app" / "sums.py").write_text("def double(n):\n    return n * 2\n\n\ndef triple(n):\n    return n * 3\n")
        (self.copy / "app" / "page.js").write_text("function label(n) { return n + '%'; }\n")
        (self.copy / "tests" / "test_sums.py").write_text(
            "import unittest\nfrom app import sums\n\n\nclass SumsTest(unittest.TestCase):\n"
            "    def test_double(self):\n        self.assertEqual(sums.double(4), 8)\n\n"
            "    def test_broken_from_the_start(self):\n        self.assertEqual(sums.double(4), 9)\n\n"
            "    def test_the_page_file_is_there(self):\n"
            "        import pathlib\n        self.assertIn('label', pathlib.Path('app/page.js').read_text())\n\n"
            "    @unittest.skip('not on this Mac')\n    def test_skipped(self):\n        self.fail('never runs')\n")

    def check(self, **case):
        case = {"name": "the fix", "file": "app/sums.py", "old": "return n * 2", "test": "tests.test_sums.SumsTest.test_double", **case}
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            code = tool(VERIFY / "take_fix_out.py").main(self.copy, sys.executable, [case])
        self.assertEqual((self.copy / "app" / "sums.py").read_text().count("return n * 2"), 1, "the file is put back")
        return code, said.getvalue()

    def test_a_test_that_fails_without_its_fix_is_proof(self):
        # The fix and what replaces it are the same size, and the run starts early in a second, so the file
        # with the fix in and the file with it out have the same size and second of change. Python takes a
        # compiled copy for current on those two facts, and the check must not run the old one.
        time.sleep(1.05 - time.time() % 1)
        (self.copy / "app" / "sums.py").write_text((self.copy / "app" / "sums.py").read_text())
        code, said = self.check(new="return n * 1")
        self.assertEqual(code, 0, said)
        self.assertIn("fails without 'the fix', as it should", said)
        self.assertIn("1 of 1 tests fail without their fix", said)

    def test_a_test_that_still_passes_is_no_proof(self):
        code, said = self.check(new="return n + n")
        self.assertEqual(code, 1, said)
        self.assertIn("STILL PASSES", said)

    def test_a_change_that_no_longer_parses_is_no_proof(self):
        code, said = self.check(new="return n *")
        self.assertEqual(code, 1, said)
        self.assertIn("PROVES NOTHING 'the fix': with the fix taken out, app/sums.py no longer parses", said)
        self.assertIn("0 of 1 tests fail without their fix, 1 cases prove nothing", said)

    def test_a_javascript_change_that_no_longer_parses_is_no_proof(self):
        code, said = self.check(file="app/page.js", old="return n + '%';", new="return n + ;",
                                test="tests.test_sums.SumsTest.test_the_page_file_is_there")
        self.assertEqual(code, 1, said)
        self.assertIn("app/page.js no longer parses", said)

    def test_a_test_that_does_not_exist_is_no_proof(self):
        code, said = self.check(new="return n * 1", test="tests.test_sums.SumsTest.test_no_such_test")
        self.assertEqual(code, 1, said)
        self.assertIn("PROVES NOTHING 'the fix': with the fix in, tests.test_sums.SumsTest.test_no_such_test does not run", said)

    def test_a_test_that_fails_with_the_fix_in_is_no_proof(self):
        code, said = self.check(new="return n * 1", test="tests.test_sums.SumsTest.test_broken_from_the_start")
        self.assertEqual(code, 1, said)
        self.assertIn("with the fix in, tests.test_sums.SumsTest.test_broken_from_the_start fails", said)

    def test_a_skipped_test_is_no_proof(self):
        code, said = self.check(new="return n * 1", test="tests.test_sums.SumsTest.test_skipped")
        self.assertEqual(code, 1, said)
        self.assertIn("the test was skipped", said)

    def test_a_change_that_stops_the_test_from_loading_is_no_proof(self):
        # the test file imports the changed file, and without the name it asks for, no test in it loads
        (self.copy / "tests" / "test_sums.py").write_text(
            "import unittest\nfrom app.sums import double\n\n\nclass SumsTest(unittest.TestCase):\n"
            "    def test_double(self):\n        self.assertEqual(double(4), 8)\n")
        code, said = self.check(old="def double(n):", new="def twice(n):")
        self.assertEqual(code, 1, said)
        self.assertIn("with the fix taken out, the test did not load", said)


class ReviewAuditTest(unittest.TestCase):
    WHOLE = "## Findings\n\n### 1. [warning] A finding\n" + "words " * 200 + "\n\n## Coverage\nRead everything.\n\nEND OF REVIEW\n"

    def setUp(self):
        self.review = tool(ROOT / ".claude" / "skills" / "accountdeck-review" / "tools" / "review.py")
        self.review.ROOT = scratch(self)
        folder = self.review.ROOT / "trial"
        for part in ("code", "out", "logs", "lead"):
            (folder / part).mkdir(parents=True)
        (folder / "code" / "run.py").write_text("print('hello')\n")
        (folder / "logs" / "manifest.before").write_text("\n".join(self.review.manifest(folder)) + "\n")
        self.folder = folder

    def audit(self):
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            return self.review.audit("trial"), said.getvalue()

    def answers(self, **texts):
        for name, text in texts.items():
            (self.folder / "out" / f"{name}.md").write_text(text)

    def test_no_answers_is_not_a_finished_review(self):
        code, said = self.audit()
        self.assertEqual(code, 1, said)
        self.assertIn("0 of 3 reviews are whole", said)

    def test_two_whole_answers_are_not_a_finished_review(self):
        self.answers(opus=self.WHOLE, sol=self.WHOLE)
        code, said = self.audit()
        self.assertEqual(code, 1, said)
        self.assertIn("2 of 3 reviews are whole", said)

    def test_three_whole_answers_pass(self):
        self.answers(opus=self.WHOLE, sonnet=self.WHOLE, sol=self.WHOLE)
        code, said = self.audit()
        self.assertEqual(code, 0, said)
        self.assertNotIn("reviews are whole", said)

    def test_a_cut_off_answer_does_not_count_as_one_of_the_three(self):
        self.answers(opus=self.WHOLE, sonnet=self.WHOLE, sol="You have hit your usage limit.\n")
        code, said = self.audit()
        self.assertEqual(code, 1, said)
        self.assertIn("2 of 3 reviews are whole", said)

    def test_an_answer_without_its_last_line_does_not_count(self):
        # a reviewer keeps a Coverage section in the file from its first finding, so that section says nothing
        self.answers(opus=self.WHOLE, sonnet=self.WHOLE, sol=self.WHOLE.replace("END OF REVIEW\n", ""))
        code, said = self.audit()
        self.assertEqual(code, 1, said)
        self.assertIn('sol.md: 1 KB, 1 findings, no last line "END OF REVIEW"', said)
        self.assertIn("2 of 3 reviews are whole", said)

    def test_a_changed_copy_fails_even_with_three_whole_answers(self):
        self.answers(opus=self.WHOLE, sonnet=self.WHOLE, sol=self.WHOLE)
        (self.folder / "code" / "run.py").write_text("print('changed')\n")
        code, said = self.audit()
        self.assertEqual(code, 1, said)
        self.assertIn("CHANGED", said)


class ChromeSweepRowsTest(DashboardTestCase):
    """The sweep reads account rows out of a page's HTML. This checks that reading against the page the
    dashboard really draws, and that a row which is gone, doubled or in the wrong list fails."""

    def drawn(self, state):
        return tool(VERIFY / "dashboard_sweep.py").not_drawn(self.browser.js("document.documentElement.outerHTML"), state["accounts"])

    def test_a_whole_page_passes_and_a_page_that_leaves_an_account_out_fails(self):
        for scenario in ("typical", "fifty", "empty", "first-run"):
            with self.subTest(scenario=scenario):
                state = fixtures.example_state(scenario, now=time.time())
                self.show(state)
                self.assertEqual(self.drawn(state), [])
        state = fixtures.example_state("typical", now=time.time())
        self.show(state)
        gone = self.browser.js("(() => { const row = document.querySelector('#other-accounts li.row');"
                               " const name = row.querySelector('.row-name').textContent; row.remove(); return name; })()")
        self.assertEqual(self.drawn(state), [f"#other-accounts drew 7 of 8 rows, missing ['{gone}']"])

    def test_a_row_drawn_twice_or_in_the_wrong_list_fails(self):
        state = fixtures.example_state("typical", now=time.time())
        self.show(state)
        self.browser.js("(() => { const row = document.querySelector('#accounts li.row');"
                        " row.parentNode.append(row.cloneNode(true)); })()")
        self.assertIn("#accounts drew 6 of 5 rows, not its own or twice", "".join(self.drawn(state)))
        self.show(state)
        self.browser.js("(() => { document.querySelector('#other-accounts ul, #other-accounts ol')"
                        ".append(document.querySelector('#accounts li.row')); })()")
        wrong = self.drawn(state)
        self.assertEqual(len(wrong), 2, wrong)
        self.assertIn("#accounts drew 4 of 5 rows, missing", wrong[0])
        self.assertIn("#other-accounts drew 9 of 8 rows, not its own or twice", wrong[1])


if __name__ == "__main__":
    unittest.main()
