# A switch that stopped part way, and Recover

Status: draft. The tests named here are in the final check. Nobody has driven Recover by hand through this skill.

A switch saves a record before it changes a login and closes the record when it is done. An open record means the switch stopped part way. Recover settles each open record: it saves the login that is live now under its own account, writes the previous account's saved login whole, and closes the record. This is the part of the app where a bug costs a real login, so a change here follows `.claude/skills/accountdeck-login-change/SKILL.md`.

## Sub-features
- The app looks for open records (`engine.look_for_unfinished`) and shows a banner with a Recover button.
- One press goes to one client. A client whose last press had to leave its record open goes after the others, and two stuck clients take turns.
- A record stays open while the login is not known to be one account's. The user's way out is to press Recover again, or to sign in again in the client and then press Recover.
- A record nobody can read or apply is set aside: kept encrypted for 30 days, out of the pending list, with a notice.
- A login file Recover cannot read is kept before it is replaced.
- A record the 2.0.0 build left open is settled by writing the previous account from the vault, saving nothing from the mixed files.

## How to get to it (user's view)
- Dashboard: a banner with a Recover button, then a sheet ("Recover the previous sign-in?").
- The menu bar item has no Recover.

## Driving it
No demo scenario starts with an open record, so Recover is driven through tests, which build the record on the fake disk:
- a crash at each step of a switch, then Recover: `tests/test_switch_safety.py`, `TerminalInterruptedSwitchTest`, `DesktopInterruptedSwitchTest`, `CodexInterruptedSwitchTest`, `MuseInterruptedSwitchTest`;
- Recover that itself fails or dies: `tests/test_second_review.py`, `DesktopRecoverThatFailsPartWayTest`, `HalfWrittenDesktopLoginTest`;
- a record that has to stay, and the order of presses: `TerminalRecordStaysWhileTheLoginIsMixedTest`;
- records of the 2.0.0 build: `RecordsOfTheShippedVersionTest`;
- unreadable logins and set-aside records: `UnreadableLoginIsKeptTest`, `SetAsideRecordsAreKeptTest`, `RecordStatusMatchesTheDiskTest`;
- Recover leaves finished switches alone: `tests/test_one_writer.py`, `RecoverLeavesFinishedSwitchesAloneTest`.

`tests/test_switch_safety.Crash` is the exception a test raises to kill the app at a step. `tests/support.recover_reads` controls the order records are read in and checks the order was the one asked for.

## Gotchas
- A test that proves a Recover fix must fail without the fix. Five did not, in the third review. Check with `tools/verify/take_fix_out.py`.
- A record can be open with every write made. Recover then has nothing to write and still has to close it.
- "Whole" means one account owns the live token and the name file names that account, or nobody is signed in. A failure inside that check counts as "nobody can say", and the record stays.

## Only on the real Mac
- A real interrupted switch: the app or the Mac going down between two writes.
- Recover on real login files, and what each client shows afterwards.
- Open records the installed version wrote, read by a new version on its first start. Check for them before an install (`accountdeck-install`).
