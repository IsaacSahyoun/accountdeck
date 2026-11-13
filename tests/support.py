"""Shared test helpers: a whole engine on synthetic data, the dashboard drawn in headless Chrome, and the
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
from unittest import mock  # noqa: E402

from accountdeck import demo, diagnostics, recovery  # noqa: E402
from accountdeck.operations import Operation  # noqa: E402
from accountdeck.server import Server  # noqa: E402

CHROME_ENV = "ACCOUNTDECK_TEST_CHROME"


def demo_engine(case, scenario):
    """The engine of demo `scenario` with its tick thread off; stopped and removed when `case` ends."""
    root = Path(tempfile.mkdtemp(prefix="accountdeck2-demo-"))
    case.addCleanup(shutil.rmtree, root, True)
    case.addCleanup(diagnostics.close)
    engine = demo.build_demo(root, scenario, start=False, inline=True)
    case.addCleanup(engine.stop)
    return engine


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


def with_reading(engine, key, *, session=None, weekly=None, reset_in=3 * 3600):
    """Give account `key` a reading taken this second, with the given percent used."""
    now = engine.clock()
    usage = copy.deepcopy(engine.store.account(key)["last_good_usage"])
    usage["checked"] = now
    for window in usage["windows"]:
        if window.get("kind") == "session" and session is not None:
            window.update(used=float(session), reset=now + reset_in)
        if window.get("kind") == "weekly" and weekly is not None:
            window.update(used=float(weekly), reset=now + 4 * 86400)
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


def find_chrome():
    """A headless Chrome for the dashboard tests: $ACCOUNTDECK_TEST_CHROME, Playwright's download, or Chrome."""
    home = Path.home()
    candidates = [os.environ.get(CHROME_ENV, "")]
    candidates += sorted(glob.glob(str(home / "Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")),
                         reverse=True)
    candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return next((path for path in candidates if path and os.access(path, os.X_OK)), None)


def render(case, state):
    """Draw `state` with the real dashboard files in headless Chrome. Returns (page HTML, console error lines).
    Skips the test, loudly, when no Chrome is installed."""
    chrome = find_chrome()
    if chrome is None:
        case.skipTest(f"no headless Chrome found; set {CHROME_ENV} to run the dashboard tests")
    server = serve(case, state)
    profile = tempfile.mkdtemp(prefix="accountdeck-test-chrome-")
    case.addCleanup(shutil.rmtree, profile, True)
    url = f"http://127.0.0.1:{server.server_port}/#token={server.token}"
    page = subprocess.run([chrome, "--headless", "--dump-dom", "--virtual-time-budget=9000", "--enable-logging=stderr",
                           "--v=0", "--no-first-run", "--disable-gpu", "--window-size=1280,2400",
                           f"--user-data-dir={profile}", url], capture_output=True, text=True, timeout=120)
    errors = [line for line in page.stderr.splitlines()
              if "CONSOLE" in line and re.search(r"Uncaught|Error|error|Refused|violat", line)]
    return page.stdout, errors


def text_of(html):
    """The visible words of a piece of page HTML, one string."""
    return " ".join(part.strip() for part in re.sub(r"<[^>]+>", "\n", html).split("\n") if part.strip())


def element(html, tag, marker):
    """The first `<tag ...marker...>…</tag>` in `html`, or "" when the page has none."""
    match = re.search(rf"<{tag}\b[^>]*{re.escape(marker)}[^>]*>.*?</{tag}>", html, re.S)
    return match.group(0) if match else ""
