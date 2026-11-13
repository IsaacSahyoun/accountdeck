# Claude Code accounts: switch, auto-switch and Undo

Status: driven 2026-11-12 through `drive.py` in the WebKit window (the journey below), on the app code of commit 5c9b5da. The tests named here are in the final check.

Client id `claude_cli`. Every open terminal follows the one account in use. A switch writes Claude Code's login file and its name file as one step, under a write-ahead record.

## Sub-features
- Switch to a saved account from its row. A sheet asks first.
- Auto-switch: when the account in use reaches the threshold, the app switches to the next one in the order shown in the Auto-switch card. An Undo is offered for 10 minutes.
- A choice the user made by hand is kept for 2 hours, unless that account runs out or needs sign-in.
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
- auto-switch while another client waits, and who is signed in during a wait: `AutoSwitchWhileAnotherClientWaitsTest`, `AutoSwitchKnowsWhoIsSignedInTest`;
- Undo clicked in the real dashboard against the real engine: `tests/test_second_review.py`, `UndoBesideAWaitTest`.

Probes: `policy_probe.py` (decisions on hand-made readings), `switch_renewal_probe.py` (Claude Code renews the login in the middle), `wait_probe.py` (auto-switch during a wait), `refill_probe.py`.

## Gotchas
- In `terminal` the account in use is Beta, and in `typical` it is Alpha. The row of the account in use has no Switch button.
- The sheet does not close after Switch Now. It shows "Working…" with a Hide button, then a report, "Switched to <name>", with Explain Last Switch and Done. A journey waits for the report and clicks Done.
- An Undo planned right after an automatic switch is refused until that switch has ended ("Finish the current operation or recovery first"). A test waits for `runner.free_for("claude_cli", beside_waits=True)` first.
- `engine.sense_active()` must run before the policy is asked anything.
- Auto-switch acts on an account's weekly limit and its Fable 5.1 weekly limit, and on no other model's (a `pinned_model` setting adds that model's, and no control sets it). The 2.0.0 build acted on the weekly limit alone while Settings and the notch named both. A reading has a Fable 5.1 limit only after the first Fable 5.1 message, and an account without one is judged on its week. An account whose Fable 5.1 limit is past the threshold is refused as a target while the account in use still works. When the account in use works for no model (its week ran out, or it needs sign-in) and no account has room in both limits, the rule moves to one whose week can still be used, and it does not move back. A manual choice, Undo included, holds for 2 hours against a Fable 5.1 limit that ran out, and not against a week that ran out. The read schedule counts an account in use as used up only when its week is. `tests/test_readings.py`, `FableLimitTest` and the decision table hold the rule, `tests/test_switch_safety.py`, `TerminalFableLimitTest` holds it through the engine, and section D of `policy_probe.py` shows it. Claude has no 5-hour limit. The 2.0.0 build needed a 5-hour number from every account: with none in Claude's answer it skips every other account as "reading incomplete", never switches, and the menu bar counts no account. `tests/test_readings.py`, `ClaudeHasNoFiveHourLimitTest` holds the fix, and section C of `policy_probe.py` shows it on Claude's own answers.
- The dashboard and the menu bar do not offer a manual terminal switch during another client's wait. The engine supports it. That is an open decision, not a bug.

## Only on the real Mac
- The real `~/.claude` login files and Keychain entry, and open terminals picking the new account up on their next message.
- Claude Code renewing its own login while the app switches.
- The menu bar's "Switch Claude Code To".
