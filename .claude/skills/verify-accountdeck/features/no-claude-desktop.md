# What an older build saved for Claude Desktop

Status: draft. The tests named here are in the final check. Nobody has driven this by hand through this skill, and an upgrade of real 2.0.0 data is a real-Mac check.

Builds before 2.1 switched Claude Desktop (client id `claude`). This build does not. A data folder one of those builds used still holds what it saved, and this build leaves all of it where it is. It never lists an account or a login of it, and it never opens a file in Claude Desktop's data folder, `~/Library/Application Support/Claude`, where its login is. A change to the store, Recover or the PC import has to keep that true.

## Sub-features
- **Account rows.** `core.Store` is given the clients this build manages. A row of any other client is never returned, counted, reordered, changed or removed, and no reading is saved on it. The row and its saved login stay in the data folder, and an older build opening the folder finds them as it left them.
- **Saved logins.** The vault copies of a Desktop account, and a PC import of one that was still waiting (`deferred-desktop-<id>`), are never read or deleted.
- **An unfinished Desktop switch.** Its open record names files in Claude Desktop's folder, which this build may not write (`Engine.roots`). The record never counts as waiting for Recover and blocks no other client. The copy that owns the logins sets it aside at start, bytes unchanged, with one notice in a run of the app that tells the user to check Claude Desktop, however many records that switch left and whenever each is set aside. It is kept 30 days from then, like every set-aside record. A record that names no client is Desktop's too: the 2.0.0 build wrote one only when it put Claude's session list back.
- **Activity lines.** A line the older build wrote about a Desktop switch or a removed Desktop account stays in the Activity list as history, with the account's name in it. The list shows the newest 12 lines, so newer ones push it out. The events table has no client column, so the store cannot tell such a line from another.
- **Settings.** `cookie_route` is not a setting of this build. Its stored value stays.
- **A PC bundle.** A PC still packs its Claude Desktop accounts. The import leaves them out, says how many, and gives that bundle no confirmation code, so the PC keeps the accounts it alone holds.
- **A running Claude Desktop** is no client's session. It is in no plan and is never signalled. The process scan on a real Mac knows Codex's app by the bundle id in a running app's `Info.plist`, so it reads that one file of every running app, `Claude.app`'s among them, and nothing else in it (`DesktopProcessTest.test_the_scan_of_a_real_mac_reads_one_file_of_claude_desktops_app`).

## How to get to it (user's view)
- The first start after an install over a build before 2.1. The dashboard has three sections and the menu bar three "Switch … To" menus.
- A notice at the top of the dashboard, once, when an unfinished Desktop switch was set aside.
- Settings, "Move from a PC": the preview and the result of an import say how many Claude Desktop accounts were left out.

## Driving it
No demo scenario holds data of an older build, so this is driven through tests, which write that data on the fake disk the way the 2.0.0 build did. All of them are in `tests/test_no_desktop.py`:
- `HiddenAccountTest`: the row, its history, its alerts and its saved logins through a start, usage checks, a switch of each client and a remove;
- `UnfinishedDesktopSwitchTest`: the set-aside, two records of one switch, a set-aside that fails, the app dying at each step of it, a copy that does not own the logins, Recover for another client beside it, and the 30 days;
- `OldSettingsTest`, `PCBundleWithDesktopTest`, `DesktopProcessTest`;
- `ImportResultOnThePageTest`: what the dashboard's banner says after an import that left a Desktop account out, alone and beside an account that failed.

`touches(folder)` in that file lists every open, listing, rename and removal under a folder while a block runs. It is how a test proves the app used no file in Claude Desktop's data folder. It cannot see a check that a path exists.

The proof folder of the removal, `~/.accountdeck/proof/no-claude-desktop-2026-11-13/`, has a run of the 2.0.0 code's own demo data through this build and back (`upgrade/`).

## Gotchas
- A test that says "nothing changed" compares bytes and dates read past the app's own store (`on_file`, `saved_logins`, `tree`). Asking the store proves nothing: the store hides the row.
- Usage history ages out for every account at once (readings after 7 days, snapshots after 90). A hidden row's history goes the same way. A test that moves the demo clock past those ages will see it go.
- The two internal settings `alert_latches` and `recommender_state` drop what they held about a Desktop account on the first check. They hold no login.
- `move_bundle.PROVIDERS` is the bundle format's list and still has `claude`. `contract.PROVIDERS` is the list of clients this build manages.

## Only on the real Mac
- The first start of 2.1 on the data the 2.0.0 build wrote: every Claude Code, Codex and Muse account is listed, and no Desktop account is.
- Claude Desktop itself: not closed, not opened, and signed in to the account it had.
- Going back to the 2.0.0 build: its Claude Desktop accounts are listed again.
