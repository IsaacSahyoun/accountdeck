"""Proof run: does the terminal auto-switch act while another client's switch waits for its sessions to
close? The stand-in job is in the real waiting phase (`waiting_idle`), which is what a Desktop, Codex or Muse
"switch when idle" is in while it waits.
Synthetic demo data only.   Usage: wait_probe.py <code copy>"""
import copy, os, pathlib, shutil, sys, tempfile, threading, time
os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"; os.environ["ACCOUNTDECK_DEMO"] = "1"
sys.path.insert(0, sys.argv[1])
from accountdeck import cli_auto, demo, diagnostics
from accountdeck.operations import Operation

class Waiting(Operation):
    def __init__(self):
        super().__init__("switch", provider="codex", label="stand-in", consent="idle", steps=(("wait", "Waiting for Codex sessions"),))
        self.release = threading.Event()
    def run(self):
        with self.step("wait", phase="waiting_idle", message="Waiting for Codex sessions to close…", cancellable=True):
            while not self.release.wait(0.05):
                self.checkpoint()

root = pathlib.Path(tempfile.mkdtemp(prefix="accountdeck2-demo-"))
try:
    engine = demo.build_demo(root, "terminal", start=False, inline=True)
    engine.sense_active()
    now = engine.clock()
    def in_use():
        return next(r for r in engine.store.accounts() if r["provider"] == "claude_cli" and r["identity"] == engine.cli.adapter.current()["identity"])
    def reading(key, session, weekly=None):
        usage = copy.deepcopy(engine.store.account(key)["last_good_usage"]); usage["checked"] = now
        for w in usage["windows"]:
            if w.get("kind") == "session": w.update(used=float(session), reset=now + 3 * 3600)
            if w.get("kind") == "weekly" and weekly is not None: w.update(used=float(weekly), reset=now + 4 * 86400)
        engine.store.observe(key, usage, at=now)
    active = in_use()
    reading(active["id"], 97)
    for r in engine.store.accounts():
        if r["provider"] == "claude_cli" and r["id"] != active["id"] and r["last_good_usage"]:
            reading(r["id"], 20, 20)
    d = cli_auto.decision(engine)
    print(f"terminal account in use: {active['label']} at 97% of its 5-hour limit")
    print("what the policy wants:", d["action"], "|", d["reason"] or d["hold"])
    waiting = Waiting()
    engine.submit(waiting)
    deadline = time.monotonic() + 5
    while waiting.phase != "waiting_idle" and time.monotonic() < deadline: time.sleep(0.02)
    print("another client's switch is waiting for sessions:", waiting.phase == "waiting_idle")
    for n in (1, 2, 3):
        cli_auto.tick(engine); time.sleep(0.3)
    print("after 3 auto-switch checks during the wait -> terminal account in use:", in_use()["label"])
    print("the other client's wait was left alone:", waiting.phase == "waiting_idle")
    waiting.release.set(); waiting.wait(10); engine.runner.wait(30)
    engine.stop()
finally:
    diagnostics.close(); shutil.rmtree(root, ignore_errors=True)
