"""The local server and the dashboard as a person uses them: every request rule of the server, every demo
scenario drawn in a browser, and the rows clicked with real mouse and key events. The browser is the WebKit
of the app's own window, or headless Chrome with ACCOUNTDECK_TEST_BROWSER=chrome. Synthetic data only.
The dashboard tests skip, loudly, when the browser they need is missing (see tests/support.in_chrome)."""
import base64
import copy
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request

from tests import support, webkit

from accountdeck import fixtures
from accountdeck.core import DeckError
from accountdeck.server import Server

WAIT = 15.0
ROOT = Path(__file__).resolve().parent.parent
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---- the server, spoken to without a browser ----

class Client:
    """One HTTP client of a test server: it sends what a dashboard page would, unless told otherwise."""

    def __init__(self, server):
        self.server, self.cookie = server, None

    def request(self, method, path, body=None, *, headers=None, host=None):
        sent = {"Host": host or f"127.0.0.1:{self.server.server_port}"}
        if method == "POST":
            sent.update({"Origin": self.server.origin, "X-AccountDeck": "1", "Content-Type": "application/json"})
        if self.cookie:
            sent["Cookie"] = self.cookie
        sent.update(headers or {})
        data = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in sent.items():
                if value is not None:
                    connection.putheader(name, value)
            if data is not None:
                connection.putheader("Content-Length", str(len(data)))
            connection.endheaders(data)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def get(self, path, **options):
        return self.request("GET", path, **options)

    def post(self, path, body=None, **options):
        return self.request("POST", path, {} if body is None else body, **options)

    def sign_in(self):
        status, headers, _ = self.post("/api/session", {"token": self.server.token})
        self.cookie = (headers.get("Set-Cookie") or "").split(";")[0] or None
        return status, headers


class FailingEngine(support.StubEngine):
    """Answers every action with the DeckError it was given."""

    def __init__(self, state, error):
        super().__init__(state)
        self.error = error

    def action(self, name, body):
        raise self.error


def serve(case, engine):
    """The real server around `engine` on a free port; shut down when `case` ends."""
    server = Server(("127.0.0.1", 0), engine)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    case.addCleanup(server.server_close)
    case.addCleanup(server.shutdown)
    return server


class ServerRulesTest(unittest.TestCase):
    def setUp(self):
        self.state = fixtures.example_state("typical")
        self.server = support.serve(self, self.state)
        self.client = Client(self.server)

    def test_the_page_and_its_files_need_no_sign_in_and_carry_the_security_headers(self):
        for path, kind in (("/", "text/html"), ("/icon.svg", "image/svg+xml"), ("/static/js/app.js", "text/javascript"),
                           ("/static/css/base.css", "text/css")):
            with self.subTest(path=path):
                status, headers, body = self.client.get(path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith(kind))
                self.assertEqual(int(headers["Content-Length"]), len(body))
                self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
                self.assertEqual((headers["Cache-Control"], headers["X-Content-Type-Options"], headers["X-Frame-Options"]),
                                 ("no-store", "nosniff", "DENY"))

    def test_only_files_inside_the_web_folder_are_served(self):
        for path in ("/static/../server.py", "/static/%2e%2e/server.py", "/static/js/../../server.py", "/static/.hidden.js",
                     "/static/js", "/static/js/missing.js", "/static/js/app.py", "/server.py", "/static/"):
            with self.subTest(path=path):
                status, _, body = self.client.get(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"ThreadingHTTPServer", body)

    def test_a_request_under_another_host_name_is_refused(self):
        for host in ("localhost:%d" % self.server.server_port, "127.0.0.1", "evil.example"):
            with self.subTest(host=host):
                self.assertEqual(self.client.get("/", host=host)[0], 403)
                self.assertEqual(self.client.post("/api/session", {"token": self.server.token}, host=host)[0], 403)

    def test_the_api_needs_the_session_the_token_opens(self):
        self.assertEqual(self.client.get("/api/state")[0], 401)
        self.assertEqual(self.client.post("/api/action", {"action": "refresh"})[0], 401)
        self.assertEqual(self.client.post("/api/session", {"token": "wrong"})[0], 401)
        self.assertEqual(self.client.post("/api/session", {"token": None})[0], 401)
        self.assertEqual(self.client.get("/api/state")[0], 401)
        self.assertEqual(self.client.sign_in()[0], 200)
        status, _, body = self.client.get("/api/state")
        self.assertEqual((status, json.loads(body)["rev"]), (200, self.state["rev"]))

    def test_a_guessed_or_foreign_session_is_refused(self):
        self.client.sign_in()
        other = support.serve(self, self.state)
        stranger = Client(other)
        stranger.cookie = self.client.cookie                    # another AccountDeck's session, or a stale one
        self.assertEqual(stranger.get("/api/state")[0], 401)
        stranger.cookie = f"{other.cookie_name}=guess" if hasattr(other, "cookie_name") else "guess"
        self.assertEqual(stranger.get("/api/state")[0], 401)

    def test_a_post_must_come_from_the_dashboard_page(self):
        self.client.sign_in()
        body = {"action": "refresh"}
        for headers in ({"Origin": None}, {"Origin": "http://evil.example"}, {"Origin": "http://127.0.0.1:1"},
                        {"X-AccountDeck": None}, {"Content-Type": "text/plain"}, {"Content-Type": None}):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.post("/api/action", body, headers=headers)[0], 403)
        self.assertEqual(self.server.engine.actions, [])
        self.assertEqual(self.client.post("/api/action", body)[0], 200)
        self.assertEqual(self.server.engine.actions, [("refresh", body)])

    def test_a_bad_body_or_an_unknown_route_is_answered_in_json(self):
        self.client.sign_in()
        for path, body, expected in (("/api/action", b"not json", 400), ("/api/action", b"[1]", 400), ("/api/action", {}, 400),
                                     ("/api/action", {"action": 7}, 400), ("/api/settings", {"patch": "x"}, 400),
                                     ("/api/nothing", {}, 404), ("/api/action", b"x" * 70000, 400)):
            with self.subTest(path=path, body=body[:12] if isinstance(body, bytes) else body):
                status, headers, answer = self.client.post(path, body)
                self.assertEqual(status, expected)
                self.assertTrue(headers["Content-Type"].startswith("application/json"))
                self.assertTrue(json.loads(answer)["error"])
        for path, expected in (("/api/nothing", 404), ("/nothing", 404), ("/api/history", 400), ("/api/history?id=a&since=x", 400),
                               ("/api/history?id=a&limit=-1", 400), ("/api/manifest?op=..", 400), ("/api/manifest", 404),
                               ("/api/state?rev=x", 400)):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path)[0], expected)
        self.assertEqual(self.client.get("/api/history?id=a&limit=9999")[0], 200)
        self.assertEqual(self.client.get("/api/diagnostics")[0], 200)
        self.assertEqual(self.server.engine.actions, [])

    def test_an_engine_refusal_keeps_its_code_and_its_plan(self):
        for error, status in ((DeckError("Choose an account.", "invalid"), 400), (DeckError("Gone.", "not_found"), 404),
                              (DeckError("Busy.", "busy"), 409), (DeckError("Changed.", "needs_reconfirm"), 409)):
            with self.subTest(code=error.code):
                if error.code == "needs_reconfirm":
                    error.plan, error.retry_after = {"plan_id": "pl_1"}, 30
                client = Client(serve(self, FailingEngine(self.state, error)))
                client.sign_in()
                got, _, body = client.post("/api/action", {"action": "switch"})
                answer = json.loads(body)
                self.assertEqual((got, answer["code"], answer["error"]), (status, error.code, str(error)))
                self.assertEqual(answer.get("plan"), getattr(error, "plan", None))
        failing = support.StubEngine(self.state)
        failing.action = lambda name, body: 1 / 0
        client = Client(serve(self, failing))
        client.sign_in()
        status, _, body = client.post("/api/action", {"action": "switch"})
        self.assertEqual((status, "division" in body.decode()), (500, False))

    def test_an_unchanged_state_is_answered_with_304(self):
        engine = support.StubEngine(self.state)
        engine.snapshot = lambda rev=None: None if rev == self.state["rev"] else self.state
        client = Client(serve(self, engine))
        client.sign_in()
        self.assertEqual(client.get(f"/api/state?rev={self.state['rev']}")[0::2], (304, b""))
        self.assertEqual(client.get("/api/state?rev=1")[0], 200)

    def test_health_answers_without_a_session_and_survives_a_failing_engine(self):
        status, _, body = self.client.get("/_health")
        self.assertEqual((status, json.loads(body)["app"]), (200, "accountdeck2"))
        engine = support.StubEngine(self.state)
        engine.health = lambda: {"stalled": True, "stall_reasons": ["tick"]}
        answer = json.loads(Client(serve(self, engine)).get("/_health")[2])
        self.assertEqual((answer["stalled"], answer["engine"]["stall_reasons"]), (True, ["tick"]))
        engine.health = lambda: 1 / 0
        answer = json.loads(Client(serve(self, engine)).get("/_health")[2])
        self.assertEqual((answer["app"], answer["stalled"]), ("accountdeck2", None))

    def test_stop_is_refused_while_an_action_runs_and_otherwise_ends_the_server(self):
        busy = support.StubEngine(self.state)
        busy.pending = {"id": "op_1"}
        client = Client(serve(self, busy))
        client.sign_in()
        status, _, body = client.post("/api/stop")
        self.assertEqual((status, json.loads(body)["code"]), (409, "busy"))
        stopped = threading.Event()
        idle = support.StubEngine(self.state)
        idle.stop = stopped.set
        server = Server(("127.0.0.1", 0), idle)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        client = Client(server)
        client.sign_in()
        self.assertEqual(client.post("/api/stop")[0], 200)
        thread.join(10)
        self.assertEqual((thread.is_alive(), stopped.is_set()), (False, True))

    def test_the_server_listens_on_this_computer_only(self):
        with self.assertRaises(ValueError):
            Server(("0.0.0.0", 0), support.StubEngine(self.state))


# ---- the dashboard in a browser: headless Chrome over the DevTools Protocol, or the app's WebKit ----

class WebSocket:
    """The little of RFC 6455 the DevTools Protocol needs: text messages to and from one local server."""

    def __init__(self, url):
        address, _, path = url.removeprefix("ws://").partition("/")
        host, _, port = address.partition(":")
        self.conn = socket.create_connection((host, int(port)), timeout=WAIT)
        key = base64.b64encode(os.urandom(16)).decode()
        self.conn.sendall((f"GET /{path} HTTP/1.1\r\nHost: {address}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        reply = b""
        while b"\r\n\r\n" not in reply:
            reply += self._read(1)
        if b" 101 " not in reply.split(b"\r\n")[0]:
            raise OSError(f"Chrome refused the DevTools connection: {reply[:60]!r}")

    def _read(self, count):
        data = b""
        while len(data) < count:
            chunk = self.conn.recv(count - len(data))
            if not chunk:
                raise OSError("Chrome closed the DevTools connection.")
            data += chunk
        return data

    def send(self, text):
        data, mask = text.encode(), os.urandom(4)
        size = len(data)
        head = bytes([0x81]) + (bytes([0x80 | size]) if size < 126 else bytes([0x80 | 126]) + struct.pack(">H", size)
                                if size < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", size))
        self.conn.sendall(head + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(data)))

    def receive(self):
        message = b""
        while True:
            first, second = self._read(2)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack(">H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._read(8))[0]
            data = self._read(size)
            opcode = first & 0x0F
            if opcode == 0x8:
                raise OSError("Chrome closed the DevTools connection.")
            if opcode in (0x0, 0x1):
                message += data
                if first & 0x80:
                    return message.decode()

    def close(self):
        self.conn.close()


class Page:
    """What the dashboard tests ask of a browser. `problems` collects what the page's console would show in
    red. An engine gives `js`, `load`, `mouse`, `type` and `press`."""

    def wait(self, expression, why=None, seconds=WAIT):
        """Waits until the expression is truthy and returns its value. Gives up after `seconds`."""
        deadline = time.monotonic() + seconds
        while True:
            value = self.js(expression)
            if value:
                return value
            if time.monotonic() > deadline:
                raise AssertionError(f"the page never showed: {why or expression}; console: {self.problems}")
            time.sleep(0.03)

    def open(self, server):
        """Opens the dashboard of `server` the way the app does and waits for its first drawing. The page of
        the test before is closed first, and what it reported is read before the list is emptied: that page
        outlives its server, and a problem it reported while no test was reading would otherwise count
        against this one."""
        self.load("about:blank")
        self.wait("location.href === 'about:blank'", "the page before to close")
        self.problems.clear()
        self.load(f"http://127.0.0.1:{server.server_port}/#token={server.token}")
        self.wait(support.DRAWN, "the dashboard with its first state")

    def click(self, element, why=None):
        """A real mouse click in the middle of the element the expression names, once it is on the page.
        Fails when something else covers that spot, as it would for a person."""
        find = f"(() => {{ const e = {element}; return e && e.isConnected ? e : null; }})()"
        self.wait(f"!!{find}", why or element)
        x, y, hit = self.js(f"""(() => {{ const e = {find}; e.scrollIntoView({{block: 'center'}});
            const r = e.getBoundingClientRect(); const x = r.x + r.width / 2, y = r.y + r.height / 2;
            const top = document.elementFromPoint(x, y); return [x, y, !!top && (top === e || e.contains(top))]; }})()""")
        if not hit:
            raise AssertionError(f"{why or element} is covered by something else")
        self.mouse(x, y)


class Chrome(Page):
    """One headless Chrome page, driven over the DevTools Protocol.
    `cleanup(function, *args)` registers what to undo when the tests that use it are done."""

    def __init__(self, cleanup):
        chrome = support.find_chrome()
        if chrome is None:
            raise unittest.SkipTest(f"no headless Chrome found; set {support.CHROME_ENV} to run the dashboard tests")
        self.sent, self.problems = 0, []
        profile = tempfile.mkdtemp(prefix="accountdeck-test-chrome-")
        cleanup(shutil.rmtree, profile, True)
        self.process = subprocess.Popen(
            [chrome, "--headless", "--remote-debugging-port=0", f"--user-data-dir={profile}", "--no-first-run",
             "--disable-gpu", "--window-size=1280,1600", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cleanup(self.process.wait, 10)
        cleanup(self.process.kill)
        port_file = Path(profile) / "DevToolsActivePort"
        self._until(port_file.exists, "Chrome did not open its DevTools port")
        port = port_file.read_text().split()[0]
        pages = []
        self._until(lambda: pages.extend(p for p in json.load(_LOCAL.open(f"http://127.0.0.1:{port}/json/list", timeout=WAIT))
                                         if p["type"] == "page") or pages, "Chrome opened no page")
        self.socket = WebSocket(pages[0]["webSocketDebuggerUrl"])
        cleanup(self.socket.close)
        for domain in ("Page", "Runtime", "Log"):
            self.call(f"{domain}.enable")

    @staticmethod
    def _until(check, failure):
        deadline = time.monotonic() + WAIT
        while not check():
            if time.monotonic() > deadline:
                raise AssertionError(failure)
            time.sleep(0.02)

    def call(self, method, **params):
        self.sent += 1
        self.socket.send(json.dumps({"id": self.sent, "method": method, "params": params}))
        while True:
            message = json.loads(self.socket.receive())
            if message.get("id") == self.sent:
                if "error" in message:
                    raise AssertionError(f"{method}: {message['error']}")
                return message["result"]
            self._note(message)

    def _note(self, event):
        """Keeps console errors, uncaught exceptions and what Chrome itself logs as an error (a refused
        request, a Content-Security-Policy violation)."""
        method, params = event.get("method"), event.get("params") or {}
        if method == "Runtime.exceptionThrown":
            details = params["exceptionDetails"]
            self.problems.append((details.get("exception") or {}).get("description") or details.get("text"))
        elif method == "Runtime.consoleAPICalled" and params["type"] in ("error", "assert"):
            self.problems.append(" ".join(str(a.get("value", a.get("description"))) for a in params["args"]))
        elif method == "Log.entryAdded" and params["entry"]["level"] == "error":
            self.problems.append(f"{params['entry']['text']} {params['entry'].get('url', '')}".strip())

    def js(self, expression):
        """The value of a JavaScript expression in the page (a promise is awaited)."""
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise AssertionError(f"{expression}: {result['exceptionDetails']['exception'].get('description')}")
        return result["result"].get("value")

    def load(self, url):
        self.call("Page.navigate", url=url)

    def mouse(self, x, y):
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", clickCount=1)

    def type(self, text):
        self.call("Input.insertText", text=text)

    def press(self, key, code=None):
        codes = {"Enter": 13, "Escape": 27, "ArrowDown": 40, "ArrowUp": 38, "Home": 36, "End": 35, " ": 32}
        for kind in ("rawKeyDown", "char", "keyUp") if key in ("Enter", " ") else ("rawKeyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", type=kind, key=key, code=code or key, windowsVirtualKeyCode=codes[key],
                      text="\r" if key == "Enter" and kind == "char" else key if kind == "char" else "")


class AppWindow(Page):
    """One page in the engine of the app's own window: WebKit, with the user agent that window sends
    (tests/webkit.py). Mouse and key presses go to the web view as events, as a person's would."""

    def __init__(self, cleanup):
        try:
            self.window = webkit.AppWindow("app")
        except OSError as exc:
            raise unittest.SkipTest(f"{exc}; the dashboard tests cannot run in WebKit") from None
        cleanup(self.window.close)
        self.seen = []

    @property
    def problems(self):
        self.seen.extend(self.window.problems())
        return self.seen

    def js(self, expression):
        try:
            return self.window.js(expression)
        except AssertionError as exc:
            raise AssertionError(f"{expression}: {exc}") from None

    def load(self, url):
        self.window.call("load", url=url)

    def mouse(self, x, y):
        self.window.call("click", x=x, y=y)

    def type(self, text):
        if not self.js(f"document.execCommand('insertText', false, {json.dumps(text)})"):
            raise AssertionError("nothing on the page takes typing")

    def press(self, key, code=None):
        self.window.call("press", key=key)


def browser(cleanup):
    """The browser the dashboard tests run in (tests/support.in_chrome): the WebKit of the app's own window,
    or headless Chrome. A change to the dashboard is checked in both."""
    return Chrome(cleanup) if support.in_chrome() else AppWindow(cleanup)


def button(text, inside="document"):
    """JavaScript for the first button under `inside` with exactly these words."""
    return f"[...{inside}.querySelectorAll('button')].find((b) => b.textContent.trim() === {json.dumps(text)})"


def row(account):
    """JavaScript for the dashboard row of an account."""
    return f"document.querySelector('li.row[data-id={json.dumps(account['id'])}]')"


def more(account):
    return f"document.querySelector('[aria-label={json.dumps('More actions for ' + account['label'])}][data-keep={json.dumps('menu-' + account['id'])}]')"


MENU = "document.getElementById('menu')"
SHEET = "document.getElementById('modal')"


class DashboardTestCase(unittest.TestCase):
    """One browser for the whole class; each test opens the page of its own server."""

    @classmethod
    def setUpClass(cls):
        cls.browser = browser(cls.addClassCleanup)

    def show(self, state):
        server = support.serve(self, state)
        self.browser.open(server)
        return server

    def account(self, state, provider, **wanted):
        return next(a for a in state["accounts"] if a["provider"] == provider
                    and all(a.get(key) == value for key, value in wanted.items()))

    def posted(self, server, count=1):
        """The actions the page has posted, once there are `count` of them."""
        deadline = time.monotonic() + WAIT
        while len(server.engine.actions) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        return [(name, {key: value for key, value in body.items() if key != "action"})
                for name, body in server.engine.actions]


class PageBeforeTest(DashboardTestCase):
    def test_a_problem_of_the_page_before_is_not_counted_against_the_next_page(self):
        """A class's tests share one browser, and each opens the page of its own server. The page of the test
        before stays open after its server is gone and keeps asking it for the state. Chrome reports each refused
        request, and the test browser reads a report only when it is next spoken to. That was after `open` had
        emptied the list, so the next test found a problem that was not its own (it failed one test in the
        whole check of 2026-11-13, in Chrome). Here the page before reports a problem while nobody is reading
        the browser, and asks its server for the state right after, which tells the test that it has."""
        state = fixtures.example_state("typical")
        before = self.show(state)
        asked, answer = threading.Event(), before.engine.snapshot
        before.engine.snapshot = lambda rev=None: (asked.set(), answer(rev))[1]
        self.browser.js("setTimeout(() => { console.error('a problem of the page before'); AD.api.poll(); }, 0), 1")
        self.assertTrue(asked.wait(WAIT), "the page before never asked for the state")
        self.show(state)
        self.assertEqual(self.browser.problems, [])


class ScenariosDrawTest(DashboardTestCase):
    def test_every_scenario_draws_every_account_with_a_clean_console(self):
        self.assertEqual(len(fixtures.SCENARIOS), 23)
        for scenario in fixtures.SCENARIOS:
            with self.subTest(scenario=scenario):
                state = fixtures.example_state(scenario, now=time.time())
                self.show(state)
                shown = self.browser.js("[...document.querySelectorAll('#accounts li.row, #other-accounts li.row')]"
                                        ".map((r) => r.querySelector('.row-name').textContent)")
                self.assertEqual(sorted(shown), sorted(a["label"] for a in state["accounts"]))
                self.assertEqual(self.browser.problems, [])
                titles = self.browser.js("[...document.querySelectorAll('.section-title')].map((t) => t.textContent)")
                self.assertEqual(titles, ["Claude Code", "Codex", "Muse Code"])


class TerminalRowsTest(DashboardTestCase):
    def setUp(self):
        self.state = fixtures.example_state("typical", now=time.time())
        self.server = self.show(self.state)
        self.beta = self.account(self.state, "claude_cli", label="Beta")

    def menu_words(self):
        return self.browser.wait(f"{MENU}.matches(':popover-open') && [...{MENU}.children].map((n) => n.textContent)", "the More menu")

    def test_more_offers_leave_out_rename_and_remove(self):
        self.browser.click(more(self.beta))
        self.assertEqual(self.menu_words(), ["Leave Out of Auto-Switch", "Rename…", "", "Remove…"])
        self.browser.press("Escape")
        self.browser.wait(f"!{MENU}.matches(':popover-open')", "the menu closed")
        gamma = self.account(self.state, "claude_cli", label="Gamma")          # already left out
        self.browser.click(more(gamma))
        self.assertEqual(self.menu_words()[0], "Let Auto-Switch Use It")

    def test_rename_posts_the_new_name(self):
        self.browser.click(more(self.beta))
        self.browser.click(button("Rename…", MENU))
        self.browser.wait(f"{SHEET}.open && document.activeElement.value === 'Beta'", "the rename sheet with the old name selected")
        self.browser.type("Work account")
        self.browser.click(button("Rename", SHEET))
        self.assertEqual([(name, body["id"], body["label"]) for name, body in self.posted(self.server)],
                         [("rename", self.beta["id"], "Work account")])
        self.browser.wait(f"!{SHEET}.open", "the sheet closed")

    def test_return_in_the_name_field_renames_and_an_unchanged_name_posts_nothing(self):
        self.browser.click(more(self.beta))
        self.browser.click(button("Rename…", MENU))
        self.browser.wait(f"{SHEET}.open && document.activeElement.value === 'Beta'")
        self.browser.press("Enter")
        self.browser.wait(f"!{SHEET}.open", "the sheet closed")
        self.browser.click(more(self.beta))
        self.browser.click(button("Rename…", MENU))
        self.browser.wait(f"{SHEET}.open && document.activeElement.value === 'Beta'")
        self.browser.type("Side")
        self.browser.press("Enter")
        self.assertEqual([(name, body["label"]) for name, body in self.posted(self.server)], [("rename", "Side")])

    def test_remove_asks_first_and_then_posts(self):
        self.browser.click(more(self.beta))
        self.browser.click(button("Remove…", MENU))
        self.browser.wait(f"{SHEET}.open && document.getElementById('modal-title').textContent === 'Remove “Beta”?'", "the question")
        self.browser.click(button("Cancel", SHEET))
        self.browser.wait(f"!{SHEET}.open")
        self.assertEqual(self.server.engine.actions, [])
        self.browser.click(more(self.beta))
        self.browser.click(button("Remove…", MENU))
        self.browser.click(button("Remove", SHEET))
        self.assertEqual([(name, body["id"]) for name, body in self.posted(self.server)], [("remove", self.beta["id"])])

    def test_the_account_in_use_cannot_be_removed(self):
        alpha = self.account(self.state, "claude_cli", active=True)
        self.browser.click(more(alpha))
        self.assertEqual(self.menu_words()[-1], "Switch to another account before removing this one.")
        self.assertEqual(self.browser.js(f"{button('Remove…', MENU)}.getAttribute('aria-disabled')"), "true")
        self.browser.click(button("Remove…", MENU))
        time.sleep(0.2)
        self.assertFalse(self.browser.js(f"{SHEET}.open"))

    def test_leave_out_posts_the_reverse_of_what_the_account_is(self):
        gamma = self.account(self.state, "claude_cli", label="Gamma")
        for account, excluded in ((self.beta, True), (gamma, False)):
            self.browser.click(more(account))
            self.browser.click(MENU + ".querySelector('[role=menuitem]')")
            self.browser.wait(f"!{MENU}.matches(':popover-open')")
        self.assertEqual([(name, body["id"], body["excluded"]) for name, body in self.posted(self.server, 2)],
                         [("cli-exclude", self.beta["id"], True), ("cli-exclude", gamma["id"], False)])

    def test_the_row_button_follows_the_account(self):
        words = {a["label"]: self.browser.js(f"({row(a)}.querySelector('[data-primary]') || {{}}).textContent || null")
                 for a in self.state["accounts"] if a["provider"] == "claude_cli"}
        self.assertEqual(words, {"Alpha": None, "Beta": "Switch", "Delta": "Switch", "Gamma": "Switch", "Epsilon": "Sign In Again"})
        self.browser.click(f"{row(self.beta)}.querySelector('[data-primary]')")
        self.assertEqual(self.posted(self.server), [("switch-plan", {"id": self.beta["id"]})])
        epsilon = self.account(self.state, "claude_cli", label="Epsilon")
        self.browser.click(f"{row(epsilon)}.querySelector('[data-primary]')")
        self.browser.wait(f"{SHEET}.open && document.getElementById('modal-title').textContent === 'Add a Terminal Account'")

    def test_arrow_keys_move_between_rows_and_return_presses_the_row_button(self):
        order = self.browser.js("[...document.querySelectorAll('#accounts li.row')].map((r) => r.dataset.id)")
        self.browser.js("document.querySelector('#accounts li.row').focus()")
        for key, expected in (("ArrowDown", order[1]), ("End", order[-1]), ("ArrowUp", order[-2]), ("Home", order[0])):
            self.browser.press(key)
            self.assertEqual(self.browser.js("document.activeElement.dataset.id"), expected)
        self.assertEqual(self.browser.js("[...document.querySelectorAll('#accounts li.row')].filter((r) => r.tabIndex === 0).length"), 1)
        target = next(a for a in order if a == self.beta["id"])
        self.browser.js(f"{row(self.beta)}.focus()")
        self.browser.press("Enter")
        self.assertEqual(self.posted(self.server), [("switch-plan", {"id": target})])


class OtherClientsRowsTest(DashboardTestCase):
    def test_the_row_button_follows_the_account(self):
        state = fixtures.example_state("typical", now=time.time())
        server = self.show(state)
        words = {(a["provider"], a["label"]): self.browser.js(f"({row(a)}.querySelector('[data-primary]') || {{}}).textContent || null")
                 for a in state["accounts"] if a["provider"] != "claude_cli"}
        self.assertEqual(words, {(a["provider"], a["label"]): None if a["active"] else "Switch…"
                                 for a in state["accounts"] if a["provider"] != "claude_cli"})
        account = self.account(state, "muse", active=False)
        self.browser.click(f"{row(account)}.querySelector('[data-primary]')")
        self.assertEqual(self.posted(server), [("switch-plan", {"id": account["id"]})])
        self.assertEqual(self.browser.problems, [])

    def test_the_account_in_use_and_not_saved_offers_save_and_no_menu(self):
        state = fixtures.example_state("first-run", now=time.time())
        server = self.show(state)
        unsaved = self.account(state, "codex", saved=False)
        self.assertEqual(self.browser.js(f"[...{row(unsaved)}.querySelectorAll('button')].map((b) => b.textContent)"),
                         ["Save Current Account"])
        self.browser.click(button("Save Current Account", row(unsaved)))
        self.assertEqual(self.posted(server), [("switch-plan", {"provider": "codex", "purpose": "capture"})])

    def test_an_account_that_needs_sign_in_offers_sign_in_again(self):
        state = fixtures.example_state("needs-signin", now=time.time())
        server = self.show(state)
        spare = self.account(state, "codex", label="Codex spare")
        self.assertEqual(spare["state"], "needs_signin")
        self.browser.click(button("Sign In Again", row(spare)))
        self.assertEqual(self.posted(server), [("switch-plan", {"provider": "codex", "purpose": "add"})])

    def test_each_client_section_offers_save_and_add_and_says_how_to_start_when_empty(self):
        state = fixtures.example_state("empty", now=time.time())
        server = self.show(state)
        sections = "[...document.querySelectorAll('#other-accounts .other-provider')]"
        self.assertEqual(self.browser.js(f"{sections}.map((s) => s.querySelector('.empty-text').textContent.slice(0, 12))"),
                         ["Sign in to C", "Use Add Acco"])
        for index, provider in enumerate(("codex", "muse")):
            self.browser.click(button("Add Account", f"{sections}[{index}]"))
            self.browser.click(button("Save Current Account", f"{sections}[{index}]"))
            self.assertEqual(self.posted(server, 2 * index + 2)[-2:], [("switch-plan", {"provider": provider, "purpose": "add"}),
                                                                      ("switch-plan", {"provider": provider, "purpose": "capture"})])

    def test_buttons_are_off_in_read_only_mode(self):
        state = fixtures.example_state("read-only", now=time.time())
        self.show(state)
        self.assertEqual(self.browser.js("[...document.querySelectorAll('#other-accounts button')].filter((b) => !b.disabled"
                                         " && b.getAttribute('aria-haspopup') !== 'menu').map((b) => b.textContent)"), [])
        self.assertEqual(self.browser.js("[...document.querySelectorAll('#accounts [data-primary]')].filter((b) => !b.disabled).length"), 0)

    def test_a_row_says_a_provider_incident_once(self):
        """Found by the screenshot compare of 2026-11-12: the row said it in a badge and again under the name."""
        state = fixtures.example_state("outage", now=time.time())
        self.show(state)
        paused = [a for a in state["accounts"] if (a.get("error") or {}).get("outage")]
        self.assertTrue(paused, "the outage scenario has no paused account")
        for account in paused:
            with self.subTest(account=account["label"]):
                words = self.browser.js(f"{row(account)}.innerText")
                self.assertEqual(words.count("Paused"), 1, words)

    def test_a_limits_note_is_whole_and_under_its_bar(self):
        """Found by the screenshot compare of 2026-11-12: beside the label a note had no room, and the row
        showed "Starts with yo" and "Back in 1h 3"."""
        self.show(fixtures.example_state("typical", now=time.time()))
        notes = self.browser.js("""[...document.querySelectorAll('#other-accounts .row .limit-note')].map((note) =>
            [note.textContent.trim(), note.scrollWidth <= note.clientWidth + 1,
             !!note.previousElementSibling && note.previousElementSibling.matches('.meter')])""")
        self.assertGreaterEqual(len(notes), 6, notes)          # two limits on each Codex and Muse row
        self.assertEqual([note for note in notes if not (note[1] and note[2])], [])

    def test_a_note_with_no_room_ends_in_dots(self):
        """`text-overflow` draws no dots on a flex or grid box. The words stop in the middle of a letter."""
        self.show(fixtures.example_state("typical", now=time.time()))
        for place in ("#other-accounts .row .limit", "#overview .limit.is-compact"):
            with self.subTest(place=place):
                squeezed = self.browser.js(f"""(() => {{
                    const note = [...document.querySelectorAll('{place} .limit-note')]
                        .sort((a, b) => b.textContent.trim().length - a.textContent.trim().length)[0];     // the longest
                    note.closest('.limit').style.width = '70px';
                    const style = getComputedStyle(note);
                    return [note.scrollWidth > note.clientWidth, style.overflowX, style.textOverflow, /flex|grid/.test(style.display)]; }})()""")
                self.assertEqual(squeezed, [True, "hidden", "ellipsis", False])


class SwitchNotchTest(DashboardTestCase):
    """The notch on a bar marks where auto-switch moves the terminals on. It is drawn on the limits the rule
    acts on (`cli_policy.considered`), the weekly one and the Fable weekly one, and on no other model's."""

    def test_the_notch_is_on_the_weekly_and_the_fable_limit_only(self):
        state = fixtures.example_state("typical", now=time.time())
        beta, in_use = self.account(state, "claude_cli", label="Beta"), self.account(state, "claude_cli", active=True)
        self.assertIsNot(beta, in_use)
        for account in (beta, in_use):
            weekly = dict(account["windows"][0], binding=False)

            def model(wid, name, label, binding=False):
                return dict(copy.deepcopy(weekly), id=wid, kind="model", model=name, label=label, short=name, binding=binding)
            # a row shows another model's limit only while it is the one that binds
            account["windows"] = [weekly, model("seven_day_fable", "Fable", "Fable weekly"),
                                  model("seven_day_sonnet", "Sonnet", "Sonnet weekly", binding=True)]
        # the row in the terminal table, and the card "In use by your terminals", whose bars carry their names
        # for a screen reader ("Weekly: 70% used")
        marks = ("[" + f"[...{row(beta)}.querySelectorAll('.limit')].map((part) => "
                 "[part.querySelector('.limit-label').textContent, !!part.querySelector('.meter-mark')]), "
                 "[...document.querySelectorAll('#overview .ov-inuse .limit-stack > .limit')].map((part) => "
                 "[part.querySelector('.meter').getAttribute('aria-valuetext').split(':')[0], !!part.querySelector('.meter-mark')]).sort()]")

        def want(sonnet):
            return [[["Weekly", True], ["Fable weekly", True], ["Sonnet weekly", sonnet]],
                    [["Fable weekly", True], ["Sonnet weekly", sonnet], ["Weekly", True]]]
        server = self.show(state)
        self.assertEqual(self.browser.js(marks), want(False))
        # A settings bundle from an older build can carry a pinned model, and the rule then acts on its limit
        # too. Nothing else in the state changes, so the row and the card are drawn again for the setting
        # alone. And back.
        for pinned, sonnet in (("sonnet", True), ("", False)):
            with self.subTest(pinned_model=pinned):
                server.engine.state = dict(copy.deepcopy(server.engine.state), rev=server.engine.state["rev"] + 1,
                                           settings=dict(state["settings"], pinned_model=pinned))
                self.browser.js("AD.api.poll()")
                wanted = json.dumps(want(sonnet), separators=(",", ":"))
                self.browser.wait(f"JSON.stringify({marks}) === {json.dumps(wanted)}", f"the notch for pinned_model={pinned!r}")


class ClaudeHasNoFiveHourLimitTest(DashboardTestCase):
    """Claude dropped its 5-hour limit. No part of the page names one for a Claude Code account. Codex and Muse
    keep theirs. The page draws the limits it is given, so the first test holds for the demo data and the
    second for what the 2.0.0 build left saved."""
    SAYS_5_HOUR = """Object.fromEntries([['the cards', document.getElementById('overview')],
        ['claude_cli', document.getElementById('accounts')],
        ...[...document.querySelectorAll('#other-accounts .other-provider')].map((part, n) => [['codex', 'muse'][n], part])]
        .map(([name, part]) => [name, part.innerText.includes('5-hour')]))"""
    CELLS = "[...document.querySelectorAll('#accounts .row:not(.is-blocked) .row-limits')].map((r) => r.children.length)"

    def test_only_codex_and_muse_show_a_5_hour_limit(self):
        self.show(fixtures.example_state("typical", now=time.time()))
        self.assertEqual(self.browser.js(self.SAYS_5_HOUR),
                         {"the cards": False, "claude_cli": False, "codex": True, "muse": True})

    def test_a_reading_the_shipped_version_saved_shows_no_5_hour_limit(self):
        # After an upgrade the page draws what 2.0.0 saved until each account is read again. The fourth
        # review: the saved 5-hour bar sat under the Weekly heading, and the card's big number named it.
        engine = support.demo_engine(self, "typical")
        now = engine.clock()
        for saved in engine.store.accounts():
            if saved["provider"] == "claude_cli" and saved["last_good_usage"]:
                engine.store.observe(saved["id"], support.shipped_reading(now, 100.0, 20.0), at=now)
        engine.sense_active()
        self.browser.open(serve(self, engine))
        self.assertEqual(self.browser.js(self.SAYS_5_HOUR),
                         {"the cards": False, "claude_cli": False, "codex": True, "muse": True})
        cells = self.browser.js(self.CELLS)
        self.assertEqual(sorted(set(cells)), [2], cells)
        card = self.browser.js("document.querySelector('#overview .ov-inuse').innerText")
        self.assertIn("20%", card)
        self.assertIn("Weekly limit", card)

    def test_the_claude_code_table_has_a_weekly_and_a_fable_column(self):
        self.show(fixtures.example_state("typical", now=time.time()))
        self.assertEqual(self.browser.js("[...document.querySelectorAll('#accounts .table-head-limits > span')].map((s) => s.textContent)"),
                         ["Weekly", "Fable weekly"])
        self.assertEqual(self.browser.js("[...document.querySelectorAll('#accounts-sort option')].map((o) => o.textContent)"),
                         ["Most room left", "Most Fable room", "Most weekly room", "Soonest weekly reset", "My order"])
        cells = self.browser.js(self.CELLS)
        self.assertEqual(sorted(set(cells)), [2], cells)

    def test_settings_name_the_limits_auto_switch_watches(self):
        self.show(fixtures.example_state("typical", now=time.time()))
        self.browser.click("document.getElementById('open-settings')")
        self.browser.wait("document.getElementById('settings').open", "Settings")
        self.browser.click("[...document.querySelectorAll('#settings-tabs [role=tab]')].find((t) => t.textContent.trim() === 'Auto-Switch')")
        self.browser.wait("document.getElementById('settings-pane-title').textContent === 'Auto-Switch'", "the Auto-Switch tab")
        words = self.browser.js("document.getElementById('settings-body').textContent")
        self.assertIn("Of its weekly or Fable weekly limit.", words)
        self.assertNotIn("5-hour", words)


class InUseCardTest(DashboardTestCase):
    """The card "In use by your terminals" says a limit once. Its big number names the limit closest to full, and
    that limit's bar sits under the number with no line of its own. With Claude's 5-hour limit gone the card said
    "70% used" twice, one above the other."""
    CARD = """(() => { const card = document.querySelector('#overview .ov-inuse');
        const text = (node) => (node ? node.textContent.trim() : null);
        return {number: text(card.querySelector('.figure-line')), caption: text(card.querySelector('.figure-caption')),
                words: card.innerText,
                limits: [...card.querySelectorAll('.limit-stack > .limit')].map((limit) => [
                    text(limit.querySelector('.limit-label')), text(limit.querySelector('.limit-note')),
                    text(limit.querySelector('.limit-value')),
                    limit.querySelector('.meter') ? limit.querySelector('.meter').getAttribute('aria-valuetext').split(',')[0] : null])}; })()"""

    def card(self, change=None):
        """The card for `typical`, after `change(the weekly limit of the account in use, its windows, now)`."""
        now = time.time()
        state = fixtures.example_state("typical", now=now)
        windows = self.account(state, "claude_cli", active=True)["windows"]
        if change:
            change(windows[0], windows, now)
        self.show(state)
        return self.browser.js(self.CARD)

    def test_the_limit_the_number_names_is_a_bar_with_no_line_of_its_own(self):
        card = self.card()
        self.assertEqual((card["number"], card["caption"].split(" · ")[0]), ("70%used", "Weekly limit"))
        self.assertEqual(card["limits"], [[None, None, None, "Weekly: 70% used"],
                                          ["Fable weekly", "Not reported", None, None]])
        self.assertEqual((card["words"].count("70%"), card["words"].count("Weekly")), (1, 1), card["words"])

    def test_the_named_limit_comes_first_when_it_is_not_the_weekly_one(self):
        def fable_is_closest_to_full(weekly, windows, now):
            windows.append(dict(weekly, id="seven_day_fable", kind="model", label="Fable weekly", short="Fable",
                                model="Fable", used=96.0, left=4.0, binding=True))
            weekly.update(used=40.0, left=60.0, binding=False)
        card = self.card(fable_is_closest_to_full)
        self.assertEqual((card["number"], card["caption"].split(" · ")[0]), ("96%used", "Fable weekly limit"))
        self.assertEqual([limit[0::2] + limit[3:] for limit in card["limits"]],
                         [[None, None, "Fable weekly: 96% used"], ["Weekly", "40% used", "Weekly: 40% used"]])

    def test_a_limit_that_just_refilled_keeps_its_line(self):
        """The caption beside the number gives a countdown. "Refilled at" is only in the limit's own line."""
        def refilled(weekly, windows, now):
            weekly.update(used=2.0, left=98.0, refilled={"at": now - 600, "confirmed": now - 240, "from": 92.0})
        card = self.card(refilled)
        self.assertEqual(card["number"], "2%used")
        self.assertEqual(card["limits"][0][0], "Weekly")
        self.assertRegex(card["limits"][0][1], r"^Refilled at \d")
        self.assertEqual(card["limits"][0][2:], ["2% used", "Weekly: 2% used"])


CLIENTS = ("claude_cli", "codex", "muse")
APPS = {"claude_cli": "Claude Code", "codex": "Codex", "muse": "Muse Code"}
METERS_SHOW = ("ready", "watch", "low", "critical")     # every other headline is words under the account's name


class EveryClientRowsTest(DashboardTestCase):
    """One row for all three clients: what a terminal row says and offers, the other clients' rows do too."""

    def setUp(self):
        self.state = fixtures.example_state("typical", now=time.time())

    def picked(self, accounts, provider):
        """One saved row of a client: the account in use for Codex and Muse, another one for Claude Code."""
        return next(a for a in accounts if a["provider"] == provider and a["active"] is (provider in ("codex", "muse")))

    def rename(self, account, name):
        self.browser.click(more(account))
        self.browser.click(button("Rename…", MENU))
        self.browser.wait(f"{SHEET}.open && document.activeElement.value === {json.dumps(account['label'])}",
                          "the rename sheet with the old name selected")
        self.browser.type(name)
        self.browser.click(button("Rename", SHEET))

    def remove(self, account):
        self.browser.click(more(account))
        self.browser.click(button("Remove…", MENU))
        title = json.dumps(f"Remove “{account['label']}”?")
        self.browser.wait(f"{SHEET}.open && document.getElementById('modal-title').textContent === {title}", "the question")
        words = self.browser.js("document.getElementById('modal-body').textContent")
        self.browser.click(button("Remove", SHEET))
        return words

    def test_more_then_rename_posts_the_new_name_for_a_row_of_each_client(self):
        for provider in CLIENTS:
            with self.subTest(provider=provider):
                server = self.show(self.state)
                account = self.picked(self.state["accounts"], provider)
                self.rename(account, f"New {provider}")
                self.assertEqual(self.posted(server), [("rename", {"id": account["id"], "label": f"New {provider}"})])
                self.browser.wait(f"!{SHEET}.open", "the sheet closed")

    def test_more_then_remove_asks_first_and_posts_for_a_row_of_each_client(self):
        for provider in CLIENTS:
            with self.subTest(provider=provider):
                server = self.show(self.state)
                account = self.picked(self.state["accounts"], provider)
                self.assertIn(f"forgets this {APPS[provider]} login", self.remove(account))
                self.assertEqual(self.posted(server), [("remove", {"id": account["id"]})])
                self.browser.wait(f"!{SHEET}.open", "the sheet closed")

    def test_the_app_renames_and_removes_the_row_that_was_clicked_for_each_client(self):
        for provider in CLIENTS:
            with self.subTest(provider=provider):
                engine = support.demo_engine(self, "typical")
                engine.sense_active()
                self.browser.open(serve(self, engine))
                account = self.picked(engine.snapshot()["accounts"], provider)
                self.rename(account, "Renamed here")
                self.browser.wait(f"{row(account)}.querySelector('.row-name').textContent === 'Renamed here'", "the new name")
                self.assertEqual(engine.store.account(account["id"])["label"], "Renamed here")
                self.remove(dict(account, label="Renamed here"))
                self.browser.wait(f"!{row(account)}", "the row gone")
                self.assertIsNone(engine.store.account(account["id"]))
                self.assertEqual(self.browser.problems, [])

    def test_only_the_terminal_account_in_use_cannot_be_removed(self):
        self.show(self.state)
        for provider in CLIENTS:
            with self.subTest(provider=provider):
                account = self.account(self.state, provider, active=True)
                self.browser.click(more(account))
                self.browser.wait(f"{MENU}.matches(':popover-open')", "the More menu")
                self.assertEqual(self.browser.js(f"{button('Remove…', MENU)}.getAttribute('aria-disabled')"),
                                 "true" if provider == "claude_cli" else None)
                self.browser.press("Escape")
                self.browser.wait(f"!{MENU}.matches(':popover-open')", "the menu closed")

    def test_a_row_says_what_its_meters_cannot_for_every_client(self):
        """An old reading, a sign-in that ended, an incident or no reading yet is words under the name."""
        aged = fixtures.example_state("typical", now=time.time())
        for provider in CLIENTS:
            self.picked(aged["accounts"], provider).update(
                age_tier="stale", stale=True, headline={"kind": "stale", "text": "Reading is 3h old", "window": None})
        states = [aged] + [fixtures.example_state(name, now=time.time())
                           for name in ("stale", "needs-signin", "outage", "loading", "codex-blocked", "rate-limited")]
        seen = set()
        for state in states:
            self.show(state)
            shown = self.browser.js("Object.fromEntries([...document.querySelectorAll('li.row[data-id]')].map((r) => "
                                    "[r.dataset.id, (r.querySelector('.row-status') || {}).textContent || null]))")
            for account in state["accounts"]:
                kind, text = account["headline"]["kind"], account["headline"]["text"]
                with self.subTest(provider=account["provider"], account=account["label"], kind=kind):
                    self.assertEqual(shown[account["id"]], None if kind in METERS_SHOW else text)
                seen.add((account["provider"], kind))
        self.assertLessEqual({(provider, "stale") for provider in CLIENTS}, seen)
        self.assertLessEqual({"signin", "out", "outage", "checking", "unknown"}, {kind for _, kind in seen})

    def test_a_row_another_computer_owns_offers_only_remove_for_every_client(self):
        for provider in CLIENTS:
            with self.subTest(provider=provider):
                state = fixtures.example_state("typical", now=time.time())
                account = self.account(state, provider, active=False) if provider != "codex" else self.account(state, provider)
                account.update(owner="on_pc", switchable=False, active=False)
                server = self.show(state)
                self.assertEqual(self.browser.js(f"[...{row(account)}.querySelectorAll('button')].map((b) => b.textContent)"),
                                 ["Remove…"])
                self.assertIn("Still on the PC", self.browser.js(f"{row(account)}.querySelector('.row-title').textContent"))
                self.browser.click(button("Remove…", row(account)))
                self.browser.wait(f"{SHEET}.open", "the question")
                self.assertIn("removes the saved row", self.browser.js("document.getElementById('modal-body').textContent"))
                self.browser.click(button("Remove", SHEET))
                self.assertEqual(self.posted(server), [("remove", {"id": account["id"]})])

    def test_arrow_keys_and_return_work_in_every_list(self):
        server = self.show(self.state)
        lists = "[...document.querySelectorAll('ol.rows')]"
        self.assertEqual(self.browser.js(f"{lists}.map((l) => [...l.children].filter((r) => r.tabIndex === 0).length)"), [1, 1, 1])
        muse = [a for a in self.state["accounts"] if a["provider"] == "muse"]
        self.browser.js(f"{row(muse[0])}.focus()")
        self.browser.press("ArrowDown")
        self.assertEqual(self.browser.js("document.activeElement.dataset.id"), muse[1]["id"])
        self.browser.press("Enter")
        self.assertEqual(self.posted(server), [("switch-plan", {"id": muse[1]["id"]})])
        self.assertEqual(self.browser.js(f"{lists}.map((l) => [...l.children].filter((r) => r.tabIndex === 0).length)"), [1, 1, 1])


class SettingsDrawTest(DashboardTestCase):
    def test_every_settings_tab_draws_its_own_words_with_a_clean_console(self):
        self.show(fixtures.example_state("typical", now=time.time()))
        self.browser.click("document.getElementById('open-settings')")
        self.browser.wait("document.getElementById('settings').open", "Settings")
        tabs = "document.querySelectorAll('#settings-tabs [role=tab]')"
        names = self.browser.js(f"[...{tabs}].map((t) => t.textContent.trim())")
        self.assertEqual(names, ["General", "Auto-Switch", "Notifications", "Menu Bar & Login", "Advanced", "About"])
        words = {}
        for index, name in enumerate(names):
            self.browser.click(f"{tabs}[{index}]")
            self.browser.wait(f"document.getElementById('settings-pane-title').textContent === {json.dumps(name)}", name)
            words[name] = self.browser.js("document.getElementById('settings-body').textContent")
        self.assertIn("Use weekly allowance before it resets", words["Auto-Switch"])
        for label in ("Show in the menu bar", "Open at login", "Listed in System Settings"):
            self.assertIn(label, words["Menu Bar & Login"])
        self.assertEqual(self.browser.problems, [])


class InstallerScriptsTest(unittest.TestCase):
    """install.sh and uninstall.sh are read here, never run: they call launchctl on the real login session."""

    def test_both_scripts_parse(self):
        for name in ("install.sh", "uninstall.sh"):
            with self.subTest(script=name):
                checked = subprocess.run(["/bin/bash", "-n", str(ROOT / name)], capture_output=True, text=True)
                self.assertEqual((checked.returncode, checked.stderr), (0, ""))

    def test_uninstall_keeps_open_at_login_until_the_app_has_quit(self):
        text = (ROOT / "uninstall.sh").read_text()
        refused = text.index("is running. Choose Quit")
        self.assertGreater(text.index('rm -f "$agent"'), text.index("done", refused),
                           "the Open at login file is removed before the wait for the app to quit")


    def test_an_install_below_a_prefix_never_opens_the_app(self):
        """A trial install has no data folder of its own. Its app, opened, runs on the real data and logins,
        and the README once called `--prefix` alone a safe trial (fourth review)."""
        text = (ROOT / "install.sh").read_text()
        start = text.index('if [ -n "$prefix" ]; then\n        mkdir -p "$prefix"')
        self.assertIn("launch=0", text[start:text.index("\n    else\n", start)])
        opened = [at for at in range(len(text)) if text.startswith('open "$APP"', at)]
        self.assertEqual(len(opened), 1, "one place opens the app")
        self.assertIn('if [ "$launch" = 1 ]; then', text[text.rindex("\n    if ", 0, opened[0]):opened[0]])
        self.assertIn("the app is not opened", (ROOT / "README.md").read_text())

    def test_help_prints_the_whole_header_of_each_script(self):
        # --help prints the comment lines at the top by their line numbers, so a line added there moves the end
        for name in ("install.sh", "uninstall.sh"):
            with self.subTest(script=name):
                lines = (ROOT / name).read_text().splitlines()
                last = next(number for number, line in enumerate(lines[1:], 1) if not line.startswith("#"))
                printed = re.search(r"sed -n '(\d+),(\d+)p' \"\$\{BASH_SOURCE\[0\]\}\"", "\n".join(lines))
                self.assertIsNotNone(printed, "the help no longer prints a line range")
                self.assertEqual(tuple(map(int, printed.groups())), (2, last))

    def test_an_install_builds_everything_in_a_work_folder_of_its_own(self):
        text = (ROOT / "install.sh").read_text()
        self.assertIn('WORK=$(mktemp -d "$SUPPORT/.install-XXXXXX")', text)
        staged = dict(re.findall(r'^\s*(APP_NEW|RT_NEW|BUNDLE_NEW)="([^"]*)"$', text, re.M))
        self.assertEqual(staged, {"APP_NEW": "$WORK/app", "RT_NEW": "$WORK/runtime", "BUNDLE_NEW": "$WORK/bundle"})

    def test_an_install_deletes_only_its_work_folder_and_the_copies_it_just_replaced(self):
        """Nothing in install.sh deletes by pattern, so the vault, the data and a running copy are out of reach."""
        text = (ROOT / "install.sh").read_text()
        self.assertEqual(set(re.findall(r'\brm -r?f? ?("[^"]*"|\S+)', text)), {'"$WORK"', '"$RT.old"', '"$APPDIR.old"', '"$APP.old"'})
        for pattern in ("-exec rm", "-delete", "xargs rm", "rm -rf $", "/*"):
            self.assertNotIn(pattern, text)


if __name__ == "__main__":
    unittest.main()
