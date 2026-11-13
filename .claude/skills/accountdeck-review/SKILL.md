---
name: accountdeck-review
description: Review one committed AccountDeck 2 change, or the whole app, with three reviewers on three models who cannot see each other's notes, then sort and prove every finding as the lead. Use after any change to switching, Recover, the vault, the job runner or the server, on the lead's own fix commits after a review, or when the message starts with /accountdeck-review.
---

# AccountDeck review

Adapted from `cutai-review`, which is pstack's `interrogate` (Lauren Tan). Three reviewers read the same code on their own and you, the lead, sort what they find. Models miss different things, so a problem two of them find without seeing each other's notes is the strongest sign it is real.

It was run three times on 2026-11-12. The verdicts are in `~/.accountdeck/reviews/` (`full-`, `refactor-` and `fixes-2026-11-12`) and are the model for a new one.

## When
- After any change to login code: switching, Recover, the vault, the job runner. Isaac's rule for sign-in changes is one builder, many independent reviewers, and no open critical or high finding.
- **On your own fix commits too.** The third review read only the fixes the lead had made after the second review. It found 9 more problems in the app and 5 tests that passed with their fix taken out. A fix nobody else has read is not done.
- On the whole app, with `-` as the base.
- Run it from a chat that did not build the change when you can. When the lead built the fixes, the reviewers are the independent read.

## 1. Scope
Write `intent.md`, three short parts, each under a `## ` heading of these words (the brief takes the file as it is):
- **What the change is meant to do.** One numbered line per fix or feature, with the function names. Take it from the ask and the commit messages.
- **Decisions already made**, so the reviewers do not report those as findings.
- **Where to spend attention**, most costly first.

Keep your own suspicions out of it. A reviewer handed a hunch tends to confirm it. No real account data, keys or anything from the live data folder goes into a review folder.

## 2. Build the folder
```sh
PY="$HOME/Library/Application Support/AccountDeck2/runtime/python/bin/python3.14"
"$PY" -E -s .claude/skills/accountdeck-review/tools/review.py new <name> <base commit | -> <head commit> intent.md [context file ...]
```
It copies committed code only into `~/.accountdeck/reviews/<name>/` and writes `BRIEF.md` from `reviewer-brief.md`, your intent and `attacks.md`. Read the brief once before you send it. A folder name is never reused.

## 3. Run the reviewers
All three get the same message and nothing else: "Read `<folder>/BRIEF.md` and follow it. Your answer file is `<folder>/out/<model>.md`."

The seats used so far: Claude Opus 5.5, Claude Sonnet 5.5 and GPT-6.1 Sol, each on its deepest setting. In T3 Code each one is a `delegate_task` child with the review role (call `orchestrator_capabilities` once first). Start all three at once and do not poll them.

What went wrong before:
- A child can sit on a permission prompt without waking you. One waited 83 minutes on an `rm`. The brief forbids `rm` and tells reviewers never to wait on a blocked command. When a reviewer's answer file has not changed for a long stretch, read its task status once and look for a pending approval.
- A child can die on the API rate limit. Its answer file is then under 1 KB.
- A reviewer writes its answer file in parts, and the file has a Coverage section from the start. On 2026-11-13 two of three answers looked whole and still grew. The brief has each reviewer end the file with the line `END OF REVIEW`, and the audit fails an answer without it.
- The reviewers run with full access and are only told to stay read-only. The audit is what checks it.

## 4. Audit
```sh
"$PY" -E -s .claude/skills/accountdeck-review/tools/review.py audit <name>
```
It says whether the copies are untouched and whether each answer is whole, and it passes only with three whole answers. Run before the reviewers finish, it ends with "this review is not done". A run that changed a copy does not count: run that seat again under a new folder name. An answer under 1 KB, without its Coverage section or without the last line `END OF REVIEW` does not count either.

## 5. Sort and prove
- Merge findings that describe the same issue, and note which models raised each one.
- **Prove each finding by a run on fake data before you act on it.** Write the run as a test that fails. Keep its output in `lead/`. A finding you could only read your way to is "traced", and you say so.
- Label each one "new with this change" or "already in the live app". `tools/verify/doctor.py` says which commit the live app runs.
- Put every finding in one of four lists, with one line on why: Fixed (or Act on), Consider, Noted, Dismissed. A dismissal says what makes the finding impossible, not that it is unlikely.
- Fix what is real with the loop in `.claude/skills/accountdeck-login-change/SKILL.md`. Each fix gets a test that fails before it, and `tools/verify/take_fix_out.py` shows the test fails again with the fix taken out.

Write `VERDICT.md` in the review folder: what was reviewed, the reviewers (model, findings, how each sorted, minutes), the audit, the lists, and a "Not checked" line.

## 6. Close the loop
- Run `tools/verify/final_check.sh` on a clean copy of the commit that holds the fixes, and put the result in the verdict with the commit and the date.
- If you made fixes, they are a new change nobody has read. Review them (step 1, a new folder, the fix commits as the change). Stop when a round finds no critical or high problem, or when Isaac says the remaining risk is his to take.

## 7. Report
Through the unslop rules, in plain words: what was real and what it would have done to the user, new or old; Consider with your pick; counts for Noted and Dismissed; what was not checked; the folder path last.

## Limits
- Reviewers read code. They do not run the app. Proof that a change works comes from `verify-accountdeck`.
- Reviewers never see the real Mac. Neither does this skill.
