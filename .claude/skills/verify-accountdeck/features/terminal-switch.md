# Claude Code accounts: switch, auto-switch and Undo

Status: driven 2026-11-12 through `drive.py` in the WebKit window (the journey below), on the app code of commit 5c9b5da. The tests named here are in the final check.

Client id `claude_cli`. Every open terminal follows the one account in use. A switch writes Claude Code's login file and its name file as one step, under a write-ahead record.

## Sub-features
- Switch to a saved account from its row. A sheet asks first.
- Auto-switch: when the account in use reaches the threshold, the app switches to the next one in the order shown in the Auto-switch card. An Undo is offered for 10 minutes.
- The early switch (Settings, "Use weekly allowance before it resets", on by default): while the account in use has room, the terminals move to the usable account whose week ends soonest, however far off that end is, so allowance is spent before it expires. Until 2026-11-14 it moved only to a week that ended within 24 hours. The target is an account a switch at the threshold could go to (a reading at most 15 minutes old, 10 points of room below the threshold in its weekly and its Fable limit) with at least 20% of its week unused. A week that has not started is not moved to, and neither is a week that ends within the hour of the week in use: such weeks end together, as they do in the order. An account whose reading is too old is passed over, not waited for: waiting was tried and taken out, because an account that cannot be read would hold every early switch back. It moves at most every 2 hours, counted from a switch that failed too, and the page says that pause only while it holds a move back. It follows the weekly limit's reset time, not the Fable limit's. With it on, a switch at the threshold goes to the account the early switch would move to next, whatever the order is set to, and that account is first in the order the page shows: one switch, not two. Two hours after an Undo of an early switch the same move is made again.
- A choice the user made by hand is kept for 2 hours, unless that account's week runs out or it needs sign-in. Once auto-switch has moved the terminals off the chosen account, nothing is held.
- Leave an account out of auto-switch, and put it back (More menu, Leave Out of Auto-Switch).
- The login Claude Code holds is saved by itself the first time it is seen.
- A terminal switch can run beside another client's "switch when idle" wait.

## How to get to it (user's view)
- Dashboard, "Claude Code accounts": the Switch button on a row, then Switch Now in the sheet.
- The Auto-switch card on the left: the toggle, and the gear for the threshold.
- The notice after an automatic switch: Undo.
- Menu bar item: "Switch Claude Code To", then an account.

## Driving it
The journey, on the `terminal` scenario (Beta is in use there):
```sh
"$PY" -E -s tools/verify/drive.py "$PY" "$COPY" terminal "$PROOF" '[
  {"accounts": true},
  {"shot": "1-before"},
  {"click": "Switch", "row": "Alpha", "client": "claude_cli"},
  {"wait": "document.getElementById(\"modal\").open"},
  {"words": "sheet"},
  {"click": "Switch Now", "in": "sheet"},
  {"wait": "document.getElementById(\"modal\").innerText.includes(\"Switched to Alpha\")"},
  {"words": "sheet"},
  {"shot": "2-report"},
  {"click": "Done", "in": "sheet"},
  {"wait": "!document.getElementById(\"modal\").open"},
  {"row": "Alpha", "client": "claude_cli"},
  {"row": "Beta", "client": "claude_cli"},
  {"accounts": true},
  {"shot": "3-after"}
]'
```
It works when the sheet goes from "Working…" to the report "Switched to Alpha", the second `accounts` line shows `('claude_cli', 'Alpha', 'in use')` and Beta no longer in use, the Alpha row carries the "In use" badge, and the Beta row has a Switch button again.

Tests to reuse:
- the files on the fake disk through a switch, a crash at each step and a renewal by Claude Code: `tests/test_switch_safety.py`, `TerminalSwitchTest`, `TerminalInterruptedSwitchTest`, `TerminalAutoSwitchTest`;
- the auto-switch decision table: `tests/test_readings.py`, `AutoSwitchTableTest`, `AutoSwitchWordsTest`, `ClaudeHasNoFiveHourLimitTest`, and `tests/test_review_findings.py`, `AutoSwitchPolicyTest`;
- the early switch: `tests/test_readings.py`, `SoonestWeekFirstTest` (the rule), and `tests/test_switch_safety.py`, `TerminalSoonestWeekTest` (through the engine, with Undo, the setting off, a switch at the threshold and a switch that fails);
- auto-switch while another client waits, and who is signed in during a wait: `AutoSwitchWhileAnotherClientWaitsTest`, `AutoSwitchKnowsWhoIsSignedInTest`;
- Undo clicked in the real dashboard against the real engine: `tests/test_second_review.py`, `UndoBesideAWaitTest`.

Probes: `policy_probe.py` (decisions on hand-made readings), `switch_renewal_probe.py` (Claude Code renews the login in the middle), `wait_probe.py` (auto-switch during a wait), `refill_probe.py`.

## Gotchas
- In `terminal` the account in use is Beta, and in `typical` it is Alpha. The row of the account in use has no Switch button.
- The sheet does not close after Switch Now. It shows "Working…" with a Hide button, then a report, "Switched to <name>", with Explain Last Switch and Done. A journey waits for the report and clicks Done.
- An Undo planned right after an automatic switch is refused until that switch has ended ("Finish the current operation or recovery first"). A test waits for `runner.free_for("claude_cli", beside_waits=True)` first.
- `engine.sense_active()` must run before the policy is asked anything.
- A running demo acts on the early switch about 10 seconds after it starts, when the week of its account in use is not the one that ends first, and minutes later when its script makes it so (a refill in `resetting`, the end of the incident in `outage`). The scenarios `resets-soon`, `refilled`, `fable-binding`, `resetting`, `outage` and `fifty` turn the setting off so they hold still (`fixtures._HOLDS_STILL`), and `ScenarioTest.test_no_demo_moves_its_terminals_by_itself` moves every scenario's clock on for 25 minutes. The `typical` demo still leaves Alpha at the threshold about 10 minutes in, as it did before. To watch the early switch in a demo, drive `refilled` and turn the setting on in Settings.
- Auto-switch acts on an account's weekly limit and its Fable weekly limit, and on no other model's (a `pinned_model` setting adds that model's, and no control sets it). The 2.0.0 build acted on the weekly limit alone while Settings and the notch named both. A reading has a Fable limit only after the first Fable message, and an account without one is judged on its week. An account with less than 10 points of room in its week or in its Fable limit is refused as a target. When every account is like that, the one in use is used up to 99% and then left for an account with both limits at 97% or less. An account whose Fable limit is above 97% is never used while the account in use still works. When the account in use works for no model (its week ran out, or it needs sign-in) and no account can be used up that way, the rule moves to one whose week can still be used, picked in the user's order by its week, and it does not move back. That switch keeps the kind of its trigger (`out` or `login`), also after "All terminal accounts are low". A manual choice, Undo included, holds for 2 hours against a Fable limit that ran out, and not against a week that ran out. The hold is for the chosen account, which the history names (`manual_for`, written with `manual_at` by `CLISwitch`): once auto-switch has moved the terminals off it, nothing is held (`cli_policy._held`). A history from before `manual_for` holds whichever account is in use. The read schedule counts an account in use as used up only when its week is. An account past the threshold, or one used for its week alone, is used only on a reading at most 15 minutes old. An older one is read again ("Checking Beta before switching"), and an account with a new reading behind it in the order is used first. An account with room and an old reading is waited for while the account in use still works. Once the account in use works for no model, an account that is ready is used at once. One gap is left: `cli_auto.tick` reads a target again when its reading is more than 3 minutes old, so while that read fails, a first choice with a reading 3 to 15 minutes old keeps the accounts behind it waiting until its reading is 15 minutes old. Claude's answer can give the Fable limit under a named key (`seven_day_fable`) and in its list, where the model's name can carry a version ("Fable 5.1", saved as `seven_day_fable_5_1`). They are one limit, and the app names it "Fable" in both cases (`usage.model_label`): the label is "Fable weekly", and the window keeps Claude's own name for the model in `model`. Older builds named it "Fable 5.1", and a reading one of them saved is shown under the new name (`usage.saved_reading`), while a line in the Activity list keeps the words it was written with. The list's entry is the one that counts, and each keeps its own id, because the forecast reads the saved samples of an id as one series. A reading's `raw_windows` says which limit came from a named key (`usage._from_keys`), so two list entries of one model stay two limits. The rule then acts on both, and the page and the menu bar show the first one (`AD.accounts.fableWindow`, `pool._windows`). No answer that names a model twice has been seen. `tests/test_readings.py`, `FableLimitTest` and the decision table hold the rule, `tests/test_switch_safety.py`, `TerminalFableLimitTest` holds it through the engine, and section D of `policy_probe.py` shows it. Claude has no 5-hour limit. The 2.0.0 build needed a 5-hour number from every account: with none in Claude's answer it skips every other account as "reading incomplete", never switches, and the menu bar counts no account. `tests/test_readings.py`, `ClaudeHasNoFiveHourLimitTest` holds the fix, and section C of `policy_probe.py` shows it on Claude's own answers.
- The dashboard and the menu bar do not offer a manual terminal switch during another client's wait. The engine supports it. That is an open decision, not a bug.

## Only on the real Mac
- The real `~/.claude` login files and Keychain entry, and open terminals picking the new account up on their next message.
- Claude Code renewing its own login while the app switches.
- The menu bar's "Switch Claude Code To".
