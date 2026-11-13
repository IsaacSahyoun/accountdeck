"""AccountDeck 2 engine: the state snapshot, the tick loop and the action facade.

`Engine` implements `server.EngineAPI` and is the one object the switch flows, the move import and
the demo build against.

- `snapshot()` builds the state from the store, the provider adapters and one observation of
  the machine, enriches it with `intel.enrich_state` and validates it with
  `contract.validate_state` (an assertion in demo and safe mode, one log line in live mode). The
  result is cached until `bump()` raises `rev`.
- The `ad2-sched` thread ticks every few seconds: it takes one look at the processes, senses the
  signed-in accounts, polls the status pages, hands due accounts to the `RefreshScheduler`, runs
  the background verifiers and the terminal auto-switch, and evaluates the alert engine. Nothing in
  the tick signals a process.
- `action()` answers the quick actions itself and hands every switch flow to `switching.dispatch`, which
  shares the engine only through its public members.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import itertools
import json
from pathlib import Path
import threading
import time
import tomllib

from . import (__version__, activity, alerts, cli_auto, contract, diagnostics, intel, move_import, switch_claude,
               switching, workfolders)
from .accounts import AccountService
from .cli_accounts import CLIAccounts
from .cli_policy import considered
from .config import SETTINGS_SCHEMA, Settings, Timings, safe_mode, validate_settings
from .core import DeckError, FrozenAccountError, Store, Vault, is_frozen, recover_transactions, vault_cipher
from .diagnostics import Manifest
from .operations import TERMINAL_PHASES, Operation, OperationRunner
from .platform import facts_mac
from .procs import ProcessSet, get_controller, unix_time
from .providers import make_providers
from .providers.claude_cli import ClaudeCLI
from .providers.muse import Muse
from .providers.status import ObservedStatus, StatuspageStatus, StatuspageSummary
from .rules import REFILL_KEEP
from .scheduler import HOT_RESET, RefreshScheduler, Target
from .usage import PRIMARY_KINDS

NOTICE_TTL = 120.0            # a non-sticky notice leaves the state after this long
MAX_NOTICES = 30
HEARTBEAT = 60.0              # the rev moves at least this often, so countdown-derived fields stay fresh
HEARTBEAT_LOG = 600.0         # engine.heartbeat is logged this often (real seconds); health() is always live
STALL_TICK = 30.0             # `stalled` when no tick completed for this long (real seconds)
STALL_READ = 1200.0           # ... or no terminal read succeeded for this long while terminal reads should run
PHASE_RELOG = 60              # a phase failing on every tick is logged again every this many ticks
SLOW_EVERY = 60.0             # shared setup, Codex conversations and the data-folder check
STATUS_MIN = 1.0
MACHINE_TTL = 1.5             # how old a process observation a snapshot may reuse (real seconds)
READY_GRACE = 30.0            # `ready` turns true after this long even if a first reading is still missing
PLAN_GRACE = 600.0            # a plan is kept this long after it expired
MAX_SETUP_BYTES = 8 * 1024 * 1024
HOT_USED, EXHAUSTED_USED = 95.0, 99.5
DEAD_CREDENTIALS = frozenset({"reauth_required", "access_expired", "web_only"})   # the active account still shows these
MANIFEST_ID = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
CONSUMER_KINDS = {"codex_tui": "tui", "codex_app_server_child": "app_server_child", "codex_desktop": "desktop"}
MUSE_CONSUMER_KINDS = {"muse_session": "tui", "muse_host": "app_server_child"}    # a terminal session, `muse serve`
SETUP_MESSAGE = ("Settings, local MCP definitions, skills, plugins and project files stay shared. "
                 "Hosted connectors may require sign-in on each account.")
SETUP_NOTE = "Claude Chat local MCPs and Claude Code MCPs are separate configurations."
READ_ONLY_OTHER = ("Another AccountDeck 2 is managing your sign-ins",
                   "Quit that copy to make changes here. This one only shows usage.")
SELF_PROTECT = ("Open AccountDeck 2 from Applications",
                "AccountDeck was started from inside Claude, so it cannot close Claude to switch accounts. "
                "Quit it and open AccountDeck 2 from Applications or the menu bar.")
OTHER_OWNER = "Another AccountDeck 2 is managing your sign-ins. Quit it to make changes here."
OUTAGE_TAIL = " Your last good readings are kept. Switching still works; verification may take longer."


class ServiceOperation(Operation):
    """A quick mutation that still runs as an operation (one at a time, visible as `pending`)."""

    def __init__(self, kind, fn, *, message, step, **fields):
        super().__init__(kind, steps=(step,), **fields)
        self.fn, self.working = fn, message

    def run(self):
        with self.step(self.steps[0].id, phase="saving", message=self.working, cancellable=False):
            self.fn()


@dataclass(frozen=True)
class Machine:
    """One observation of the processes, shared by the tick and the snapshots built after it."""
    kinds: dict
    report: object                # activity.ActivityReport
    consumers: list
    registry: dict
    names: dict
    self_protect: bool
    fingerprint: tuple
    taken: float                  # real monotonic time of the observation
    muse: list = field(default_factory=list)     # Muse Code processes using the Muse login

    @staticmethod
    def empty():
        blank = activity.ActivityReport([], 0, 0, 0, 0, [], True, None)
        return Machine({kind: [] for kind in ("claude_desktop", "claude_code", "claude_foreign", "codex_daemon",
                                              "codex_desktop")}, blank, [], {}, {}, False, ("unavailable",), 0.0)


class Engine:
    tick_seconds = 5.0

    def __init__(self, paths, *, owner_lock=None, providers=None, procs=None, vault=None, status_sources=None,
                 timings=None, clock=time.time, start=True, inline=False, mode="live",
                 real_time=time.time, real_monotonic=time.monotonic):
        """`owner_lock` is the machine-wide lock run.py took: None means this engine owns the
        sign-ins outright (tests, demo), an unheld lock means another AccountDeck 2 does. `clock`
        is the engine's time (the demo passes a virtual one); `real_time` and `real_monotonic`
        only detect a resume from sleep and age cached observations."""
        self.paths, self.owner_lock, self.clock, self.mode = paths, owner_lock, clock, mode
        self.real_time, self.real_monotonic, self.inline = real_time, real_monotonic, inline
        self.timings = timings or Timings()
        self.lock = threading.RLock()
        # Liveness (`health()`): ticks, failing tick phases and the last read per provider.
        self._health_lock = threading.Lock()
        self._ticks, self._last_tick, self._last_tick_real, self._failing, self._reads = 0, None, None, {}, {}
        self._tick_assertions = []
        self._beat = {"at": real_monotonic(), "ticks": 0, "ok": 0, "failed": 0}
        self._read_baseline = real_monotonic()     # start or last resume: read staleness counts from here
        self._build_lock, self._machine_lock = threading.Lock(), threading.Lock()
        self._sense_lock, self._slow_lock = threading.Lock(), threading.Lock()
        self.stopping = threading.Event()
        self.rev, self._cache, self._stopped, self.thread = 1, None, False, None
        self.store = Store(paths.data_dir, clock=clock)
        self.vault = vault or Vault(paths.data_dir / "vault", protect=vault_cipher(paths.data_dir))
        stored = {key: value for key in SETTINGS_SCHEMA if (value := self.store.setting(key)) is not None}
        self.prefs = Settings(stored, save=self.store.set_setting, home=paths.home)
        if "log_level" in stored:
            diagnostics.set_level(self.prefs.get("log_level"))
        self.procs = procs or get_controller(paths, demo=mode == "demo")
        self.providers = dict(providers) if providers else make_providers(paths, procs=self.procs)
        self.providers.setdefault("claude_cli", ClaudeCLI(paths, clock=clock))
        self.providers.setdefault("muse", Muse(paths, self.procs))
        self.observed = {name: ObservedStatus(clock=clock) for name in self.providers}
        self.status_sources = self._default_sources() if status_sources is None else dict(status_sources)
        self.status_pages, self._status_due, self._status_busy = {}, {}, set()
        self._status_threads = []
        self._counts = {}
        self.notices, self._notice_ids = [], itertools.count(1)
        self.plans, self.login = {}, None
        self.active_identity = {name: None for name in self.providers}
        self.identity_known = {name: False for name in self.providers}
        self.background_verifiers = []
        self.closers = []                    # called once by stop(), e.g. a Codex sign-in helper
        self.scheduler = RefreshScheduler(self._refresh, inline=inline)
        self.accounts = AccountService(self.store, self.vault, self.providers, self.prefs, clock=clock,
                                       observed=self.observed, levels=self.levels,
                                       lock_for=self.scheduler.account_lock,
                                       lock_wait=self.timings.lock_wait, on_change=self.bump)
        self.runner = OperationRunner(on_change=self.bump, on_finish=self._finished, on_settle=self._settled,
                                      clock=clock)
        self.cli = CLIAccounts(self, self.providers["claude_cli"])
        if self.owns_login and not self.cli.adapter.readonly_reason:
            self.cli.adopt_stashes()
            self.cli.restore_enrollment()
        self._recovery_lock = threading.Lock()      # two operations can finish at the same moment
        self.recovery = self._scan_recovery()
        self._sweep_work()
        self.alert_engine = alerts.AlertEngine(started=clock())
        self._machine, self._slow, self._idle_state = None, None, {}
        self._read_once = set()
        self._started_at = real_monotonic()
        self._wall, self._last_heartbeat = real_time(), clock()
        self._previous_state, self._alerts_rev, self._invalid_logged = None, None, False
        self.tray_handle = None              # the menu bar item while it shows (run.py, update_settings)
        self.sense_active()
        self._restore_login()
        self._counts = self._count_accounts(self.store.accounts())
        if start:
            self.start()

    def _default_sources(self):
        """The real status pages, only where the network is allowed at all."""
        if self.mode != "live" or safe_mode():
            return {}
        return {"claude": StatuspageSummary(clock=self.clock), "codex": StatuspageStatus(clock=self.clock)}

    def start(self):
        """Starts the `ad2-sched` thread (once)."""
        with self.lock:
            if self.thread is None and not self._stopped:
                self.thread = threading.Thread(target=self._loop, name="ad2-sched", daemon=True)
                self.thread.start()

    # ---- what switch code and the server read ----
    @property
    def pending(self):
        """The running operation, or None; an operation waiting in `needs_reconfirm` is not running."""
        pending = self.runner.pending()
        return pending if pending is not None and pending["phase"] not in TERMINAL_PHASES else None

    @property
    def owns_login(self):
        return self.owner_lock is None or bool(self.owner_lock.held)

    @property
    def read_only(self):
        """Another AccountDeck 2 holds the owner lock: this one only shows usage."""
        return not self.owns_login

    def bump(self):
        with self.lock:
            self.rev += 1
            self._cache = None

    def notice(self, message, level="info", sticky=True):
        """Posts a notice; a sticky one stays until dismissed, another leaves after two minutes."""
        with self.lock:
            self.notices.append({"id": f"n_{next(self._notice_ids)}", "level": level if level in contract.LEVELS else "info",
                                 "message": str(message), "at": self.clock(), "sticky": bool(sticky)})
            del self.notices[:-MAX_NOTICES]
        self.bump()

    def submit(self, op):
        """Runs an operation on `ad2-ops`; DeckError `busy` while another one runs."""
        result = {"op": self.runner.submit(op)}
        self.bump()
        return result

    def roots(self):
        return [self.paths.claude_profile, self.paths.codex_home, self.paths.claude_projects, self.paths.muse_config]

    def _sweep_work(self):
        """Once at start, before any read: the helper folders a crash left behind are cleaned up
        (workfolders.sweep). Another AccountDeck 2 that owns the sign-ins may be using them."""
        if not self.owns_login:
            return
        try:
            workfolders.sweep(self)
        except Exception as exc:
            diagnostics.log_exception("engine.work_sweep_failed", exc)

    def _scan_recovery(self):
        return recover_transactions(self.vault)

    def look_for_unfinished(self):
        """Reads the open switch records again. Jobs end and Recover is pressed on different threads, so the
        look and the assignment are one step: the newest look at the records is the one that stays."""
        with self._recovery_lock:
            self.recovery = self._scan_recovery()

    def _restore_login(self):
        """Once after construction: an add-account sign-in that was waiting when the service last stopped
        (`engine.login`) is shown again. It never stops the start."""
        try:
            switch_claude.restore_enrollment(self)
        except Exception as exc:
            diagnostics.log_exception("engine.restore_login_failed", exc)

    # ---- who is signed in ----
    def sense_active(self):
        """Reads which account each provider is signed in to (read-only) and tracks the unsaved one."""
        changed = False
        with self._sense_lock:
            now = self.clock()
            for name, adapter in self.providers.items():
                try:
                    if name == "claude_cli":
                        identity = self.cli.mirror()
                        known = True
                    elif name in ("codex", "muse"):
                        found = adapter.active_identity()
                        known, identity = found.status != "unknown", found.identity if found.status == "ok" else None
                    else:
                        known, identity = True, adapter.active_identity()
                except DeckError as exc:
                    known, identity = name == "claude_cli" and exc.code == "login_needed", None
                except Exception as exc:       # a vault/store/disk error: unknown, and the next provider still runs
                    diagnostics.log_exception("engine.sense_failed", exc, provider=name)
                    known, identity = False, None
                if not known:            # keep what was last known; the switch code sees identity_known False
                    changed |= self.identity_known[name]
                    self.identity_known[name] = False
                    continue
                changed |= (identity, True) != (self.active_identity[name], self.identity_known[name])
                self.active_identity[name], self.identity_known[name] = identity, True
                if name == "claude_cli":
                    continue
                before = self.accounts.unsaved[name]
                self.accounts.sense_unsaved(name, identity)
                changed |= (before is None) != (self.accounts.unsaved[name] is None)
                if identity is not None:
                    self.accounts.touch_active(name, identity, now)
        if changed:
            self.bump()

    # ---- provider status ----
    def status(self, provider, now=None):
        """The contract status of a provider: the status page and what our own reads observed."""
        now = self.clock() if now is None else now
        with self.lock:
            page = self.status_pages.get("claude" if provider == "claude_cli" else provider)
        observed = self.observed[provider].evaluate(now=now, accounts=self._counts.get(provider))
        return intel.classify_outage(provider, page, observed, now=now)

    def levels(self):
        now = self.clock()
        return {name: self.status(name, now)["level"] for name in self.providers}

    def _poll_status(self, now):
        for name, source in self.status_sources.items():
            if name in self._status_busy or now < self._status_due.get(name, 0):
                continue
            self._status_busy.add(name)
            if self.inline:
                self._fetch_status(name, source)
                continue
            thread = threading.Thread(target=self._fetch_status, args=(name, source), name="ad2-status", daemon=True)
            self._status_threads = [t for t in self._status_threads if t.is_alive()] + [thread]
            thread.start()

    def _fetch_status(self, name, source):
        try:
            before = self.status(name)["level"]
            try:
                page = source.fetch()
            except DeckError as exc:       # a safety refusal must not stop the polling loop
                diagnostics.log("status.fetch_refused", lvl="warning", provider=name, code=exc.code)
                page = None
            except Exception as exc:       # nor may a parsing bug turn it into a request every tick
                diagnostics.log_exception("status.fetch_failed", exc, provider=name)
                page = None
            with self.lock:
                if page is not None:
                    self.status_pages[name] = page
            now = self.clock()
            self._status_due[name] = now + max(source.poll_interval(), STATUS_MIN)
            if before in ("degraded", "outage") and self.status(name, now)["level"] == "ok":
                self.notice(f"{intel.PROVIDER_NAMES.get(name, name)} is back to normal.", "good", sticky=False)
                self.scheduler.request_all([row["id"] for row in self.store.accounts() if row["provider"] == name], now)
            self.bump()
        finally:
            self._status_busy.discard(name)

    # ---- reads ----
    def _pressure(self, usage, now):
        """(hot, exhausted, resets) of a last good reading."""
        windows_ = [w for w in map(intel.normalize_window, (usage or {}).get("windows") or ())
                    if w is not None and w["kind"] in PRIMARY_KINDS]
        current = [w for w in windows_ if w["reset"] is None or w["reset"] > now]     # a passed reset says nothing yet
        hot = any((w["used"] or 0) >= HOT_USED or w["blocked"] or (w["reset"] is not None and w["reset"] - now <= HOT_RESET)
                  for w in current)
        exhausted = bool(current) and all((w["used"] or 0) >= EXHAUSTED_USED or w["blocked"] for w in current)
        return hot, exhausted, tuple(sorted({w["reset"] for w in windows_ if w["reset"] is not None}))

    def targets(self, now=None, rows=None):
        """One scheduler Target per readable saved account and per unsaved signed-in account. Another
        computer's account and a Desktop profile still waiting for conversion are never read."""
        now = self.clock() if now is None else now
        rows = self.store.accounts() if rows is None else rows
        read_only = self.read_only
        levels = {name: self.status(name, now)["level"] for name in self.providers}
        targets = []
        for row in rows:
            if is_frozen(row) or not move_import.readable(row):
                continue
            hot, exhausted, resets = self._pressure(row["last_good_usage"], now)
            if row["provider"] == "claude_cli":
                values = considered({"windows": (row["last_good_usage"] or {}).get("windows", [])}, self.prefs.public())
                hot = any(used is not None and used >= 80 for _, used, _ in values)
                exhausted = any(used is not None and used >= 100 for _, used, _ in values)
            targets.append(Target(row["id"], row["provider"], row["identity"],
                                  active=row["identity"] == self.active_identity.get(row["provider"]),
                                  hot=hot, exhausted=exhausted, resets=resets, paused_until=row["paused_until"],
                                  read_only=read_only, level=levels[row["provider"]]))
        for name, unsaved in self.accounts.unsaved.items():
            if unsaved is not None:
                hot, exhausted, resets = self._pressure(unsaved["usage"], now)
                targets.append(Target(contract.UNSAVED_IDS[name], name, unsaved["identity"], active=True, hot=hot,
                                      exhausted=exhausted, resets=resets, read_only=read_only, level=levels[name]))
        return targets

    def _refresh(self, key):
        """One read on an `ad2-refresh` worker. The terminal auto-switch policy is not evaluated here:
        it runs on the tick thread only (one writer of `cli_history`, and a policy error can never
        turn a good reading into a failed one); the next tick, at most 5 s later, sees this reading."""
        row = self.store.account(key)
        provider = row["provider"] if row else next((name for name, unsaved in contract.UNSAVED_IDS.items()
                                                     if unsaved == key), None)
        result = "error"
        try:
            if is_frozen(row):
                result = "skipped"           # another computer's sign-in is never read or renewed here
            elif row and row["provider"] == "claude_cli":
                result = self.cli.read(key)
            else:
                result = self.accounts.read(key, active_identity=dict(self.active_identity), read_only=self.read_only)
            return result
        finally:
            self._record_read(provider, result)
            self._read_once.add(key)
            self.bump()

    def _record_read(self, provider, outcome):
        if provider is None or outcome == "skipped":
            return
        now, real = self.clock(), self.real_monotonic()
        with self._health_lock:
            entry = self._reads.setdefault(provider, {"ok": 0, "failed": 0, "last_ok_at": None, "last_failed_at": None,
                                                      "last_outcome": None, "ok_real": None})
            entry["last_outcome"] = outcome if isinstance(outcome, str) else "error"
            if outcome == "ok":
                entry.update(ok=entry["ok"] + 1, last_ok_at=now, ok_real=real)
                self._beat["ok"] += 1
            else:
                entry.update(failed=entry["failed"] + 1, last_failed_at=now)
                self._beat["failed"] += 1

    # ---- the tick ----
    def tick(self):
        """Every phase runs on its own: one that raises is logged (`engine.phase_failed`) and listed in
        `health()`, and the phases after it, the scheduler above all, still run."""
        now = self.clock()
        self._tick_assertions = []
        phase = self._phase
        phase("resume", self._check_resume, now)
        phase("observe", self._observe, fresh=True)
        if self.runner.quiet:      # also during another client's wait: auto-switch acts then, on who is signed in now
            phase("sense", self.sense_active)
        if not self.runner.busy:
            # A stuck enrollment must not stop every account's reads.
            phase("enrollment", self.cli.poll_enrollment, event="cli.enrollment_failed")
        phase("status", self._poll_status, now)
        phase("schedule", self._schedule, now)
        phase("slow", self._refresh_slow, now)
        phase("verifiers", self._tick_verifiers, now)
        phase("autoswitch", cli_auto.tick, self)
        phase("tidy", self._tidy, now)
        phase("convert", self._convert_deferred, now)
        if now - self._last_heartbeat >= HEARTBEAT:
            self._last_heartbeat = now
            self.bump()
        phase("alerts", self._evaluate_alerts, now)
        self._ticked(now)
        if self._tick_assertions:       # an invalid state stays loud in safe mode and the demo
            raise self._tick_assertions[0]

    def _schedule(self, now):
        rows = self.store.accounts()
        self._counts = self._count_accounts(rows)
        self.scheduler.set_paused(bool(self.prefs.get("refresh_paused")))
        if self.scheduler.tick(self.targets(now, rows), now, self.clock):
            self.bump()

    def _phase(self, name, fn, *args, event="engine.phase_failed", **kwargs):
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            with self._health_lock:
                entry = self._failing.setdefault(name, {"since": self.clock(), "count": 0, "error_type": None})
                entry["count"] += 1
                entry["error_type"] = type(exc).__name__
                count = entry["count"]
            if count == 1 or count % PHASE_RELOG == 0:
                diagnostics.log_exception(event, exc, phase=name, count=count)
            if isinstance(exc, AssertionError) and (safe_mode() or self.mode == "demo"):
                self._tick_assertions.append(exc)
            return None
        if name in self._failing:
            with self._health_lock:
                entry = self._failing.pop(name, None)
            if entry is not None:
                diagnostics.log("engine.phase_recovered", phase=name, count=entry["count"],
                                seconds=round(self.clock() - entry["since"]))
        return result

    def _ticked(self, now):
        real = self.real_monotonic()
        with self._health_lock:
            self._ticks += 1
            self._last_tick, self._last_tick_real = now, real
            self._beat["ticks"] += 1
            beat = None
            if real - self._beat["at"] >= HEARTBEAT_LOG:
                beat, self._beat = self._beat, {"at": real, "ticks": 0, "ok": 0, "failed": 0}
        if beat is not None:
            health = self.health()
            diagnostics.log("engine.heartbeat", ticks=beat["ticks"], total=health["ticks"], reads_ok=beat["ok"],
                            reads_failed=beat["failed"], failing=sorted(health["failing_phases"]),
                            stalled=health["stall_reasons"] or None)

    def health(self):
        """Liveness for /_health and /api/diagnostics: no secrets, only times, counts and phase names.
        `stalled`: no tick completed for 30 s, or (with terminal accounts whose checks are on) no terminal
        read succeeded for 20 min since start, the last resume or the last success."""
        real, now = self.real_monotonic(), self.clock()
        with self._health_lock:
            ticks, last_tick, last_real = self._ticks, self._last_tick, self._last_tick_real
            failing = {name: dict(entry) for name, entry in self._failing.items()}
            reads = {name: {k: v for k, v in entry.items() if k != "ok_real"} for name, entry in self._reads.items()}
            cli_ok = (self._reads.get("claude_cli") or {}).get("ok_real")
            baseline = self._read_baseline
        reasons = []
        if not self._stopped:
            if real - (self._started_at if last_real is None else last_real) >= STALL_TICK:
                reasons.append("tick")
            paused = (bool(self.prefs.get("refresh_paused")) or self.scheduler.provider_pause.get("claude_cli")
                      or bool(self.cli.adapter.readonly_reason))
            if (self._counts.get("claude_cli") and not paused
                    and real - max(baseline, cli_ok if cli_ok is not None else baseline) >= STALL_READ):
                reasons.append("claude_cli_reads")
        return {"ticks": ticks, "last_tick_at": last_tick,
                "tick_age_s": None if last_real is None else round(real - last_real, 1), "now": now,
                "failing_phases": failing, "reads": reads, "stalled": bool(reasons), "stall_reasons": reasons}

    def _loop(self):
        while not self.stopping.is_set():
            try:
                self.tick()
            except Exception as exc:
                diagnostics.log_exception("engine.tick_failed", exc)
            self.stopping.wait(self.tick_seconds)

    def _check_resume(self, now):
        wall = self.real_time()
        jumped = wall - self._wall > 3 * self.tick_seconds
        self._wall = wall
        if jumped:
            self._read_baseline = self.real_monotonic()
            self.scheduler.resume(now)
            self.alert_engine.resumed(now)
            for name in self.status_sources:
                self._status_due[name] = max(self._status_due.get(name, 0), now + 30)
            diagnostics.log("engine.resumed")
            self.bump()

    def _tick_verifiers(self, now):
        for verifier in list(self.background_verifiers):
            try:
                finished = verifier.tick(now)
            except Exception as exc:       # a broken verifier is dropped, never retried into a loop
                diagnostics.log_exception("engine.verifier_failed", exc)
                finished = True
            if finished:
                try:
                    self.background_verifiers.remove(verifier)
                except ValueError:
                    pass

    def _tidy(self, now):
        with self.lock:
            kept = [n for n in self.notices if n["sticky"] or now - n["at"] <= NOTICE_TTL]
            expired = len(kept) != len(self.notices)
            self.notices = kept
            for key, plan in list(self.plans.items()):
                expires = plan.get("expires") if isinstance(plan, dict) else None
                if isinstance(expires, (int, float)) and now - expires > PLAN_GRACE:
                    self.plans.pop(key, None)
        if expired:
            self.bump()

    def _convert_deferred(self, now):
        """Claude Desktop profiles imported from the PC before Claude Desktop was set up here (move_import)."""
        move_import.convert_deferred(self, now)

    def _evaluate_alerts(self, now):
        try:
            state = self.snapshot()
            if state["rev"] == self._alerts_rev:
                return
            latches = self.prefs.get("alert_latches")
            new, latches = self.alert_engine.evaluate(
                self._previous_state, state, latches, now, low_left=self.prefs.get("alert_low_left"),
                reset_minutes=self.prefs.get("notify_reset_minutes"))
            self.prefs.set_internal("alert_latches", latches)
            self._previous_state, self._alerts_rev = state, state["rev"]
            if self.prefs.get("alerts_enabled") and new:
                for item in new:
                    self.store.add_alert(item)
                self.bump()
        except (DeckError, AssertionError):
            raise
        except Exception as exc:
            diagnostics.log_exception("engine.alerts_failed", exc)

    # ---- observing the machine ----
    def _observe(self, *, fresh=False):
        with self._machine_lock:
            cached = self._machine
            if not fresh and cached is not None and self.real_monotonic() - cached.taken < MACHINE_TTL:
                return cached
            try:
                rows = self.procs.snapshot()          # one look per observation, shared by everything below
                kinds = self.procs.classify(rows)
                report = activity.report(self.paths, self.procs, now=self.clock(), idle_hold_s=self.timings.idle_hold,
                                         state=self._idle_state, kinds=kinds)
                consumers = self.procs.codex_consumers(rows=rows)
                muse = self.procs.muse_consumers(rows=rows)
                ancestors = {row.pid for row in self.procs.ancestors_of_self()}
            except DeckError as exc:
                diagnostics.log("engine.observe_failed", lvl="warning", code=exc.code)
                machine = Machine.empty()
            except Exception as exc:       # snapshots build on this too; an OS error must not fail /api/state
                diagnostics.log_exception("engine.observe_failed", exc)
                machine = Machine.empty()
            else:
                mine = {row.pid for row in kinds["claude_desktop"] + kinds["claude_code"]}
                shown = (kinds["claude_desktop"], kinds["claude_code"], consumers, kinds["codex_daemon"], muse)
                machine = Machine(
                    kinds, report, consumers, activity.read_registry(self.paths), {row.pid: row.name for row in rows},
                    bool(ancestors & mine),
                    (tuple(ProcessSet.of(group).signature() for group in shown),
                     tuple((s.pid, s.status, s.name) for s in report.sessions), report.foreign, report.registry_version,
                     bool(ancestors & mine)),
                    self.real_monotonic(), muse)
            self._machine = machine
        if cached is not None and cached.fingerprint != machine.fingerprint:
            self.bump()
        return machine

    def _refresh_slow(self, now):
        with self._slow_lock:
            if self._slow is None or now - self._slow["at"] >= SLOW_EVERY:
                try:
                    base, kind, _reason = facts_mac.claude_base(self.paths)
                except Exception as exc:   # snapshots build on this too; fall back to the configured folder
                    diagnostics.log_exception("engine.data_folder_failed", exc)
                    base, kind = Path(self.paths.claude_profile), None
                try:
                    conversations = self.providers["codex"].sessions()
                except (DeckError, OSError, ValueError, KeyError):
                    conversations = []
                self._slow = {"at": now, "data_folder": kind, "sessions": conversations,
                              "setup": shared_setup(self.paths, base)}
            return self._slow

    @staticmethod
    def _count_accounts(rows):
        counts = {}
        for row in rows:
            counts[row["provider"]] = counts.get(row["provider"], 0) + 1
        return counts

    # ---- the snapshot ----
    def snapshot(self, rev=None):
        """The state, or None when `rev` is the current revision."""
        with self.lock:
            if rev is not None and rev == self.rev:
                return None
            if self._cache is not None:
                return copy.deepcopy(self._cache)
        with self._build_lock:
            with self.lock:
                if self._cache is not None:
                    return copy.deepcopy(self._cache)
                built_rev = self.rev
            state = self._build(built_rev)
            with self.lock:
                if self.rev == built_rev:
                    self._cache = state
            return copy.deepcopy(state)

    def _build(self, rev):
        now = self.clock()
        settings = self.prefs.public()
        machine, slow = self._observe(), self._refresh_slow(now)
        rows = self.store.accounts()
        self._counts = self._count_accounts(rows)
        status = {name: self.status(name, now) for name in self.providers}
        refills = {}
        for refill in self.store.refills(now - REFILL_KEEP):
            refills.setdefault(refill["account"], {})[refill["window"]] = {
                "at": refill["reset_at"], "confirmed": refill["confirmed_at"], "from": refill["previous_used"]}
        accounts = [self._raw_account(row, now, status, refills.get(row["id"], {})) for row in rows]
        accounts += self._unsaved_rows(now, status)
        runner = self.runner.snapshot()
        pending = runner["pending"]
        with self.lock:
            notices = [dict(n) for n in self.notices if n["sticky"] or now - n["at"] <= NOTICE_TTL]
            login = self._public_login()
        providers = {}
        for name in self.providers:
            providers[name] = {
                "status": status[name],
                "active": next((a["id"] for a in accounts if a["provider"] == name and a["active"]), None),
                "running": self._running(name, machine, now),
                "switching": bool(pending and pending["provider"] == name and pending["phase"] not in TERMINAL_PHASES),
                "paused": bool(self.prefs.get("refresh_paused")) or self.scheduler.provider_pause.get(name, False)}
        providers["claude"].update(data_folder=slow["data_folder"], folder_warning=None)
        providers["claude_cli"].update(autoswitch=cli_auto.state(self), enrollment=copy.deepcopy(self.cli.phase),
                                        readonly_reason=self.cli.adapter.readonly_reason)
        raw = {"app": "accountdeck2", "version": __version__, "rev": rev, "mode": self.mode, "safe_mode": safe_mode(),
               "ready": self._ready(rows, now), "read_only": self.read_only, "settings": settings,
               "providers": providers, "accounts": accounts, "pending": pending, "report": runner["report"],
               "login": login, "recovery": {"pending": bool(self.recovery), "count": len(self.recovery)},
               "notices": notices, "alerts": self.store.alerts(), "banners": self._banners(status, machine, rows),
               "setup": slow["setup"], "sessions": slow["sessions"], "cwd": settings["cwd"],
               "events": self.store.events(), "last_manifest": self.store.setting("last_manifest"),
               "platform": contract.platform_block(self._notification_state())}
        raw["move_import"] = move_import.state(self, rows)       # the PC-to-Mac import
        state, picks = intel.enrich_state(raw, now=now, settings=settings,
                                          recommender_state=self.prefs.get("recommender_state"))
        self.prefs.set_internal("recommender_state", picks)
        errors = contract.validate_state(state)
        if errors:
            if safe_mode() or self.mode == "demo":
                raise AssertionError("Invalid engine snapshot: " + "; ".join(errors[:8]))
            if not self._invalid_logged:
                self._invalid_logged = True
                diagnostics.log("state.invalid", lvl="error", count=len(errors), first=errors[0][:200])
        return state

    def _notification_state(self):
        """macOS notification permission as the running menu bar item reports it, else "unavailable"."""
        handle = self.tray_handle
        if handle is None or not handle.running:
            return "unavailable"
        try:
            state = handle.notification_state()
        except Exception as exc:          # a broken platform answer must not fail /api/state
            diagnostics.log_exception("menubar.notification_state_failed", exc)
            return "unavailable"
        return state if state in contract.NOTIFICATION_STATES else "unavailable"

    def _ready(self, rows, now):
        """True once every account has been read (or failed to read) since start, or after a grace period."""
        if self.real_monotonic() - self._started_at >= READY_GRACE:
            return True
        if any(row["last_good_at"] is None and not row["error"] and row["id"] not in self._read_once for row in rows):
            return False
        return all(u["last_good_at"] is not None or u["error"] for u in self.accounts.unsaved.values() if u)

    def _public_login(self):
        login = self.login
        if not isinstance(login, dict) or login.get("provider") not in contract.PROVIDERS:
            return None
        return {key: value for key, value in login.items() if not str(key).startswith("_")
                and isinstance(value, (str, int, float, bool, type(None)))}

    def _running(self, provider, machine, now):
        kinds = machine.kinds
        if provider == "claude_cli":
            return {"checked": now}
        if provider == "claude":
            report = machine.report
            sessions = []
            for s in report.sessions:
                folder = Path(str((machine.registry.get(s.pid) or {}).get("cwd") or "")).name or None
                sessions.append({"pid": s.pid, "name": s.name, "status": s.status, "since": s.since, "cwd_short": folder})
            return {"desktop": bool(kinds["claude_desktop"]), "desktop_pids": len(kinds["claude_desktop"]),
                    "checked": now, "sessions": sessions, "busy": report.busy, "unknown": report.unknown,
                    "foreign_claude": report.foreign, "registry_version": report.registry_version}
        if provider == "muse":
            return {"consumers": muse_consumers(machine.muse, machine.names), "checked": now}
        consumers = [{"pid": row.pid, "kind": CONSUMER_KINDS[row.kind], "parent": machine.names.get(row.parent),
                      "started": unix_time(row.ctime)} for row in machine.consumers if row.kind in CONSUMER_KINDS]
        return {"consumers": consumers, "daemon": bool(kinds["codex_daemon"]), "desktop": bool(kinds["codex_desktop"]),
                "checked": now}

    def _raw_account(self, row, now, status, refills):
        name, key, flags = row["provider"], row["id"], row["flags"]
        active = row["identity"] == self.active_identity.get(name)
        error = None
        if row["error"]:
            error = {"code": row["error_code"] or "network", "message": row["error"],
                     "since": flags.get("error_since", now), "outage": status[name]["level"] in ("degraded", "outage")}
        paused = row["paused_until"] if row["paused_until"] and row["paused_until"] > now else None
        active_at = flags.get("last_active_at")
        samples, trend = self.accounts.series(row, now)
        return {"id": key, "provider": name, "label": row["label"], "email": row["email"], "plan": row["plan"],
                **({"excluded": flags.get("excluded", False), "health": flags.get("health", "ok"),
                    "live_expired": flags.get("live_expired", False)} if name == "claude_cli" else {}),
                "saved": True, "active": active,
                "switchable": bool(flags.get("restorable", True)) and not is_frozen(row),
                "imported_from": row["imported_from"], "owner": row["owner"], "usage": row["last_good_usage"],
                "credential": {"state": "live" if active and row["credential_state"] not in DEAD_CREDENTIALS
                               else row["credential_state"],
                               "expires_in": None if row["expires_at"] is None else row["expires_at"] - now,
                               "last_outcome": row["last_outcome"],
                               "dormant_days": None if active_at is None else max(0, int((now - active_at) / 86400)),
                               "session_expires_in_days": None if flags.get("session_expires_at") is None
                               else (flags["session_expires_at"] - now) / 86400,
                               "revoked_hint": bool(flags.get("revoked_hint"))},
                "last_good_at": row["last_good_at"], "checked": flags.get("checked", row["last_good_at"]),
                "refreshing": self.scheduler.running(key), "error": error, "paused_until": paused,
                "next_check": self.scheduler.planned(key), "sort_order": row["sort_order"], "refills": refills,
                "samples": samples, "trend": trend}

    def _unsaved_rows(self, now, status):
        rows = []
        for name, row in self.accounts.unsaved.items():
            if row is None:
                continue
            key = contract.UNSAVED_IDS[name]
            error = copy.deepcopy(row["error"])
            if error:
                error["outage"] = status[name]["level"] in ("degraded", "outage")
            rows.append({"id": key, "provider": name, "label": row["label"] or f"Current {name.title()} account",
                         "email": row["email"], "plan": row["plan"], "saved": False, "active": True,
                         "switchable": False, "usage": row["usage"], "last_good_at": row["last_good_at"],
                         "checked": row["checked"], "error": error, "refreshing": self.scheduler.running(key),
                         "next_check": self.scheduler.planned(key),
                         "credential": {"state": "live", "expires_in": row["expires_in"],
                                        "last_outcome": row["last_outcome"], "dormant_days": None,
                                        "session_expires_in_days": None, "revoked_hint": False}})
        return rows

    def _banners(self, status, machine, rows):
        items = []
        if self.mode == "demo":
            items.append(banner("demo", "Demo mode", "Demo mode — synthetic accounts; no real apps are touched."))
        if safe_mode():
            items.append(banner("safe_mode", "Safe mode",
                                "Safe mode is on: AccountDeck 2 will not touch real apps or files."))
        if not self.owns_login:
            items.append(banner("read_only", *READ_ONLY_OTHER))
        if self.recovery:
            items.append(banner("recovery", "An interrupted switch needs review",
                                "Your previous sign-in files are kept safely. Recover to put them back.",
                                actions=["recover"]))
        if self.prefs.get("refresh_paused"):
            items.append(banner("paused", "Usage checks are paused", "Turn them back on in Settings."))
        if machine.self_protect:
            items.append(banner("self_protect", *SELF_PROTECT))
        for name, page in status.items():
            if page["level"] not in ("degraded", "outage"):
                continue
            if name == "claude_cli" and page["source"] == "statuspage" and status["claude"]["level"] == page["level"]:
                continue        # the terminal reads Claude's own status page: one banner says it
            title = (page["incident"] or {}).get("title") or page["summary"]
            shown = intel.PROVIDER_NAMES[name]
            if page["level"] == "outage":
                items.append(banner("outage", f"{shown} is having an incident",
                                    title.rstrip(".") + "." + OUTAGE_TAIL, provider=name, link=page["url"]))
            else:
                items.append(banner("degraded", f"{shown} is running slowly",
                                    title.rstrip(".") + ". Readings may be delayed; your last good readings are kept.",
                                    provider=name, link=page["url"]))
        items += move_import.banners(rows)
        return items

    # ---- settings ----
    def settings(self):
        return self.prefs.public()

    def update_settings(self, patch):
        """Saves `patch` and applies its desktop side: `start_on_login` is the per-user LaunchAgent
        (autostart → platform/autostart_mac.py) and `tray` shows or hides the menu bar item
        (platform/statusbar_mac.py). Safe mode stores the values and touches nothing."""
        validate_settings(patch)
        if "start_on_login" in patch and not safe_mode():
            from . import autostart
            autostart.set_enabled(patch["start_on_login"])
        changed = self.prefs.update(patch)
        if changed:
            diagnostics.log("settings.changed", keys=sorted(changed))
        if "log_level" in changed:
            diagnostics.set_level(changed["log_level"])
        if "tray" in changed and not safe_mode():
            self._show_menu_bar(changed["tray"])
        self.bump()

    def _show_menu_bar(self, wanted):
        handle = self.tray_handle
        if wanted:
            if handle is not None and handle.running:
                return                      # `run.py --app` already shows it: never a second item
            try:
                from .platform import statusbar_mac
                self.attach_tray(statusbar_mac.start(self))
            except Exception as exc:
                diagnostics.log_exception("menubar.start_failed", exc)
                self.notice("The menu bar item could not be shown.", "warning")
        else:
            if handle is not None:
                handle.stop()
            self.attach_tray(None)

    def attach_tray(self, handle):
        self.tray_handle = handle

    # ---- actions ----
    def action(self, name, body):
        if name not in contract.ACTIONS or not isinstance(body, dict):
            raise DeckError("That action is not available.", "invalid")
        if name in ("switch-plan", "switch", "cli-exclude", "refresh") and isinstance(body.get("id"), str):
            row = self.store.account(body["id"])
            if is_frozen(row):
                raise FrozenAccountError(row)   # every provider's switch and read path, before any planner runs
        if move_import.check_action(self, name, body):      # accounts still on the PC, Desktop waiting to convert
            self.bump()
            return {}
        handler = getattr(self, "_do_" + name.replace("-", "_"), None)
        if handler is None:
            return self._dispatch(name, body)
        result = handler(body)
        self.bump()
        return result or {}

    def _dispatch(self, name, body):
        result = switching.dispatch(self, name, body)
        self.bump()
        return result or {}

    def _account_id(self, body):
        key = body.get("id")
        if not isinstance(key, str) or not key:
            raise DeckError("Choose an account.", "invalid")
        return key

    def _do_demo_clock(self, body):
        seconds = body.get("seconds")
        if self.mode != "demo" or not safe_mode() or not hasattr(self.clock, "advance"):
            raise DeckError("The clock control is only available in the synthetic demo.", "safety")
        if type(seconds) not in (float, int) or not -86400 <= seconds <= 86400:
            raise DeckError("Choose a demo offset within one day.", "invalid")
        self.paths.require_cli_paths()
        self.clock.advance(seconds - getattr(self, "_demo_offset", 0))
        self._demo_offset = seconds
        self.scheduler.request_all([r["id"] for r in self.store.accounts()], self.clock())
        return {"now": self.clock()}

    def _do_refresh(self, body):
        now = self.clock()
        key = body.get("id")
        if self.prefs.get("refresh_paused"):
            raise DeckError("Usage checks are paused. Turn them back on in Settings.", "invalid")
        targets = self.targets(now)
        if key is None:
            self.scheduler.request_all([t.id for t in targets if not t.paused_until or t.paused_until <= now], now)
            return None
        target = next((t for t in targets if t.id == key), None) if isinstance(key, str) else None
        if target is None:
            raise DeckError("That account no longer exists.", "not_found")
        if target.paused_until and target.paused_until > now:
            raise DeckError("Usage checks are paused for this account.", "rate_limited", target.paused_until - now)
        self.scheduler.request(key, now)
        return None

    def _do_rename(self, body):
        key = self._account_id(body)
        if key in contract.UNSAVED_IDS.values():
            raise DeckError("Save this account first.", "invalid")
        row = self.store.account(key)
        if row and row["provider"] == "claude_cli":
            self.cli.rename(key, body.get("label"))
        else:
            self.accounts.rename(key, body.get("label"))

    def _do_remove(self, body):
        key = self._account_id(body)
        self._own_the_login()
        if key in contract.UNSAVED_IDS.values():
            raise DeckError("This account isn't saved yet, so there is nothing to remove.", "invalid")
        account = self.store.account(key)
        if account is None:
            raise DeckError("That account no longer exists.", "not_found")

        def remove():
            if account["provider"] == "claude_cli":
                with self.scheduler.account_lock(key):
                    if is_frozen(self.store.account(key)):
                        # Remote rows have no usable local login. Keep any retained move
                        # recovery credentials until confirmation; remove only metadata.
                        self.store.remove(key)
                        op.metadata_only = True
                        self.scheduler.forget(key)
                        self.bump()
                        return
                    with self.cli.gate(key):
                        # Nobody signed in to Claude Code (None) means no saved account is in use.
                        if self.cli.adapter.active_identity() == account["identity"]:
                            raise DeckError("The account in use by your terminals cannot be removed. Switch to another "
                                            "account first.", "invalid")
                        self.accounts.remove(key)
                self.scheduler.forget(key)
                return
            self.accounts.remove(key)
            self.scheduler.forget(key)
        op = ServiceOperation("remove", remove, provider=account["provider"], account=key,
                              label=account["label"], message="Removing the account…",
                              step=("remove", "Removing the account"))
        return self.submit(op)

    def _do_cancel(self, body):
        self.runner.cancel(body.get("op"))

    def _do_dismiss(self, body):
        wanted = body.get("notice")
        with self.lock:
            self.notices = [n for n in self.notices if wanted is not None and n["id"] != wanted]

    def _do_dismiss_report(self, body):
        self.runner.dismiss_report()

    # The Mac side of the move (accountdeck/move_import.py has the shapes).
    def _do_import_bundle_preview(self, body):
        if self.runner.busy:
            raise DeckError("Finish or cancel the current action first.", "busy")
        return {"preview": move_import.preview(self, body.get("path"), body.get("passphrase"))}

    def _do_import_bundle(self, body):
        self._own_the_login()
        return move_import.start_import(self, body)

    def _do_import_bundle_dismiss(self, body):
        move_import.dismiss(self, body.get("bundle_id"))

    def _do_test_notification(self, body):
        handle = self.tray_handle
        if handle is not None and handle.running:
            handle.test_notification()
            return {}
        raise DeckError("Notifications need the menu bar item. Turn it on in Settings.", "unsupported")

    def _own_the_login(self):
        if not self.owns_login:
            raise DeckError(OTHER_OWNER, "read_only")

    # ---- after an operation ----
    def _settled(self, op):
        """Runs on `ad2-ops` after run() returned and before the operation shows as ended, so whoever
        sees it finished also sees the account it signed in."""
        if not getattr(op, "metadata_only", False):
            self.sense_active()

    def _finished(self, op, report):
        """Runs on `ad2-ops` after any operation ended (`report` is None for a `needs_reconfirm`)."""
        self.look_for_unfinished()
        now = self.clock()
        if report is not None:
            self._record_outcome(op, report, now)
        for name in ([op.provider] if op.provider else list(self.providers)):
            active = self.store.find(name, self.active_identity.get(name))
            if active is not None and report is not None and op.kind != "remove":
                self.scheduler.request(active["id"], now)
        self.bump()

    def _record_outcome(self, op, report, now):
        summary = diagnostics.redact_text(report["summary"] or "")
        if report["outcome"] in ("success", "failed") and op.kind != "remove" and summary and summary != "Done.":
            self.store.event(op.kind, summary)
        if report.get("manifest"):
            self.store.set_setting("last_manifest", report["manifest"])
        if op.kind == "switch" and op.provider and report["outcome"] in ("success", "failed"):
            verified = report["verified"] or ("failed" if report["outcome"] == "failed" else "unconfirmed")
            if self.prefs.get("alerts_enabled"):
                alert = alerts.switch_result(op.label or "your account", op.provider, verified, now)
                if op.provider == "claude_cli" and getattr(op, "reason", "") and verified == "verified":
                    alert.update(title="Switched terminals", message=op.reason)
                self.store.add_alert(alert)

    # ---- what the server asks for ----
    def history(self, account, since=None, limit=700):
        if account in contract.UNSAVED_IDS.values():
            return {"account": account, "readings": [], "snapshots": []}
        if not self.store.account(account):
            raise DeckError("That account no longer exists.", "not_found")
        return self.store.history(account, since=since, limit=limit)

    def manifest(self, op=None):
        directory = self.paths.data_dir / "logs" / "manifests"
        try:
            if op is not None:
                return Manifest.load(op, directory=directory) if op and set(op) <= MANIFEST_ID else None
            last = self.store.setting("last_manifest")
            found = Manifest.load(last, directory=directory) if isinstance(last, str) and set(last) <= MANIFEST_ID else None
            return found or Manifest.latest(directory=directory)
        except (OSError, ValueError):
            return None

    def diagnostics_tail(self, lines=200):
        return {"lines": diagnostics.tail(lines),
                "path": str(diagnostics.log_path() or self.paths.data_dir / "logs" / diagnostics.LOG_FILE),
                "health": self.health()}

    def stop(self):
        """Cancels what can be cancelled and stops every background thread; safe to call twice."""
        with self.lock:
            if self._stopped:
                return
            self._stopped = True
        self.stopping.set()
        if self.tray_handle is not None:
            try:
                self.tray_handle.stop()
            except Exception as exc:
                diagnostics.log_exception("menubar.stop_failed", exc)
        self.runner.stop(self.timings.lock_wait + self.timings.verify_hard + self.timings.graceful_wait + 30)
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(15)
        for thread in self._status_threads:
            thread.join(5)
        self.scheduler.shutdown()
        for close in self.closers:
            try:
                close()
            except Exception as exc:
                diagnostics.log_exception("engine.closer_failed", exc)
        self.store.close()




def muse_consumers(rows, names):
    """The contract consumers for Muse processes: a terminal session is "tui", `muse serve` "app_server_child"."""
    return [{"pid": row.pid, "kind": MUSE_CONSUMER_KINDS[row.kind], "parent": names.get(row.parent),
             "started": unix_time(row.ctime)} for row in rows if row.kind in MUSE_CONSUMER_KINDS]


def banner(kind, title, message, *, actions=(), provider=None, link=None):
    return {"kind": kind, "title": title, "message": message, "actions": list(actions), "provider": provider,
            "link": link}


def _server_names(path, key, fmt):
    """(present, [server names], error) of one config file: names only, never commands or secrets."""
    path = Path(path)
    if not path.is_file():
        return False, [], False
    try:
        if path.stat().st_size > MAX_SETUP_BYTES:
            return True, [], True
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text) if fmt == "toml" else json.loads(text)
        servers = data.get(key) if isinstance(data, dict) else None
        return True, [str(name)[:80] for name in servers][:50] if isinstance(servers, dict) else [], False
    except (OSError, ValueError):
        return True, [], True


def shared_setup(paths, claude_base):
    """The `setup` block: which MCP definitions the two apps share (names only)."""
    groups = []
    for label, path, key, fmt in (("Codex settings & MCPs", paths.codex_home / "config.toml", "mcp_servers", "toml"),
                                  ("Claude Code settings & MCPs", paths.claude_user_config, "mcpServers", "json"),
                                  ("Claude Chat local MCPs", Path(claude_base) / "claude_desktop_config.json",
                                   "mcpServers", "json")):
        present, servers, error = _server_names(path, key, fmt)
        groups.append({"label": label, "present": present, "servers": servers, "error": error})
    return {"message": SETUP_MESSAGE, "note": SETUP_NOTE, "groups": groups}


def build_engine(paths, *, owner_lock):
    """The live engine run.py serves."""
    return Engine(paths, owner_lock=owner_lock)
