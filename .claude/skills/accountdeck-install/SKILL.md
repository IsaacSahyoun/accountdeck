---
name: accountdeck-install
description: Put a build of AccountDeck 2 into the live app on this Mac, which holds Isaac's real logins. Covers the trial install in a temp folder, the diff against the live app, the backup, the install, the checks only the real Mac can show, and the way back. Use only when Isaac asks, in this chat, to install a named commit. Never on your own.
disable-model-invocation: true
---

# AccountDeck install

This is the one skill here that touches the live app. Everything before step 6 is safe to run at any time. Steps 6 to 9 need Isaac's OK in this chat for this exact commit, and he has to be at the Mac, because the checks in step 9 are his.

The ideas come from pstack's shipping rule (a pass covers one exact build) and Isaac's deploy rule (build from what is live plus the change, show the diff against live, snapshot before and after).

```sh
PY="$HOME/Library/Application Support/AccountDeck2/runtime/python/bin/python3.14"
LIVE="$HOME/Library/Application Support/AccountDeck2"
PROOF=~/.accountdeck/proof/install-<date>; mkdir -p "$PROOF"
```

## What `install.sh` does
- It copies the folder it is run from into `$LIVE/app`, leaving out tests, tools, `AGENTS.md`, `.claude` and anything that looks like data. It builds the new folder aside and swaps it in.
- It keeps the runtime when its pinned versions are unchanged, and rebuilds `~/Applications/AccountDeck 2.app` only when the result differs.
- It never touches the data: the vault, the database, the settings.
- It does **not** stop a running app. A running app keeps its old Python code in memory and would serve the new page files from disk. So the app is quit first.
- `--prefix <folder>` installs everything below that folder and skips the Mac's own registration. `--no-launch` opens nothing. Together they make a trial that cannot reach the live app. Never use `--prefix` without `--no-launch`.

## 1. The build
```sh
COPY=$(mktemp -d); git archive <commit> | tar -x -C "$COPY"
"$PY" -E -s tools/verify/doctor.py "$COPY" | tee "$PROOF/doctor-before.txt"
```
The doctor names the copy's commit and the commit the live app runs now. Then check three things, and stop if one is missing:
- `tools/verify/final_check.sh` passed on this exact commit. A later commit voids the pass. Run it again.
- The last review verdict in `~/.accountdeck/reviews/` has no open critical or high finding, and it covers this commit. Commits made after the last review were read by nobody.
- Isaac has seen the look: `tools/verify/look_sweep.py` against the live commit, when the page changed.

## 2. Trial install
```sh
TRIAL=$(mktemp -d)
(cd "$COPY" && bash install.sh --no-launch --prefix "$TRIAL") | tee "$PROOF/trial-1.txt"
(cd "$COPY" && bash install.sh --no-launch --prefix "$TRIAL") | tee "$PROOF/trial-2.txt"
T="$TRIAL/Library/Application Support/AccountDeck2"
```
The first run downloads Python and two packages, about 30 MB, each checked against its SHA-256. The second run must say "unchanged" for the app code, the runtime and the app. Then drive the installed form with its own runtime, on fake data:
```sh
"$PY" -E -s tools/verify/drive.py "$T/runtime/python/bin/python3.14" "$T/app" typical "$PROOF/trial" '[
  {"more": "Beta", "client": "claude"}, {"click": "Rename…", "in": "menu"},
  {"wait": "document.getElementById(\"modal\").open"}, {"type": "Work account"},
  {"click": "Rename", "in": "sheet"}, {"wait": "!document.getElementById(\"modal\").open"},
  {"row": "Work account", "client": "claude"}
]' | tee "$PROOF/trial-drive.txt"
```
This catches a file the app needs that the installer leaves out.

## 3. What the real install will change
Read-only. Run it and show Isaac the result.
```sh
diff -rq "$LIVE/app" "$T/app" -x __pycache__ -x .source-sha256 -x install.json | tee "$PROOF/will-change.txt"
cmp "$LIVE/runtime/PINS" "$T/runtime/PINS" && echo "the runtime stays"
for f in Contents/MacOS/python3 Contents/Info.plist Contents/Resources/AccountDeck2.icns; do
  cmp "$HOME/Applications/AccountDeck 2.app/$f" "$TRIAL/Applications/AccountDeck 2.app/$f" && echo "same: $f"
done
```
- The first line is the list of files that will change.
- When all three app files are the same, the install leaves the app's signed program alone. On 2026-11-12 a trial of the refactored code showed exactly that.
- When `Contents/MacOS/python3` differs, tell Isaac before going on. macOS ties Keychain access and notification permission to that program, and may ask for his password or his permission again on first start.

## 4. Nothing in flight
Ask Isaac to look at the live dashboard:
- no switch is running;
- no banner offers Recover. If one does, he presses Recover with the installed version first. Never carry an open switch record across an install.

Then he chooses Quit AccountDeck 2 in the menu bar. Check it is gone: `pgrep -f "run.py --app"` prints nothing.

If Isaac asks you to quit it for him: POST `/api/session` with the token from `$LIVE/runtime.json`, then POST `/api/stop`, on the port the doctor printed, with the headers `X-AccountDeck: 1` and `Origin`. Never print the token. An answer of 409 means a switch is running: stop and tell him.

## 5. Back up
After the app has quit, so the files are still:
```sh
STAMP=$(date +%Y-%m-%d-%H%M)
cp -Rp "$LIVE" "$LIVE-backup-$STAMP"
cp -Rp "$HOME/Applications/AccountDeck 2.app" "$LIVE-backup-$STAMP/AccountDeck 2.app"
```
About 120 MB. The backup holds the encrypted vault. It stays on this Mac and is never copied anywhere else.

## 6. Install
```sh
(cd "$COPY" && bash install.sh --no-launch) | tee "$PROOF/install.txt"
"$PY" -E -s tools/verify/doctor.py "$COPY" | tee "$PROOF/doctor-after.txt"
```
`install.txt` should say the app code was installed and the runtime and the app are unchanged. `doctor-after.txt` must say the live app's code is the commit you meant.

Then tag that commit, so the doctor can still name the live app's code after `main` moves on. Name the tag by the app's version (`accountdeck/__init__.py`) when the version changed since the last tag, and `installed-<date>` when it did not:
```sh
git tag -a <name> <commit> -m "AccountDeck <version>: installed <date>"
```

## 7. Open it
```sh
open "$HOME/Applications/AccountDeck 2.app"
```
A dashboard left open in Safari from before is the old page talking to the new app. Reload it.

## 8. If it goes wrong
The code goes back first. Quit the app, then install the commit the doctor named in `doctor-before.txt`, the same way (steps 1 and 6 with that commit). The data stays as it is.

Restore the data only when the old code cannot open it:
```sh
trash "$LIVE"; cp -Rp "$LIVE-backup-$STAMP" "$LIVE"; trash "$LIVE/AccountDeck 2.app"
```
A restored vault holds the logins as they were at the backup. A client that renewed its login since then may ask for a new sign-in. Say that before you do it.

The database's schema number is 2 in both the 2.0.0 build and the code after it. Whether the 2.0.0 code reads everything the newer code writes has not been checked.

## 9. The checks only the real Mac can show
Isaac does these, and you write down what he saw, with the commit, in `$PROOF/real-mac.txt`. Until they are done, the report says the build is installed and not checked on real logins.

1. The app opens, the icon is in the menu bar, and the dashboard window shows every saved account with no banner. No password prompt appeared, or one did and he chose Always Allow.
2. Each client's account marked "In use" is the one he is really signed in to.
3. A terminal switch: Switch to another Claude Code account, send a message in a terminal that was already open, and Claude Code answers as the new account. Switch back.
4. A Desktop "switch when idle": with a Claude Code session working in Claude Desktop, choose Switch When Claude Is Idle. The banner shows it waiting. When the session ends, Claude closes and opens again as the new account. Switch back.
5. A Codex or Muse switch, if he uses them.
6. The menu bar: a "Switch … To" submenu, Refresh Now, and Open Dashboard in Browser.
7. The first automatic switch when it comes, and its Undo.
8. Recover, only if a banner ever offers it. Do not break a switch on purpose to see it.
9. A sheet's look: open any Switch… sheet. The page behind the sheet is blurred and the sheet's words are easy to read. The test pictures cannot show the blur, so only this settles it.

A failed check is a bug report with what he saw. Go back with section 8 when a real login is at risk.

## Report
What was installed (commit), what changed on disk, what Isaac checked and saw, what is still not checked, where the backup is, and the one command that goes back.
