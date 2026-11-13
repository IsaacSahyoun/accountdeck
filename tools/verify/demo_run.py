"""One demo of any copy of the code, for the proof tools: started on fake data, spoken to, stopped, and its
own temp folder removed. A demo forces safe mode and never reads a real login.

By hand, to look at a demo yourself:  demo_run.py <python> <code copy> <scenario> [<demo clock, ISO>]
It prints the demo's address and its own pid, and runs until that pid gets Ctrl-C or `kill <pid>`. Then it
stops the demo through the demo's API and removes the demo's temp folder. Never stop a demo by name:
`pkill -f run.py` also matches the live app."""
import glob
import http.cookiejar
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

PATTERN = os.path.join(tempfile.gettempdir(), "accountdeck2-demo-*")


class Demo:
    """`python -E -s run.py --demo <scenario>` in `copy`. `now` is the demo clock's start (ISO), so two
    versions of the code can show the same moment."""

    def __init__(self, python, copy, scenario, now=None):
        before = set(glob.glob(PATTERN))
        command = [python, "-E", "-s", "run.py", "--demo", scenario, "--port", "0", "--no-browser"]
        self.process = subprocess.Popen(command + (["--now", now] if now else []), cwd=copy,
                                        env=dict(os.environ, ACCOUNTDECK_SAFE_MODE="1"),
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        line = self.process.stdout.readline().strip()
        self.folders = set(glob.glob(PATTERN)) - before         # the data folder this demo made
        if "#token=" not in line:
            self.process.kill()
            raise RuntimeError(f"no demo address: {line[:200]} {self.process.stdout.read()[:1500]}")
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
            return refused.code, refused.read()

    def stop(self):
        """Asks the demo to stop. A demo that is in the middle of a switch answers 409 and is ended here.
        Returns the output the demo left, which holds the traceback when it crashed."""
        try:
            self.call("/api/session", {"token": self.token})
            code, _ = self.call("/api/stop", {})
            self.process.wait(timeout=20 if code == 200 else 1)
        except Exception:
            self.process.kill()
            self.process.wait()
        for folder in self.folders:
            shutil.rmtree(folder, ignore_errors=True)
        return (self.process.stdout.read() or "").strip()


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
