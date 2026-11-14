---
name: accountdeck-login-change
description: The loop for any change to AccountDeck 2's login code (a client's switch, Recover, the vault, the switch record, the job runner). Covers the crash table before code, tests for every crash point, client and old-version record, the take-the-fix-out check, and the three-model review of the change and of its fixes. Use when you fix a bug or build a feature in that code, or when the message starts with /accountdeck-login-change.
---

# AccountDeck login change

For a change to `accountdeck/switch_*.py`, `switching.py`, `recovery.py`, `core.py`, `operations.py`, `file_switch.py`, `cli_accounts.py` or `providers/`. This code writes real logins, and a bug in it signs a real account out or files a login under the wrong account. Isaac's rule for it is one builder, many independent reviewers, and no open critical or high finding.

It is the `/fixbug` loop with AccountDeck's own steps added. Where the two differ, this one wins. The ideas come from pstack's `blast-radius` and `statechart` skills, without the model checker.

Every critical finding in the three reviews of 2026-11-12 was one of three kinds: the app dying between two writes, two jobs running at once, or data an older version wrote. The steps below aim at those three.

## 1. Old or new
Reproduce the problem on fake data first, as a test that fails. Then run the same test on a copy of the commit the live app runs (`tools/verify/doctor.py` names it). Label the bug "already in the live app" or "new with this branch". A bug you cannot reproduce is not ready to fix.

## 2. The crash table, before any code
Write it in your notes and keep it for the reviewers. For the path you are changing:

| After write | On disk | The client opens as | The record says | Recover then |
|---|---|---|---|---|
| none yet | old login, whole | the old account | open, nothing written | closes the record, changes nothing |
| 1: … | … | … | … | … |

- One row per write, in order, for the switch and for Recover's own writes.
- Include the files the client writes by itself: Claude Code or Codex renewing its token.
- A row where the client would open as a mix of two accounts, or where Recover would save a mix, is a bug in the design. Change the write order until no such row is left. The rule that has worked: take the account's name off first and put it back last, so a half-written login reads as signed out.

Then name the one fact the change is safe because of, in one sentence, and plan the test that proves it by a run. If you cannot name it, you do not understand the change yet.

## 3. Build it, one change at a time
Read `.claude/skills/accountdeck-review/attacks.md` and write a test for every attack the change can reach. The ones that are never optional:
- **The app dies at each write.** `tests/test_switch_safety.py` has the pattern: `Crash` is the exception that kills the app at a step. `TerminalInterruptedSwitchTest` has `test_the_app_dies_at_each_step`, and Codex and Muse have their own `InterruptedSwitchTest` classes. After each crash the test presses Recover and checks who is signed in and what the vault holds.
- **Recover fails part way, and is pressed twice.** `TerminalRecordStaysWhileTheLoginIsMixedTest` in `tests/test_second_review.py`.
- **Data the installed version wrote.** `TerminalInterruptedSwitchTest.test_a_record_the_shipped_app_left_is_settled_after_a_renewal` builds a record the way the 2.0.0 build did, and `tests/test_no_desktop.py` builds the rows, saved logins and records that build kept for Claude Desktop, which this build no longer manages. If you change what a record holds, add the old shape.
- **A job beside the wait.** `tests/test_one_writer.py` (`Beside`, `until`) and `watch_the_lane_wait` in `tests/test_second_review.py`. A test that acts during a wait first waits for the signal that the job is held there. It never sleeps.
- **All three clients.** A fix for one switch usually has two siblings. Say which clients the bug cannot reach, and why.
- **The reverse.** If the change adds a state (left out, set aside, kept, held), add the way back and test it.

Every test fails before its fix and passes after. Keep both outputs.

## 4. Take each fix out
```sh
COPY=$(mktemp -d); git archive HEAD | tar -x -C "$COPY"
"$PY" -E -s tools/verify/take_fix_out.py "$COPY" "$PY" cases.json
```
`cases.json` names each fix, the exact text to remove and the test that should then fail (the format is in the tool's header). A test that still passes proves nothing: tighten it until it fails. The tool also says "PROVES NOTHING" and exits 1, and the message names the cause. With the fix in, a test that fails or does not load means the test or the copy is wrong. With the fix taken out, a file that no longer parses or a test that no longer loads means the case is wrong: take out the call, not a definition other code imports. Five tests in the third review passed without their fix, and this check is what found them.

## 5. Prove it
Follow `.claude/skills/verify-accountdeck/SKILL.md`: the doctor, the feature file for what you changed, and the whole check on a clean copy of the commit.
```sh
tools/verify/final_check.sh "$COPY" ~/.accountdeck/proof/<task>/final under-load
```
If the change alters what a switch asks or refuses, it has to reach the dashboard's sheet and the menu bar's alert. Check both.

## 6. Review
Commit locally, then run `.claude/skills/accountdeck-review/SKILL.md` on the change. Fix what is real with this same loop, and review the fixes too. Stop when a round finds no critical or high problem.

## 7. Report
- What was wrong and what it would have done to a real login, in plain words. New or old.
- What you changed and the one fact it is safe because of.
- Proof: the test that fails before and passes after, the take-the-fix-out result, the final check, each with its commit and date.
- Not checked: nothing here runs against a real login. Name the real-Mac check that would close the gap (`accountdeck-install` has the list).
- The commit last. Local only. Never push and never install without Isaac's OK: both are his call.

## Rules that do not bend
- Fake data only. The repo's `AGENTS.md` has the list of what never to touch.
- "I cannot tell" is never "it is fine". A check that fails counts as the unsafe answer: the record stays, the login is kept, the switch is refused.
- A switch record is written before the login changes and closed after. Never reorder those.
- Keep before you replace. A file the app cannot read is saved before it is written over.
