"""When to read each account's usage: the cadence table, reset-boundary reads, backoff,
the per-identity request governor, staggered refresh-all and the refresh worker pool.

The planning is pure (`base_interval`, `interval`, `RequestGovernor.delay`) and
driven by the engine's tick with an injectable clock; only `RefreshScheduler.tick` starts work, on a
pool of three `ad2-refresh` threads (or inline, for tests) with at most one Codex probe and one Muse
helper at a time, and never two reads of one account at once.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import random
import threading

from . import diagnostics

HOUR = 3600.0
ACTIVE, INACTIVE = 300.0, 900.0
READ_ONLY_INACTIVE = 1800.0          # Codex probes of inactive accounts while another AccountDeck 2 owns the sign-ins
HOT_RESET = 900.0                    # the active account is "hot" when its binding window resets this soon
BOUNDARY_DELAY, CONFIRM_DELAY = 45.0, 300.0
MAX_INTERVAL = 1800.0
BACKOFF_FACTOR = 1.5
JITTER = 0.10
STAGGER = 3.0
RESUME_DELAY = 30.0
PER_HOUR, SPACING, URGENT_SPACING = 20, 180.0, 60.0
URGENT_BURST = 4                     # urgent reads at URGENT_SPACING before pacing starts (per rolling hour)
WORKERS = 3


@dataclass(frozen=True)
class Target:
    """What the scheduler needs to know about one account."""
    id: str
    provider: str
    key: str                       # the identity; the governor counts (provider, key) (Codex accounts are not governed)
    active: bool = False
    hot: bool = False              # binding window critical/out or resetting within 15 min
    exhausted: bool = False        # every gating window is out
    resets: tuple = ()             # reset times of the last good reading (past ones too, until confirmed)
    paused_until: float | None = None
    read_only: bool = False
    level: str = "ok"              # the provider's status level (shown only: an incident never slows reads)


def base_interval(target: Target) -> float:
    if target.provider == "claude_cli":
        return 600.0 if target.exhausted or not target.active else 75.0 if target.hot else ACTIVE
    if target.active:
        return ACTIVE
    return READ_ONLY_INACTIVE if target.read_only else INACTIVE


def interval(target: Target, failures: int) -> float:
    """The cadence with backoff: ×1.5 per failed read (≤ 30 min). A status-page incident alone changes
    nothing (the owner's choice, 2026-10-09): incidents often name another product, and slower reads
    let an account run out between two of them."""
    base = base_interval(target)
    if failures:
        return min(base * BACKOFF_FACTOR ** failures, max(base, MAX_INTERVAL))
    return base


def governor_key(target: Target) -> tuple:
    """Each provider gets its own budget for an identity."""
    return (target.provider, target.key)


class RequestGovernor:
    """Per (provider, Claude identity): at most 20 requests an hour and at least 180 s between two. Urgent reads of
    the active account may come 60 s apart for a short burst, then are paced so the hour's budget is never spent:
    spending it early left a hot account unread for ~20 minutes, exactly when it ran out."""

    def __init__(self, *, per_hour=PER_HOUR, spacing=SPACING, urgent_spacing=URGENT_SPACING):
        self.per_hour, self.spacing, self.urgent_spacing = per_hour, spacing, urgent_spacing
        self._sent: dict[str, deque] = {}
        self._background_until = {}
        self._lock = threading.RLock()

    def delay(self, key, now, *, urgent=False) -> float:
        """Seconds until a request for `key` may go (0: now)."""
        with self._lock:
            sent = self._sent.get(key)
            if not sent:
                return 0.0
            while sent and sent[0] <= now - HOUR:
                sent.popleft()
            if not sent:
                return 0.0
            spacing = self.spacing
            if urgent:
                # After the burst, spread the rest of the hour: (20 - 4) reads an hour = one per 225 s.
                spacing = self.urgent_spacing if len(sent) < URGENT_BURST else \
                    max(self.urgent_spacing, HOUR / max(1, self.per_hour - URGENT_BURST))
            wait = sent[-1] + spacing - now
            if len(sent) >= self.per_hour:
                wait = max(wait, sent[0] + HOUR - now)
            return max(0.0, wait)

    def record(self, key, now) -> None:
        with self._lock:
            self._sent.setdefault(key, deque()).append(now)

    def reserve(self, key, now, count, *, paced=False, urgent=False, leave=0, hold=0):
        """Reserve a request batch atomically; background reads also respect the spacing."""
        with self._lock:
            if paced:
                wait = max(self.delay(key, now, urgent=urgent), self._background_until.get(key, 0) - now)
                if wait > 0:
                    return wait
            sent = self._sent.setdefault(key, deque())
            while sent and sent[0] <= now - HOUR:
                sent.popleft()
            excess = len(sent) + count + leave - self.per_hour
            wait = max(0.0, sent[excess - 1] + HOUR - now) if 0 < excess <= len(sent) else HOUR if excess > 0 else 0
            if leave:
                self._background_until[key] = now + wait + hold
            if wait:
                return wait
            sent.extend([now] * count)
            return 0.0


@dataclass
class _Plan:
    next: float
    failures: int = 0
    boundaries: dict = field(default_factory=dict)    # reset -> 1 (first read done) | 2 (confirmed)
    running: bool = False
    last: float | None = None


class RefreshScheduler:
    """`run(account_id)` reads one account and returns the outcome: "ok", "rate_limited",
    "outage" or any other failure kind (it must not raise). `done()` is called once the end of a read is
    noted here. The page shows whether a read is running from that note (`running`), and `run` has told the
    page of its reading before the note is made."""

    def __init__(self, run, *, workers=WORKERS, rng=random.random, governor=None, inline=False, done=None):
        self.run = run
        self.done = done
        self.rng = rng
        self.governor = governor or RequestGovernor()
        self.inline = inline
        self._plans: dict[str, _Plan] = {}
        self._lock = threading.RLock()
        self._helpers = {"codex": threading.Lock(), "muse": threading.Lock()}    # one helper process each
        self._helper_reads = set()           # the clients with a helper read started and not yet finished
        self._pool = None if inline else ThreadPoolExecutor(workers, thread_name_prefix="ad2-refresh")
        self._paused = False
        self.locks = {}
        self.provider_pause = {name: False for name in ("codex", "claude_cli", "muse")}

    # ---- planning ----
    def _schedule(self, target: Target, plan: _Plan, after: float) -> None:
        wait = interval(target, plan.failures) * (1 - JITTER + 2 * JITTER * self.rng())
        due = after + wait
        for reset in target.resets:
            stage = plan.boundaries.get(reset, 0)
            if stage == 0:
                due = min(due, reset + BOUNDARY_DELAY)
            elif stage == 1:
                due = min(due, reset + CONFIRM_DELAY)
        if target.paused_until is not None:
            due = max(due, target.paused_until)
        plan.next = due

    def due(self, targets, now) -> list[Target]:
        """The targets to read now (governor and pause permitting), and every other plan updated."""
        if self._paused:
            return []
        ready, picked = [], set()
        with self._lock:
            known = {t.id for t in targets}
            for key in [key for key in self._plans if key not in known]:
                del self._plans[key]
            for target in targets:
                if target.provider == "claude_cli" and not target.active and sum(
                        t.provider == "claude_cli" and not t.active for t in ready) >= 2:
                    continue
                if self.provider_pause[target.provider]:
                    continue
                plan = self._plans.get(target.id)
                if plan is None:
                    plan = self._plans[target.id] = _Plan(next=now)
                if plan.running:
                    continue
                if target.paused_until is not None and target.paused_until > now:
                    plan.next = max(plan.next, target.paused_until)
                if plan.next > now:
                    self._schedule_boundary_only(target, plan)
                    continue
                if target.provider == "claude_cli":
                    urgent = target.active and target.hot
                    gkey = governor_key(target)
                    wait = self.governor.delay(gkey, now, urgent=urgent)
                    if wait <= 0 and gkey in picked:      # another account of this identity starts in this tick
                        wait = self.governor.urgent_spacing if urgent else self.governor.spacing
                    if wait > 0:
                        plan.next = now + wait
                        continue
                    picked.add(gkey)
                ready.append(target)
        return ready

    def _schedule_boundary_only(self, target, plan):
        """A reset that appeared since the plan was made can pull the next read forward."""
        for reset in target.resets:
            stage = plan.boundaries.get(reset, 0)
            if stage == 0:
                plan.next = min(plan.next, reset + BOUNDARY_DELAY)
            elif stage == 1:
                plan.next = min(plan.next, reset + CONFIRM_DELAY)
        if target.paused_until is not None:
            plan.next = max(plan.next, target.paused_until)

    # ---- running ----
    def tick(self, targets, now, clock) -> list[str]:
        """Starts every due read; returns their account ids. `clock` dates each completion. A Codex or Muse
        read is not started while another read of the same client runs: it stays due for the next tick, so
        it never holds a worker while it waits for the helper."""
        started = []
        for target in self.due(targets, now):
            with self._lock:
                plan = self._plans[target.id]
                if plan.running or target.provider in self._helper_reads:
                    continue
                if target.provider == "claude_cli":
                    wait = self.governor.reserve(governor_key(target), now, 1,
                                                 paced=True, urgent=target.active and target.hot)
                    if wait:
                        plan.next = now + wait
                        continue
                plan.running = True
                if target.provider in self._helpers:
                    self._helper_reads.add(target.provider)
            started.append(target.id)
            if self.inline:
                self._read(target, clock)
                continue
            try:
                self._pool.submit(self._read, target, clock)
            except RuntimeError:       # the pool shut down: never leave the plan marked running
                with self._lock:
                    plan.running = False
                    self._helper_reads.discard(target.provider)
                started.pop()
        return started

    def _read(self, target, clock):
        outcome = "network"
        try:
            if self.provider_pause[target.provider]:   # its switch may hold the lock: never park a worker on it
                outcome = "skipped"
                return
            with self.account_lock(target.id):
                if self.provider_pause[target.provider]:
                    outcome = "skipped"
                elif target.provider in self._helpers:
                    with self._helpers[target.provider]:
                        # A switch may have paused this provider while the read waited for the lock.
                        outcome = "skipped" if self.provider_pause[target.provider] else self.run(target.id)
                else:
                    outcome = self.run(target.id)
        except Exception as exc:     # a read must never kill the scheduler
            diagnostics.log_exception("refresh.crash", exc, lvl="error", provider=target.provider,
                                      acct=diagnostics.account_ref(target.id))
        finally:
            self.completed(target, outcome, clock())
            if self.done is not None:
                self.done()

    def completed(self, target: Target, outcome: str, now: float) -> None:
        with self._lock:
            plan = self._plans.setdefault(target.id, _Plan(next=now))
            plan.running, plan.last = False, now
            self._helper_reads.discard(target.provider)
            if outcome == "ok":
                plan.failures = 0
            elif outcome not in ("rate_limited", "skipped"):
                plan.failures = min(plan.failures + 1, 8)
            if outcome not in ("rate_limited", "skipped"):      # these gave no reading, so the boundary read is still owed
                for reset in target.resets:
                    if now >= reset + CONFIRM_DELAY:
                        plan.boundaries[reset] = 2
                    elif now >= reset + BOUNDARY_DELAY:
                        plan.boundaries[reset] = max(plan.boundaries.get(reset, 0), 1)
            plan.boundaries = {r: s for r, s in plan.boundaries.items() if r in target.resets}
            self._schedule(target, plan, now)

    # ---- requests from the engine ----
    def request(self, account_id, now) -> None:
        """Read this account at the next tick (the governor still applies)."""
        with self._lock:
            plan = self._plans.setdefault(account_id, _Plan(next=now))
            plan.next = min(plan.next, now)

    def request_all(self, account_ids, now) -> None:
        """Refresh-all: 3 s apart, in the given order."""
        with self._lock:
            for index, account_id in enumerate(account_ids):
                due = now + index * STAGGER
                plan = self._plans.setdefault(account_id, _Plan(next=due))
                plan.next = due

    def defer(self, account_id, due) -> None:
        """Nothing is read for this account before `due` (the demo uses it so seeded readings stay)."""
        with self._lock:
            plan = self._plans.setdefault(account_id, _Plan(next=due))
            plan.next = due

    def resume(self, now) -> None:
        """After a resume from sleep nothing is read for 30 s."""
        with self._lock:
            for plan in self._plans.values():
                plan.next = max(plan.next, now + RESUME_DELAY)

    def planned(self, account_id):
        with self._lock:
            plan = self._plans.get(account_id)
            return None if plan is None else plan.next

    def running(self, account_id) -> bool:
        with self._lock:
            plan = self._plans.get(account_id)
            return bool(plan and plan.running)

    def forget(self, account_id) -> None:
        with self._lock:
            self._plans.pop(account_id, None)

    def helper_lock(self, provider):
        """The lock every read of `provider` ("codex", "muse") holds while its helper runs. A switch holds it
        while it changes the provider's files, so no read that started earlier runs across the change."""
        return self._helpers[provider]

    def account_lock(self, account_id):
        """The account's lock: a read holds it while it runs, and saving or removing the account waits for
        it. Reentrant, so the operation that holds it may save the account itself."""
        with self._lock:
            return self.locks.setdefault(account_id, threading.RLock())

    def pause(self, provider=True) -> None:
        """Pauses reads of one provider (a name) or of everything (a bool, the `refresh_paused` setting)."""
        if isinstance(provider, str):
            self.provider_pause[provider] = True
        else:
            self._paused = bool(provider)

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)

    def resume_provider(self, provider):
        self.provider_pause[provider] = False

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
