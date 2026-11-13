# Account rows and the More menu

Status: rename driven 2026-11-12 through `drive.py` in the WebKit window (below), on the app code of commit 5c9b5da. Remove and Sign In Again are a draft. The tests named here are in the final check.

One row design serves all four clients (`accountdeck/web/js/rows.js`).

## Sub-features
- A row shows the name, badges (In use, Next up, Left out, Not saved), plan and email, and the limits with their meters.
- Under the name, a row says what its meters cannot: needs sign-in, out and back in, paused, reading is old. Otherwise it shows plan and email.
- The row button follows the account: Switch, Switch…, Sign In Again, Save Current Account, or none for the account in use.
- More menu: Rename…, Remove…, and for Claude Code accounts Leave Out of Auto-Switch (or Let Auto-Switch Use It).
- Remove asks first. The Claude Code account in use cannot be removed until Claude Code is signed out.
- Arrow keys move between rows and Return presses the row button.
- Hide emails (Settings) masks every email on the page.

## How to get to it (user's view)
- Dashboard: every section's list. The "…" button at the right of a row opens the More menu.
- Menu bar item: the "Switch <client> To" submenus list the account names. It has no rename or remove.

## Driving it
```sh
"$PY" -E -s tools/verify/drive.py "$PY" "$COPY" typical "$PROOF" '[
  {"more": "Beta", "client": "claude"},
  {"click": "Rename…", "in": "menu"},
  {"wait": "document.getElementById(\"modal\").open && document.activeElement.value === \"Beta\""},
  {"type": "Work account"},
  {"click": "Rename", "in": "sheet"},
  {"wait": "!document.getElementById(\"modal\").open"},
  {"row": "Work account", "client": "claude"},
  {"accounts": true}
]'
```
It works when the row reads "Work account" and `accounts` shows `('claude', 'Work account', '')` while the Claude Code account named Beta keeps its name.

Handles: a row is `li.row[data-id="<account id>"]`, the menu is `#menu` (a popover), a sheet is `#modal` (a dialog), and the row button carries `data-primary`.

Tests to reuse: `tests/test_dashboard.py`, `TerminalRowsTest`, `OtherClientsRowsTest`, `EveryClientRowsTest` (each action for a row of each client); `tests/test_review_findings.py`, `DashboardRowsTest`; `tests/test_second_review.py`, `RemoveTheSignedOutAccountTest` (the real engine). `remove_probe.py` removes a terminal account while signed in and while signed out.

## Gotchas
- Two clients can hold an account with the same name. Always name the client.
- A `shot` grows the window to the page's height, and the menu closes when the window changes size. The menu also fades in, so a picture taken right after `more` shows no menu. To picture an open menu, wait for the fade and keep the window as it is:
  ```json
  {"more": "Beta", "client": "claude"},
  {"wait": "getComputedStyle(document.getElementById(\"menu\")).opacity === \"1\""},
  {"shot": "menu-open", "view": true}
  ```
- A limit's note sits under its bar in every row, and a note with no room ends in dots. `OtherClientsRowsTest.test_a_limits_note_is_whole_and_under_its_bar` and `test_a_note_with_no_room_ends_in_dots` hold that, since commit 6020902. The 2.0.0 build put the note beside the bar in the Desktop, Codex and Muse rows and cut it in the middle of a letter at the app window's first width, 1180 ("Resets in 3d 1" for "Resets in 3d 17h"). `look_sweep.py` lists a cut note as `cut limit-note`. The words are whole in the page's text, so a test that reads only text does not see a cut.
- The page builds rows from account text with `textContent`, never as HTML. Keep it that way.

## Only on the real Mac
- Removing the account a real client is signed in to, and what that client shows afterwards.
