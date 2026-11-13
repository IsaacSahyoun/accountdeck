"""One demo of any copy of the code, for the proof tools: started on fake data, spoken to, stopped, and its
own temp folder removed. A demo forces safe mode and never reads a real login.

Every demo gets a temp folder of its own (`accountdeck2-demo-run-*`) and makes its data folder inside it, so
stopping one removes that folder and nothing another run made.

By hand, to look at a demo yourself:  demo_run.py <python> <code copy> <scenario> [<demo clock, ISO>]
It prints the demo's address and its own pid, and runs until that pid gets Ctrl-C or `kill <pid>`. Then it
stops the demo through the demo's API and removes the demo's temp folder. Never stop a demo by name:
`pkill -f run.py` also matches the live app."""
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
import urllib.error
import urllib.request


def end_on_kill():
    """For a tool's main program: `kill <pid>` ends it the way Ctrl-C does, so its `finally` still runs and
    stops the demo it started. Without this a killed tool leaves its demo running."""
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))


class Demo:
    """`python -E -s run.py --demo <scenario>` in `copy`. `now` is the demo clock's start (ISO), so two
    versions of the code can show the same moment."""
    START_LIMIT = 60        # seconds a demo gets to print its address

    def __init__(self, python, copy, scenario, now=None):
        self.home = tempfile.mkdtemp(prefix="accountdeck2-demo-run-")       # the demo's temp folder, and only its
        command = [python, "-E", "-s", "run.py", "--demo", scenario, "--port", "0", "--no-browser"]
        self.process = subprocess.Popen(command + (["--now", now] if now else []), cwd=copy,
                                        env=dict(os.environ, ACCOUNTDECK_SAFE_MODE="1", TMPDIR=self.home),
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        line, said = "", ""
        try:    # the first line is the demo's address; a demo that says nothing is ended here, not waited on forever
            if select.select([self.process.stdout], [], [], self.START_LIMIT)[0]:
                line = self.process.stdout.readline().strip()
        finally:
            if "#token=" not in line:       # no address, or the tool was cut off while its demo was starting
                self.process.kill()
                said = self.process.stdout.read()[:1500]
                self.process.wait()
                shutil.rmtree(self.home, ignore_errors=True)
        if "#token=" not in line:
            raise RuntimeError(f"no demo address: {line[:200]} {said}")
        self.url = line.split(": ", 1)[1]
        self.origin, self.token = self.url.split("/#token=")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                  urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

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
        self.stopped = None
        try:
            self.call("/api/session", {"token": self.token})
            self.stopped, _ = self.call("/api/stop", {})
            self.ended = self.process.wait(timeout=20 if self.stopped == 200 else 1)
        except Exception:
            self.process.kill()
            self.process.wait()
            self.ended = "killed"
        self.left = os.listdir(self.home) if os.path.isdir(self.home) else []
        shutil.rmtree(self.home, ignore_errors=True)
        with self.process.stdout as said:
            return (said.read() or "").strip()


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
