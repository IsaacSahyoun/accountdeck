"""The only module that enumerates, signals, terminates, launches or activates processes (PLAN §4).

Three files make up process control on the Mac: this one (rows, classification, consent, the test-ownership
registry, the self-guarded spawn, the controller interface and the synthetic `FakeProcessController`),
`platform/libproc.py` (read-only libSystem facts: pids, paths, start times, descriptors) and
`platform/procs_mac.py` (the live controller, the one place that sends a signal).

Every mutating path runs the interlock in this order (§4.1-4.3):

1. safe mode: the target must be test-owned (registered by a test or spawned by this process);
2. the consent token must cover the target (§4.3 rules 1, 2, 4 and 5);
3. the target is re-verified behind a kqueue NOTE_EXIT watch, which pins the process the way a handle
   would: start time, executable path and bundle id are re-read and must match what was consented to;
4. the syscall.

The self-guarding wrappers (`_popen` here, `_signal` in procs_mac) repeat step 1 themselves, so a bug higher
up cannot reach a real process or app from a test or a demo. A row whose
identity cannot be read (start time 0, bundle "?") never passes step 3: unknown identity fails closed.
"""
from __future__ import annotations

import abc
import calendar
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import heapq
import itertools
import json
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
import uuid

from . import config, diagnostics
from .config import SafetyViolation
from .core import DeckError
from .platform import facts_mac, libproc
from .platform.libproc import BUNDLE_ID_PATTERN, OS_ERROR

__all__ = [
    "SafetyViolation", "ProcessInfo", "ProcessSet", "ConsentToken", "KINDS", "classify", "desktop_main",
    "consent_refusal", "proc_start_matches", "filetime", "unix_time", "register_test_root", "register_owned_pid",
    "register_test_bundle_id", "is_test_owned_path", "ProcessController",
    "FakeProcessController", "get_controller", "run_security", "PIPE", "DEVNULL",
]

KINDS = ("claude_desktop", "claude_code", "claude_foreign", "claude_unknown", "codex_tui", "codex_daemon",
         "codex_daemon_updater", "codex_app_server_child", "codex_desktop", "muse_session", "muse_host", "other")
CONSUMER_KINDS = frozenset({"codex_tui", "codex_app_server_child", "codex_desktop"})
MUSE_KINDS = frozenset({"muse_session", "muse_host"})     # every Muse process uses the Muse login
STOPPABLE_CONSUMER_KINDS = frozenset({"codex_tui", "codex_app_server_child"})

# ---- what this Mac's processes are called (docs/mac/PLAN.md §1.2, §8), compared lower-case ----
# Claude Desktop is "Claude" with "Claude Helper …" processes, its Code tabs run "claude", and so does
# Claude Code in a terminal. ChatGPT.app (bundle com.openai.codex) is the Codex app: everything inside that
# bundle, its own codex app-server included, is the Codex Desktop consumer and is never stopped. ShipIt is
# Claude's updater inside Claude.app: stopping it mid-update could break the app, so it is never Claude
# Desktop and never signalled.
CLAUDE_NAMES = frozenset({"claude", "claude helper", "claude helper (gpu)", "claude helper (renderer)",
                          "claude helper (plugin)"})
CODEX_NAMES = frozenset({"codex"})
CODEX_APP_NAMES = frozenset({"chatgpt"})
CODEX_APP_BUNDLE_IDS = frozenset({"com.openai.codex"})
NOT_CLAUDE_NAMES = frozenset({"chrome-native-host", "cowork-svc", "shipit"})
PROTECTED_NAMES = frozenset({"chrome-native-host", "cowork-svc", "chatgpt", "shipit"})   # never signalled
# Muse Code runs as ~/.local/bin/muse-bin-<version> (its launcher `exec`s it), so such a row is "muse".
MUSE_NAMES = frozenset({"muse"})
CMDLINE_NAMES = frozenset({"codex", "chatgpt", "muse"})    # the only rows whose argv is read
INSPECTED_NAMES = CLAUDE_NAMES | CODEX_NAMES | CODEX_APP_NAMES | NOT_CLAUDE_NAMES | MUSE_NAMES
# Claude Desktop's bundled Claude Code: ~/Library/Application Support/Claude/claude-code/<version>/claude.
CODE_CHILD_PATH = re.compile(r"/claude/claude-code/[^/]+/claude$", re.IGNORECASE)
# The native installer's Claude Code runs as ~/.local/share/claude/versions/<version>, so its executable is
# named by the version: such a row is "claude".
NATIVE_CLAUDE_PATH = re.compile(r"/\.local/share/claude/versions/[^/]+$")
CODEX_APP_PATH = re.compile(r"(?:^|/)ChatGPT\.app/Contents/", re.IGNORECASE)   # if its plist is unreadable
MUSE_BIN_PATH = re.compile(r"/muse-bin-[^/]+$")     # Muse Code's binary, whatever its version string

MAX_DESKTOP_ROWS = 40
# META_API_KEY takes priority over Muse's account login, so no helper may inherit it.
API_KEY_NAMES = ("OPENAI_API_KEY", "CODEX_API_KEY", "META_API_KEY")
PIPE, DEVNULL = subprocess.PIPE, subprocess.DEVNULL   # spawn() stdio for callers, which may not name subprocess
READY_FOR_QUIT = "marking readyForQuit"
QUIT_STARTED_LINE = "Starting onQuitCleanup, hiding windows"
QUIT_DONE_LINE = "Successully ran all onQuitCleanup handlers, marking readyForQuit"  # sic: Claude's own spelling
LAUNCH_LINES = ("[account] Account details received via IPC", "loaded token cache from disk with 1 entries")
LAUNCH_FAILED_MESSAGE = "Claude didn't open by itself. Open it from Applications."

FILETIME_EPOCH = 11644473600          # seconds from 1601-01-01 (FILETIME) to 1970-01-01 (Unix)
DAEMON_STOP_MARGIN = 15.0             # beyond shutdownGraceSeconds for `codex app-server daemon stop` itself
DAEMON_EXIT_WAIT = 10.0
DAEMON_START_WAIT = 30.0
REMEMBERED_LIMIT = 256

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
    """A row's name: the executable's file name, "claude" for the native installer's versioned binary."""
    if not path:
        return fallback
    if NATIVE_CLAUDE_PATH.search(path):
        return "claude"
    if MUSE_BIN_PATH.search(path):
        return "muse"
    return path.rpartition("/")[2] or fallback


def is_code_child_path(path) -> bool:
    """True for Claude Desktop's bundled Claude Code (…/Claude/claude-code/<version>/claude)."""
    return bool(path) and CODE_CHILD_PATH.search(str(path)) is not None


def _is_code_child(row) -> bool:
    return row.name.lower() in CLAUDE_NAMES and is_code_child_path(row.path)


def proc_start_matches(ctime: int | None, proc_start) -> bool:
    """Whether a Claude Code sessions-registry `procStart` names the process that started at `ctime`.
    Claude Code on macOS writes `ps -o lstart` text in UTC ("Thu Oct  8 04:36:45 2026", whole seconds);
    synthetic registries (tests, the demo) write the FILETIME number itself."""
    if not ctime or not isinstance(proc_start, str) or not proc_start.strip():
        return False
    text = proc_start.strip()
    if text.isdigit():
        return int(text) == ctime
    stamp = _lstart_seconds(text)
    return stamp is not None and stamp == (ctime - libproc.UNIX_TO_FILETIME) // 10_000_000


_MONTHS = {name: number for number, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def _lstart_seconds(text: str) -> int | None:
    """Unix seconds of `ps -o lstart` text in UTC ("Thu Oct  8 04:36:45 2026"), parsed without the locale."""
    parts = text.split()
    if len(parts) != 5 or parts[1] not in _MONTHS or not parts[2].isdigit() or not parts[4].isdigit():
        return None
    clock = parts[3].split(":")
    if len(clock) != 3 or not all(part.isdigit() for part in clock):
        return None
    try:
        return calendar.timegm((int(parts[4]), _MONTHS[parts[1]], int(parts[2]), *map(int, clock), 0, 0, 0))
    except (ValueError, OverflowError):
        return None


def _log(ev: str, **fields) -> None:
    diagnostics.log(ev, **fields)


def _refusal(reason: str, message: str) -> SafetyViolation:
    """A SafetyViolation carrying a machine-readable `reason`; logged as proc.refused (§4.3)."""
    _log("proc.refused", reason=reason)
    error = SafetyViolation(message)
    error.reason = reason
    return error


# ---- process rows, sets and consent (PLAN §4.2, §4.3) ----

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
    """What the user confirmed: the process pairs one job may signal (§4.3). Never serialised."""
    job_id: str
    provider: str
    purpose: str
    account: str | None
    mode: str                                # "idle" | "now"
    pairs: tuple[tuple[int, int], ...]
    families: tuple[str, ...]
    issued: float
    expires: float

    def covers(self, target: ProcessInfo) -> bool:
        if (target.pid, target.ctime) in self.pairs:
            return True
        # A Code child whose parent is covered counts only if it already existed when the user
        # confirmed; one that appeared later must go back to the user (needs_reconfirm).
        parent_ctime = dict(self.pairs).get(target.parent)
        return (target.family is None and _is_code_child(target)
                and parent_ctime is not None and parent_ctime < target.ctime <= filetime(self.issued))


def consent_refusal(token: ConsentToken | None, target: ProcessInfo, *, action: str, now: float,
                    protected=frozenset()) -> str | None:
    """Consent rules 1, 2, 4 and 5 of PLAN §4.3 for `action` ("request_close" | "terminate" |
    "stop_consumer"); returns the refusal reason, or None when the token allows the action."""
    if token is None:
        return "no_consent"
    if now >= token.expires:
        return "expired"
    if target.name.lower() in PROTECTED_NAMES:
        return "protected"
    if target.pid in protected:
        return "self"
    if action == "stop_consumer":
        if target.kind not in STOPPABLE_CONSUMER_KINDS:
            return "kind"
        return None if (target.pid, target.ctime) in token.pairs else "not_covered"
    if not token.covers(target):
        return "not_covered"
    if target.kind == "claude_desktop":
        return None if token.families and target.family == token.families[0] else "family"
    if action == "terminate" and target.kind == "claude_code" and target.family is None and _is_code_child(target):
        return None
    return "kind"


# ---- classification (PLAN §4.4): pure over rows, so synthetic tables drive every branch ----

def classify(rows, paths) -> dict[str, list[ProcessInfo]]:
    """Every row with its kind, grouped by kind (all KINDS present as keys)."""
    grouped = {kind: [] for kind in KINDS}
    for row in with_kinds(rows, paths):
        grouped[row.kind].append(row)
    return grouped


def desktop_main(desktop_rows) -> ProcessInfo | None:
    """The desktop row whose parent is not an older desktop row (the Electron browser process)."""
    by_pid = {row.pid: row for row in desktop_rows}
    roots = [row for row in desktop_rows
             if (parent := by_pid.get(row.parent)) is None or parent.ctime >= row.ctime]
    return min(roots, key=lambda row: row.ctime) if roots else None


def with_kinds(rows, paths) -> list[ProcessInfo]:
    """`rows` with their `kind` set. Claude Desktop is every row from its bundle (`paths.claude_family`)."""
    rows = list(rows)
    desktop = {row.pid: row for row in rows
               if row.family not in (None, "?") and row.family == paths.claude_family
               and row.name.lower() not in PROTECTED_NAMES}
    return [replace(row, kind=_kind(row, desktop, paths)) for row in rows]


def _kind(row: ProcessInfo, desktop: dict, paths) -> str:
    name = row.name.lower()
    if desktop.get(row.pid) is row:
        return "claude_desktop"
    if name in NOT_CLAUDE_NAMES:
        return "other"
    if row.path and CODEX_APP_PATH.search(str(row.path)):
        return "codex_desktop"
    if row.family not in (None, "?") and (row.family in paths.codex_families or row.family in CODEX_APP_BUNDLE_IDS):
        return "codex_desktop"
    if name in CLAUDE_NAMES:
        if row.family == "?":
            return "claude_unknown"
        parent = desktop.get(row.parent)
        if row.family is None and _is_code_child(row) and parent is not None and 0 < parent.ctime < row.ctime:
            return "claude_code"
        return "claude_foreign"
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


# ---- test-ownership registry (PLAN §4.1; process-wide; only tests and the demo call these) ----

_registry_lock = threading.Lock()
_test_roots: list[str] = []                               # normalised absolute paths
_test_bundle_ids: set[str] = set()
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
    `family` lets such a process stand in for an app bundle, e.g. `paths.claude_family`."""
    _require_safe_mode("register_owned_pid")
    if not ctime or libproc.creation_time(pid) != ctime or not libproc.descends_from_self(pid, ctime):
        raise _refusal("not_descendant", "Only processes started by this test can be registered.")
    with _registry_lock:
        _owned_pids[pid] = (ctime, family)


def register_test_bundle_id(bundle_id: str) -> None:
    """Allows activate_app/launch_claude (`open -b`) for a test's synthetic bundle id. Starting /usr/bin/open
    is still refused in safe mode, so a test also replaces the spawn; no test opens a real app."""
    _require_safe_mode("register_test_bundle_id")
    if not isinstance(bundle_id, str) or not BUNDLE_ID_PATTERN.fullmatch(bundle_id):
        raise _refusal("bundle_id", "Only a bundle id such as com.example.App can be registered.")
    with _registry_lock:
        _test_bundle_ids.add(bundle_id)


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


def _is_test_bundle_id(bundle_id: str) -> bool:
    with _registry_lock:
        return bundle_id in _test_bundle_ids


def _check_spawn_target(executable: str, *, keychain: str | None = None) -> None:
    """Safe-mode spawn policy (ORCH-1): only this Python or an executable under a test root, and
    /usr/bin/security only for a command whose keychain file (`keychain`, checked by run_security to be
    the command's trailing argument) is a test's own, never the login keychain (docs/mac/PLAN.md §1.5)."""
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


# /usr/bin/security for platform/keychain.py (docs/mac/PLAN.md §1.5, PLATFORM-MAC.md §8): only the three
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


# ---- controllers (PLAN §4.2) ----

class ProcessController(abc.ABC):
    """Process control for one AccountDeck instance. Read-only methods may run on any thread;
    mutating methods refuse unless the interlock (module docstring) passes."""

    def __init__(self, paths, timings=None):
        self.paths = paths
        self.timings = timings or config.Timings()
        self.owned: dict[int, int] = {}       # pid -> ctime of processes this instance spawned
        self.helpers: dict[int, int] = {}     # spawn_helper() children: excluded from codex_consumers()
        self.last_launch: dict | None = None  # {"method", "hresult", "retries", "exit"} of the last launch

    # ---- read-only ----
    @abc.abstractmethod
    def snapshot(self) -> list[ProcessInfo]:
        """The inspected rows with their kinds set."""

    def classify(self, rows) -> dict[str, list[ProcessInfo]]:
        return classify(rows, self.paths)

    def claude_desktop(self) -> list[ProcessInfo]:
        return self.classify(self.snapshot())["claude_desktop"]

    def claude_desktop_main(self) -> ProcessInfo | None:
        return desktop_main(self.claude_desktop())

    def claude_code_sessions(self) -> list[ProcessInfo]:
        return self.classify(self.snapshot())["claude_code"]

    def claude_foreign(self) -> list[ProcessInfo]:
        return self.classify(self.snapshot())["claude_foreign"]

    def codex_consumers(self, rows=None) -> list[ProcessInfo]:
        """Processes using the Codex login (in `rows`, a snapshot the caller already took, or a fresh one);
        AccountDeck's own helpers (and their children) do not count, but terminals it opened do (AGENTS.md)."""
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
    def descendants(self, pid: int, exclude=()) -> list[ProcessInfo]: ...

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

    def file_is_free(self, path) -> bool:
        return facts_mac.file_is_free(path)

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

    def spawn_helper(self, args, **options) -> subprocess.Popen:
        """A background helper (Codex RPC) without API keys in its environment."""
        options.update(strip_api_keys=True)
        process = self.spawn(args, **options)
        self._remember(self.helpers, process.pid)
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
    def request_close(self, token: ConsentToken, target: ProcessInfo) -> None: ...

    @abc.abstractmethod
    def terminate(self, token: ConsentToken, target: ProcessInfo) -> bool: ...

    @abc.abstractmethod
    def wait_exit(self, targets: list[ProcessInfo], timeout: float) -> list[ProcessInfo]: ...

    @abc.abstractmethod
    def stop_consumer(self, token: ConsentToken, target: ProcessInfo) -> bool: ...

    def _interlock(self, action: str, token, target: ProcessInfo) -> None:
        """Interlock steps 1 and 2: test ownership in safe mode, then the consent rules."""
        self._require_test_owned(action, target)
        reason = consent_refusal(token, target, action=action, now=time.time(), protected=self._protected_pids())
        if reason:
            raise _refusal(reason, CONSENT_MESSAGE)

    @abc.abstractmethod
    def _require_test_owned(self, action: str, target: ProcessInfo) -> None: ...

    def _protected_pids(self) -> set[int]:
        return {os.getpid()} | {row.pid for row in self.ancestors_of_self()}

    def _helper_tree(self) -> set[tuple[int, int]]:
        return set()

    # ---- interlocked, launch ----
    @abc.abstractmethod
    def activate_app(self, bundle_id: str) -> int | None: ...

    @abc.abstractmethod
    def launch_claude(self, bundle_id: str | None) -> int | None: ...

    @abc.abstractmethod
    def launch_codex(self, codex_exe, home, cwd, session=None) -> None: ...

    @abc.abstractmethod
    def codex_daemon_stop(self, codex_exe, home, grace_s: float) -> str: ...

    @abc.abstractmethod
    def codex_daemon_start(self, codex_exe, home) -> bool: ...

    @abc.abstractmethod
    def open_folder(self, path) -> None: ...

    @abc.abstractmethod
    def open_terminal(self, cwd, env) -> None: ...

class FakeProcessController(ProcessController):
    """An in-memory Mac process table for tests and the demo (§4.2). Nothing it does reaches a real process:
    spawn() is the real, safe-mode-gated implementation; everything else acts on the table, with the live controller's rules (consent, re-verification, "only the main may be asked to
    quit"). Scripted behaviour:

    quits_on_close     request_close (SIGTERM) runs Claude's quit: markers in main.log, then the desktop set
                       and its Code children exit after `exit_delay_s` (False: Claude ignores it)
    helpers_self_exit  when the main process is terminated its helpers and Code children exit too
                       (False: they stay as stragglers until terminated one by one)
    exit_delay_s       how long a graceful quit takes to exit after its cleanup markers
    holds              paths file_is_free() reports busy while any desktop process runs
    relaunch_delay_s   launch_claude(): the new desktop set appears after this delay
    relaunch_identity  after relaunch: config.json lastKnownAccountUuid and a LocalSessionManager
                       main.log line name this account uuid
    launch_fails_with  launch_claude()/activate_app() start nothing: `open` exited with this status
    sessions_registry  {pid: row} written to paths.claude_sessions/<pid>.json at construction
    mainlog_script     [(delay_s, line)] appended to main.log after each launch (default: LAUNCH_LINES)
    ancestors          rows ancestors_of_self() returns (self-protection tests)
    unkillable         pids that survive terminate() (abort tests)
    daemon_stops       False: codex_daemon_stop() times out and the daemon stays

    Every call is recorded in `.calls`; scheduled effects run lazily on the next call."""

    CLAUDE_HELPERS = ("Claude Helper", "Claude Helper (Renderer)", "Claude Helper (GPU)")
    CLAUDE_CODE_VERSION = "2.1.293"
    CODEX_VERSION = "0.161.0"

    def __init__(self, paths, rows=(), *, timings=None, quits_on_close: bool = True, helpers_self_exit: bool = True,
                 exit_delay_s: float = 0.0, holds=(), relaunch_delay_s: float = 0.0,
                 relaunch_identity: str | None = None, launch_fails_with: int | None = None,
                 sessions_registry: dict | None = None, mainlog_script=None, ancestors=(), unkillable=(),
                 daemon_stops: bool = True):
        super().__init__(paths, timings)
        self.quits_on_close = quits_on_close
        self.helpers_self_exit, self.exit_delay_s = helpers_self_exit, exit_delay_s
        self.holds = {Path(path) for path in holds}
        self.relaunch_delay_s, self.relaunch_identity = relaunch_delay_s, relaunch_identity
        self.launch_fails_with = launch_fails_with
        self.mainlog_script = (list(mainlog_script) if mainlog_script is not None
                               else [(0.0, line) for line in LAUNCH_LINES])
        self.ancestors = list(ancestors)
        self.unkillable = set(unkillable)
        self.daemon_stops = daemon_stops
        self.calls: list[tuple] = []
        self._table: dict[int, ProcessInfo] = {}
        self._registry_files: set[int] = set()
        self._events: list[tuple[float, int, object]] = []
        self._sequence = itertools.count()
        self._lock = threading.RLock()
        self._next_pid = 20000
        self._last_ctime = 0
        self._helper_count = 3
        self._quitting: set[int] = set()
        for row in rows:
            self._table[row.pid] = replace(row, kind="other")
        for pid, row in (sessions_registry or {}).items():
            self._write_session(int(pid), row)

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

    def add_claude_desktop(self, helpers: int = 3, *, pid: int | None = None) -> ProcessInfo:
        """A running Claude Desktop: Claude.app's main process plus `helpers` helper processes (all of the
        app's bundle); returns the main row."""
        with self._lock:
            app = self._applications() / "Claude.app" / "Contents"
            family = self.paths.claude_family
            main = self.add_row("Claude", path=app / "MacOS" / "Claude", family=family, parent=1, pid=pid)
            for index in range(helpers):
                name = self.CLAUDE_HELPERS[index % len(self.CLAUDE_HELPERS)]
                helper = app / "Frameworks" / f"{name}.app" / "Contents" / "MacOS" / name
                self.add_row(name, path=helper, family=family, parent=main.pid)
            self._helper_count = helpers
            return main

    def add_code_session(self, name: str = "Code session", status: str = "busy", *, parent: int | None = None,
                         version: str = "2.1.284", status_age_s: float = 60.0, entrypoint: str = "claude-desktop",
                         registry: bool = True) -> ProcessInfo:
        """A Code-tab `claude` under the desktop main (or `parent`) with its sessions-registry row."""
        with self._lock:
            if parent is None:
                main = desktop_main(self.classify(list(self._table.values()))["claude_desktop"])
                if main is None:
                    raise ValueError("add_claude_desktop() first")
                parent = main.pid
            path = Path(self.paths.claude_profile) / "claude-code" / version / "claude"
            row = self.add_row("claude", path=path, parent=parent)
            if registry:
                now_ms = int(time.time() * 1000)
                self._write_session(row.pid, {
                    "pid": row.pid, "sessionId": str(uuid.uuid4()), "cwd": str(self.paths.home), "startedAt": now_ms,
                    "procStart": str(row.ctime), "version": version, "kind": "interactive", "entrypoint": entrypoint,
                    "name": name, "status": status, "updatedAt": now_ms,
                    "statusUpdatedAt": int((time.time() - status_age_s) * 1000)})
            return row

    def add_foreign_claude(self) -> ProcessInfo:
        """Claude Code in a terminal, as the native installer runs it (named by its version)."""
        path = Path(self.paths.home) / ".local" / "share" / "claude" / "versions" / self.CLAUDE_CODE_VERSION
        return self.add_row("claude", path=path, parent=1)

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

    def set_session_status(self, pid: int, status: str, *, age_s: float = 0.0) -> None:
        """Rewrites a Code session's registry status as if Claude Code had updated it `age_s` ago."""
        file = Path(self.paths.claude_sessions) / f"{pid}.json"
        row = json.loads(file.read_text(encoding="utf-8"))
        row.update(status=status, statusUpdatedAt=int((time.time() - age_s) * 1000), updatedAt=int(time.time() * 1000))
        self._write_session(pid, row)

    def exit_process(self, pid: int) -> None:
        """The process exits by itself."""
        with self._lock:
            self._exit(pid)

    # ---- internals ----
    def _write_session(self, pid: int, row: dict) -> None:
        directory = Path(self.paths.claude_sessions)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{pid}.json").write_text(json.dumps(row), encoding="utf-8")
        self._registry_files.add(pid)

    def _append_log(self, line: str) -> None:
        log = Path(self.paths.claude_main_log)
        log.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] [info] {line}\n")

    def _schedule(self, delay: float, action) -> None:
        heapq.heappush(self._events, (time.monotonic() + max(0.0, delay), next(self._sequence), action))

    def _tick(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._events and self._events[0][0] <= now:
                _due, _seq, action = heapq.heappop(self._events)
                action()

    def _exit(self, pid: int) -> None:
        self._table.pop(pid, None)
        self._quitting.discard(pid)
        if pid in self._registry_files:
            self._registry_files.discard(pid)
            (Path(self.paths.claude_sessions) / f"{pid}.json").unlink(missing_ok=True)

    def _rows(self) -> list[ProcessInfo]:
        return with_kinds(self._table.values(), self.paths)

    def _desktop(self) -> list[ProcessInfo]:
        return [row for row in self._rows() if row.kind == "claude_desktop"]

    def _dependants(self, main_pid: int) -> list[int]:
        """The desktop set's other processes and its Code children."""
        rows = self._rows()
        desktop = {row.pid for row in rows if row.kind == "claude_desktop"}
        return [row.pid for row in rows if row.pid != main_pid
                and (row.kind == "claude_desktop" or (row.kind == "claude_code" and row.parent in desktop))]

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

    def _require_test_owned(self, action: str, target: ProcessInfo) -> None:
        """The table is not the machine, so there is no real process to protect."""

    def _protected_pids(self) -> set[int]:
        return {row.pid for row in self.ancestors}

    def _quit(self, main_pid: int) -> None:
        """Claude's own quit after SIGTERM: cleanup markers, then the whole desktop set exits."""
        if main_pid in self._quitting:
            return
        self._quitting.add(main_pid)
        self._append_log(QUIT_STARTED_LINE)
        self._schedule(min(self.exit_delay_s, 0.05), lambda: self._append_log(QUIT_DONE_LINE))

        def leave():
            for pid in [main_pid] + self._dependants(main_pid):
                if pid not in self.unkillable:
                    self._exit(pid)
        self._schedule(self.exit_delay_s, leave)

    def _relaunch(self) -> None:
        pid = self._next_pid
        self._next_pid += 4

        def appear():
            main = self.add_claude_desktop(self._helper_count, pid=pid)
            if self.relaunch_identity:
                config_file = Path(self.paths.claude_profile) / "config.json"
                try:
                    data = json.loads(config_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    data = {}
                data = data if isinstance(data, dict) else {}
                data["lastKnownAccountUuid"] = self.relaunch_identity
                config_file.parent.mkdir(parents=True, exist_ok=True)
                config_file.write_text(json.dumps(data), encoding="utf-8")
                self._append_log(f"[LocalSessionManager] Initialization succeeded - accountId={self.relaunch_identity}")
            for delay, line in self.mainlog_script:
                self._schedule(delay, lambda line=line: self._append_log(line))
            return main
        self._schedule(self.relaunch_delay_s, appear)

    # ---- read-only ----
    def snapshot(self) -> list[ProcessInfo]:
        with self._lock:
            self._tick()
            return self._rows()

    def creation_time(self, pid: int) -> int | None:
        with self._lock:
            self._tick()
            row = self._table.get(pid)
            return row.ctime if row else None

    def descendants(self, pid: int, exclude=()) -> list[ProcessInfo]:
        with self._lock:
            self._tick()
            rows = self._rows()
            skipped = {name.lower() for name in exclude}
            root = self._table.get(pid)
            found, queue = [], [root] if root else []
            while queue:
                parent = queue.pop()
                for row in rows:
                    if row.parent == parent.pid and row.ctime > parent.ctime and row.name.lower() not in skipped:
                        found.append(row)
                        queue.append(row)
            return found

    def ancestors_of_self(self) -> list[ProcessInfo]:
        return list(self.ancestors)

    def file_is_free(self, path) -> bool:
        with self._lock:
            self._tick()
            if Path(path) in self.holds and self._desktop():
                return False
        return facts_mac.file_is_free(path)

    # ---- interlocked, consent-bound ----
    def request_close(self, token: ConsentToken, target: ProcessInfo) -> None:
        """SIGTERM to the verified Claude Desktop main, as the live controller sends it."""
        with self._lock:
            self._tick()
            self._interlock("request_close", token, target)
            main = desktop_main(self._desktop())
            if main is None or (main.pid, main.ctime) != (target.pid, target.ctime):
                raise _refusal("not_main", CHANGED_MESSAGE)
            if not self._verify(target):
                return
            self.calls.append(("request_close", target.pid))
            if self.quits_on_close:
                self._quit(target.pid)

    def terminate(self, token: ConsentToken, target: ProcessInfo) -> bool:
        with self._lock:
            self._tick()
            self._interlock("terminate", token, target)
            if not self._verify(target):
                return False
            self.calls.append(("terminate", target.pid, target.ctime))
            if target.pid in self.unkillable:
                return True
            dependants = self._dependants(target.pid) if target.kind == "claude_desktop" else []
            main = desktop_main(self._desktop())
            self._exit(target.pid)
            if main is not None and main.pid == target.pid and self.helpers_self_exit:
                # Chromium helpers exit on their own when the browser process is gone.
                self._schedule(0.05, lambda: [self._exit(pid) for pid in dependants if pid not in self.unkillable])
            return True

    def wait_exit(self, targets, timeout: float) -> list[ProcessInfo]:
        def survivors():
            with self._lock:
                self._tick()
                return [t for t in targets if (row := self._table.get(t.pid)) is not None and row.ctime == t.ctime]
        self.wait_until(lambda: not survivors(), timeout, interval=0.02)
        return survivors()

    def stop_consumer(self, token: ConsentToken, target: ProcessInfo) -> bool:
        with self._lock:
            self._tick()
            self._interlock("stop_consumer", token, target)
            if not self._verify(target):
                return False
            self.calls.append(("stop_consumer", target.pid, target.ctime))
            if target.pid not in self.unkillable:
                self._exit(target.pid)
            return True

    # ---- launch ----
    def activate_app(self, bundle_id: str) -> int | None:
        with self._lock:
            self._tick()
            self.calls.append(("activate_app", bundle_id))
            return self._open()

    def launch_claude(self, bundle_id: str | None) -> int | None:
        with self._lock:
            self._tick()
            self.calls.append(("launch_claude", bundle_id))
            return self._open()

    def _open(self) -> None:
        """`open -b`: nothing new when Claude already runs (macOS brings it forward), else it starts. The new
        app's pid is not known to the caller (its parent is launchd), as with the live controller."""
        code = self.launch_fails_with or 0
        self.last_launch = {"method": "open", "hresult": None, "retries": 0, "exit": code}
        if code:
            raise DeckError(LAUNCH_FAILED_MESSAGE, OS_ERROR)
        if desktop_main(self._desktop()) is None:
            self._relaunch()
        return None

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
            self._tick()
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

    def open_folder(self, path) -> None:
        self.calls.append(("open_folder", str(path)))
        if config.safe_mode():
            raise _refusal("not_test_path", SAFE_MODE_MESSAGE)

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

