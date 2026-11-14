# AccountDeck 2

A Mac menu bar app in Python 3.14 with a web dashboard on `127.0.0.1`. It saves and switches the logins of three clients on one Mac user account: Claude Code in the terminal (`claude_cli`), Codex (`codex`) and Muse Code (`muse`). Builds before 2.1 also switched Claude Desktop (`claude`). This one does not: it never lists the accounts those builds saved for it and never opens a file in Claude Desktop's data folder, `~/Library/Application Support/Claude` (`tests/test_no_desktop.py`). The app shows the dashboard in a window of its own, a WKWebView, so WebKit is the browser engine a person sees.

## The rule that comes first
The copy of this app installed on this Mac holds real logins. A dev run or a test that reaches them can sign real accounts out.

- Start `run.py` only with `--demo`. Never pass `--app`, `--serve` or `--data` in a dev run.
- Never run, edit or delete anything in `~/Library/Application Support/AccountDeck2/` or `~/Applications/AccountDeck 2.app`. That is the live app and its data. One thing there is used and never changed: the Python in `runtime/python/`, which the commands below run.
- Never stop the live app's process and never call its port. `tools/verify/doctor.py` prints both. The one exception is the quit step of the `accountdeck-install` skill, when Isaac asks for it.
- Never run `install.sh` without `--no-launch --prefix <a temp folder>`, or `uninstall.sh` without `--prefix <a temp folder>` (it has no `--no-launch`). Never open an app installed that way: it would run on the real logins. The real install is the `accountdeck-install` skill, and it needs Isaac's OK every time.
- Never run `claude`, `codex`, `muse` or `security` to test something. They use the real logins.
- Kill only processes you started, by pid. Never kill by name: `pkill -f run.py` matches the live app. A demo's temp folder (`$TMPDIR/accountdeck2-demo-*`) is yours only if your run made it.
- Never run `rm`. Use `trash`, or let a tool remove its own temp folder.

## Commands
Run them from the repo root. This Mac has no `node`.

```sh
PY="$HOME/Library/Application Support/AccountDeck2/runtime/python/bin/python3.14"

# the tests (fake data; the dashboard tests run in WebKit, the app window's engine)
ACCOUNTDECK_SAFE_MODE=1 "$PY" -E -s -m unittest discover -s tests -t .
ACCOUNTDECK_SAFE_MODE=1 "$PY" -E -s -m unittest tests.test_dashboard.TerminalRowsTest
ACCOUNTDECK_TEST_BROWSER=chrome ACCOUNTDECK_SAFE_MODE=1 "$PY" -E -s -m unittest tests.test_dashboard   # same tests in Chrome

# a clean copy of a commit, and the whole check on it
COPY=$(mktemp -d); git archive <commit> | tar -x -C "$COPY"
"$PY" -E -s tools/verify/doctor.py "$COPY"
tools/verify/final_check.sh "$COPY" ~/.accountdeck/proof/<task> under-load

# a demo on fake data to look at (it prints its address and "kill <pid>"; stop it with that pid, never by name)
"$PY" -E -s tools/verify/demo_run.py "$PY" . typical

# leftover code, and JavaScript syntax
"$PY" -E -s tools/slop_scan.py
/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc -e "checkSyntax('accountdeck/web/js/rows.js')"
```

## Where things are
- `accountdeck/engine.py` runs everything. `switching.py` sends an action to a client's switch: `switch_cli.py` (Claude Code), `switch_codex.py`, `switch_muse.py`.
- `recovery.py` settles a switch that stopped part way. `core.py` holds the encrypted vault, the store and the write-ahead switch record (`FileTransaction`). `operations.py` is the job runner.
- `server.py` is the local web server and its request rules. `web/` is the dashboard: plain JavaScript and CSS, no build step.
- `platform/` is the Mac side: the menu bar item, the dashboard window (`dashboard_window.py`), the Keychain.
- `demo.py` and `fixtures.py` build the 23 demo scenarios the tests and tools use.
- `tests/` is the test suite. `tests/webkit_host.swift` and `tests/webkit.py` are the WebKit window the dashboard tests drive.
- `tools/verify/` holds the proof tools. `tools/slop_scan.py` lists leftover code, and `tests/test_keep_clean.py` fails on a new item.

## How to work here
- The repo is public on GitHub, `fernandobarnatini-debug/accountdeck`. A push needs Isaac's OK every time. `main` is the newest code and the only branch. A tag marks each build that was installed, `v2.0.0` so far. `tools/verify/doctor.py` says which commit the live app runs, by `main` or by tag.
- To prove a change, follow `.claude/skills/verify-accountdeck/SKILL.md`. A change to the dashboard passes in both engines, and `tools/verify/look_sweep.py` shows what it did to the look.
- A change to switching, Recover, the vault or the job runner follows `.claude/skills/accountdeck-login-change/SKILL.md`. It gets one builder and three independent reviewers.
- A review follows `.claude/skills/accountdeck-review/SKILL.md`.
- Every fix has a test that fails without it. `tools/verify/take_fix_out.py` checks that.
- A test that acts while a job waits must wait for a signal that the job is held, never sleep. `watch_the_lane_wait` in `tests/test_second_review.py` is the pattern.
- Commit locally when asked. Push only when Isaac says so.
