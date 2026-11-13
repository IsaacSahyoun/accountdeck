"""Proof run: remove a saved terminal account while Claude Code is signed in, and while it is signed out.
Synthetic demo data only.  Usage: remove_probe.py <code copy> signed-in|signed-out"""
import os, pathlib, shutil, sys, tempfile
COPY, MODE = sys.argv[1], sys.argv[2]
os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"; os.environ["ACCOUNTDECK_DEMO"] = "1"
sys.path.insert(0, COPY)
from accountdeck import demo, diagnostics
from accountdeck.core import DeckError

root = pathlib.Path(tempfile.mkdtemp(prefix="accountdeck2-demo-"))
try:
    engine = demo.build_demo(root, "terminal", start=False, inline=True)
    rows = [a for a in engine.snapshot()["accounts"] if a["provider"] == "claude_cli"]
    victim = next(a for a in rows if not a["active"])
    if MODE == "signed-out":
        engine.cli.paths.claude_cli_credentials.unlink()      # what signing out of Claude Code leaves: no login file
    print(f"{MODE}: terminal accounts before: {[a['label'] for a in rows]}; removing {victim['label']} (not the one in use)")
    try:
        result = engine.action("remove", {"id": victim["id"]})
        engine.runner.wait(30)
    except DeckError as exc:
        print("   remove refused at once:", exc.code, "-", exc)
    state = engine.snapshot()
    report = state.get("report") or {}
    print("   report:", report.get("outcome"), "-", (report.get("error") or {}).get("message") or report.get("summary"))
    print("   terminal accounts after:", [a["label"] for a in state["accounts"] if a["provider"] == "claude_cli"])
    engine.stop()
finally:
    diagnostics.close(); shutil.rmtree(root, ignore_errors=True)
