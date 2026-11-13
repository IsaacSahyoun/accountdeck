# The feature map

One file per part of the app a user touches. Each file says what the part does, every way a user gets to it, how to drive it on fake data, what proves it works, and what only the real Mac can show.

Status words: **driven** means someone ran the journey through this skill on the build the file's status line names. **draft** means the tests named in the file pass, and nobody has driven it by hand through this skill yet.

| File | What it covers | Status |
|---|---|---|
| `terminal-switch.md` | Claude Code accounts: switch, auto-switch, Undo, leave out | driven 2026-11-12 |
| `rows-and-menus.md` | account rows for all four clients, the More menu, rename, remove, sign in again | driven 2026-11-12 (rename) |
| `desktop-switch.md` | Claude Desktop: the plan sheet, switch now, switch when idle, save, add | driven 2026-11-12 (Switch Now); the rest is a draft |
| `codex-and-muse.md` | Codex and Muse Code switches | draft |
| `recover.md` | a switch that stopped part way, and the Recover action | draft |
| `usage-and-status.md` | meters, resets, refills, stale readings, pauses, outages | draft |
| `dashboard-page.md` | the page itself: both engines, the look, the server's request rules | driven 2026-11-12 (look sweep) |
| `menu-bar-and-window.md` | the menu bar item and the app's own window | real Mac only |

Not mapped yet: moving accounts from a PC (`move_import.py`, `move_bundle.py`; tests in `tests/test_readings.py`, `MoveImportTest` and `ValidatorTest`), and the Settings sheet (`SettingsDrawTest`).

A proof that drives one convenient way in is incomplete when the file lists others. The dashboard and the menu bar both start a switch. Only the dashboard can be driven on fake data, so the menu bar's side goes on the real-Mac list.
