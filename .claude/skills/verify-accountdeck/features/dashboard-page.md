# The dashboard page itself

Status: driven 2026-11-12. `look_sweep.py` drew all 23 scenarios in the WebKit window for the 2.0.0 build and for the refactor, and the dashboard tests ran in both engines.

`accountdeck/web/`: one HTML file, plain JavaScript and CSS, no build step. `accountdeck/server.py` serves it on `127.0.0.1` only.

## Sub-features
- The page loads its files, trades the token in its address for a session cookie, and draws the first state.
- It asks for a new state on a timer, and again when the window comes back to the front.
- Light and dark follow the Mac unless Settings says otherwise. Theme, Hide emails, alert choices and the sort order are kept in the browser's own storage.
- In the app's window the page shows no browser notification controls (the user agent ends in `AccountDeck2App/2`).
- The server's request rules: the `Host` header must be `127.0.0.1:<port>`; a POST needs the page's own `Origin`, the header `X-AccountDeck: 1` and a JSON body; the session cookie is HttpOnly and SameSite=Strict; every answer carries a strict Content-Security-Policy and may not be framed.

## How to get to it (user's view)
- The app's own window (WebKit), from the menu bar's Open Dashboard or by opening the app.
- The default browser, from "Open Dashboard in Browser". On this Mac that is Safari, which is WebKit too.

## Driving it
- Both engines: the dashboard tests run in WebKit, and again with `ACCOUNTDECK_TEST_BROWSER=chrome`.
- The look: `look_sweep.py shoot` on the code before and after, then `compare` and a diff of the `.txt` files (commands in `SKILL.md`).
- Request rules: `tests/test_dashboard.py`, `ServerRulesTest` (12 tests), and `demo_sweep.py`, which sends a POST from another origin and with no origin, asks for files outside the web folder, and tries a wrong token on every scenario.
- Many requests at once: `static_burst.py <python> <copy> <parallel> <rounds>`, and `tests/test_review_findings.py`, `DashboardLoadTest`.

Web states worth a look after a change to the page, none of which has a test yet:
- two windows on the same app at once;
- a window left open while the app restarts (the token and the port change);
- a window left open across an install: the old page talks to the new server. Reload it after an install;
- the window hidden and shown again;
- a narrow window (the app's window can be as small as 720 by 520).

## Gotchas
- A new route gets every rule above. Without a session only the page's own files, `/_health` and the sign-in itself answer, and no GET changes anything.
- The page acts on what the server answered, not on what it knew before the click. A sheet that opened by the page's last-known state was a bug in the third review.
- Never build HTML from account text. The page uses `textContent` everywhere, and the Content-Security-Policy allows no inline script.
- WebKit and Chrome drew every scenario the same way on 2026-11-12, cut-off notes included. That is one day's result, not a promise.

## Only on the real Mac
- The real window: `accountdeck/platform/dashboard_window.py` keeps the window's size and place, sends other addresses to the default browser, and loads the page again when its web process dies. The test window in `tests/webkit_host.swift` uses the same engine and user agent but is not that code.
- Safari itself, with its own extensions and settings.
