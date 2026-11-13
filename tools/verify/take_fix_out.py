"""Does each test fail when its fix is taken out? A test that still passes without the code it is meant to
prove proves nothing (the idea is called mutation testing).

Usage: take_fix_out.py <scratch copy> <python> <cases.json>

The cases file is a JSON list. Each case names one fix and the test that should catch its loss:
  {"name": "the journal goes with the account name", "file": "accountdeck/providers/claude.py",
   "old": "<the exact text of the fix, found once in the file>", "new": "<what the code was before, or empty>",
   "test": "tests.test_second_review.HalfWrittenDesktopLoginTest.test_names_nobody"}

Each fix is taken out, its test is run, and the file is put back. The copy must be a scratch copy
(`git archive <commit> | tar -x -C <folder>`), never a checkout: this script edits files in it."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main(copy, python, cases):
    if (copy / ".git").exists():
        sys.exit("That is a checkout. Give a scratch copy: git archive <commit> | tar -x -C <folder>")
    env, still_pass = dict(os.environ, ACCOUNTDECK_SAFE_MODE="1"), 0
    for case in cases:
        path = copy / case["file"]
        text = path.read_text(encoding="utf-8")
        if text.count(case["old"]) != 1:
            sys.exit(f"'{case['name']}': the old text is in {case['file']} {text.count(case['old'])} times, not once")
        path.write_text(text.replace(case["old"], case.get("new", "")), encoding="utf-8")
        try:
            run = subprocess.run([python, "-E", "-s", "-m", "unittest", case["test"]], cwd=copy, env=env,
                                 capture_output=True, text=True)
        finally:
            path.write_text(text, encoding="utf-8")
        lines = [line for line in run.stderr.splitlines() if line.strip()]
        why = next((line for line in lines if line.startswith(("AssertionError", "accountdeck."))), "")
        if run.returncode:
            print(f"fails without '{case['name']}', as it should: {lines[-1]}  {why[:150]}")
        else:
            still_pass += 1
            print(f"STILL PASSES without '{case['name']}': {case['test']} does not prove this fix")
    print(f"\n{len(cases) - still_pass} of {len(cases)} tests fail without their fix")
    return 1 if still_pass else 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    sys.exit(main(Path(sys.argv[1]), sys.argv[2], json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))))
