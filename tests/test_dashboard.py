"""The local server and the dashboard as a person uses them: every request rule of the server, every demo
scenario drawn by headless Chrome, and the rows clicked through the Chrome DevTools Protocol. Synthetic data
only. The dashboard tests skip, loudly, when no Chrome is installed (see tests/support.find_chrome)."""
import base64
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request

from tests import support

from accountdeck import fixtures
from accountdeck.core import DeckError
from accountdeck.server import Server

WAIT = 15.0
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


# ---- headless Chrome, driven over the DevTools Protocol ----

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


class Browser:
    """One headless Chrome page. `problems` collects what the page's console would show in red.
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

    def wait(self, expression, why=None):
        """Waits until the expression is truthy and returns its value."""
        deadline = time.monotonic() + WAIT
        while True:
            value = self.js(expression)
            if value:
                return value
            if time.monotonic() > deadline:
                raise AssertionError(f"the page never showed: {why or expression}; console: {self.problems}")
            time.sleep(0.03)

    def open(self, server):
        """Opens the dashboard of `server` the way the app does and waits for its first drawing."""
        self.problems.clear()
        self.call("Page.navigate", url=f"http://127.0.0.1:{server.server_port}/#token={server.token}")
        self.wait("!!(window.AD && AD.app && AD.app.state) && !document.querySelector('#accounts .loading-line')",
                  "the dashboard with its first state")

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
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", clickCount=1)

    def type(self, text):
        self.call("Input.insertText", text=text)

    def press(self, key, code=None):
        codes = {"Enter": 13, "Escape": 27, "ArrowDown": 40, "ArrowUp": 38, "Home": 36, "End": 35, " ": 32}
        for kind in ("rawKeyDown", "char", "keyUp") if key in ("Enter", " ") else ("rawKeyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", type=kind, key=key, code=code or key, windowsVirtualKeyCode=codes[key],
                      text="\r" if key == "Enter" and kind == "char" else key if kind == "char" else "")


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
    """One Chrome for the whole class; each test opens the page of its own server."""

    @classmethod
    def setUpClass(cls):
        cls.browser = Browser(cls.addClassCleanup)

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
                self.assertEqual(titles, ["Claude Code accounts", "Claude Desktop", "Codex", "Muse Code"])


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
    def test_a_saved_account_offers_switch(self):
        state = fixtures.example_state("typical", now=time.time())
        server = self.show(state)
        for count, provider in enumerate(("claude", "muse"), 1):
            account = self.account(state, provider, active=False)
            self.browser.click(button("Switch…", f"[...document.querySelectorAll('#other-accounts li.row')]"
                                                 f".find((r) => r.querySelector('.row-name').textContent === {json.dumps(account['label'])})"))
            self.assertEqual(self.posted(server, count)[-1], ("switch-plan", {"id": account["id"]}))
        self.assertEqual(self.browser.problems, [])

    def test_the_account_in_use_and_not_saved_offers_save(self):
        state = fixtures.example_state("first-run", now=time.time())
        server = self.show(state)
        self.browser.click(button("Save Current Account", "document.querySelector('#other-accounts li.row')"))
        self.assertEqual(self.posted(server), [("switch-plan", {"provider": "claude", "purpose": "capture"})])

    def test_an_account_that_needs_sign_in_offers_sign_in_again(self):
        state = fixtures.example_state("needs-signin", now=time.time())
        server = self.show(state)
        beta = self.account(state, "claude", label="Beta")
        self.assertEqual(beta["state"], "needs_signin")
        self.browser.click(button("Sign In Again", "[...document.querySelectorAll('#other-accounts li.row')]"
                                                   ".find((r) => r.querySelector('.row-name').textContent === 'Beta')"))
        self.assertEqual(self.posted(server), [("switch-plan", {"provider": "claude", "purpose": "add"})])

    def test_each_client_section_offers_save_and_add_and_says_how_to_start_when_empty(self):
        state = fixtures.example_state("empty", now=time.time())
        server = self.show(state)
        sections = "[...document.querySelectorAll('#other-accounts .other-provider')]"
        self.assertEqual(self.browser.js(f"{sections}.map((s) => s.querySelector('.empty-text').textContent.slice(0, 12))"),
                         ["Sign in to C", "Sign in to C", "Use Add Acco"])
        for index, provider in enumerate(("claude", "codex", "muse")):
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


if __name__ == "__main__":
    unittest.main()
