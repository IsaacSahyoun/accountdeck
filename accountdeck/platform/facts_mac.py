"""Facts about this Mac besides processes: the data folder's permissions and where the Codex CLI and Muse
Code are installed. Machine locations come from config; process control lives only in procs.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import stat

from .. import config
from ..core import DeckError

CODEX_APP = "ChatGPT.app"                                   # the Codex app on macOS
CODEX_IN_APP = ("Contents", "Resources", "codex-cli", "bin", "codex")
CODEX_PACKAGE = (".local", "share", "openai-codex", "current", "bin", "codex")
CODEX_LAUNCHER = (".local", "bin", "codex")
MAX_LAUNCHER_BYTES = 64 << 10
# Mach-O and universal-binary magic numbers as they appear on disk.
NATIVE_MAGIC = frozenset({b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",
                          b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"})
LAUNCHER_PATH = re.compile(r"""(?:\$\{HOME\}|\$HOME|~)?/[^\s"'`;|&<>()$]*/codex(?=["'\s;)]|$)""")
VAULT_MESSAGE = "Could not restrict access to the local account vault."
NOT_INSTALLED_MESSAGE = "Install Codex CLI and sign in once, then try again."
# Muse Code's installer keeps a bash launcher at ~/.local/bin/muse that updates itself and `exec`s the
# versioned binary beside it, ~/.local/bin/muse-bin-<version>; ~/.local/bin/.muse-version names the current one.
MUSE_BIN_DIR = (".local", "bin")
MUSE_VERSION_FILE = ".muse-version"
MUSE_BINARY = re.compile(r"muse-bin-(\d+)\.(\d+)\.(\d+)-R(\d+)(?:\.(\d+))?")
MUSE_NOT_INSTALLED = "Install Muse Code and sign in once, then try again."


# ---- the data folder and helper work folders ----

def secure_directory(path) -> None:
    """Creates `path` private to this user: a real folder (not a symlink) owned by this user with mode 0700.
    A folder copied from elsewhere keeps its old mode, so the mode is set on every call and read back."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError(path)
        os.chmod(path, 0o700)
        if stat.S_IMODE(os.stat(path).st_mode) != 0o700:
            raise PermissionError(path)
    except OSError:
        raise DeckError(VAULT_MESSAGE, "vault") from None


def remove_work_directory(path, root) -> None:
    """Removes an isolated helper directory under `root`, including read-only cache files."""
    path, root = Path(path), Path(root).resolve()
    resolved = path.resolve()
    if resolved == root or not resolved.is_relative_to(root) or path.is_symlink():
        raise DeckError("Temporary-folder cleanup stopped at its safety boundary.", "recovery")

    def retry(function, target, error):
        target = Path(target)
        if not target.resolve().is_relative_to(root) or not isinstance(error, PermissionError):
            raise error
        target.chmod(stat.S_IWRITE | stat.S_IREAD)
        function(target)

    if path.exists():
        shutil.rmtree(path, onexc=retry)


# ---- Codex ----

def codex_exe(paths) -> Path:
    """The Codex CLI binary. `paths.codex_exe` when it exists (synthetic paths); live, in this order: `codex`
    on PATH, ~/.local/bin/codex (launchd's PATH does not include it), the standalone package's current
    binary, then the CLI bundled in ChatGPT.app. A shell-script launcher (the standalone installer's
    ~/.local/bin/codex `exec`s the package binary) is resolved to the binary it starts, so the path matches
    the running Codex processes. Only a native (Mach-O) binary is ever returned."""
    if paths.codex_exe is not None and Path(paths.codex_exe).is_file():
        return Path(paths.codex_exe)
    if paths.mode == "live":
        home = Path(paths.home)
        candidates = [shutil.which("codex"), home.joinpath(*CODEX_LAUNCHER), home.joinpath(*CODEX_PACKAGE)]
        candidates += [Path(folder) / CODEX_APP / Path(*CODEX_IN_APP) for folder in config.mac_applications(home)]
        for candidate in candidates:
            found = _codex_binary(Path(candidate), home) if candidate else None
            if found is not None:
                return found
    raise DeckError(NOT_INSTALLED_MESSAGE, "not_installed")


def muse_exe(paths) -> Path:
    """The Muse Code binary. `paths.muse_exe` when it exists (synthetic paths); live, the binary
    ~/.local/bin/.muse-version names, else the newest ~/.local/bin/muse-bin-<version>. The launcher
    script is never returned: it checks for updates over the network before it starts Muse. Only a
    native (Mach-O) binary is ever returned."""
    if paths.muse_exe is not None and Path(paths.muse_exe).is_file():
        return Path(paths.muse_exe)
    if paths.mode == "live":
        folder = Path(paths.home).joinpath(*MUSE_BIN_DIR)
        try:
            named = (folder / MUSE_VERSION_FILE).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            named = ""
        candidates = [folder / f"muse-bin-{named}"] if MUSE_BINARY.fullmatch(f"muse-bin-{named}") else []
        try:
            found = [entry for entry in folder.iterdir() if MUSE_BINARY.fullmatch(entry.name)]
        except OSError:
            found = []
        found.sort(key=lambda entry: tuple(int(part or 0) for part in MUSE_BINARY.fullmatch(entry.name).groups()),
                   reverse=True)
        for candidate in candidates + found:
            if _is_native(candidate):
                return candidate
    raise DeckError(MUSE_NOT_INSTALLED, "not_installed")


def _codex_binary(path: Path, home: Path) -> Path | None:
    """`path` itself when it is a native binary, the binary it starts when it is a launcher script, else None."""
    if _is_native(path):
        return path.resolve()
    for target in _launcher_targets(path, home):
        if _is_native(target):
            return target.resolve()
    return None


def _is_native(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        with open(path, "rb") as handle:
            return handle.read(4) in NATIVE_MAGIC
    except OSError:
        return False


def _launcher_targets(script: Path, home: Path) -> list[Path]:
    """Absolute `…/codex` paths a `#!` launcher names ($HOME and ~ expanded; anything else with a variable
    is skipped), then the standalone package's current binary."""
    try:
        if not script.is_file() or script.stat().st_size > MAX_LAUNCHER_BYTES:
            return []
        text = script.read_bytes().decode("utf-8", "replace")
    except OSError:
        return []
    if not text.startswith("#!"):
        return []
    found = []
    for match in LAUNCHER_PATH.finditer(text):
        value = match.group()
        for prefix in ("${HOME}", "$HOME", "~"):
            if value.startswith(prefix + "/"):
                value = str(home) + value[len(prefix):]
                break
        target = Path(value)
        if target.is_absolute() and target != script:
            found.append(target)
    return found + [home.joinpath(*CODEX_PACKAGE)]
