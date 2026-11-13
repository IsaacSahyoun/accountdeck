# Codex and Muse Code switches

Status: draft. The tests named here are in the final check. Nobody has driven these by hand through this skill.

Client ids `codex` and `muse`. Each keeps its login in a file. A switch waits for the client's sessions to close, writes the file under a write-ahead record, and refuses at its write step when the client has started again.

## Sub-features
- Switch… on a row, with a plan sheet that lists running sessions.
- Wait for sessions to close, then switch.
- Save Current Account and Add Account. An empty section says how to start.
- Codex: a login renewed by Codex during or before a switch is saved under its own account.
- Muse: usage comes from `muse usage --json`, and Muse must keep its login in a file for the app to save it.

## How to get to it (user's view)
- Dashboard, "Codex" and "Muse Code" sections.
- Menu bar item: "Switch Codex To" and "Switch Muse Code To".

## Driving it
No journey has been driven by hand. The plan sheets open the same way as Desktop's (`desktop-switch.md`): `{"click": "Switch…", "row": "Muse side", "client": "muse"}` on `typical`.

Tests to reuse:
- `tests/test_switch_safety.py`: `CodexSwitchTest`, `CodexInterruptedSwitchTest`, `CodexRenewalTest`, `CodexHelperFolderTest`, `MuseSwitchTest`, `MuseInterruptedSwitchTest`;
- `tests/test_muse.py` (58 tests): the usage parser, the adapter, the engine, and `RealFormatTest` for the real file format;
- `tests/test_second_review.py`, `CodexRecoverWaitsForUsageChecksTest`;
- every row action for a Codex and a Muse row: `tests/test_dashboard.py`, `EveryClientRowsTest`.

## Gotchas
- `typical` has one Codex account and two Muse accounts. `codex-blocked` has every Codex account out.
- Neither client has a test that kills the app at each write, the way Claude Code and Claude Desktop do (`test_the_app_dies_at_each_step`). Their crash cover is the interrupted-switch tests named above.
- The shape of `muse usage --json` on a real account has one open question: whether the answer carries `asOfMs`. The tests use the shape seen so far.

## Only on the real Mac
- The real Codex and Muse login files, and a real session that is open during a switch.
- One real `muse usage --json` answer, to settle `asOfMs`.
