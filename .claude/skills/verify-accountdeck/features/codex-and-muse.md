# Codex and Muse Code switches

Status: a Muse Switch Now driven 2026-11-13 through `drive.py` in the WebKit window (the journey below), on the app code of the commit that took Claude Desktop out. The wait for sessions, a Codex switch, Save Current Account and Add Account are a draft: their tests pass and nobody has driven them through this skill.

Client ids `codex` and `muse`. Each keeps its login in a file. A switch waits for the client's sessions to close, writes the file under a write-ahead record, and refuses at its write step when the client has started again.

## Sub-features
- Switch… on a row, with a plan sheet that lists running sessions.
- Wait for sessions to close, then switch. A Codex session or daemon that starts after that wait, while the switch waits for a job beside it, fails the switch: no daemon is stopped and nothing is written.
- Save Current Account and Add Account. An empty section says how to start.
- Codex: a login renewed by Codex during or before a switch is saved under its own account.
- Muse: usage comes from `muse usage --json`, and Muse must keep its login in a file for the app to save it.

## How to get to it (user's view)
- Dashboard, "Codex" and "Muse Code" sections.
- Menu bar item: "Switch Codex To" and "Switch Muse To".

## Driving it
A whole Switch Now on the `typical` scenario, where Muse main is in use and no Muse session is open. Save the steps as `$PROOF/steps.json`:
```json
[
  {"in_use": "Muse main", "client": "muse"},
  {"shot": "1-before"},
  {"click": "Switch…", "row": "Muse side", "client": "muse"},
  {"wait": "document.getElementById(\"modal\").open"},
  {"js": "document.querySelector(\"#modal details\").open = true"},
  {"words": "sheet"},
  {"shot": "2-plan"},
  {"click": "Switch Now", "in": "sheet"},
  {"wait": "[...document.querySelectorAll(\"#modal button\")].some((b) => b.textContent.trim() === \"Done\")", "seconds": 60},
  {"words": "sheet"},
  {"shot": "3-report"},
  {"click": "Done", "in": "sheet"},
  {"wait": "!document.getElementById(\"modal\").open"},
  {"row": "Muse side", "client": "muse"},
  {"in_use": "Muse side", "client": "muse"},
  {"accounts": true},
  {"shot": "4-after"}
]
```
```sh
"$PY" -E -s tools/verify/drive.py "$PY" "$COPY" typical "$PROOF" "$PROOF/steps.json" 2>&1 | tee "$PROOF/drive.txt"
```
It works when you see, in order:
- the plan: "Switch Muse to Muse side?", with Cancel and Switch Now, and under "What will happen" the three steps and what stays;
- the report: "Switched to Muse side" and "Muse is ready as Muse side.";
- Muse side "In use" in its row, Muse main with a Switch… button again, and `in_use` passing for Muse side.

The page proof says nothing about the files. `MuseSwitchTest` below proves those on the fake disk, in a run of its own.

Each stage of a Codex switch has a scenario that holds it there: `switching` (waiting for a session), `stopping`, `needs-reconfirm`, `report`. `look_sweep.py` and `ScenariosDrawTest` draw all four.

Tests to reuse:
- `tests/test_switch_safety.py`: `CodexSwitchTest`, `CodexInterruptedSwitchTest`, `CodexRenewalTest`, `CodexHelperFolderTest`, `MuseSwitchTest`, `MuseInterruptedSwitchTest`;
- `tests/test_muse.py` (59 tests): the usage parser, the adapter, the engine, and `RealFormatTest` for the real file format;
- `tests/test_second_review.py`, `CodexRecoverWaitsForUsageChecksTest`, `ConfirmationStillHoldsAfterTheLaneWaitTest`, `CodexCancelDuringTheLaneWaitTest` (a terminal switch beside a Codex wait); `tests/test_switch_safety.py`, `WaitDeadlineTest`;
- every row action for a Codex and a Muse row: `tests/test_dashboard.py`, `EveryClientRowsTest`.

## Gotchas
- `typical` has one Codex account and two Muse accounts. `codex-blocked` has every Codex account out.
- "What will happen" in a plan sheet is a closed disclosure, not a button. `words` prints its lines only after the `js` step above opens it.
- The progress list ends with a step named Done. To wait for the report, wait for a Done button, not the word.
- The `switching` and `stopping` demos answer 409 to a stop. The tools end those demos themselves.
- Neither client has a test that kills the app at each write, the way Claude Code does (`test_the_app_dies_at_each_step`). Their crash cover is the interrupted-switch tests named above.
- The shape of `muse usage --json` on a real account has one open question: whether the answer carries `asOfMs`. The tests use the shape seen so far.

## Only on the real Mac
- The real Codex and Muse login files, and a real session that is open during a switch.
- One real `muse usage --json` answer, to settle `asOfMs`.
