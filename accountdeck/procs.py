"""The only module that enumerates, signals, terminates, launches or activates processes.

Three files make up process control on the Mac: this one (rows, classification, consent, the test-ownership
registry, the self-guarded spawn, the controller interface and the synthetic `FakeProcessController`),
`platform/libproc.py` (read-only libSystem facts: pids, paths, start times, descriptors) and
`platform/procs_mac.py` (the live controller, the one place that sends a signal).

Every mutating path runs the interlock in this order:

1. safe mode: the target must be test-owned (registered by a test or spawned by this process);
2. the consent token must cover the target (`consent_refusal`);
3. the target is re-verified behind a kqueue NOTE_EXIT watch, which pins the process the way a handle
   would: start time, executable path and bundle id are re-read and must match what was consented to;
4. the syscall.

The only process AccountDeck ever signals is a Codex session the user asked it to stop (`stop_consumer`).

The self-guarding wrappers (`_popen` here, `_signal` in procs_mac) repeat step 1 themselves, so a bug higher
up cannot reach a real process or app from a test or a demo. A row whose
identity cannot be read (start time 0, bundle "?") never passes step 3: unknown identity fails closed.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from . import config, diagnostics
from .config import SafetyViolation
from .core import DeckError
from .platform import libproc

__all__ = [
    "SafetyViolation", "ProcessInfo", "ProcessSet", "ConsentToken", "KINDS", "classify",
    "consent_refusal", "filetime", "unix_time", "register_test_root", "register_owned_pid",
    "is_test_owned_path", "ProcessController",
    "FakeProcessController", "get_controller", "run_security", "PIPE", "DEVNULL",
]

KINDS = ("codex_tui", "codex_daemon", "codex_daemon_updater", "codex_app_server_child", "codex_desktop",
         "muse_session", "muse_host", "other")
CONSUMER_KINDS = frozenset({"codex_tui", "codex_app_server_child", "codex_desktop"})
MUSE_KINDS = frozenset({"muse_session", "muse_host"})     # every Muse process uses the Muse login
STOPPABLE_CONSUMER_KINDS = frozenset({"codex_tui", "codex_app_server_child"})

# ---- what this Mac's processes are called, compared lower-case ----
# ChatGPT.app (bundle com.openai.codex) is the Codex app: everything inside that bundle, its own codex
# app-server included, is the Codex Desktop consumer and is never stopped. An app's updater (ShipIt) and
# its browser and service helpers are never a session, and stopping an updater mid-update could break the app.
CODEX_NAMES = frozenset({"codex"})
CODEX_APP_NAMES = frozenset({"chatgpt"})
CODEX_APP_BUNDLE_IDS = frozenset({"com.openai.codex"})
NOT_A_SESSION_NAMES = frozenset({"chrome-native-host", "cowork-svc", "shipit"})
PROTECTED_NAMES = frozenset({"chrome-native-host", "cowork-svc", "chatgpt", "shipit"})   # never signalled
# Muse Code runs as ~/.local/bin/muse-bin-<version> (its launcher `exec`s it), so such a row is "muse".
MUSE_NAMES = frozenset({"muse"})
CMDLINE_NAMES = frozenset({"codex", "chatgpt", "muse"})    # the only rows whose argv is read
INSPECTED_NAMES = CODEX_NAMES | CODEX_APP_NAMES | MUSE_NAMES
# The Claude Code that Claude Desktop bundles: ~/Library/Application Support/Claude/claude-code/<version>/claude.
# It is never the terminal's Claude Code (find_claude_cli).
CODE_CHILD_PATH = re.compile(r"/claude/claude-code/[^/]+/claude$", re.IGNORECASE)
CODEX_APP_PATH = re.compile(r"(?:^|/)ChatGPT\.app/Contents/", re.IGNORECASE)   # if its plist is unreadable
MUSE_BIN_PATH = re.compile(r"/muse-bin-[^/]+$")     # Muse Code's binary, whatever its version string

# META_API_KEY takes priority over Muse's account login, so no helper may inherit it.
API_KEY_NAMES = ("OPENAI_API_KEY", "CODEX_API_KEY", "META_API_KEY")
PIPE, DEVNULL = subprocess.PIPE, subprocess.DEVNULL   # spawn() stdio for callers, which may not name subprocess

FILETIME_EPOCH = 11644473600          # seconds from 1601-01-01 (FILETIME) to 1970-01-01 (Unix)
DAEMON_STOP_MARGIN = 15.0             # beyond shutdownGraceSeconds for `codex app-server daemon stop` itself
DAEMON_EXIT_WAIT = 10.0
DAEMON_START_WAIT = 30.0
REMEMBERED_LIMIT = 256
HELPER_MARK = ".helper"         # in a helper's private folder: "<pid> <start time>" of the process using it

CONSENT_MESSAGE = "AccountDeck did not get permission to stop this process, so it left it alone. Nothing was changed."
CHANGED_MESSAGE = "The process changed since you confirmed, so AccountDeck left it alone. Nothing was changed."
SAFE_MODE_MESSAGE = "Safe mode: AccountDeck only acts on processes, apps and folders a test or demo owns."


def filetime(seconds: float) -> int:
    """Unix seconds in FILETIME units (100 ns intervals since 1601), the unit of ProcessInfo.ctime."""
    return int((seconds + FILETIME_EPOCH) * 10_000_000)


def unix_time(ctime: int | None) -> float | None:
    """A ProcessInfo.ctime as Unix seconds; None when the start time is unknown (0 or None)."""
    return ctime / 10_000_000 - FILETIME_EPOCH if ctime else None


def process_name(path: str | None, fallback: str = "") -> str:
    """A row's name: the executable's file name, "muse" for Muse Code's versioned binary."""
    if not path:
        return fallback
    if MUSE_BIN_PATH.search(path):
        return "muse"
    return path.rpartition("/")[2] or fallback


def is_code_child_path(path) -> bool:
    """True for Claude Desktop's bundled Claude Code (…/Claude/claude-code/<version>/claude)."""
    return bool(path) and CODE_CHILD_PATH.search(str(path)) is not None


def mark_helper_home(folder, pid, ctime) -> None:
    """Notes in `folder` that process `pid`, started at `ctime`, is using it. A note that cannot be written
    is only logged: the helper already runs."""
    if not ctime:
        return
    try:
        (Path(folder) / HELPER_MARK).write_text(f"{pid} {ctime}")
    except OSError as exc:
        diagnostics.log("procs.helper_mark_failed", "warning", error=type(exc).__name__)


def helper_running(folder) -> bool:
    """True while the helper noted in `folder` still runs: the same pid, started at the same moment. False
    when the folder has no note."""
    try:
        pid, ctime = (int(part) for part in (Path(folder) / HELPER_MARK).read_text().split())
    except (OSError, ValueError):
        return False
    return libproc.creation_time(pid) == ctime


def _log(ev: str, **fields) -> None:
    diagnostics.log(ev, **fields)


def _refusal(reason: str, message: str) -> SafetyViolation:
    """A SafetyViolation carrying a machine-readable `reason`; logged as proc.refused."""
    _log("proc.refused", reason=reason)
    error = SafetyViolation(message)
    error.reason = reason
    return error


# ---- process rows, sets and consent ----

@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    parent: int
    name: str
    path: str | None
    ctime: int                      # start time in FILETIME units (100 ns); 0 if unknown
    family: str | None = None       # bundle id of the enclosing .app; None = none; "?" = could not be read
    cmdline: str | None = None      # read only for Codex/ChatGPT rows
    kind: str = "other"             # one of KINDS; set by classify()


@dataclass(frozen=True)
class ProcessSet:
    """A verified (pid, creation time) snapshot that plans and consents bind to."""
    pairs: tuple[tuple[int, int], ...]

    def __post_init__(self):
        object.__setattr__(self, "pairs", tuple(sorted({(int(pid), int(ctime)) for pid, ctime in self.pairs})))

    @classmethod
    def of(cls, rows) -> "ProcessSet":
        return cls(tuple((row.pid, row.ctime) for row in rows))

    def signature(self) -> str:
        text = ";".join(f"{pid}:{ctime}" for pid, ctime in self.pairs)
        return "sha256:" + hashlib.sha256(text.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class ConsentToken:
    """What the user confirmed: the process pairs one job may signal. Never serialised."""
    job_id: str
    provider: str
    purpose: str
    account: str | None
    mode: str                                # "idle" | "now"
    pairs: tuple[tuple[int, int], ...]
    issued: float
    expires: float


def consent_refusal(token: ConsentToken | None, target: ProcessInfo, *, now: float,
                    protected=frozenset()) -> str | None:
    """The consent rules for stopping one Codex session, the only thing AccountDeck ever signals. Returns the
    refusal reason, or None when the token allows it: a session of a kind that may be stopped, that the
    user's confirmation named by pid and start time."""
    if token is None:
        return "no_consent"
    if now >= token.expires:
        return "expired"
    if target.name.lower() in PROTECTED_NAMES:
        return "protected"
    if target.pid in protected:
        return "self"
    if target.kind not in STOPPABLE_CONSUMER_KINDS:
        return "kind"
    return None if (target.pid, target.ctime) in token.pairs else "not_covered"


# ---- classification: pure over rows, so synthetic tables drive every branch ----

def classify(rows, paths) -> dict[str, list[ProcessInfo]]:
    """Every row with its kind, grouped by kind (all KINDS present as keys)."""
    grouped = {kind: [] for kind in KINDS}
    for row in with_kinds(rows, paths):
        grouped[row.kind].append(row)
    return grouped


def with_kinds(rows, paths) -> list[ProcessInfo]:
    """`rows` with their `kind` set."""
    return [replace(row, kind=_kind(row, paths)) for row in rows]


def _kind(row: ProcessInfo, paths) -> str:
    name = row.name.lower()
    if name in NOT_A_SESSION_NAMES:
        return "other"
    if row.path and CODEX_APP_PATH.search(str(row.path)):
        return "codex_desktop"
    if row.family not in (None, "?") and (row.family in paths.codex_families or row.family in CODEX_APP_BUNDLE_IDS):
        return "codex_desktop"
    if name in CODEX_NAMES:
        command = row.cmdline or ""
        if "app-server" not in command:
            return "codex_tui"
        if "daemon pid-update-loop" in command:
            return "codex_daemon_updater"
        if "--listen unix://" in command or "--managed-daemon" in command:
            return "codex_daemon"
        return "codex_app_server_child"
    if name in CODEX_APP_NAMES and row.family == "?":
        return "codex_desktop"   # cannot tell whether it is the Codex app: count it as a consumer
    if name in MUSE_NAMES and MUSE_BIN_PATH.search(str(row.path or "")):
        # Only Muse Code's own binary counts: another program named muse (a Mac app, a Homebrew tool) is not it.
        # `muse serve` is a session host an editor or app keeps running; anything else is a terminal session.
        try:
            words = shlex.split(row.cmdline or "")
        except ValueError:
            words = []
        return "muse_host" if len(words) > 1 and words[1] == "serve" else "muse_session"
    return "other"


# ---- test-ownership registry (process-wide; only tests and the demo call these) ----

_registry_lock = threading.Lock()
_test_roots: list[str] = []                               # normalised absolute paths
_owned_pids: dict[int, tuple[int, str | None]] = {}       # pid -> (ctime, family attribution)
_spawned: dict[int, int] = {}                             # safe mode: pid -> ctime of every child we started


def _norm(path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _require_safe_mode(what: str) -> None:
    if not config.safe_mode():
        raise _refusal("registry", f"{what} is only available to tests and the demo.")


def register_test_root(path) -> None:
    """Marks a temporary directory as test-owned: executables, folders and targets under it may be used."""
    _require_safe_mode("register_test_root")
    root = _norm(path)
    if not root.startswith(_norm(tempfile.gettempdir()) + os.sep):
        raise _refusal("test_root", "Test roots must be temporary directories.")
    with _registry_lock:
        if root not in _test_roots:
            _test_roots.append(root)


def register_owned_pid(pid: int, ctime: int, *, family: str | None = None) -> None:
    """Marks a process this test started (directly or through a process it started) as test-owned.
    `family` lets such a process stand in for an app bundle, e.g. one of `paths.codex_families`."""
    _require_safe_mode("register_owned_pid")
    if not ctime or libproc.creation_time(pid) != ctime or not libproc.descends_from_self(pid, ctime):
        raise _refusal("not_descendant", "Only processes started by this test can be registered.")
    with _registry_lock:
        _owned_pids[pid] = (ctime, family)


def is_test_owned_path(path) -> bool:
    if not path:
        return False
    target = _norm(path)
    with _registry_lock:
        roots = tuple(_test_roots)
    return any(target == root or target.startswith(root + os.sep) for root in roots)


def _is_owned_pid(pid: int, ctime: int | None) -> bool:
    if not ctime:
        return False
    with _registry_lock:
        registered = _owned_pids.get(pid)
        return (registered is not None and registered[0] == ctime) or _spawned.get(pid) == ctime


def _is_registered(pid: int) -> bool:
    with _registry_lock:
        return pid in _owned_pids or pid in _spawned


def _family_attribution(pid: int, ctime: int | None) -> str | None:
    with _registry_lock:
        registered = _owned_pids.get(pid)
    return registered[1] if registered is not None and registered[0] == ctime else None


def _check_spawn_target(executable: str, *, keychain: str | None = None) -> None:
    """Safe-mode spawn policy (ORCH-1): only this Python or an executable under a test root, and
    /usr/bin/security only for a command whose keychain file (`keychain`, checked by run_security to be
    the command's trailing argument) is a test's own, never the login keychain."""
    if not config.safe_mode():
        return
    if executable == SECURITY_TOOL and keychain is not None and _is_test_keychain(keychain):
        return
    name = os.path.basename(executable) or executable
    if os.path.isabs(executable) and os.path.isfile(executable):
        try:
            if os.path.samefile(executable, sys.executable):
                return
        except OSError:
            pass
        if is_test_owned_path(executable):
            return
    raise _refusal("spawn", f"Safe mode refuses to start {name[:60]}: only this Python or a test's own "
                            "executables may run.")


# ---- the self-guarding wrappers: the last line before a launch or a request ----

def _popen(argv: list[str], *, keychain: str | None = None, **options) -> subprocess.Popen:
    _check_spawn_target(argv[0], keychain=keychain)
    process = subprocess.Popen(argv, **options)
    if config.safe_mode():
        with _registry_lock:
            _spawned[process.pid] = libproc.creation_time(process.pid) or 0
    return process


# /usr/bin/security for platform/keychain.py: only the three
# generic-password commands, which never touch the keychain search list or the default keychain.
SECURITY_TOOL = "/usr/bin/security"
_SECURITY_COMMANDS = ("find-generic-password", "add-generic-password", "delete-generic-password")
_LOGIN_KEYCHAIN = re.compile(r"login\.keychain(?:-db)?", re.IGNORECASE)


def _is_test_keychain(keychain: str) -> bool:
    return (os.path.isabs(keychain) and not _LOGIN_KEYCHAIN.fullmatch(os.path.basename(keychain))
            and is_test_owned_path(keychain))


def run_security(args: list[str], *, keychain=None, stdin: bytes | None = None,
                 timeout: float = 30.0) -> tuple[int, bytes, bytes]:
    """Runs one /usr/bin/security generic-password command and returns (exit code, stdout, stderr).

    `args` is either the command itself (`find-generic-password …`) or `["-i"]` with the command as one
    line on `stdin`; a value goes on stdin unless it is too long for one line (platform/keychain.py
    `_set_long`, which passes it as `-X` hex, as Claude Code does). `keychain` is the keychain file the command names
    as its last argument (for `-i`, the last token of the line), or None for the user's default keychain;
    safe mode allows only a test's own keychain file. On timeout (an access dialog nobody answers) the
    child is killed and TimeoutError raised. Callers never log `stdin`, stdout or stderr."""
    argv = [SECURITY_TOOL, *[os.fspath(arg) for arg in args]]
    path = None if keychain is None else os.fspath(keychain)
    if argv[1:] == ["-i"]:
        line = (stdin or b"").decode("ascii", "replace")
        command = line[:-1] if line.endswith("\n") else line
        named = path is None or command.endswith(' "' + path.replace("\\", "\\\\").replace('"', '\\"') + '"')
        valid = "\n" not in command and "\r" not in command and command.split(" ", 1)[0] in _SECURITY_COMMANDS
    else:
        named = path is None or argv[-1] == path
        valid = stdin is None and len(argv) > 2 and argv[1] in _SECURITY_COMMANDS
    if not (valid and named):
        raise _refusal("spawn", "Only one generic-password keychain command naming its keychain file may run.")
    process = _popen(argv, keychain=path, stdin=PIPE if stdin is not None else DEVNULL, stdout=PIPE,
                     stderr=PIPE)
    try:
        out, err = process.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()                      # this call's own child, still unreaped: its pid cannot be reused
        process.communicate()
        raise TimeoutError("security did not finish in time") from None
    return process.returncode, out or b"", err or b""


# ---- controllers ----

class ProcessController(abc.ABC):
    """Process control for one AccountDeck instance. Read-only methods may run on any thread;
    mutating methods refuse unless the interlock (module docstring) passes."""

    def __init__(self, paths, timings=None):
        self.paths = paths
        self.timings = timings or config.Timings()
        self.owned: dict[int, int] = {}       # pid -> ctime of processes this instance spawned
        self.helpers: dict[int, int] = {}     # spawn_helper() children: excluded from codex_consumers()

    # ---- read-only ----
    @abc.abstractmethod
    def snapshot(self) -> list[ProcessInfo]:
        """The inspected rows with their kinds set."""

    def classify(self, rows) -> dict[str, list[ProcessInfo]]:
        return classify(rows, self.paths)

    def codex_consumers(self, rows=None) -> list[ProcessInfo]:
        """Processes using the Codex login (in `rows`, a snapshot the caller already took, or a fresh one);
        AccountDeck's own helpers (and their children) do not count, but terminals it opened do."""
        rows = [row for row in (self.snapshot() if rows is None else rows) if row.kind in CONSUMER_KINDS]
        excluded = self._helper_tree() if rows else set()
        return [row for row in rows if (row.pid, row.ctime) not in excluded]

    def muse_consumers(self, rows=None) -> list[ProcessInfo]:
        """Muse processes using the Muse login (in `rows`, or a fresh snapshot); AccountDeck's own Muse probes
        and sign-ins (spawn_helper children) do not count."""
        rows = [row for row in (self.snapshot() if rows is None else rows) if row.kind in MUSE_KINDS]
        excluded = self._helper_tree() if rows else set()
        return [row for row in rows if (row.pid, row.ctime) not in excluded]

    def codex_daemon(self) -> ProcessInfo | None:
        daemons = self.classify(self.snapshot())["codex_daemon"]
        return daemons[0] if daemons else None

    @abc.abstractmethod
    def creation_time(self, pid: int) -> int | None: ...

    @abc.abstractmethod
    def ancestors_of_self(self) -> list[ProcessInfo]: ...

    def wait_until(self, predicate, timeout: float, interval: float = 0.3, cancel=None) -> bool:
        """Polls `predicate` until it holds (True) or `timeout` passes or `cancel` is set (False)."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if predicate():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            pause = min(interval, remaining)
            if cancel is not None:
                if cancel.wait(pause):
                    return False
            else:
                time.sleep(pause)

    # ---- spawn ----
    def spawn(self, args, *, cwd=None, env=None, stdin=None, stdout=None, stderr=None, text=None, encoding=None,
              strip_api_keys=False, remove_env=()) -> subprocess.Popen:
        """Starts a process. `env` holds overrides merged onto this process's environment, after the names in
        `remove_env` (and with `strip_api_keys` the API keys) were dropped from it. In safe mode only this
        Python or an executable under a test root may start (checked before anything else)."""
        argv = [os.fspath(arg) for arg in args]
        if not argv:
            raise DeckError("There is nothing to start.", "invalid")
        _check_spawn_target(argv[0])
        environment = None
        if env or strip_api_keys or remove_env:
            environment = config.child_env(remove=(API_KEY_NAMES if strip_api_keys else ()) + tuple(remove_env),
                                           **{str(key): str(value) for key, value in (env or {}).items()})
        process = _popen(argv, cwd=cwd, env=environment, stdin=stdin, stdout=stdout, stderr=stderr, text=text,
                         encoding=encoding)
        self._remember(self.owned, process.pid)
        return process

    def spawn_helper(self, args, *, home=None, **options) -> subprocess.Popen:
        """A background helper (a Codex app-server, Muse) without API keys in its environment. `home` is the
        private folder it was handed a login in: the process is noted there, so the cleanup at the next
        start (workfolders.sweep) leaves the folder alone while this helper still runs."""
        options.update(strip_api_keys=True)
        process = self.spawn(args, **options)
        self._remember(self.helpers, process.pid)
        if home is not None:
            mark_helper_home(home, process.pid, self.helpers.get(process.pid))
        return process

    def find_claude_cli(self):
        """Claude Code on PATH; Claude Desktop's bundled copy never counts."""
        found = shutil.which("claude")
        if not found or is_code_child_path(found) or "/claude/claude-code/" in found.lower():
            return None
        path = Path(found)
        if config.safe_mode() and not is_test_owned_path(path):
            raise SafetyViolation("Only a synthetic Claude Code executable may be used in safe mode.")
        return path

    def cli_enrollment_exited(self, process):
        # Popen.poll asks about this exact child, which cannot be reaped (and its pid reused) behind our back.
        return process.poll() is not None

    def cli_enrollment_token(self, process):
        return {"pid": process.pid, "ctime": self.owned.get(process.pid)}

    def _remember(self, registry: dict, pid: int) -> None:
        registry[pid] = libproc.creation_time(pid) or 0
        if len(registry) > REMEMBERED_LIMIT:
            for stale in [p for p, ctime in registry.items() if libproc.creation_time(p) != ctime]:
                del registry[stale]

    # ---- interlocked, consent-bound ----
    @abc.abstractmethod
    def stop_consumer(self, token: ConsentToken, target: ProcessInfo) -> bool:
        """Stops one Codex session the user's confirmation named. False when it had already exited."""

    def _interlock(self, token, target: ProcessInfo) -> None:
        """Interlock steps 1 and 2: test ownership in safe mode, then the consent rules."""
        self._require_test_owned(target)
        reason = consent_refusal(token, target, now=time.time(), protected=self._protected_pids())
        if reason:
            raise _refusal(reason, CONSENT_MESSAGE)

    @abc.abstractmethod
    def _require_test_owned(self, target: ProcessInfo) -> None: ...

    def _protected_pids(self) -> set[int]:
        return {os.getpid()} | {row.pid for row in self.ancestors_of_self()}

    def _helper_tree(self) -> set[tuple[int, int]]:
        return set()

    # ---- interlocked, launch ----
    @abc.abstractmethod
    def launch_codex(self, codex_exe, home, cwd, session=None) -> None: ...

    @abc.abstractmethod
    def codex_daemon_stop(self, codex_exe, home, grace_s: float) -> str: ...

    @abc.abstractmethod
    def codex_daemon_start(self, codex_exe, home) -> bool: ...

    @abc.abstractmethod
    def open_terminal(self, cwd, env) -> None: ...

class FakeProcessController(ProcessController):
    """An in-memory Mac process table for tests and the demo. Nothing it does reaches a real process:
    spawn() is the real, safe-mode-gated implementation; everything else acts on the table, with the live
    controller's rules (consent, re-verification). Scripted behaviour:

    ancestors          rows ancestors_of_self() returns (self-protection tests)
    unkillable         pids that survive stop_consumer()
    daemon_stops       False: codex_daemon_stop() times out and the daemon stays

    Every call is recorded in `.calls`."""

    CODEX_VERSION = "0.161.0"

    def __init__(self, paths, rows=(), *, timings=None, ancestors=(), unkillable=(), daemon_stops: bool = True):
        super().__init__(paths, timings)
        self.ancestors = list(ancestors)
        self.unkillable = set(unkillable)
        self.daemon_stops = daemon_stops
        self.calls: list[tuple] = []
        self._table: dict[int, ProcessInfo] = {}
        self._lock = threading.RLock()
        self._next_pid = 20000
        self._last_ctime = 0
        for row in rows:
            self._table[row.pid] = replace(row, kind="other")

    # ---- building the table ----
    def add_row(self, name: str, *, path=None, family=None, parent: int = 1, cmdline=None,
                pid: int | None = None) -> ProcessInfo:
        with self._lock:
            if pid is None:
                pid = self._next_pid
            self._next_pid = max(self._next_pid, pid) + 4
            self._last_ctime = max(self._last_ctime + 1, filetime(time.time()))
            row = ProcessInfo(pid, parent, name, None if path is None else str(path), self._last_ctime, family,
                              cmdline)
            self._table[pid] = row
            return row

    def _applications(self) -> Path:
        return Path(self.paths.home) / "Applications"

    MUSE_VERSION = "1.4.13-R6996.1"

    def add_muse(self, kind: str = "muse_session", *, parent: int = 1) -> ProcessInfo:
        """A Muse Code process of the given kind (muse_session: a terminal session; muse_host: `muse serve`)."""
        exe = Path(self.paths.home) / ".local" / "bin" / f"muse-bin-{self.MUSE_VERSION}"
        arguments = {"muse_session": [], "muse_host": ["serve"]}[kind]
        return self.add_row("muse", path=exe, parent=parent, cmdline=shlex.join([str(exe), *arguments]))

    def add_codex(self, kind: str = "codex_tui", *, parent: int = 1) -> ProcessInfo:
        """A Codex process of the given kind (codex_tui, codex_daemon, codex_daemon_updater,
        codex_app_server_child, codex_desktop)."""
        if kind == "codex_desktop":
            family = self.paths.codex_families[0] if self.paths.codex_families else min(CODEX_APP_BUNDLE_IDS)
            return self.add_row("ChatGPT", path=self._applications() / "ChatGPT.app" / "Contents" / "MacOS" / "ChatGPT",
                                family=family, parent=parent)
        exe = Path(self.paths.home) / ".local" / "share" / "openai-codex" / self.CODEX_VERSION / "bin" / "codex"
        arguments = {"codex_tui": [], "codex_daemon": ["app-server", "--listen", "unix://", "--managed-daemon"],
                     "codex_daemon_updater": ["app-server", "daemon", "pid-update-loop"],
                     "codex_app_server_child": ["app-server"]}
        return self.add_row("codex", path=exe, parent=parent, cmdline=shlex.join([str(exe), *arguments[kind]]))

    def exit_process(self, pid: int) -> None:
        """The process exits by itself."""
        with self._lock:
            self._exit(pid)

    # ---- internals ----
    def _exit(self, pid: int) -> None:
        self._table.pop(pid, None)

    def _rows(self) -> list[ProcessInfo]:
        return with_kinds(self._table.values(), self.paths)

    def _verify(self, target: ProcessInfo) -> bool:
        """Interlock step 3 against the table: False when the process is gone."""
        row = self._table.get(target.pid)
        if row is None:
            return False
        if not target.ctime or row.ctime != target.ctime:
            raise _refusal("ctime_mismatch", CHANGED_MESSAGE)
        if not target.path or row.path != target.path:
            raise _refusal("path_mismatch", CHANGED_MESSAGE)
        if row.family == "?" or row.family != target.family:
            raise _refusal("family_mismatch", CHANGED_MESSAGE)
        return True

    def _require_test_owned(self, target: ProcessInfo) -> None:
        """The table is not the machine, so there is no real process to protect."""

    def _protected_pids(self) -> set[int]:
        return {row.pid for row in self.ancestors}

    # ---- read-only ----
    def snapshot(self) -> list[ProcessInfo]:
        with self._lock:
            return self._rows()

    def creation_time(self, pid: int) -> int | None:
        with self._lock:
            row = self._table.get(pid)
            return row.ctime if row else None

    def ancestors_of_self(self) -> list[ProcessInfo]:
        return list(self.ancestors)

    # ---- interlocked, consent-bound ----
    def stop_consumer(self, token: ConsentToken, target: ProcessInfo) -> bool:
        with self._lock:
            self._interlock(token, target)
            if not self._verify(target):
                return False
            self.calls.append(("stop_consumer", target.pid, target.ctime))
            if target.pid not in self.unkillable:
                self._exit(target.pid)
            return True

    # ---- launch ----
    def launch_codex(self, codex_exe, home, cwd, session=None) -> None:
        with self._lock:
            self.calls.append(("launch_codex", str(codex_exe), str(cwd), session))
            self.add_codex("codex_tui")

    def find_claude_cli(self):
        return self.paths.temp_root / "bin" / "claude"

    def launch_claude_cli(self, claude_exe, config_dir, cwd):
        self.paths.require_cli_paths()
        if not all(is_test_owned_path(p) for p in (claude_exe, config_dir, cwd)):
            raise SafetyViolation("Only synthetic enrollment paths are allowed.")
        process = {"exited": False}
        self.calls.append(("launch_claude_cli", str(config_dir), process))
        return process

    def cli_enrollment_exited(self, process):
        return process["exited"]

    def cli_enrollment_token(self, process):
        return dict(process)

    def codex_daemon_stop(self, codex_exe, home, grace_s: float) -> str:
        with self._lock:
            self.calls.append(("codex_daemon_stop", str(codex_exe), grace_s))
            if self.codex_daemon() is None:
                return "absent"
            if not self.daemon_stops:
                return "timeout"
            for row in self._rows():
                if row.kind in ("codex_daemon", "codex_daemon_updater"):
                    self._exit(row.pid)
            return "stopped"

    def codex_daemon_start(self, codex_exe, home) -> bool:
        with self._lock:
            self.calls.append(("codex_daemon_start", str(codex_exe)))
            self.add_codex("codex_daemon")
            return True

    def open_terminal(self, cwd, env) -> None:
        self.calls.append(("open_terminal", str(cwd)))
        if config.safe_mode():
            raise _refusal("not_test_path", SAFE_MODE_MESSAGE)


def get_controller(paths, *, demo: bool, allow_readonly: bool = False) -> ProcessController:
    """The fake controller in demo mode; the live Mac controller otherwise (it refuses safe mode unless
    `allow_readonly=True`)."""
    if demo:
        return FakeProcessController(paths)
    # Imported on use: procs_mac builds on this module, so it cannot be imported while this one loads.
    from .platform.procs_mac import MacProcessController
    return MacProcessController(paths, allow_readonly=allow_readonly)

