# Claude Desktop: the plan sheet, switch now and switch when idle

Status: Switch Now driven 2026-11-12 through `drive.py` in the WebKit window (the journey below), on the app code of commit 5c9b5da, once by a reader who had only this skill. Switch When Claude Is Idle, Save Current Account and Add Account are a draft: their tests pass and nobody has driven them through this skill.

Client id `claude`. A Desktop switch closes Claude, writes its cookies and settings under a write-ahead record, and opens Claude again. The account name comes off the settings first and goes back last, so a write that dies part way reads as signed out, never as a mix of two accounts.

## Sub-features
- The plan sheet: who is in use now, who comes after, which sessions are open, what will happen.
- Switch Now: closes Claude even when a session is working. When a Code session is working, the sheet asks once more first ("Switch now and interrupt …?", with Cancel and Switch Now Anyway).
- Switch When Claude Is Idle: waits until no session is working, then switches. The wait can be cancelled. A confirmation lasts 30 minutes.
- When the sessions change during the wait, the sheet asks again ("please confirm again").
- A terminal switch may run beside the wait. Before Claude is closed, the app waits for that job and looks at the sessions again.
- Save Current Account and Add Account.
- The report after a switch, and a background check that the sign-in took.

## How to get to it (user's view)
- Dashboard, "Claude Desktop": Switch… on a row opens the plan sheet. The banner at the top shows a running switch, with Show Progress, and with Cancel while the switch can still be cancelled.
- Menu bar item: "Switch Claude Desktop To", then an account, then a prompt with Switch Now and Switch When Idle.

## Driving it
A whole Switch Now on the `typical` scenario, where Alpha is in use and a Code session is working. Save the steps as `$PROOF/steps.json`:
```json
[
  {"in_use": "Alpha", "client": "claude"},
  {"shot": "1-before"},
  {"click": "Switch…", "row": "Gamma", "client": "claude"},
  {"wait": "document.getElementById(\"modal\").open"},
  {"words": "sheet"},
  {"shot": "2-plan"},
  {"click": "Switch Now", "in": "sheet"},
  {"click": "Switch Now Anyway", "in": "sheet"},
  {"wait": "!!document.querySelector(\"#modal li.step[data-status=active]\")"},
  {"words": "sheet"},
  {"shot": "3-progress"},
  {"wait": "[...document.querySelectorAll(\"#modal button\")].some((b) => b.textContent.trim() === \"Done\")", "seconds": 60},
  {"words": "sheet"},
  {"shot": "4-report"},
  {"click": "Done", "in": "sheet"},
  {"wait": "!document.getElementById(\"modal\").open"},
  {"row": "Gamma", "client": "claude"},
  {"row": "Alpha", "client": "claude"},
  {"in_use": "Gamma", "client": "claude"},
  {"accounts": true},
  {"shot": "5-after"}
]
```
```sh
"$PY" -E -s tools/verify/drive.py "$PY" "$COPY" typical "$PROOF" "$PROOF/steps.json" 2>&1 | tee "$PROOF/drive.txt"
```
It works when you see, in order:
- the plan: "Switch Claude to Gamma?", the open sessions, and Switch Now, Cancel and Switch When Claude Is Idle;
- the second ask, because a session is working;
- the progress list, from "Checking Gamma's saved sign-in" to "Checking the sign-in";
- the report: "Switched to Gamma" and "Claude is signed in as Gamma";
- Gamma "In use" in its row, Alpha with a Switch… button again, and `in_use` passing for Gamma.

The page proof says nothing about the files. `DesktopSwitchTest` below proves those on the fake disk, in a run of its own.

Each stage of a switch has a scenario that holds it there: `switching` (waiting for a session), `stopping`, `needs-reconfirm`, `report`. `look_sweep.py` and `ScenariosDrawTest` draw all four.

Tests to reuse:
- the files on the fake disk through a switch and a crash at each write: `tests/test_switch_safety.py`, `DesktopSwitchTest`, `DesktopInterruptedSwitchTest`, `DesktopBackgroundCheckTest`;
- a half-written login, and records the 2.0.0 build wrote: `tests/test_second_review.py`, `HalfWrittenDesktopLoginTest`, `RecordsOfTheShippedVersionTest`;
- two jobs at once: `tests/test_switch_safety.py`, `RunnerLanesTest`, `WaitDeadlineTest`; `tests/test_one_writer.py`; `tests/test_second_review.py`, `ConfirmationStillHoldsAfterTheLaneWaitTest`, `DesktopCancelDuringTheLaneWaitTest`.

## Gotchas
- The whole switch takes about 22 seconds in a demo: "Checking the sign-in" watches Claude for 20 real seconds (`STABLE_SECONDS` in `claude_verify.py`). A plain `wait` gives up at 15.
- The progress list ends with a step named Done. To wait for the report, wait for a Done button, not the word.
- The app names the old account as in use until the report comes. The row changes to the new account when "Checking the sign-in" ends.
- The progress sheet is titled "Working…" when it opens straight from the plan, and "Switching to Gamma" when it is opened again from the banner. Seen 2026-11-12. The same code is in the 2.0.0 build.
- Switch When Claude Is Idle, in `typical`, checks the saved sign-in and then waits on the working session "Refactor billing". Every step's name is in the list from the start, so wait on the active step: `(document.querySelector("#modal li.step[data-status=active]") || {innerText: ""}).innerText.includes("Waiting for Claude to finish")`. During the wait the banner reads "Switching to Gamma", and every Switch button on the page is greyed out, the Claude Code ones too. Driven this far on 2026-11-13 on commit 8c36701. Nobody has driven the end of the wait or its Cancel.
- In the demo, the cookies Claude holds for the account in use are not the same bytes as that account's saved copy. Both count as that account's.
- Claude leaves a settings journal beside its settings file. A test of a half-written login needs the journal there, written the way Claude writes it (`HalfWrittenDesktopLoginTest.claude_is_killed_mid_save`).
- A test that cancels "during the second wait" must first wait until the job is held there (`watch_the_lane_wait`).
- The `switching` and `stopping` demos answer 409 to a stop. The tools end those demos themselves.

## Only on the real Mac
- Claude Desktop really closing and opening again, and which account it opens as.
- Chat and Cowork activity, which the app cannot see.
- "Switch when idle" with a real session that starts working again during the wait.
- The menu bar's switch prompt.
