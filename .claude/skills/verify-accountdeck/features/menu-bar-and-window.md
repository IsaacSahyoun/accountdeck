# The menu bar item and the app's own window

Status: real Mac only. No fake-data run drives AppKit.

`accountdeck/platform/menubar.py` builds the menu, `statusbar_mac.py` shows it, and `dashboard_window.py` holds the dashboard in a WKWebView.

## Sub-features
- The menu: status lines, Open Dashboard (⌘O), Open Dashboard in Browser (hold Option), a "Switch <client> To" submenu per client, Refresh Now, Start at Login, Quit.
- A switch from the menu asks in an alert, with Switch Now or Switch When Idle.
- The window: made on first use, brought forward later. While it is open the app has a Dock icon and an app menu. ⌘W closes it and the app is a menu bar app again.
- Mac notifications for the app's alerts, and a menu item to fix them when macOS has them off.

## How to get to it (user's view)
- The icon in the menu bar, and `~/Applications/AccountDeck 2.app`.

## Driving it
- `menubar.menu_model(state, now, ...)` builds the whole menu from a state and touches nothing of the Mac, so a test can call it with a fixture state. No test does yet.
- Everything else here is checked by a person on the real Mac. The checks are in `.claude/skills/accountdeck-install/SKILL.md`.

## Gotchas
- The dashboard and the menu bar both start a switch. A change to what a switch asks or refuses has to reach both: the page's sheet (`web/js/switch.js`) and the menu's alert (`menubar.py`).
- Switch items in the menu are off while any action is running.

## Only on the real Mac
All of it.
