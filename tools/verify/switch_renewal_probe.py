"""Proof run: what is left signed in when Claude Code renews the login AccountDeck just wrote, between the
swap and AccountDeck's own check. Driven through the login store.
Synthetic demo data only.   Usage: switch_renewal_probe.py <code copy>"""
import json, os, pathlib, shutil, sys, tempfile
COPY = sys.argv[1]
os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"; os.environ["ACCOUNTDECK_DEMO"] = "1"
sys.path.insert(0, COPY)
from accountdeck import demo, diagnostics, switch_cli, switching
from accountdeck.core import DeckError

root = pathlib.Path(tempfile.mkdtemp(prefix="accountdeck2-demo-"))
try:
    engine = demo.build_demo(root, "terminal", start=False, inline=True)
    service = engine.cli
    paths = service.paths
    rows = [a for a in engine.snapshot()["accounts"] if a["provider"] == "claude_cli"]
    names = {service.row(a["id"])["identity"]: a["label"] for a in rows}

    def who():
        config = json.loads(pathlib.Path(paths.claude_user_config).read_text())
        token = service.adapter.resolve_account(service.adapter.store.read()).get("accountUuid")
        engine.sense_active()
        shown = [a["label"] for a in engine.snapshot()["accounts"] if a["provider"] == "claude_cli" and a["active"]]
        return {"account name in .claude.json": names.get((config.get("oauthAccount") or {}).get("accountUuid"), "?"),
                "login token belongs to": names.get(token, "?"), "AccountDeck shows in use": shown}

    print("before:", who())
    usable = [a for a in rows if not a["active"] and a.get("health", "ok") == "ok" and not a.get("excluded")]
    target, third = usable[0], usable[1]
    print("switching to:", target["label"])
    wanted = service.vault.get(target["id"])["credentials"]["claudeAiOauth"]["refreshToken"]
    real_read, fired = service.adapter.store.read, []
    def read(*args, **kwargs):
        if not fired:
            try: live = json.loads(pathlib.Path(paths.claude_cli_credentials).read_text())
            except (OSError, ValueError): live = {}
            if (live.get("claudeAiOauth") or {}).get("refreshToken") == wanted:
                fired.append(True)
                oauth = live["claudeAiOauth"]
                live["claudeAiOauth"] = dict(oauth, refreshToken=oauth["refreshToken"] + "-renewed",
                                             expiresAt=(service.clock() + 8 * 3600) * 1000)
                pathlib.Path(paths.claude_cli_credentials).write_text(json.dumps(live))
                print("  (Claude Code renewed the new login here, right after the swap)")
        return real_read(*args, **kwargs)
    service.adapter.store.read = read
    try:
        switch_cli.perform(service, target["id"])
        print("switch result: success")
    except DeckError as exc:
        print("switch result: FAILED with", repr(exc.code), "-", str(exc))
    print("after:", who())
    engine.recovery = engine._scan_recovery()
    print("records waiting for Recover:", len(engine.recovery))
    if engine.recovery:
        switching.dispatch(engine, "recover", {})
        engine.recovery = engine._scan_recovery()
        print("after one Recover press, still waiting:", len(engine.recovery), "|", who())
    plan = switch_cli.plan(engine, third["id"])
    print("plan to switch to", third["label"], "-> blockers:", [b["message"] for b in plan["blockers"]])
    service.mirror()
    kept = service.vault.get(target["id"])["credentials"]["claudeAiOauth"]["refreshToken"].endswith("-renewed")
    print("the renewed login is the one saved for", target["label"] + ":", kept)
    engine.stop()
finally:
    diagnostics.close(); shutil.rmtree(root, ignore_errors=True)
