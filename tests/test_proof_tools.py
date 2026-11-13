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
import signal
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
            self.addCleanup(shutil.rmtree, made[-1], True)      # when the tool is broken, nothing is left here
            return made[-1]
        started = time.monotonic()
        with mock.patch.object(demo_run.Demo, "START_LIMIT", 0.3), mock.patch.object(demo_run.tempfile, "mkdtemp", note):
            with self.assertRaisesRegex(RuntimeError, "no demo address"):
                demo_run.Demo(str(launcher), str(ROOT), "typical")
        self.assertLess(time.monotonic() - started, 15, "it waited for the silent demo to end by itself")
        self.assertEqual([os.path.isdir(folder) for folder in made], [False], "its temp folder is removed")

    def test_a_demo_that_prints_half_a_line_is_ended_too(self):
        launcher = scratch(self) / "python"
        launcher.write_text("#!/bin/sh\nprintf starting\nexec sleep 30\n")
        launcher.chmod(0o755)
        demo_run, started = tool(VERIFY / "demo_run.py"), time.monotonic()
        with mock.patch.object(demo_run.Demo, "START_LIMIT", 0.3):
            with self.assertRaisesRegex(RuntimeError, "no demo address: starting"):
                demo_run.Demo(str(launcher), str(ROOT), "typical")
        self.assertLess(time.monotonic() - started, 15, "it waited for the rest of a line that never came")
        self.assertEqual(demo_run.LIVE, set())

    def cut_off(self, demo_run, **patched):
        """Starts a demo while `patched` parts of demo_run are swapped. Returns the demo's process and its
        temp folder, however the start went. Both are ended when the test ends."""
        made, started, popen, mkdtemp = [], [], demo_run.subprocess.Popen, demo_run.tempfile.mkdtemp

        def note_process(*args, **how):
            started.append(popen(*args, **how))
            self.addCleanup(started[-1].wait)
            self.addCleanup(started[-1].kill)
            return patched["after_start"](started[-1]) if "after_start" in patched else started[-1]

        def note_folder(**how):
            made.append(mkdtemp(**how))
            self.addCleanup(shutil.rmtree, made[-1], True)
            return made[-1]
        with mock.patch.object(demo_run.subprocess, "Popen", note_process), mock.patch.object(demo_run.tempfile, "mkdtemp", note_folder):
            with mock.patch.object(demo_run.urllib.request, "build_opener", patched.get("opener", demo_run.urllib.request.build_opener)):
                with self.assertRaises(SystemExit) as left:
                    demo_run.Demo(sys.executable, str(ROOT), "typical")
        return started[0], made[0], left.exception.code

    def test_a_tool_cut_off_just_after_its_demo_gave_its_address_ends_that_demo(self):
        # the kill lands when the address is read and the constructor has not returned: no caller holds the demo
        def killed(*_):
            raise SystemExit(143)
        demo_run = tool(VERIFY / "demo_run.py")
        process, home, _ = self.cut_off(demo_run, opener=killed)
        self.assertIsNotNone(process.poll(), "the demo still runs")
        self.assertFalse(os.path.isdir(home), "the demo's temp folder is still there")

    def test_a_kill_that_lands_while_a_demo_is_being_started_waits_until_the_demo_is_known(self):
        demo_run = tool(VERIFY / "demo_run.py")

        def killed_right_now(process):
            demo_run.exit_asked(signal.SIGTERM)                     # the signal arrives before Popen has returned
            return process
        process, home, code = self.cut_off(demo_run, after_start=killed_right_now)
        self.assertEqual(code, 143)
        self.assertIsNotNone(process.poll(), "the demo still runs")
        self.assertFalse(os.path.isdir(home), "the demo's temp folder is still there")

    def test_a_tool_cut_off_while_it_stops_its_demo_still_ends_it(self):
        demo_run = tool(VERIFY / "demo_run.py")
        demo = demo_run.Demo(sys.executable, str(ROOT), "typical")
        self.addCleanup(shutil.rmtree, demo.home, True)
        self.addCleanup(demo.process.wait)
        self.addCleanup(demo.process.kill)
        call = demo.call

        def killed_at_the_stop(path, body=None):
            if path == "/api/stop":
                raise SystemExit(143)
            return call(path, body)
        with mock.patch.object(demo, "call", killed_at_the_stop), self.assertRaises(SystemExit):
            demo.stop()
        demo_run.end_all()                                          # what the tool's exit runs
        self.assertIsNotNone(demo.process.poll(), "the demo still runs")
        self.assertFalse(os.path.isdir(demo.home), "the demo's temp folder is still there")

    def test_a_sweep_that_is_killed_stops_the_demo_it_started(self):
        # The burst tool, with far more rounds than it gets to, is killed while it runs. Its temp folder is
        # this test's, so a sweep that leaves its demo's folder behind leaves it in there.
        temp = scratch(self)
        sweep = subprocess.Popen([sys.executable, "-E", "-s", str(VERIFY / "static_burst.py"), sys.executable, str(ROOT), "2", "5000"],
                                 env=dict(os.environ, TMPDIR=str(temp)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        self.assertEqual([path.name for path in temp.glob("accountdeck2-demo-run-*")], [], "the demo's temp folder is still there")

    def test_a_tool_that_is_killed_while_its_demo_is_up_stops_it(self):
        # The same kill at the other moment: the demo has given its address and the tool is at its work,
        # inside the `try` whose `finally` stops the demo. The tool here is the shape every sweep has.
        temp = scratch(self)
        (temp / "tool.py").write_text(
            f"import sys, time\nsys.path.insert(0, {str(VERIFY)!r})\nfrom demo_run import Demo, end_on_kill\nend_on_kill()\n"
            f"demo = Demo(sys.executable, {str(ROOT)!r}, 'typical')\ntry:\n    print(demo.process.pid, flush=True)\n"
            "    time.sleep(120)\nfinally:\n    demo.stop()\n    print('stop', demo.stopped, demo.ended, flush=True)\n")
        run = subprocess.Popen([sys.executable, "-E", "-s", str(temp / "tool.py")], env=dict(os.environ, TMPDIR=str(temp)),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(run.stdout.close)
        self.addCleanup(run.wait)
        self.addCleanup(run.kill)
        demo = run.stdout.readline().strip()                        # the tool says its demo's pid when the demo is up
        self.assertTrue(demo.isdigit(), "the tool never had its demo up")
        self.addCleanup(lambda: subprocess.run(["kill", "-9", demo], capture_output=True))
        run.terminate()
        self.assertEqual(run.stdout.readline().strip(), "stop 200 0", "the tool's own stop did not run, or the demo did not stop by itself")
        self.assertEqual(run.wait(timeout=40), 143)
        self.assertNotEqual(subprocess.run(["kill", "-0", demo], capture_output=True).returncode, 0, "the demo is still running")
        self.assertEqual([path.name for path in temp.glob("accountdeck2-demo-run-*")], [], "the demo's temp folder is still there")

    def test_every_tool_that_starts_a_demo_ends_on_a_kill(self):
        # `kill <pid>` ends a Python program on the spot, with no `finally` and no exit step, unless it asks otherwise
        tools = [path for path in sorted(VERIFY.glob("*.py")) if "Demo(" in path.read_text() and path.name != "demo_run.py"]
        self.assertGreaterEqual(len(tools), 5, "the sweeps, the burst and drive.py start demos")
        self.assertEqual([path.name for path in tools if "\nend_on_kill()" not in path.read_text() and "\n    end_on_kill()" not in path.read_text()], [])


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
        (self.copy / "app" / "setup.sh").write_text("#!/bin/bash\nlaunch=0\n")
        (self.copy / "app" / "page.css").write_text(".note { display: block; }\n")
        (self.copy / "tests" / "test_sums.py").write_text(
            "import unittest\nfrom app import sums\n\n\nclass SumsTest(unittest.TestCase):\n"
            "    def test_double(self):\n        self.assertEqual(sums.double(4), 8)\n\n"
            "    def test_broken_from_the_start(self):\n        self.assertEqual(sums.double(4), 9)\n\n"
            "    def test_the_page_file_is_there(self):\n"
            "        import pathlib\n        self.assertIn('label', pathlib.Path('app/page.js').read_text())\n\n"
            "    def test_the_script_and_the_style(self):\n        import pathlib\n"
            "        self.assertIn('launch=0', pathlib.Path('app/setup.sh').read_text())\n"
            "        self.assertIn('display: block', pathlib.Path('app/page.css').read_text())\n\n"
            "    @unittest.skip('not on this Mac')\n    def test_skipped(self):\n        self.fail('never runs')\n\n"
            # without the fix this one says so in the file `held` and stays in the check until the run that started it is gone
            "    def test_held(self):\n        import os, pathlib, time\n        if sums.double(4) != 8:\n"
            "            pathlib.Path('held').write_text(str(os.getpid()))\n"
            "            parent, until = os.getppid(), time.monotonic() + 60\n"
            "            while os.getppid() == parent and time.monotonic() < until:\n                time.sleep(0.05)\n"
            "        self.assertEqual(sums.double(4), 8)\n")

    def check(self, **case):
        case = {"name": "the fix", "file": "app/sums.py", "old": "return n * 2", "test": "tests.test_sums.SumsTest.test_double", **case}
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            code = tool(VERIFY / "take_fix_out.py").main(self.copy, sys.executable, [case])
        self.assertEqual((self.copy / "app" / "sums.py").read_text().count("return n * 2"), 1, "the file is put back")
        return code, said.getvalue()

    def test_a_test_that_fails_without_its_fix_is_proof(self):
        code, said = self.check(new="return n * 1")
        self.assertEqual(code, 0, said)
        self.assertIn("fails without 'the fix', as it should", said)
        self.assertIn("1 of 1 tests fail without their fix", said)

    def test_a_fix_taken_out_is_not_run_from_an_old_compiled_copy(self):
        # Python takes a compiled copy for current when the file's size and second of change are what they
        # were. Here the fix and what replaces it are the same size, and every write of the file gets the
        # same time of change, as when all of it happens within one second.
        sums, then, write = self.copy / "app" / "sums.py", time.time() - 500, Path.write_text

        def write_at_that_time(path, text, **how):
            write(path, text, **how)
            if path == sums:
                os.utime(path, (then, then))
        os.utime(sums, (then, then))
        with mock.patch.object(Path, "write_text", write_at_that_time):
            code, said = self.check(new="return n * 1")
        self.assertEqual(code, 0, said)
        self.assertIn("fails without 'the fix', as it should", said)

    def test_a_run_that_is_killed_puts_the_fix_back(self):
        cases, held, sums = self.copy / "cases.json", self.copy / "held", self.copy / "app" / "sums.py"
        cases.write_text(json.dumps([{"name": "the fix", "file": "app/sums.py", "old": "return n * 2", "new": "return n * 1",
                                      "test": "tests.test_sums.SumsTest.test_held"}]))
        temp = scratch(self)                # the run's temp folder is this test's, so what a killed run leaves is in here
        run = subprocess.Popen([sys.executable, "-E", "-s", str(VERIFY / "take_fix_out.py"), str(self.copy), sys.executable, str(cases)],
                               env=dict(os.environ, TMPDIR=str(temp)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(run.wait)
        self.addCleanup(run.kill)
        deadline = time.monotonic() + 60
        while not (held.exists() and held.read_text()) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(held.exists(), "the test never ran with the fix taken out")
        test = held.read_text()                                     # the pid of the test the run started
        self.addCleanup(lambda: subprocess.run(["kill", "-9", test], capture_output=True))
        run.terminate()
        run.wait(timeout=30)
        self.assertEqual(sums.read_text().count("return n * 2"), 1, "the killed run left the copy with the fix taken out")
        self.assertEqual(run.returncode, 143)
        self.assertNotEqual(subprocess.run(["kill", "-0", test], capture_output=True).returncode, 0, "the test it started still runs")
        self.assertEqual(os.listdir(temp), [], "the killed run left its compiled files behind")

    def test_a_cases_file_with_no_case_is_an_error(self):
        with self.assertRaisesRegex(SystemExit, "names no case"):
            tool(VERIFY / "take_fix_out.py").main(self.copy, sys.executable, [])

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

    def test_a_shell_change_that_no_longer_parses_is_no_proof(self):
        code, said = self.check(file="app/setup.sh", old="launch=0", new="launch=)",
                                test="tests.test_sums.SumsTest.test_the_script_and_the_style")
        self.assertEqual(code, 1, said)
        self.assertIn("app/setup.sh no longer parses", said)

    def test_a_change_to_a_file_nothing_parses_says_so(self):
        code, said = self.check(file="app/page.css", old="display: block", new="display: inline",
                                test="tests.test_sums.SumsTest.test_the_script_and_the_style")
        self.assertEqual(code, 0, said)
        self.assertIn("nothing here parses a .css file", said)
        code, said = self.check(new="return n * 1")
        self.assertNotIn("nothing here parses", said)

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

    def test_a_last_line_that_only_ends_in_those_words_does_not_count(self):
        self.answers(opus=self.WHOLE, sonnet=self.WHOLE,
                     sol=self.WHOLE.replace("END OF REVIEW\n", "Still writing; the final marker will be END OF REVIEW\n"))
        code, said = self.audit()
        self.assertEqual(code, 1, said)
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

    def swept(self, state):
        """What `sweep` says about the page as it is drawn now. The demo and Chrome are stand-ins: the demo
        answers with `state`, and Chrome prints this page's HTML."""
        sweep, folder = tool(VERIFY / "dashboard_sweep.py"), scratch(self)
        (folder / "page.html").write_text(self.browser.js("document.documentElement.outerHTML"))
        (folder / "chrome").write_text(f'#!/bin/sh\ncat "{folder / "page.html"}"\n')
        (folder / "chrome").chmod(0o755)

        class Demo:
            url, token = "http://127.0.0.1:1/#token=t", "t"

            def __init__(self, *_):
                pass

            def call(self, path, body=None):
                return 200, json.dumps(state).encode()

            def stop(self):
                return ""
        with mock.patch.object(sweep, "Demo", Demo):
            return sweep.sweep(sys.executable, str(ROOT), str(folder / "chrome"), "typical")

    def test_the_sweep_fails_a_scenario_whose_page_leaves_an_account_out(self):
        state = fixtures.example_state("typical", now=time.time())
        self.show(state)
        ok, words = self.swept(state)
        self.assertTrue(ok, words)
        self.assertIn("rows=13/13", words)
        self.browser.js("document.querySelector('#other-accounts li.row').remove()")
        ok, words = self.swept(state)
        self.assertFalse(ok, words)
        self.assertIn("ROWS #other-accounts drew 7 of 8 rows", words)


class WholeCheckSummaryTest(unittest.TestCase):
    """`suite` in final_check.sh writes the short summary of one pass of the tests. It is run here as the
    script has it, with a stand-in for Python that prints the end of a unittest run."""

    def summary(self, last_line):
        script = (VERIFY / "final_check.sh").read_text()
        start = script.index("suite() {")
        folder = scratch(self)
        (folder / "python").write_text(f"#!/bin/sh\necho 'Ran 3 tests in 0.1s'\necho\necho '{last_line}'\n")
        (folder / "python").chmod(0o755)
        done = subprocess.run(["/bin/zsh", "-c", script[start:script.index("\n}\n", start) + 3] + "suite pass\n"],
                              env=dict(os.environ, PY=str(folder / "python"), LOGS=str(folder)), capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        return (folder / "pass.txt").read_text()

    def test_a_pass_that_skipped_tests_says_so(self):
        self.assertIn("SKIPPED: tests did not run in this pass, see pass-full.txt", self.summary("OK (skipped=2)"))

    def test_a_pass_that_ran_every_test_does_not(self):
        said = self.summary("OK")
        self.assertNotIn("SKIPPED", said)
        self.assertIn("OK\nexit 0", said)


if __name__ == "__main__":
    unittest.main()
