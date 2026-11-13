---
name: verify-accountdeck
description: Prove an AccountDeck 2 change works the way a user sees it, on fake data only. Covers making a safe code copy, the doctor check, driving the dashboard in the WebKit of the app's own window, the test suite in both browser engines, the look compare, and saving evidence that survives cleanup. Use when you reproduce an AccountDeck bug, prove a fix or a feature, or are asked to verify a feature (/verify-accountdeck <feature>).
---

# verify-accountdeck

How to prove an AccountDeck 2 change. Read this file, then the file in `features/` for the part of the app you are proving. Each feature file lists its sub-features, every way a user gets to them, the commands that drive them and what you should see when they work.

Adapted from pstack's `create-verification-skill` (Lauren Tan) and shaped like `verify-cutai`.

Run every command from the repo root. A run owns two things:
- a code copy in a temp folder, `$COPY`. Cleanup removes it.
- `~/.accountdeck/proof/<task>/`, the evidence. Nothing in this skill deletes it. Pick a task name in lowercase letters, digits and dashes, like `rename-menu`.

```sh
export ACCOUNTDECK_SAFE_MODE=1
PY="$HOME/Library/Application Support/AccountDeck2/runtime/python/bin/python3.14"
PROOF=~/.accountdeck/proof/<task>; mkdir -p "$PROOF"
```
An agent's shell forgets variables between commands. Start every command with these three lines, and after Launch add `COPY=$(cat "$PROOF/copy-path.txt")`.

## Rules that come first
- Everything here runs on fake data: a demo, or the tests in safe mode. The live app on this Mac holds real logins. Never run, edit or stop it, never call its port, and never start `run.py` without `--demo`. The repo's `AGENTS.md` has the full list.
- AccountDeck is a web dashboard and a native menu bar item on top of an engine that writes login files. A proof names which of the three it covers. A dashboard proof says nothing about the files on disk, and a file proof says nothing about what the page shows.
- The app shows the dashboard in a WKWebView, and Safari is the only browser every Mac has. WebKit is the engine that counts. Chrome is the second engine, for people who open the dashboard in a Chrome-family browser.
- The real Mac needs Isaac's go every time (see "The real Mac" below).

## 1. Launch
Make the copy you will prove. A commit:
```sh
COPY=$(mktemp -d); git archive <commit> | tar -x -C "$COPY"
```
Your working tree, uncommitted changes included:
```sh
COPY=$(mktemp -d); git ls-files -co --exclude-standard -z | tar --null -T - -cf - | tar -xf - -C "$COPY"
```
Then save where the copy is, because `mktemp` cannot tell you again:
```sh
echo "$COPY" > "$PROOF/copy-path.txt"
```
The tools below start a demo from `$COPY` themselves and stop it when they finish, so you rarely start one by hand. When you want to look at one yourself:
```sh
"$PY" -E -s tools/verify/demo_run.py "$PY" "$COPY" <scenario> [2026-11-12T15:00:00] &
```
It prints the demo's address, `http://127.0.0.1:<port>/#token=<token>`, and then `stop this demo with: kill <pid>`. Stop it with that pid and nothing else. The tool then stops the demo through the demo's own API and removes the demo's temp folder. Never stop a demo by name: `pkill -f run.py` also matches the live app. The optional time sets the demo clock, so two versions of the code can show the same moment. A demo forces safe mode and keeps its data in a temp folder of its own.

The 23 scenarios are in `accountdeck/fixtures.py` (`SCENARIOS`, one line each). The ones used most:

| Scenario | What it sets up |
|---|---|
| `typical` | all four clients with saved accounts, one in use each, a mix of meter states |
| `terminal` | Claude Code accounts with the automatic order and an Undo |
| `first-run` | first start: the terminal login saved by itself, Desktop signed in but not saved |
| `switching`, `stopping`, `needs-reconfirm`, `report` | a Desktop switch held at each stage |
| `needs-signin`, `stale`, `rate-limited`, `outage`, `blocked-cloudflare` | each kind of trouble a row can show |
| `empty`, `loading`, `read-only`, `fifty` | no accounts, first readings in flight, another copy owns the logins, 50 accounts |

## 2. Doctor
```sh
"$PY" -E -s tools/verify/doctor.py "$COPY" 2>&1 | tee "$PROOF/doctor.txt"
```
It changes nothing. It does read two things in the live app's folder: the app's code files, to say which commit they are, and the port number in `runtime.json`. It prints no token. Run it before the first proof and whenever something looks off. Each line is OK, WARN or STOP:
- which commit the copy is, or which files differ from one and which are new;
- which commit the live app runs, and its process and port, so nothing gets mistaken for it;
- demos running now and demo temp folders left behind, none of which are yours unless you made them;
- that the WebKit window builds, and where headless Chrome is.

Any STOP means no proof until it is fixed. It stops on a copy that sits inside the live app. A WARN is a fact to read, not a stop. A demo or a demo folder it lists was there before you started, so it is not yours. Your report names `doctor.txt`, so every result names its build.

The exit code after `| tee` is tee's. Read the last line instead: "no STOP: this copy can be driven" from the doctor, "ok" or "FAILED" from `drive.py`.

## 3. Drive
There are five ways in. Use the one the feature file names.

**The tests.** `tests/test_switch_safety.DeckCase` gives a whole engine on a fake disk with fake clients. `tests/test_dashboard.DashboardTestCase` gives a browser page on a server that answers with one fixed state. A class that inherits both clicks the real dashboard against the real engine. Write the journey as a test. That is your fail-then-pass proof.
```sh
ACCOUNTDECK_SAFE_MODE=1 "$PY" -E -s -m unittest tests.test_dashboard.TerminalRowsTest
ACCOUNTDECK_TEST_BROWSER=chrome ACCOUNTDECK_SAFE_MODE=1 "$PY" -E -s -m unittest tests.test_dashboard.TerminalRowsTest
```
The dashboard tests run in WebKit unless `ACCOUNTDECK_TEST_BROWSER=chrome`. A dashboard change passes in both. From the repo root these commands test your working tree. To test a copy of a commit, run them inside it: `(cd "$COPY" && ACCOUNTDECK_SAFE_MODE=1 "$PY" -E -s -m unittest ...)`.

**Step by step.** `drive.py` starts a demo of `$COPY`, opens its dashboard in the WebKit of the app's window and runs your steps with real mouse and key events. The step list is in its header.
```sh
"$PY" -E -s tools/verify/drive.py "$PY" "$COPY" typical "$PROOF" '[
  {"shot": "1-before"},
  {"more": "Beta", "client": "claude"},
  {"click": "Rename…", "in": "menu"},
  {"wait": "document.getElementById(\"modal\").open && document.activeElement.value === \"Beta\""},
  {"type": "Work account"},
  {"click": "Rename", "in": "sheet"},
  {"wait": "!document.getElementById(\"modal\").open"},
  {"row": "Work account", "client": "claude"},
  {"accounts": true},
  {"shot": "2-after"}
]' 2>&1 | tee "$PROOF/drive.txt"
```
`accounts` prints what the app itself says, not what the page shows, and checks nothing. `{"in_use": "Gamma", "client": "claude"}` is the step that fails when the app does not say so. Use both to check the side effect, and quote the `accounts` line in your report.

- A long step list goes in a file, `$PROOF/steps.json`, passed in place of the JSON. The file is evidence too, and a single quote in a step no longer breaks the command.
- A `shot` overwrites a picture of the same name. Point your tries at `$PROOF/explore` and keep `$PROOF` for the final run.
- A `shot` grows the window to the page's height and back, which closes an open menu. `{"shot": "name", "view": true}` takes only what the window shows and leaves the menu open. `features/rows-and-menus.md` has the steps.
- `drive.py` drives WebKit only. The Chrome side of a journey comes from a test run with `ACCOUNTDECK_TEST_BROWSER=chrome`.
- `drive.py` removes the demo's fake disk when it ends. For a login change, the files on the fake disk are proven by the tests the feature file names. Say in your report that the page proof and the disk proof are two runs.
- The demo runs from `$COPY`, and the helpers that find rows and buttons come from the repo's own `tests/test_dashboard.py`. On a copy of an older commit a row or a button may not be found.

**The look.** `look_sweep.py` takes a picture of every demo dashboard as the app window draws it, light and dark, and saves the words on each page and the words the page cuts off.
```sh
BEFORE=$(mktemp -d); git archive <the commit you started from> | tar -x -C "$BEFORE"; echo "$BEFORE" > "$PROOF/before-path.txt"
"$PY" -E -s tools/verify/look_sweep.py shoot "$PY" "$BEFORE" "$PROOF/look-before"
"$PY" -E -s tools/verify/look_sweep.py shoot "$PY" "$COPY" "$PROOF/look-after"
"$PY" -E -s tools/verify/look_sweep.py compare "$PROOF/look-before" "$PROOF/look-after" "$PROOF/look-diff"
diff "$PROOF/look-before/typical.txt" "$PROOF/look-after/typical.txt"
```
`compare` counts the pixels that differ and writes the differing bands, before above after. Every difference is either meant or a bug. When a change moves the whole page, the pixel count is no use: read the words diff and look at the pictures.

**The sweeps and probes.** `demo_sweep.py` starts every scenario, reads its API and pages and checks the server's request rules. `dashboard_sweep.py` draws every scenario in Chrome. `static_burst.py` fetches the page's files in parallel bursts. The `*_probe.py` files each ask one question the first review raised. Each one's header says how to run it.

**The whole check.** Before you call a code change done (a proof of one feature with no code change does not need it):
```sh
tools/verify/final_check.sh "$COPY" "$PROOF/final" under-load
```
It runs the doctor, the suite in WebKit, the suite in Chrome, the probes, the bursts, both sweeps, the look sweep, the syntax checks and the leftover scanner, then the suite a third time with every core busy. Read every file it writes. It wants a clean copy of a commit. The busy loops end by themselves when the script ends or is cut off.

Traps when driving:
- This Mac has no `node`. Check JavaScript syntax with `jsc` (the command is in `AGENTS.md`).
- A demo in the background ignores Ctrl-C, and a killed demo leaves its temp folder. The tools stop a demo through its API and remove its own folder. The `switching` and `stopping` scenarios answer 409 to a stop, so the tools end those themselves.
- A `wait` or a `click` gives up after 15 seconds, and the failure reads "the page never showed". A Desktop switch in a demo takes about 22 seconds, because "Checking the sign-in" watches Claude for 20 real seconds. Give that wait `"seconds": 60`.
- A sheet's progress list ends with a step named Done, and the report has a Done button. To wait for the report, wait for the button, not the word.
- A picture of an open sheet shows the page's words through the sheet, sharp. The sheet blurs what is behind it (`backdrop-filter`), and the picture of a window that is not on screen does not draw the blur. It is the camera, as far as anyone knows. The real-Mac checks in the install skill settle it.
- The same name can be in two clients. `typical` has a Beta in Claude Code and a Beta in Claude Desktop. Name the client.
- The page knows when it is in the app's window, by the user agent `AccountDeck2App/2`, and shows no browser notification controls there. The WebKit window sends that user agent.
- The WebKit window is never shown on your screen, and the page still counts as visible and in front. A page that stops updating is a bug, not the window.
- A test that acts "while the job is waiting" must wait for a signal that the job is held there, not sleep. `watch_the_lane_wait` in `tests/test_second_review.py` is the pattern.
- Call `engine.sense_active()` before you ask the auto-switch policy anything, or it sees no account in use. `cli_policy.evaluate` is pure, so a decision can be run on hand-made readings.
- In the demo, the cookies Claude Desktop holds for the account in use are not the same bytes as that account's saved copy. A test that asks "whose cookies are these" accepts both.
- The server of commit 359ac07, the 2.0.0 build, let only 5 requests wait, so a page load failed at random in Chrome (6 of 35 loads on 2026-11-12). A half-drawn dashboard on that build is this bug, not your change.
- `text-overflow: ellipsis` draws no dots on a flex or grid box. The words are cut in the middle of a letter instead. `look_sweep.py` lists those as `cut`.
- In zsh, `kill $PIDS` does not split the variable. Keep pids in an array.

## 4. Evidence
Everything goes in `~/.accountdeck/proof/<task>/`: `doctor.txt`, the output of each tool, pictures, and the before and after output of the test that proves the change.

What counts as proof:
- Drive the real user path, with real clicks and keys. A demo scenario may set up the starting state, but the action itself is the user's.
- Capture the action and its result, not only the last screen: a picture or a row's words before and after.
- Check the side effect as well as the page: what the app says (`in_use`, `accounts`), and for a login change what is on the fake disk, which the feature file's tests prove.
- Cover every way in that the feature file lists, and both engines for anything the page draws. `drive.py` is the WebKit side. A test run with `ACCOUNTDECK_TEST_BROWSER=chrome` is the Chrome side.
- A test that proves a fix fails without the fix. Show it: `tools/verify/take_fix_out.py` (its header has the format).
- Name the build by citing `doctor.txt`. A STOP in it means no proof.
- Anything fake data cannot show goes on the real-Mac list. Each feature file's "Only on the real Mac" names those.
- Never weaken a journey to make it pass. If the app fails a journey, report a bug.

## 5. Cleanup
```sh
trash "$(cat "$PROOF/copy-path.txt")"
[ -f "$PROOF/before-path.txt" ] && trash "$(cat "$PROOF/before-path.txt")"
pgrep -fl "run.py --demo"        # none of yours should be left
```
The tools stop the demos they start and remove those demos' temp folders. The proof folder stays. Run the cleanup after a failed attempt too. Kill only processes you started, by pid and never by name. A demo temp folder that the `doctor.txt` you saved at the start already lists is not yours: leave it where it is.

## The real Mac
Nothing in this skill touches the real app, and no fake-data run proves these:
- the real Keychain, and the real login files of Claude Code, Claude Desktop, Codex and Muse;
- Claude Desktop really closing and opening again;
- the menu bar item and its menus, and Mac notifications;
- the app's own window: its size and place kept between runs, ⌘W and ⌘Q, links that open in the default browser;
- an upgrade of data the installed version wrote.

They are checked once, by Isaac, after an install. The list of checks is in `.claude/skills/accountdeck-install/SKILL.md`. Until then a report says "not checked on the real Mac".

## The feature map
`features/README.md` lists the feature files. Each one starts with a status line: driven through this skill on a named build, or a draft whose tests pass but that nobody has driven by hand. When you learn a new trap, put it in the feature file, not in a dated note.

## Keeping it honest
Run `/maintain-verification-skill` on this skill after a large change to the app. Fix only what is wrong in the skill. A broken app is a bug report for Isaac, never a docs change.
