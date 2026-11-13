"""One demo of any copy of the code, for the proof tools: started on fake data, spoken to, stopped, and its
own temp folder removed. A demo forces safe mode and never reads a real login.

Every demo gets a temp folder of its own (`accountdeck2-demo-run-*`) and makes its data folder inside it, so
stopping one removes that folder and nothing another run made. This process keeps a list of the demos it
started (`LIVE`) and ends them when it exits, however it got there.

By hand, to look at a demo yourself:  demo_run.py <python> <code copy> <scenario> [<demo clock, ISO>]
It prints the demo's address and its own pid, and runs until that pid gets Ctrl-C or `kill <pid>`. Then it
stops the demo through the demo's API and removes the demo's temp folder. Never stop a demo by name:
`pkill -f run.py` also matches the live app."""
import atexit
import http.cookiejar
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request


LIVE = set()                                # the demos this process started and has not ended
_exit = {"held": False, "asked": 0}         # an exit asked for while a demo is being started waits for it


def exit_asked(number, _frame=None):
    """Ctrl-C or `kill <pid>` for a tool: it exits, and `end_all` ends its demos on the way out. While a demo
    is being started, the exit waits until that demo is in LIVE. Otherwise the new process would be lost."""
    if _exit["held"]:
        _exit["asked"] = number
    else:
        sys.exit(128 + number)


def end_on_kill():
    """For a tool's main program: `kill <pid>` and Ctrl-C end it through `exit_asked`."""
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, exit_asked)


@atexit.register
def end_all():
    """Ends every demo this process still has. It runs when the process exits."""
    for demo in list(LIVE):
        demo.end()


class Demo:
    """`python -E -s run.py --demo <scenario>` in `copy`. `now` is the demo clock's start (ISO), so two
    versions of the code can show the same moment."""
    START_LIMIT = 60        # seconds a demo gets to print its address

    def __init__(self, python, copy, scenario, now=None):
        self.home, self.process, self.said = None, None, b""
        command = [python, "-E", "-s", "run.py", "--demo", scenario, "--port", "0", "--no-browser"]
        try:
            _exit["held"] = True
            try:
                self.home = tempfile.mkdtemp(prefix="accountdeck2-demo-run-")       # the demo's temp folder, and only its
                self.process = subprocess.Popen(command + (["--now", now] if now else []), cwd=copy,
                                                env=dict(os.environ, ACCOUNTDECK_SAFE_MODE="1", TMPDIR=self.home),
                                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                LIVE.add(self)
            finally:
                _exit["held"] = False
            if _exit["asked"]:
                sys.exit(128 + _exit["asked"])
            self._read(self.START_LIMIT, whole_line=True)       # the first line is the demo's address
            line, _, self.said = self.said.partition(b"\n")
            line = line.decode("utf-8", "replace").strip()
            if "#token=" not in line:
                raise RuntimeError(f"no demo address: {line[:200]} {self.end()[:1500]}")
            self.url = line.split(": ", 1)[1]
            self.origin, self.token = self.url.split("/#token=")
            self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                      urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        except BaseException:       # no address, or the tool was cut off: nobody else gets to stop this demo
            self.end()
            raise

    def _read(self, seconds, whole_line=False):
        """Adds what the demo prints to `self.said`, until its output ends, `seconds` have passed or, with
        `whole_line`, a line is complete. A read with no limit waits for as long as the demo stays silent."""
        deadline, out = time.monotonic() + seconds, self.process.stdout
        while not (whole_line and b"\n" in self.said):
            if not select.select([out], [], [], max(0, deadline - time.monotonic()))[0]:
                return
            chunk = os.read(out.fileno(), 65536)
            if not chunk:
                return
            self.said += chunk

    def call(self, path, body=None):
        """(status, bytes) of a GET, or of a POST the way the dashboard page sends one."""
        headers, data = {"Accept": "application/json"}, None
        if body is not None:
            data = json.dumps(body).encode()
            headers.update({"Content-Type": "application/json", "X-AccountDeck": "1", "Origin": self.origin})
        try:
            with self.opener.open(urllib.request.Request(self.origin + path, data=data, headers=headers), timeout=20) as answer:
                return answer.status, answer.read()
        except urllib.error.HTTPError as refused:
            with refused:
                return refused.code, refused.read()

    def stop(self):
        """Asks the demo to stop. A demo that is in the middle of a switch answers 409 and is ended here.
        Returns the output the demo left, which holds the traceback when it crashed. `self.stopped` is the
        demo's answer to the stop, `self.ended` says how the process went (its exit code, or "killed"), and
        `self.left` is what it left in its temp folder: nothing, when it ended by itself."""
        self.stopped, self.ended = None, "killed"
        try:
            self.call("/api/session", {"token": self.token})
            self.stopped, _ = self.call("/api/stop", {})
            self.ended = self.process.wait(timeout=20 if self.stopped == 200 else 1)
        except Exception:
            pass                    # a demo that did not stop by itself is ended below
        self.left = os.listdir(self.home) if os.path.isdir(self.home) else []
        return self.end()

    def end(self):
        """Ends the demo's process if it still runs, removes the demo's temp folder and returns what the demo
        printed after its address. Safe to call twice, and on a demo that never started."""
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()
            if not self.process.stdout.closed:
                with self.process.stdout:
                    self._read(5)
        if self.home:
            shutil.rmtree(self.home, ignore_errors=True)
        LIVE.discard(self)
        said, self.said = self.said, b""
        return said.decode("utf-8", "replace").strip()


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        sys.exit(__doc__)
    asked = threading.Event()
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, lambda *_: asked.set())
    demo = Demo(sys.argv[1], sys.argv[2], sys.argv[3], now=sys.argv[4] if len(sys.argv) == 5 else None)
    print(f"{demo.url}\nstop this demo with: kill {os.getpid()}", flush=True)
    while not asked.wait(0.5) and demo.process.poll() is None:
        pass
    left = demo.stop()
    print("stopped, and its temp folder is removed" + (f"\n{left[-1200:]}" if "Traceback" in left else ""))
