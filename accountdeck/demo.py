"""Demo mode: a whole AccountDeck 2 on synthetic data.

`build_demo(root, scenario)` builds one of `fixtures.SCENARIOS` under `root` (a temporary folder) and
returns the started `Engine`; `run.py --demo <scenario>` serves it. Everything the demo touches is
synthetic and inside `root`:

- the machine is `Paths.synthetic(root)` with a Claude Desktop profile (macOS format, under a synthetic
  "Claude Safe Storage" password), a Codex home and a Muse config folder that `providers.fake` writes, and
  `procs.FakeProcessController` (an in-memory Mac process table);
- the providers are `FakeClaude` / `FakeCodex` / `FakeMuse` / `DemoCLI` following scripts, so usage moves as the
  virtual clock runs: windows pass their reset, refill and start over, as they would for real accounts;
- the clock is `DemoClock`: real time from a start (`--now`) on, so a demo shows the moment it was
  meant to; tests move it with `advance()`.

The accounts, windows, errors, banners and pending operations of each scenario come from
`fixtures.example_state(scenario)`, the same fixture the dashboard tests render, so the demo and the
fixtures cannot drift apart. The saved accounts are seeded through the real `AccountService` and
`CLIAccounts` (capture and save) and the store, then read by the real engine.
"""
from __future__ import annotations

import copy
import datetime as dt
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

from . import diagnostics, fixtures
from .config import Paths, SafetyViolation
from .core import DeckError, Vault, atomic_write, json_bytes
from .diagnostics import Manifest
from .engine import Engine
from .operations import Operation, OperationCancelled
from .procs import FakeProcessController, filetime, is_test_owned_path
from .providers.claude_cli import ClaudeCLI, object_file, profile as cli_profile
from .providers.errors import ProviderError
from .switch_cli import CLISwitch
from .providers.fake import (FakeClaude, FakeCodex, FakeMuse, FakeStatusSource, Timeline, build_fake_home,
                             test_cipher)
from .usage import claude_usage, codex_usage, muse_usage

__all__ = ["DemoClock", "DemoProcesses", "build_demo", "SCENARIOS"]

SCENARIOS = tuple(fixtures.SCENARIOS)
NAMESPACE = uuid.UUID("6b1d6f0e-3f7a-4d0a-9d55-7a3f2f1c0d11")
REFILL_LAG = 60.0               # a provider reports a passed reset this long afterwards
LATE_REFILL = 150.0             # ... and a reset that had passed when the demo started, this long after the start
FROZEN = 6 * 3600.0             # accounts a scenario shows as stale or broken are not read for this long
LOADING_S = 25.0                # the `loading` scenario's first readings arrive after this long
OUTAGE_S = 600.0                # the `outage` scenario's incident ends after this long
HISTORY_OFFSETS = (1800, 3600, 2 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 3 * 86400)
DEFAULT_WINDOWS = {"claude": (("seven_day", 20.0, 400000.0),),
                   "claude_cli": (("seven_day", 20.0, 400000.0),),
                   "codex": (("codex:primary", 10.0, 10000.0), ("codex:secondary", 15.0, 300000.0)),
                   "muse": (("muse:window", 8.0, 10000.0), ("muse:weekly", 20.0, 300000.0))}


class DemoClock:
    """Virtual time: real time from `start` on (at `speed` times real speed), plus whatever `advance` added."""

    def __init__(self, start=None, speed=1.0):
        self.start = time.time() if start is None else float(start)
        self.speed = float(speed)
        self._origin, self._offset, self._lock = time.monotonic(), 0.0, threading.Lock()

    def __call__(self):
        with self._lock:
            return self.start + self._offset + (time.monotonic() - self._origin) * self.speed

    def advance(self, seconds):
        with self._lock:
            self._offset += float(seconds)


class _Gate:
    """Holds the fake providers' answers back until the virtual clock reaches `opens` (or the engine stops)."""

    def __init__(self, clock, opens):
        self.clock, self.opens, self.engine = clock, opens, None

    def wait(self):
        for _ in range(1200):        # never longer than a minute of real time
            if self.clock() >= self.opens or (self.engine is not None and self.engine.stopping.is_set()):
                return
            time.sleep(0.05)


class DemoClaude(FakeClaude):
    gate = None

    def _payload(self, profile):
        if self.gate is not None:
            self.gate.wait()
        return super()._payload(profile)


class DemoCodex(FakeCodex):
    gate = None

    def _probe(self, work, identity, refresh):
        if self.gate is not None:
            self.gate.wait()
        return super()._probe(work, identity, refresh)


class DemoMuse(FakeMuse):
    gate = None

    def _run_usage(self, config_home, *, file_mode, isolated):
        if self.gate is not None:
            self.gate.wait()
        return super()._run_usage(config_home, file_mode=file_mode, isolated=isolated)


class DemoCLI(ClaudeCLI):
    def __init__(self, paths, scripts, clock, ids):
        super().__init__(paths, clock=clock)
        self.timelines = {key: Timeline(script, clock) for key, script in scripts.items()}
        self.ids = ids

    def resolve_account(self, credentials, *, timeout=None):
        key = credentials["claudeAiOauth"]["accessToken"].removeprefix("demo-access-")
        return {"accountUuid": self.ids.get(key), "emailAddress": "", "organizationUuid": ""}

    def usage(self, credentials):
        key = credentials["claudeAiOauth"]["accessToken"].removeprefix("demo-access-")
        data = self.timelines[key].current()
        return claude_usage(data, now=self.clock())

    def refresh(self, credentials, *, timeout=None):
        result = copy.deepcopy(credentials)
        result["claudeAiOauth"]["expiresAt"] = (self.clock() + 8 * 3600) * 1000
        result["claudeAiOauth"]["refreshToken"] += "-rotated"
        return result


class DemoProcesses(FakeProcessController):
    """The demo's machine. Opening a folder or a terminal is a no-op that stays inside the demo folder."""

    def add_session(self, name, status, *, since, cwd_name, parent, entrypoint="claude-desktop", version="2.1.284"):
        """A Code-tab session of Claude Desktop whose registry row carries the demo's own timestamps."""
        path = Path(self.paths.claude_profile) / "claude-code" / version / "claude"
        row = self.add_row("claude", path=path, parent=parent)
        self._write_session(row.pid, {
            "pid": row.pid, "sessionId": str(uuid.uuid4()), "cwd": str(Path(self.paths.home) / "projects" / cwd_name),
            "startedAt": int(since * 1000), "procStart": str(row.ctime), "version": version, "kind": "interactive",
            "entrypoint": entrypoint, "name": name, "status": status, "updatedAt": int(since * 1000),
            "statusUpdatedAt": int(since * 1000)})
        return row

    def age(self, pid, started):
        """Backdates a process to `started` (Unix seconds); returns its row."""
        with self._lock:
            row = self._table[pid]
            self._table[pid] = replace(row, ctime=filetime(started))
            return self._table[pid]

    def open_terminal(self, cwd, env):
        self.calls.append(("open_terminal", str(cwd)))
        if not is_test_owned_path(cwd):
            raise SafetyViolation("Safe mode: the demo only opens its own folders.")


# ---- usage scripts ----

def _iso(when):
    return None if when is None else dt.datetime.fromtimestamp(when, dt.timezone.utc).isoformat()


def _minutes(key):
    if key in ("codex:primary", "muse:window"):
        return 300
    return 10080 if key.startswith("seven_day") or key in ("codex:secondary", "muse:weekly") else None


def _windows(account, provider, base):
    """[{"key", "used", "reset"}] of an account's last reading (a default one when it has none)."""
    found = [dict(w) for w in account.get("raw_windows") or ()]
    if not found:
        found = [{"key": key, "used": used, "reset": base + reset} for key, used, reset in DEFAULT_WINDOWS[provider]]
    return found


def _claude_payload(windows, credits=()):
    payload = {w["key"]: {"utilization": w["used"], "resets_at": _iso(w["reset"])} for w in windows}
    spend = next((c for c in credits if c.get("label") == "Usage credits"), None)
    if spend is not None:
        payload["spend"] = {"enabled": bool(spend.get("enabled"))}
    return payload


def _codex_payload(windows, account=None):
    bucket = {"limitId": "codex"}
    by_key = {w["key"]: w for w in windows}
    for key, slot in (("codex:primary", "primary"), ("codex:secondary", "secondary")):
        if key in by_key:
            w = by_key[key]
            bucket[slot] = {"usedPercent": w["used"], "windowDurationMins": _minutes(key),
                            "resetsAt": None if w["reset"] is None else int(w["reset"])}
    payload = {"rateLimits": bucket}
    if account is None:
        return payload
    blocked = {w["id"]: w["blocked"] for w in account["windows"] + account["other_windows"] if w.get("blocked")}
    reached = next((why for why in blocked.values() if why != "not_allowed"), None)
    if reached:
        bucket["rateLimitReachedType"] = reached
    if "not_allowed" in blocked.values():
        payload["ordinaryUsageAllowed"] = False
    credit = next((c for c in account["credits"] if c.get("label") == "Codex credits"), None)
    if credit is not None:
        bucket["credits"] = {"balance": credit.get("balance"), "unlimited": credit.get("unlimited"),
                             "hasCredits": credit.get("hasCredits")}
    resets = account.get("reset_credits") or []
    if resets:
        payload["rateLimitResetCredits"] = {"availableCount": sum(r["count"] for r in resets), "credits": [
            {"status": "available", "expiresAt": r["expires"], "title": r["title"]} for r in resets for _ in range(r["count"])]}
    return payload


def _muse_payload(windows, account=None):
    """What `muse usage --json` prints for these windows."""
    plan = (account or {}).get("plan") or "Max"
    allowance = {"tier": f"Muse Code {plan} Usage"}
    by_key = {w["key"]: w for w in windows}
    for key, name in (("muse:window", "window"), ("muse:weekly", "weekly")):
        if key in by_key:
            w = by_key[key]
            # Like real `muse usage --json` (1.4.13): no window length.
            allowance[name] = {"usedPercent": w["used"], "resetsAtMs": None if w["reset"] is None else int(w["reset"] * 1000)}
    return {"allowance": allowance, "source": "mint"}


def _payload(provider, windows, account=None):
    if provider == "muse":
        return _muse_payload(windows, account)
    return _codex_payload(windows, account) if provider == "codex" else _claude_payload(
        windows, () if account is None else account["credits"])


def _usage(provider, payload, now):
    parse = {"codex": codex_usage, "muse": muse_usage}.get(provider, claude_usage)
    return parse(payload, now=now)


def _rolling(provider, account, base, windows):
    """A script [(offset, payload)]: the reading as it is now, and each window passing its reset (a
    minute late) and starting over, so a demo left running keeps moving."""
    events = []
    for w in windows:
        minutes = _minutes(w["key"])
        if w["reset"] is None or minutes is None:
            continue
        late = w["reset"] <= base
        offset = LATE_REFILL if late else w["reset"] - base + REFILL_LAG
        if provider in ("codex", "muse"):
            after = {"used": 0.0, "reset": max(w["reset"], base + offset) + minutes * 60}
        else:
            after = {"used": min(w["used"] or 0.0, 3.0), "reset": w["reset"] + minutes * 60}
        events.append((offset, w["key"], after))
    script = [(0.0, _payload(provider, windows, account))]
    for boundary in sorted({offset for offset, _key, _after in events}):
        current = []
        for w in windows:
            change = next((after for offset, key, after in events if key == w["key"] and offset <= boundary), None)
            current.append({**w, **change} if change else dict(w))
        script.append((boundary, _payload(provider, current, account)))
    return script


def _script(provider, account, base, windows):
    """The provider script of one account, by what its scenario says is wrong with it."""
    rolling = _rolling(provider, account, base, windows)
    code = (account.get("error") or {}).get("code")
    if code == "blocked":
        return [(0.0, ProviderError("blocked", http=403))]
    if code not in ("rate_limited", "outage"):
        return rolling
    recover = OUTAGE_S if code == "outage" else max(30.0, (account.get("paused_until") or base) - base + 30.0)
    current = [payload for offset, payload in rolling if offset <= recover][-1]
    return [(0.0, ProviderError(code, http=429 if code == "rate_limited" else 503)), (recover, current)] + [
        entry for entry in rolling if entry[0] > recover]


class _UnheldLock:
    """The owner lock as seen by a second AccountDeck 2: someone else holds it."""
    held = False


# ---- the pending operation and the report ----

class _Showcase(Operation):
    """A switch held in one phase for a screenshot or a walk-through. It touches no file, no process and
    no sign-in; it ends when the user cancels it or the demo stops."""

    def __init__(self, engine, frozen, *, account):
        super().__init__(frozen["kind"], provider=frozen["provider"], account=account, label=frozen["label"],
                         consent=frozen["consent"], steps=[(s["id"], s["label"]) for s in frozen["steps"]],
                         eta_seconds=frozen["eta_seconds"])
        self.engine, self.frozen = engine, frozen

    def run(self):
        frozen = self.frozen
        for step in frozen["steps"]:
            if step["status"] == "pending":
                break
            if step["status"] == "active":
                with self.step(step["id"], phase=frozen["phase"], message=frozen["message"],
                               cancellable=frozen["cancellable"], eta_seconds=frozen["eta_seconds"]):
                    if step["detail"]:
                        self.note(step["detail"])
                    if frozen["phase"] == "waiting_idle":
                        self.set_waiting(self._working())
                    while not self.engine.stopping.is_set():
                        self.sleep(0.25)
                raise OperationCancelled("The demo ended.")
            with self.step(step["id"]):
                if step["detail"] or step["status"] != "done":
                    self.note(step["detail"], status=None if step["status"] == "done" else step["status"])
        if frozen["phase"] == "needs_reconfirm":
            raise DeckError(frozen["message"], "needs_reconfirm", plan=self._plan())

    def _sessions(self):
        return self.engine.snapshot()["providers"]["claude"]["running"]["sessions"]

    def _working(self):
        return [{"pid": s["pid"], "name": s["name"], "status": s["status"]} for s in self._sessions()
                if s["status"] != "idle"]

    def _plan(self):
        """The fresh plan of a switch to this account with one more busy session (the fixture `busy` plan,
        fitted to this demo's accounts and sessions)."""
        state, now = self.engine.snapshot(), self.engine.clock()
        plan = fixtures.example_plan("busy", now)
        accounts = {a["id"]: a for a in state["accounts"]}
        target = accounts.get(self.account)
        current = accounts.get(state["providers"]["claude"]["active"])
        if target is not None:
            plan["target"] = {"id": target["id"], "label": target["label"], "email": target["email"], "plan": target["plan"],
                              "windows": target["windows"], "checked": target["checked"], "age_tier": target["age_tier"],
                              "dormant_days": target["credential"]["dormant_days"]}
        if current is not None:
            plan["current"] = {"id": current["id"], "label": current["label"], "windows": current["windows"]}
        sessions = [{k: s[k] for k in ("pid", "name", "status", "since")} for s in self._sessions()]
        sessions.append({"pid": 5120, "name": "New session", "status": "busy", "since": now - 15})
        busy = [s for s in sessions if s["status"] != "idle"]
        plan["running"].update(sessions=sessions, busy=len(busy))
        plan["process_set"] = [[s["pid"], 133812345678901234 + s["pid"]] for s in sessions]
        plan["signature"] = "sha256:" + hashlib.sha256(json.dumps(sorted(plan["process_set"])).encode()).hexdigest()
        names = ", ".join(f'"{s["name"]}"' for s in busy)
        plan["warnings"][0] = {"code": "busy", "message": f"{len(busy)} Code sessions are working: {names}."}
        return plan


class _Finished(Operation):
    """A switch that already ended, for the `report` and `outage` scenarios: the engine records it like any
    other, so the report card, the event log and "Explain last switch" all have something to show."""
    STEPS = (("prepare", "planning", "Checking the saved sign-in"), ("stop", "stopping", "Closing Claude"),
             ("capture", "saving", "Saving the current sign-in"), ("restore", "restoring", "Restoring the account"),
             ("launch", "launching", "Opening Claude"), ("verify", "verifying", "Checking the sign-in"))

    def __init__(self, engine, report, *, account):
        super().__init__("switch", provider="claude", account=account, label=report["label"], consent="now",
                         steps=[(sid, text) for sid, _phase, text in self.STEPS])
        self.engine, self.fixture = engine, report

    def run(self):
        for sid, phase, _text in self.STEPS:
            with self.step(sid, phase=phase, message=_text + "…", cancellable=False):
                pass
        fixture = self.fixture
        directory = Path(self.engine.paths.data_dir) / "logs" / "manifests"
        manifest = Manifest.start(self.id, "switch", directory=directory)
        manifest.set("stop.tier", fixture["stop_tier"]).set("stop.ms", fixture["stop_ms"])
        manifest.set("stop.helpers_self_exited", fixture["helpers_self_exited"]).set("verification.result", "unconfirmed")
        manifest.set("verification.reason", fixture["verify_reason"])
        for item in fixture["carried"]:
            manifest.append("carried", {"label": item["label"], "detail": item["detail"]})
        manifest.finish()
        self.report.update({key: fixture[key] for key in ("verified", "verified_at", "verify_reason", "stop_tier", "stop_ms",
                                                          "helpers_self_exited", "stragglers", "summary", "carried",
                                                          "account_bound")}, manifest=self.id)


# ---- the builder ----

class _Builder:
    def __init__(self, root, scenario, now, *, start, inline):
        self.scenario, self.start, self.inline = scenario, start, inline
        self.clock = DemoClock(now)
        self.base = self.clock()
        self.paths = Paths.synthetic(Path(root))
        self.spec = fixtures.example_state(scenario, self.base)
        self.accounts = self.spec["accounts"]
        # spec id -> the account's identity (a Muse account's is its email)
        self.ids = {a["id"]: a["email"].casefold() if a["provider"] == "muse" else self._uuid(a["id"]) for a in self.accounts}
        self.keys = {}                                                         # spec id -> the saved account's id

    # -- identities --
    def _uuid(self, name):
        return str(uuid.uuid5(NAMESPACE, f"{self.scenario}/{name}"))

    def _org(self, identity):
        return str(uuid.uuid5(NAMESPACE, identity + "/org"))

    def _active(self, provider):
        return next((a for a in self.accounts if a["provider"] == provider and a["active"]), None)

    def _home(self, claude=None, codex=None, muse=None):
        claude = claude or self._uuid("spare-claude")
        codex = codex or self._uuid("spare-codex")
        return build_fake_home(self.paths, claude_identity=claude, claude_org=self._org(claude), codex_identity=codex,
                               now=self.base, codex_exp_offset=9 * 86400, muse_email=muse,
                               muse_storage=None)

    # -- the world --
    def build(self):
        diagnostics.setup(self.paths)
        signed_in = {p: (self.ids[a["id"]] if (a := self._active(p)) else None) for p in fixtures.PROVIDERS}
        if any(signed_in.values()):
            self._home(signed_in["claude"], signed_in["codex"], signed_in["muse"])
        if self.scenario == "first-run":
            self._sign_out(signed_in)          # nothing is seeded, so sign out what the scenario has no account for
        self.gate = None if self.inline or self.scenario != "loading" else _Gate(self.clock, self.base + LOADING_S)
        claude, codex, cli, muse = self._providers()
        self.procs = self._machine()
        if self._active("claude_cli"):
            self._cli_live(self._active("claude_cli"))
        # `read-only`: another AccountDeck 2 holds the owner lock, so this one only shows usage.
        owner_lock = _UnheldLock() if self.scenario == "read-only" else None
        engine = Engine(self.paths, owner_lock=owner_lock,
                        providers={"claude": claude, "codex": codex, "claude_cli": cli, "muse": muse}, procs=self.procs,
                        vault=Vault(self.paths.data_dir / "vault", protect=test_cipher),
                        status_sources=self._status_sources(), clock=self.clock, start=False, inline=self.inline,
                        mode="demo")
        if self.gate is not None:
            self.gate.engine = engine
        try:
            if self.scenario != "first-run":
                self._seed(engine, signed_in)
            else:            # the engine saved the signed-in terminal login by itself (CLIAccounts.mirror)
                a = self._active("claude_cli")
                key = engine.store.find("claude_cli", self.ids[a["id"]])["id"]
                self.keys[a["id"]] = key
                self._state(engine, key, a)
            self._settings(engine)
            self._extras(engine)
            engine.sense_active()
            engine.bump()
            if self.start:
                engine.start()
        except BaseException:
            engine.stop()
            raise
        return engine

    # -- providers and scripts --
    def _providers(self):
        claude_scripts, codex_scripts, identities, cli_scripts, muse_scripts = {}, {}, {}, {}, {}
        for a in self.accounts:
            identity, provider = self.ids[a["id"]], a["provider"]
            windows = _windows(a, provider, self.base)
            script = _script(provider, a, self.base, windows)
            if provider == "claude_cli":
                if a["id"] == "cli_alpha" and self.scenario == "typical":
                    more = [dict(w) for w in windows]
                    more[0]["used"] = 95
                    script.append((600, _claude_payload(more)))
                    script.sort(key=lambda entry: entry[0])
                cli_scripts[a["id"]] = script
                continue
            {"claude": claude_scripts, "codex": codex_scripts, "muse": muse_scripts}[provider][identity] = script
            if provider == "claude":
                identities[identity] = {"email": a["email"], "plan": a["plan"] or "max", "org": self._org(identity)}
        claude = DemoClaude(self.paths, scripts=claude_scripts, identities=identities, now=self.clock)
        codex = DemoCodex(self.paths, scripts=codex_scripts, now=self.clock)
        muse = DemoMuse(self.paths, scripts=muse_scripts, now=self.clock)
        claude.gate = codex.gate = muse.gate = self.gate
        return claude, codex, DemoCLI(self.paths, cli_scripts, self.clock, self.ids), muse

    def _cli_profile(self, account):
        key = account["id"]
        credentials = {"claudeAiOauth": {"accessToken": "demo-access-" + key,
            "refreshToken": "demo-refresh-" + key, "expiresAt": (self.base + 8 * 3600) * 1000,
            "subscriptionType": "max", "scopes": ["user:profile"]}, "trustedDeviceToken": "demo-device-" + key}
        return cli_profile(credentials, {"accountUuid": self.ids[key], "emailAddress": account["email"],
                           "organizationUuid": self._org(self.ids[key])}, self.base)

    def _cli_live(self, account):
        saved = self._cli_profile(account)
        atomic_write(self.paths.claude_cli_credentials, json_bytes({**saved["credentials"],
                     "mcpOAuth": {"demo": "demo-mcp-shared"}, "pluginSecrets": {"demo": "demo-plugin-shared"}}))
        current = object_file(self.paths.claude_user_config, missing=True)
        current["oauthAccount"] = saved["oauthAccount"]
        atomic_write(self.paths.claude_user_config, json_bytes(current))

    def _status_sources(self):
        if self.scenario != "outage":
            return {}
        status = self.spec["providers"]["claude"]["status"]
        incident = {"level": "outage", "incident": status["incident"], "since": status["since"]}
        return {"claude": FakeStatusSource([(0, incident), (OUTAGE_S, "ok")], now=self.clock, url=status["url"])}

    # -- the machine --
    def _machine(self):
        # The quit request is SIGTERM, which Claude (Electron) answers with its own quit.
        procs = DemoProcesses(self.paths)
        running = self.spec["providers"]["claude"]["running"]
        if running["desktop"]:
            main = procs.add_claude_desktop(helpers=max(0, running["desktop_pids"] - 1))
            for s in running["sessions"]:
                procs.add_session(s["name"], s["status"], since=s["since"], cwd_name=s["cwd_short"] or "project",
                                  parent=main.pid)
        for _ in range(running["foreign_claude"]):
            procs.add_foreign_claude()
        codex = self.spec["providers"]["codex"]["running"]
        if codex["daemon"]:
            procs.add_codex("codex_daemon")
        kinds = {"tui": "codex_tui", "app_server_child": "codex_app_server_child", "desktop": "codex_desktop"}
        for c in codex["consumers"]:
            name = c["parent"] or "Terminal"
            parent = procs.add_row(name, path=f"/System/Applications/Utilities/{name}.app/Contents/MacOS/{name}")
            row = procs.add_codex(kinds[c["kind"]], parent=parent.pid)
            procs.age(row.pid, c["started"])
        kinds = {"tui": "muse_session", "app_server_child": "muse_host"}
        for c in self.spec["providers"]["muse"]["running"]["consumers"]:
            name = c["parent"] or "Terminal"
            parent = procs.add_row(name, path=f"/System/Applications/Utilities/{name}.app/Contents/MacOS/{name}")
            row = procs.add_muse(kinds[c["kind"]], parent=parent.pid)
            procs.age(row.pid, c["started"])
        return procs

    # -- seeding the saved accounts --
    def _seed(self, engine, signed_in):
        saved = [a for a in self.accounts if a.get("saved", True)]
        for a in sorted(saved, key=lambda a: a["active"]):          # the signed-in accounts last: their files stay in place
            provider = a["provider"]
            if provider == "claude_cli":
                profile = self._cli_profile(a)
                profile.update(health=a.get("health", "ok"), excluded=a.get("excluded", False),
                               verified=profile["fingerprint"])     # synthetic: the demo knows each owner
                self.keys[a["id"]] = engine.cli.save(profile, source="demo")
                continue
            if a["active"]:
                self._home(signed_in["claude"], signed_in["codex"], signed_in["muse"])
            else:
                self._home(self.ids[a["id"]] if provider == "claude" else None,
                           self.ids[a["id"]] if provider == "codex" else None,
                           a["email"] if provider == "muse" else None)
            self.keys[a["id"]] = engine.accounts.capture(provider, label=a["label"], claude_running=False)
        if any(signed_in.values()):
            self._home(signed_in["claude"], signed_in["codex"], signed_in["muse"])
        self._sign_out(signed_in)
        if self._active("claude_cli"):
            self._cli_live(self._active("claude_cli"))
        store = engine.store
        store.reorder([self.keys[a["id"]] for a in saved])
        for a in saved:
            self._state(engine, self.keys[a["id"]], a)
        engine.sense_active()

    def _sign_out(self, signed_in):
        """A provider with no signed-in account in the scenario has nobody signed in on the machine either."""
        if any(signed_in.values()):
            if signed_in["claude"] is None:
                (Path(self.paths.claude_profile) / "config.json").unlink(missing_ok=True)
            if signed_in["codex"] is None:
                (Path(self.paths.codex_home) / "auth.json").unlink(missing_ok=True)
            if signed_in["muse"] is None:
                (Path(self.paths.muse_config) / "auth.json").unlink(missing_ok=True)

    def _state(self, engine, key, a):
        """Gives a saved account the reading, history, credential and error its scenario shows."""
        store, provider, base = engine.store, a["provider"], self.base
        if provider == "claude_cli" and self.scenario != "first-run":
            store.update(key, label=a["label"])
        windows = _windows(a, provider, base)
        at = a["last_good_at"]
        flags = dict(store.account(key)["flags"])
        if at is not None:
            self._history(store, key, provider, a, windows, at)
            usage = _usage(provider, _payload(provider, windows, a), at)
            store.observe(key, usage, at=at)
        credential = a["credential"]
        if a["checked"] is not None:
            flags["checked"] = a["checked"]
        if credential["dormant_days"] is not None:
            flags["last_active_at"] = base - credential["dormant_days"] * 86400
        if credential["session_expires_in_days"] is not None:
            flags["session_expires_at"] = base + credential["session_expires_in_days"] * 86400
        if credential["revoked_hint"]:
            flags["revoked_hint"] = True
        fields = {"email": a["email"], "plan": a["plan"], "credential_state": credential["state"],
                  "last_outcome": credential["last_outcome"], "flags": flags,
                  "expires_at": None if credential["expires_in"] is None else base + credential["expires_in"],
                  "paused_until": a["paused_until"]}
        if a["imported_from"]:
            fields.update(imported_from=a["imported_from"], owner=a["owner"])
        if a["error"]:
            flags["error_since"] = a["error"]["since"]
            fields.update(error=a["error"]["message"], error_code=a["error"]["code"])
        store.update(key, **fields)
        for window in a["windows"]:
            if window["refilled"]:
                refill = window["refilled"]
                store.record_refill(key, window["id"], reset_at=refill["at"], confirmed_at=refill["confirmed"],
                                    previous_used=refill["from"])
        if a["last_good_at"] is not None:        # an account with no reading yet is read at once
            engine.scheduler.defer(key, base + self._quiet(a))

    def _quiet(self, a):
        """How long the engine leaves an account's seeded state alone: a stale or broken account keeps
        showing what the scenario is about; the others follow their normal cadence."""
        if a["age_tier"] != "fresh" or (a["error"] and a["error"]["code"] not in ("rate_limited", "outage", "blocked")):
            return FROZEN
        return max(30.0, (a["next_check"] or self.base) - self.base)

    def _history(self, store, key, provider, a, windows, at):
        """Earlier readings of the same epoch, growing linearly to the shown one, for pace and trend."""
        for when in sorted(at - offset for offset in HISTORY_OFFSETS):
            chosen = []
            for w in windows:
                minutes = _minutes(w["key"])
                if minutes is None or w["reset"] is None or not w["used"]:
                    continue
                epoch = w["reset"] - minutes * 60
                if epoch <= when < at:
                    chosen.append({**w, "used": round(w["used"] * (when - epoch) / (at - epoch), 1)})
            if chosen:
                usage = _usage(provider, _payload(provider, chosen), when)
                store.observe(key, usage, at=when)

    # -- settings, alerts, notices, events, the pending operation and the report --
    def _settings(self, engine):
        patch = self.spec["settings"]
        wanted = {key: value for key, value in patch.items()
                  if value != fixtures.DEFAULT_SETTINGS.get(key) and key != "cwd"}
        if wanted:
            engine.update_settings(wanted)

    def _extras(self, engine):
        spec, keys = self.spec, self.keys
        if self.scenario in ("terminal", "resets-soon"):
            alpha = next(a for a in self.accounts if a["id"] == "cli_alpha")
            self._cli_live(alpha)
            engine.sense_active()
            engine.submit(CLISwitch(engine, keys["cli_beta"], kind="threshold",
                reason="Alpha reached 95% of its weekly limit; switched terminals to Beta."))
            engine.runner.wait(10)
            engine.runner.dismiss_report()
        if self.scenario != "first-run":
            for event in reversed(spec["events"]):
                engine.store.event(event["kind"], event["message"])
            for notice in spec["notices"]:
                engine.notice(notice["message"], notice["level"], sticky=notice["sticky"])
        for alert in reversed(spec["alerts"]):
            engine.store.add_alert({**{k: alert[k] for k in ("kind", "level", "title", "message", "at", "window")},
                                    "account": keys.get(alert["account"]), "snoozed_until": alert["snoozed_until"]})
        if spec["pending"] is not None:
            frozen = spec["pending"]
            account = keys.get(frozen["account"])
            engine.submit(_Showcase(engine, frozen, account=account))
            self._settle(engine, frozen["phase"])
        if spec["report"] is not None:
            engine.submit(_Finished(engine, spec["report"], account=keys.get(spec["report"]["account"])))
            engine.runner.wait(30)

    @staticmethod
    def _settle(engine, phase):
        """Waits (a few seconds at most) until the showcase operation has reached its phase."""
        for _ in range(200):
            pending = engine.runner.pending()
            if pending is not None and pending["phase"] == phase:
                return
            time.sleep(0.025)

def build_demo(root, scenario, now=None, *, start=True, inline=False):
    """The engine of demo `scenario` (one of `SCENARIOS`) on synthetic data under `root`, started.
    `now` is the virtual clock's start (Unix seconds; default: the real time). `start=False` leaves the
    tick thread off and `inline=True` reads on the caller's thread, for tests that tick by hand; move the
    virtual clock with `engine.clock.advance(seconds)`."""
    if scenario not in fixtures.SCENARIOS:
        raise DeckError(f"Unknown demo scenario {scenario!r}. Choose one of: {', '.join(SCENARIOS)}.", "invalid")
    return _Builder(root, scenario, now, start=start, inline=inline).build()
