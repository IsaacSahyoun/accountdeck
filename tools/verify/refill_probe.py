"""Proof run: does a refill get recorded for a Claude Code (terminal) account? Synthetic demo data only.
Usage: refill_probe.py <code copy>"""
import copy, os, pathlib, shutil, sys, tempfile
os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"; os.environ["ACCOUNTDECK_DEMO"] = "1"
sys.path.insert(0, sys.argv[1])
from accountdeck import demo, diagnostics, intel
from accountdeck.providers import errors

root = pathlib.Path(tempfile.mkdtemp(prefix="accountdeck2-demo-"))
try:
    engine = demo.build_demo(root, "terminal", start=False, inline=True)
    now = engine.clock()
    row = next(r for r in engine.store.accounts() if r["provider"] == "claude_cli" and r["identity"] == engine.active_identity["claude_cli"])
    key = row["id"]
    before = copy.deepcopy(row["last_good_usage"])
    week = next(w for w in before["windows"] if w.get("kind") == "weekly")
    week.update(used=92.0, reset=now - 120)                       # 92% used, and that week ended two minutes ago
    after = copy.deepcopy(before)
    next(w for w in after["windows"] if w.get("kind") == "weekly").update(used=0.0, reset=now + 7 * 86400)   # a new, empty week
    before["checked"], after["checked"] = now - 600, now          # read ten minutes ago, and read now
    engine.store.observe(key, before, at=now - 600)
    print("the shared refill rule sees these two readings as:", [(r["window"], r["previous_used"]) for r in intel.detect_refill(before, after)])
    had = len(engine.store.refills(0))
    engine.cli.adapter.usage = lambda credentials: copy.deepcopy(after)
    outcome = engine.cli.read(key)
    stored = next(w for w in engine.store.account(key)["last_good_usage"]["windows"] if w.get("kind") == "weekly")
    print(f"terminal read of {row['label']}: outcome={outcome}, stored weekly used={stored['used']}")
    print("refills recorded by the terminal read:", len(engine.store.refills(0)) - had)
    state = engine.snapshot()
    account = next(a for a in state["accounts"] if a["id"] == key)
    print("weekly limit in the dashboard state:", {k: next(w for w in account["windows"] if w["kind"] == "weekly").get(k) for k in ("state", "refilled")})
    engine.stop()
finally:
    diagnostics.close(); shutil.rmtree(root, ignore_errors=True)
print("--- Retry-After ---")
print("retry_after_seconds('99999999') =", errors.retry_after_seconds("99999999"), "seconds =", round(errors.retry_after_seconds("99999999") / 86400 / 365, 1), "years")
