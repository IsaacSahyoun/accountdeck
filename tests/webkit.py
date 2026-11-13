"""The dashboard in the engine of the app's own window: the Python side of tests/webkit_host.swift.

The app shows the dashboard in a WKWebView (accountdeck/platform/dashboard_window.py), and Safari is the
browser every Mac has, so WebKit is the engine a person really sees. `host()` builds the host once per
version of its source; `AppWindow` starts one and speaks to it. The window is never shown on the screen,
the page's storage is thrown away with the process, and the process ends when its caller does.
This module imports nothing of AccountDeck, so a tool can drive any copy of the code with it."""
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading

SOURCE = Path(__file__).with_name("webkit_host.swift")
WAIT = 30.0


def host():
    """The built host, or None when this Mac has no Swift compiler."""
    swiftc = shutil.which("swiftc")
    if swiftc is None:
        return None
    folder = Path(tempfile.gettempdir()) / f"accountdeck-test-webkit-{hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:16]}"
    built = folder / "webkit_host"
    if not built.exists():
        folder.mkdir(exist_ok=True)
        scratch = folder / f"webkit_host.{os.getpid()}"        # two test runs may build at once
        done = subprocess.run([swiftc, "-O", "-o", str(scratch), str(SOURCE)], capture_output=True, text=True)
        if done.returncode:
            raise RuntimeError("tests/webkit_host.swift did not build:\n" + done.stderr[-2000:])
        os.replace(scratch, built)
    return str(built)


class AppWindow:
    """One page in a WKWebView. `agent` is "app" (the user agent of the app's own window) or "browser"."""

    def __init__(self, agent="app"):
        path = host()
        if path is None:
            raise OSError("no Swift compiler (swiftc): the WebKit host cannot be built")
        self.process = subprocess.Popen([path, agent], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put("")

    def call(self, op, **params):
        """One command and its answer. An answer that never comes ends the host, so no late answer is
        ever read as the answer to a later command."""
        try:
            self.process.stdin.write(json.dumps(dict(params, op=op)) + "\n")
            self.process.stdin.flush()
            line = self.lines.get(timeout=WAIT)
        except (OSError, ValueError, queue.Empty) as exc:
            self.process.kill()
            raise AssertionError(f"the WebKit host did not answer {op}: {type(exc).__name__}") from None
        if not line:
            raise AssertionError(f"the WebKit host ended before it answered {op}")
        answer = json.loads(line)
        if "error" in answer:
            raise AssertionError(f"{op}: {answer['error']}")
        return answer

    def js(self, expression):
        """The value of a JavaScript expression in the page (a promise is awaited)."""
        return json.loads(self.call("js", expression=expression)["value"])

    def problems(self):
        """What the page's console showed in red since the last call."""
        return self.call("problems")["problems"]

    def close(self):
        if not self.process.stdin.closed:
            self.process.stdin.close()              # the host ends when its input does
        try:
            self.process.wait(10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(10)
        self.reader.join(10)
        self.process.stdout.close()
