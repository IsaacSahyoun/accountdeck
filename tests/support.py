"""Shared test helpers: a whole engine on synthetic data, the dashboard drawn in a browser, and the
stand-ins the review-finding tests use. Everything runs in safe mode; the real apps, their logins and the
Keychain are never touched."""
import os

os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"
os.environ["ACCOUNTDECK_DEMO"] = "1"

import contextlib  # noqa: E402
import copy  # noqa: E402
import glob  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from unittest import mock  # noqa: E402

from accountdeck import demo, diagnostics, recovery  # noqa: E402
from accountdeck.operations import Operation  # noqa: E402
from accountdeck.server import Server  # noqa: E402
from tests import webkit  # noqa: E402

CHROME_ENV = "ACCOUNTDECK_TEST_CHROME"
BROWSER_ENV = "ACCOUNTDECK_TEST_BROWSER"        # "chrome" for headless Chrome; anything else is the app's WebKit
# JavaScript that is true once the dashboard has drawn its first state.
DRAWN = "!!(window.AD && AD.app && AD.app.state) && !document.querySelector('#accounts .loading-line')"


def demo_engine(case, scenario):
    """The engine of demo `scenario` with its tick thread off; stopped and removed when `case` ends."""
    root = Path(tempfile.mkdtemp(prefix="accountdeck2-demo-"))
    case.addCleanup(shutil.rmtree, root, True)
    case.addCleanup(diagnostics.close)
    engine = demo.build_demo(root, scenario, start=False, inline=True)
    case.addCleanup(engine.stop)
    return engine


def fable_window(used, reset):
    """A Fable 5.1 weekly limit as `usage.claude_usage` saves it, in the 2.0.0 build and now."""
    return {"id": "seven_day_fable", "kind": "model", "label": "Fable 5.1 weekly", "short": "Fable 5.1", "model": "Fable",
            "used": used, "known": True, "reset": reset, "minutes": 10080, "started": True, "blocked": None, "group": None,
            "scope": "Fable", "primary": True, "severity_hint": None, "is_active": None}


def shipped_reading(now, five, weekly, fable=None):
    """A Claude account's reading as the 2.0.0 build saved it, with a 5-hour limit in it, and with a Fable 5.1
    weekly limit when `fable` gives its percent used. It is what that build's own `usage.claude_usage` made of
    an answer, written out here so the new parser has no part in it."""
    def window(wid, kind, label, short, minutes, used, reset):
        return {"id": wid, "kind": kind, "label": label, "short": short, "model": None, "used": used, "known": True,
                "reset": reset, "minutes": minutes, "started": True, "blocked": None, "group": None, "scope": None,
                "primary": True, "severity_hint": None, "is_active": None}
    model = [] if fable is None else [fable_window(fable, now + 4 * 86400)]
    return {"checked": now, "credits": [], "reset_credits": [], "tokens": None,
            "raw_windows": [{"key": "five_hour", "used": five, "reset": now + 7200},
                            {"key": "seven_day", "used": weekly, "reset": now + 4 * 86400}]
                           + [{"key": "seven_day_fable", "used": fable, "reset": now + 4 * 86400} for _ in model],
            "windows": [window("five_hour", "session", "5-hour", "5h", 300, five, now + 7200),
                        window("seven_day", "weekly", "Weekly", "7d", 10080, weekly, now + 4 * 86400)] + model}


@contextlib.contextmanager
def recover_reads(order):
    """Recover looks the open records up again when it is pressed. Inside this block that look returns
    `order`. Yields the records Recover then reached, in the order it reached them."""
    reached = []

    def look(vault):
        for key in order:
            reached.append(key)
            yield key

    with mock.patch.object(recovery, "recover_transactions", look):
        yield reached


def terminal_rows(engine):
    return [a for a in engine.snapshot()["accounts"] if a["provider"] == "claude_cli"]


def terminal_on_disk(engine):
    """Who Claude Code is signed in as, read from its own two files: the account named in .claude.json and
    the account the provider says the login token belongs to. A healthy login has the same id in both."""
    service = engine.cli
    config = json.loads(Path(service.paths.claude_user_config).read_text())
    token = service.adapter.resolve_account(service.adapter.store.read())
    return {"name": (config.get("oauthAccount") or {}).get("accountUuid"), "token": token.get("accountUuid")}


def claude_code_renews(engine):
    """What Claude Code does to its own login on a renewal: same account, new refresh token, later expiry."""
    service = engine.cli
    path = Path(service.paths.claude_cli_credentials)
    credentials = json.loads(path.read_text())
    credentials["claudeAiOauth"] = dict(credentials["claudeAiOauth"],
                                        refreshToken=credentials["claudeAiOauth"]["refreshToken"] + "-renewed",
                                        expiresAt=(service.clock() + 8 * 3600) * 1000)
    path.write_text(json.dumps(credentials))


def once_the_login_is(engine, refresh_token, then):
    """Run `then()` once, at the first read of the live terminal login after it holds `refresh_token`.
    This is how a test acts "right after AccountDeck's swap, before its own check of the result"."""
    store = engine.cli.adapter.store
    real_read, fired = store.read, []

    def read(*args, **kwargs):
        if not fired:
            try:
                live = json.loads(Path(engine.cli.paths.claude_cli_credentials).read_text())
            except (OSError, ValueError):
                live = {}
            if (live.get("claudeAiOauth") or {}).get("refreshToken") == refresh_token:
                fired.append(True)
                then()
        return real_read(*args, **kwargs)

    store.read = read
    return fired


def with_reading(engine, key, *, weekly, fable=None):
    """Give a Claude account a reading taken this second, with this percent of its week used. `fable` is the
    percent used of its Fable 5.1 weekly limit. Without it the reading has no such limit."""
    now = engine.clock()
    usage = copy.deepcopy(engine.store.account(key)["last_good_usage"])
    usage["checked"] = now
    for window in usage["windows"]:
        if window.get("kind") == "weekly":
            window.update(used=float(weekly), reset=now + 4 * 86400)
    usage["windows"] = [w for w in usage["windows"] if w.get("kind") != "model"]
    if fable is not None:
        usage["windows"].append(fable_window(float(fable), now + 4 * 86400))
    engine.store.observe(key, usage, at=now)


class WaitingOperation(Operation):
    """Stands in for another client's "switch when idle": an operation that sits in its waiting step until
    the test releases it, as a Desktop or Codex switch does while a session stays busy."""

    def __init__(self, provider="codex"):
        super().__init__("switch", provider=provider, label="stand-in", consent="idle",
                         steps=(("wait", "Waiting for sessions to close"),))
        self.release = threading.Event()

    def run(self):
        with self.step("wait", phase="waiting_idle", message="Waiting for sessions to close…", cancellable=True):
            while not self.release.wait(0.05):
                self.checkpoint()


class StubEngine:
    """The engine as the local server sees it, answering with one fixed dashboard state."""
    pending = None

    def __init__(self, state):
        self.state, self.actions = state, []

    def snapshot(self, rev=None):
        return self.state

    def settings(self):
        return self.state.get("settings", {})

    def history(self, account, since, limit):
        return {"account": account, "readings": [], "snapshots": []}

    def diagnostics_tail(self, lines):
        return {"lines": [], "path": "", "health": {}}

    def manifest(self, op):
        return None

    def action(self, name, body):
        self.actions.append((name, body))
        return {}

    def update_settings(self, patch):
        pass

    def stop(self):
        pass


def serve(case, state):
    """The real local server on a free port, answering with `state`; shut down when `case` ends."""
    server = Server(("127.0.0.1", 0), StubEngine(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    case.addCleanup(server.server_close)
    case.addCleanup(server.shutdown)
    return server


def in_chrome():
    """Which browser draws the dashboard in tests. The app shows it in a WKWebView and Safari is on every Mac,
    so WebKit is the engine a person sees and the one tests use. ACCOUNTDECK_TEST_BROWSER=chrome runs them in
    headless Chrome instead, for the people who open the dashboard in a Chrome-family browser."""
    return os.environ.get(BROWSER_ENV) == "chrome"


def find_chrome():
    """A headless Chrome for the dashboard tests: $ACCOUNTDECK_TEST_CHROME, Playwright's download, or Chrome."""
    home = Path.home()
    candidates = [os.environ.get(CHROME_ENV, "")]
    candidates += sorted(glob.glob(str(home / "Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")),
                         reverse=True)
    candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return next((path for path in candidates if path and os.access(path, os.X_OK)), None)


def render(case, state):
    """Draw `state` with the real dashboard files. Returns (page HTML, console error lines). The browser is
    the one `in_chrome` names. Skips the test, loudly, when that browser is missing."""
    server = serve(case, state)
    url = f"http://127.0.0.1:{server.server_port}/#token={server.token}"
    if not in_chrome():
        return render_in_webkit(case, url)
    chrome = find_chrome()
    if chrome is None:
        case.skipTest(f"no headless Chrome found; set {CHROME_ENV} to run the dashboard tests")
    profile = tempfile.mkdtemp(prefix="accountdeck-test-chrome-")
    case.addCleanup(shutil.rmtree, profile, True)
    page = subprocess.run([chrome, "--headless", "--dump-dom", "--virtual-time-budget=9000", "--enable-logging=stderr",
                           "--v=0", "--no-first-run", "--disable-gpu", "--window-size=1280,2400",
                           f"--user-data-dir={profile}", url], capture_output=True, text=True, timeout=120)
    errors = [line for line in page.stderr.splitlines()
              if "CONSOLE" in line and re.search(r"Uncaught|Error|error|Refused|violat", line)]
    return page.stdout, errors


def render_in_webkit(case, url):
    try:
        window = webkit.AppWindow("app")
    except OSError as exc:
        case.skipTest(f"{exc}; the dashboard tests cannot run in WebKit")
    case.addCleanup(window.close)
    window.call("load", url=url)
    deadline = time.monotonic() + 15
    while not window.js(DRAWN):
        if time.monotonic() > deadline:
            raise AssertionError(f"the page never drew; console: {window.problems()}")
        time.sleep(0.03)
    # The page has drawn its first state and asks for nothing more until its next poll.
    return window.js("document.documentElement.outerHTML"), window.problems()


def text_of(html):
    """The visible words of a piece of page HTML, one string."""
    return " ".join(part.strip() for part in re.sub(r"<[^>]+>", "\n", html).split("\n") if part.strip())


def element(html, tag, marker):
    """The first `<tag ...marker...>…</tag>` in `html`, or "" when the page has none."""
    match = re.search(rf"<{tag}\b[^>]*{re.escape(marker)}[^>]*>.*?</{tag}>", html, re.S)
    return match.group(0) if match else ""
