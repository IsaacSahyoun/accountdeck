"""Does each test fail when its fix is taken out? A test that still passes without the code it is meant to
prove proves nothing (the idea is called mutation testing).

Usage: take_fix_out.py <scratch copy> <python> <cases.json>

The cases file is a JSON list. Each case names one fix and the test that should catch its loss:
  {"name": "the journal goes with the account name", "file": "accountdeck/providers/claude.py",
   "old": "<the exact text of the fix, found once in the file>", "new": "<what the code was before, or empty>",
   "test": "tests.test_second_review.HalfWrittenDesktopLoginTest.test_a_login_that_stopped_before_its_account_name_names_nobody"}

Each test first runs on the copy as it is, and has to pass there. Then each fix is taken out, its test is run,
and the file is put back. A case proves its fix only when the test ran and failed. A case proves nothing, and
the script says so and exits 1, when the test fails with the fix in, when the changed file no longer parses,
when the test did not load or was skipped, or when it did not finish in 15 minutes.

The copy must be a scratch copy (`git archive <commit> | tar -x -C <folder>`), never a checkout: this script
edits files in it."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
LONGEST = 900       # seconds one run of a test may take; a test that hangs without its fix needs a time limit of its own


def run_test(copy, python, test):
    """What one run of a test says: "passes", "fails", or why the run says nothing about the test. With it,
    the lines unittest wrote. Each run compiles the code from the files as they are now. Python would
    otherwise reuse the compiled copy of a file whose size and second of change are the same, and a fix
    taken out in that second would still be running."""
    with tempfile.TemporaryDirectory(prefix="accountdeck-take-fix-out-") as compiled:
        try:
            done = subprocess.run([python, "-E", "-s", "-X", f"pycache_prefix={compiled}", "-m", "unittest", test], cwd=copy,
                                  env=dict(os.environ, ACCOUNTDECK_SAFE_MODE="1"), capture_output=True, text=True, timeout=LONGEST)
        except subprocess.TimeoutExpired:
            return f"the test did not finish in {LONGEST // 60} minutes", []
    lines = [line for line in done.stderr.splitlines() if line.strip()]
    ran = re.search(r"^Ran (\d+) tests?", done.stderr, re.M)
    skipped = re.search(r"skipped=(\d+)", lines[-1] if lines else "")
    if "unittest.loader._FailedTest" in done.stderr:
        return "the test did not load", lines
    if not ran or ran.group(1) == "0":
        return "no test ran", lines
    if skipped and skipped.group(1) == ran.group(1):
        return "the test was skipped", lines
    return ("fails" if done.returncode else "passes"), lines


def broken(path):
    """The error when a changed file is no longer code that parses. A test that fails on that has not looked
    at the fix."""
    if path.suffix == ".py":
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as exc:
            return f"SyntaxError: {exc}"
    if path.suffix == ".js" and os.access(JSC, os.X_OK):
        done = subprocess.run([JSC, "-e", f"checkSyntax({json.dumps(str(path))})"], capture_output=True, text=True)
        if done.returncode:
            return ((done.stdout + done.stderr).strip().splitlines() or ["does not parse"])[0]
    return ""


FAILED = ("AssertionError", "accountdeck.")                                      # how a line starts that says why
BROKE = FAILED + ("AttributeError", "ImportError", "ModuleNotFoundError", "SyntaxError", "NameError")


def why(lines, starts=FAILED):
    return next((line for line in lines if line.startswith(starts)), "")[:150]


def main(copy, python, cases):
    if (copy / ".git").exists():
        sys.exit("That is a checkout. Give a scratch copy: git archive <commit> | tar -x -C <folder>")
    with_fix, proved, still_pass = {}, 0, 0
    for case in cases:
        name, test, path = case["name"], case["test"], copy / case["file"]
        text = path.read_text(encoding="utf-8")
        if text.count(case["old"]) != 1:
            sys.exit(f"'{name}': the old text is in {case['file']} {text.count(case['old'])} times, not once")
        if test not in with_fix:
            with_fix[test] = run_test(copy, python, test)
        if with_fix[test][0] != "passes":
            said, lines = with_fix[test]
            print(f"PROVES NOTHING '{name}': with the fix in, {test} {'fails' if said == 'fails' else 'does not run'} "
                  f"({said}): {lines[-1] if lines else ''}  {why(lines, BROKE)}")
            continue
        path.write_text(text.replace(case["old"], case.get("new", "")), encoding="utf-8")
        try:
            error = broken(path)
            said, lines = ("", []) if error else run_test(copy, python, test)
        finally:
            path.write_text(text, encoding="utf-8")
        if error:
            print(f"PROVES NOTHING '{name}': with the fix taken out, {case['file']} no longer parses: {error}")
        elif said == "fails":
            proved += 1
            print(f"fails without '{name}', as it should: {lines[-1]}  {why(lines)}")
        elif said == "passes":
            still_pass += 1
            print(f"STILL PASSES without '{name}': {test} does not prove this fix")
        else:
            print(f"PROVES NOTHING '{name}': with the fix taken out, {said}: {why(lines, BROKE)}")
    nothing = len(cases) - proved - still_pass
    print(f"\n{proved} of {len(cases)} tests fail without their fix" + (f", {nothing} cases prove nothing" if nothing else ""))
    return 1 if proved < len(cases) else 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    sys.exit(main(Path(sys.argv[1]), sys.argv[2], json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))))
