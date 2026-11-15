"""User operations: switch, quit, add, capture, remove, recover and the move import run
one at a time on the `ad2-ops` thread and are observed as the PendingOperation and SwitchReport of
/api/state."""
from __future__ import annotations

import contextlib
import copy
from dataclasses import dataclass
import secrets
import threading
import time

from .core import DeckError
from .diagnostics import account_ref, frames, log


PHASES = ("queued", "planning", "waiting_idle", "stopping", "saving", "restoring", "verifying", "done", "unconfirmed",
          "failed", "cancelled", "needs_reconfirm")
TERMINAL_PHASES = frozenset({"done", "unconfirmed", "failed", "cancelled", "needs_reconfirm"})
STEP_STATUSES = ("pending", "active", "done", "skipped", "warning", "failed")
GENERIC_FAILURE = "The action could not be completed. Your saved accounts are preserved."
# The contract requires a non-empty pending message and report summary; these stand in until the
# operation supplies its own copy.
STARTING_MESSAGE = "Starting…"
DONE_SUMMARY = "Done."


def _report_defaults():
    """SwitchReport fields an Operation may fill through `report`, with their values when it
    does not. The runner sets op, kind, provider, account, label, finished, outcome and error."""
    return {"verified": None, "verified_at": None, "verify_reason": None, "summary": None, "manifest": None}


class OperationCancelled(DeckError):
    """Raised at a checkpoint once the user cancelled; the operation ends as `cancelled`. An
    operation that had to put something back first raises it again with its own message, e.g.
    "Cancelled. Claude was reopened with the same account." """

    def __init__(self, message="Cancelled. Nothing was changed."):
        super().__init__(message, "cancelled")


@dataclass
class Step:
    id: str
    label: str
    status: str = "pending"
    detail: str = ""
    started: float | None = None
    ended: float | None = None

    def snapshot(self):
        return {"id": self.id, "label": self.label, "status": self.status, "detail": self.detail,
                "started": self.started, "ended": self.ended}


class Operation:
    """One user operation. Subclasses implement run(), which executes on the `ad2-ops` thread and
    reports progress with step(), set_phase(), note() and set_waiting().

    Cancelling: while `cancellable`, OperationRunner.cancel() sets `cancel_event`; the request takes
    effect at the next checkpoint() (entering a step is one, and so is sleep()), so run() decides
    where stopping is safe. Once `cancellable` is False, cancel is refused as `busy`.

    run() ends by returning (outcome success; phase `unconfirmed` when report["verified"] is
    "unconfirmed", else `done`), raising OperationCancelled (cancelled), raising DeckError with code
    `needs_reconfirm` and `plan=<fresh SwitchPlan>` (the operation stays pending in that phase until
    another one is submitted or it is cancelled), or raising anything else (failed; an exception that
    is not a DeckError is reported with code `invalid` and GENERIC_FAILURE, never its own text).
    Fields placed in `report` (see _report_defaults) are merged into the SwitchReport; the summary
    defaults to the ending message ("Done." on success).

    `token` holds the ConsentToken of this job; it is never serialised."""

    not_cancellable_message = "The switch is past the point of no return."
    # True for an operation that may run next to another client's operation while that one only waits for
    # its sessions to close (OperationRunner.submit). Such a wait lasts until its confirmation runs out (30 minutes).
    beside_waits = False

    def __init__(self, kind, *, provider=None, account=None, label=None, consent=None, steps=(),
                 eta_seconds=None, token=None):
        self.kind, self.provider, self.account, self.label = kind, provider, account, label
        self.consent, self.eta_seconds, self.token = consent, eta_seconds, token
        self.steps = [Step(step_id, step_label) for step_id, step_label in steps]
        if len({step.id for step in self.steps}) != len(self.steps):
            raise ValueError("step ids must be unique")
        self.id = None
        self.started = None
        self.phase = "queued"
        self.message = STARTING_MESSAGE
        self.cancellable = True
        self.waiting_on = []
        self.current_step = None
        self.report = {}
        self.beside = False       # set by the runner: this operation runs next to a waiting one
        self.wait_over = False    # set by the runner once this operation leaves its wait: nothing starts beside it
        self.plan = None
        self.clock = time.time
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.done_event = threading.Event()
        self._notify = None
        self._leave_wait = None   # set by the runner: OperationRunner._leave_wait

    def run(self):
        raise NotImplementedError

    # ---- progress ----
    @contextlib.contextmanager
    def step(self, step_id, *, phase=None, message=None, cancellable=None, eta_seconds=None):
        """Run the block as step `step_id`: active, then done (or the warning/skipped status note()
        gave it), failed on an error, skipped when cancelled or reconfirming. Entering is a checkpoint."""
        self._moving_to(phase)
        self.checkpoint()
        step = self._step(step_id)
        with self.lock:
            self._apply(phase, message, cancellable, eta_seconds)
            step.status, step.started, step.ended = "active", self.clock(), None
            self.current_step = step.id
        self._changed()
        began = time.monotonic()
        try:
            yield step
        except BaseException as exc:
            status = "skipped" if getattr(exc, "code", None) in ("cancelled", "needs_reconfirm") else "failed"
            self._end_step(step, status, began, force=True)
            raise
        self._end_step(step, "done", began)

    def set_phase(self, phase, message=None, *, cancellable=None, eta_seconds=None):
        """Move to a running phase; the terminal phases are set when run() ends."""
        self._moving_to(phase)
        with self.lock:
            self._apply(phase, message, cancellable, eta_seconds)
        self._changed()

    def note(self, detail, *, status=None, step_id=None):
        """Set the detail line (and optionally the status) of a step, by default the active one."""
        if status is not None and status not in STEP_STATUSES:
            raise ValueError(f"unknown step status {status!r}")
        step = self._step(step_id or self.current_step)
        with self.lock:
            step.detail = detail
            if status is not None:
                step.status = status
        self._changed()

    def set_waiting(self, sessions):
        """What the operation waits for in `waiting_idle`: [{"pid", "name", "status"}]."""
        rows = [{"pid": row["pid"], "name": row["name"], "status": row["status"]} for row in sessions]
        with self.lock:
            if self.phase != "waiting_idle":
                raise ValueError("waiting_on is only shown in the waiting_idle phase")
            self.waiting_on = rows
        self._changed()

    def _moving_to(self, phase):
        """Before this operation leaves its wait for sessions and starts to change things, the operation
        running beside it ends, so only one operation writes at a time. That can take a while, so a cancel
        made meanwhile, and a confirmation that ran out meanwhile, stop this operation here."""
        if phase in (None, "waiting_idle") or self.phase != "waiting_idle" or self._leave_wait is None:
            return
        if self._leave_wait(self):
            self.checkpoint()
            if self.token is not None and time.time() >= self.token.expires:
                raise DeckError("Your confirmation ran out while another switch was finishing. Nothing was "
                                "changed. Switch again.", "stale_plan")

    # ---- cancellation ----
    def checkpoint(self):
        if self.cancel_event.is_set():
            raise OperationCancelled()

    def sleep(self, seconds):
        """Wait, ending early through OperationCancelled when the user cancels."""
        if self.cancel_event.wait(seconds):
            raise OperationCancelled()

    # ---- observation ----
    def snapshot(self):
        """This operation as the snapshot's `pending` shows it; `plan` is added while the phase is `needs_reconfirm`."""
        with self.lock:
            pending = {"id": self.id, "kind": self.kind, "provider": self.provider, "account": self.account,
                       "label": self.label, "phase": self.phase, "message": self.message,
                       "cancellable": self.cancellable, "started": self.started, "eta_seconds": self.eta_seconds,
                       "consent": self.consent, "waiting_on": [dict(row) for row in self.waiting_on],
                       "steps": [step.snapshot() for step in self.steps]}
            if self.phase == "needs_reconfirm":
                pending["plan"] = copy.deepcopy(self.plan)
        return pending

    def wait(self, timeout=None):
        return self.done_event.wait(timeout)

    # ---- internals ----
    def _apply(self, phase, message, cancellable, eta_seconds):
        if phase is not None and (phase not in PHASES or phase in TERMINAL_PHASES):
            raise ValueError(f"{phase!r} is not a running phase")
        if message is not None and (not isinstance(message, str) or not message):
            raise ValueError("the pending message must be a non-empty string")
        if phase is not None:
            self.phase = phase
            if phase == "waiting_idle":
                self.wait_over = False
            else:
                self.waiting_on = []
        if message is not None:
            self.message = message
        if cancellable is not None:
            self.cancellable = cancellable
        if eta_seconds is not None:
            self.eta_seconds = eta_seconds

    def _step(self, step_id):
        for step in self.steps:
            if step.id == step_id:
                return step
        raise ValueError(f"unknown step {step_id!r}")

    def _end_step(self, step, status, began, force=False):
        with self.lock:
            if force or step.status == "active":
                step.status = status
            step.ended = self.clock()
            if status == "done":
                self.current_step = None
        log("op.step", op=self.id, kind=self.kind, step=step.id, status=step.status,
             ms=round((time.monotonic() - began) * 1000))
        self._changed()

    def _finish(self, phase, message):
        with self.lock:
            self.phase, self.message, self.waiting_on = phase, message, []
            self.cancellable = phase == "needs_reconfirm"      # cancelling it just closes the prompt
            for step in self.steps:
                if step.status in ("pending", "active"):
                    step.status = "skipped"

    def _changed(self):
        if self._notify is not None:
            self._notify()


class OperationRunner:
    """Runs one Operation at a time on a thread named `ad2-ops`. One more may run beside it while it only
    waits for a client's sessions to close (phase `waiting_idle`): an operation of another client that sets
    `beside_waits`. The waiting operation stays the one shown as pending and keeps the report slot; the
    one beside it reports through its callbacks only. The waiting operation does not leave its wait until
    the one beside it has ended, so two operations never change logins at the same time.

    `on_change()` is called after every visible change (for the state `rev`), `on_settle(op)` after
    run() returned but before the operation is published as ended (so observers that see it finished
    also see what it changed, e.g. the re-sensed active account), `on_finish(op, report)` after an
    operation ended (`report` is None for `needs_reconfirm`); all three run outside every lock
    and their exceptions are logged, never propagated into an operation. `clock` stamps the
    timestamps shown to the user (the demo passes its virtual clock)."""

    def __init__(self, *, on_change=None, on_finish=None, on_settle=None, clock=time.time):
        self.on_change, self.on_finish, self.on_settle, self.clock = on_change, on_finish, on_settle, clock
        self.lock = threading.Lock()
        self._current = None      # the running operation, or one left pending in needs_reconfirm
        self._beside = None       # the operation running next to a waiting `_current`
        self._thread = self._beside_thread = None
        self._report = None
        self._closed = False

    @property
    def current(self):
        with self.lock:
            return self._current

    @property
    def busy(self):
        with self.lock:
            return self._running()

    @property
    def quiet(self):
        """True while no operation changes anything: none runs, or the only one still waits for a client's
        sessions to close. Such a wait lasts until its confirmation runs out (30 minutes), and the files it
        will change are untouched until then."""
        with self.lock:
            return self._lane(None, True) is not None

    def free_for(self, provider, *, beside_waits=False):
        """True when an operation of `provider` could start now (see `submit`)."""
        with self.lock:
            return self._lane(provider, beside_waits) is not None

    def unfinished(self, provider):
        """The operations of `provider` that have not ended: the one in the main slot and the one beside it.
        `pending` shows the main slot only, so a terminal switch beside another client's wait is not in it."""
        with self.lock:
            return [op for op in (self._current, self._beside)
                    if op is not None and op.provider == provider and not op.done_event.is_set()]

    def submit(self, op):
        """Start op and return its id; `busy` while another operation runs, unless op may run beside that
        one's wait (the class docstring). An operation left in `needs_reconfirm` is replaced, and the
        previous report is cleared."""
        if op.id is not None:
            raise ValueError("an operation can be submitted once")
        with self.lock:
            if self._closed:
                raise DeckError("AccountDeck is shutting down.", "busy")
            lane = self._lane(op.provider, op.beside_waits)
            if lane is None:
                raise DeckError("Finish or cancel the current action first.", "busy")
            op.id = f"op_{int(self.clock() * 1000):x}{secrets.token_hex(3)}"
            op.started, op.clock, op._notify = self.clock(), self.clock, self._changed
            op._leave_wait = self._leave_wait
            op.beside = lane == "beside"
            thread = threading.Thread(target=self._execute, args=(op,), name="ad2-ops", daemon=True)
            if op.beside:
                self._beside, self._beside_thread = op, thread
            else:
                self._current, self._thread, self._report = op, thread, None
        thread.start()
        self._changed()
        return op.id

    def cancel(self, op_id=None):
        """Ask the running operation to stop at its next checkpoint, or drop one left in
        `needs_reconfirm`. False when there is nothing to cancel; `busy` once the operation is past
        its last cancellation point."""
        with self.lock:
            op = self._current
            if op is None or op_id not in (None, op.id):
                return False
            if not op.done_event.is_set():
                with op.lock:
                    if not op.cancellable:
                        raise DeckError(op.not_cancellable_message, "busy")
                    op.cancel_event.set()
                return True
            self._current = None
        self._changed()
        return True

    def pending(self):
        with self.lock:
            op = self._current
        return op.snapshot() if op is not None else None

    def report(self):
        with self.lock:
            return copy.deepcopy(self._report)

    def snapshot(self):
        return {"pending": self.pending(), "report": self.report()}

    def dismiss_report(self):
        with self.lock:
            dismissed, self._report = self._report is not None, None
        if dismissed:
            self._changed()
        return dismissed

    def wait(self, timeout=None):
        """Wait until the current operation, one beside it and their callbacks have finished; False on
        timeout."""
        with self.lock:
            threads = [thread for thread in (self._thread, self._beside_thread) if thread is not None]
        for thread in threads:
            thread.join(timeout)
        return not any(thread.is_alive() for thread in threads)

    def stop(self, timeout=None):
        """Shutdown: refuse new operations, cancel what runs while it still can be, and wait."""
        with self.lock:
            self._closed = True
            running = [op for op in (self._current, self._beside) if op is not None and not op.done_event.is_set()]
        for op in running:
            with op.lock:
                if op.cancellable:
                    op.cancel_event.set()
        return self.wait(timeout)

    # ---- the ad2-ops threads ----
    def _running(self):
        return self._lane(None, False) is None

    def _leave_wait(self, op):
        """Called by the waiting operation `op` before it goes on: the operation beside it ends first
        (callbacks included), and from then on none starts beside it. Returns True when it had to wait."""
        waited = False
        while True:
            with self.lock:
                beside, thread = self._beside, self._beside_thread
                if beside is None or (beside.done_event.is_set() and not thread.is_alive()):
                    op.wait_over = True
                    return waited
            waited = True
            beside.done_event.wait()
            thread.join()

    def _lane(self, provider, beside_waits):
        """Where an operation of `provider` would run if submitted now: "main" when nothing runs, "beside"
        when the only thing running is another client's operation still in its `waiting_idle` wait and this
        one may run beside such a wait, else None. The caller holds the lock."""
        current, beside = self._current, self._beside
        if beside is not None and not beside.done_event.is_set():
            return None
        if current is None or current.done_event.is_set():
            return "main"
        if beside_waits and current.phase == "waiting_idle" and not current.wait_over and current.provider != provider:
            return "beside"
        return None

    def _execute(self, op):
        began = time.monotonic()
        log("op.start", op=op.id, kind=op.kind, acct=account_ref(op.account))
        failure = None
        try:
            op.set_phase("planning")
            op.run()
        except BaseException as exc:     # every failure ends the operation; nothing escapes this thread
            failure = exc
        code = failure.code if isinstance(failure, DeckError) else None
        error = None
        if failure is None:
            phase = "unconfirmed" if op.report.get("verified") == "unconfirmed" else "done"
            outcome, message = "success", op.report.get("summary") or DONE_SUMMARY
        elif code in ("cancelled", "needs_reconfirm"):
            phase, outcome, message = code, "cancelled", str(failure)
        else:
            phase, outcome = "failed", "failed"
            message = (str(failure) if code else "") or GENERIC_FAILURE
            # The contract has no code for "unexpected"; such a failure is reported like a
            # DeckError raised without a code. diagnostics.log keeps its exception type.
            error = {"code": code or "invalid", "message": message, "step": op.current_step}
        if phase == "needs_reconfirm":
            op.plan = failure.plan
        if self.on_settle is not None:      # still pending (not terminal) while the result is sensed
            self._callback(self.on_settle, op)
        op._finish(phase, message)
        report = None
        if phase != "needs_reconfirm":
            report = {**_report_defaults(), **copy.deepcopy(op.report), "op": op.id, "kind": op.kind,
                      "provider": op.provider, "account": op.account, "label": op.label,
                      "finished": self.clock(), "outcome": outcome, "error": error}
            report["summary"] = report["summary"] or message
        with self.lock:
            if not op.beside:
                if report is not None or op.cancel_event.is_set():    # a cancelled prompt does not linger
                    self._current = None
                if report is not None:
                    self._report = report
            op.done_event.set()
        ms = round((time.monotonic() - began) * 1000)
        if phase == "failed":
            log("op.fail", "warning", op=op.id, kind=op.kind, step=op.current_step, status=phase, ms=ms,
                 code=code, error_type=None if code else type(failure).__name__,
                 frames=None if code else frames(failure))
        else:
            log("op.done", op=op.id, kind=op.kind, status=phase, ms=ms)
        self._changed()
        if self.on_finish is not None:
            self._callback(self.on_finish, op, copy.deepcopy(report))

    def _changed(self):
        if self.on_change is not None:
            self._callback(self.on_change)

    @staticmethod
    def _callback(function, *args):
        try:
            function(*args)
        except Exception as exc:         # a listener's bug must not break the operation it observes
            log("op.callback_error", "warning", error_type=type(exc).__name__, frames=frames(exc))
