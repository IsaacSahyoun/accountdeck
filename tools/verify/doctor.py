"""Read-only check before a proof run: is this code copy safe to drive, which build is the live app, and is
everything a proof needs on this Mac. One OK, WARN or STOP line per fact. A STOP means no proof until it is
fixed. It reads no login, prints no token and changes nothing, except that it builds the WebKit test host
in the temp folder when that has not been built yet.

Usage: doctor.py <code copy>          (run it with the app's own Python, from this repo)"""
import glob
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests import webkit  # noqa: E402

LIVE = Path.home() / "Library/Application Support/AccountDeck2"
LIVE_APP = Path.home() / "Applications/AccountDeck 2.app"
JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
stops = 0


def say(level, text):
    global stops
    stops += level == "STOP"
    print(f"{level:4} {text}")


def git(*args):
    done = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def tree(commit):
    """{path: blob hash} of every file in a commit of this repo."""
    rows = (line.split(None, 3) for line in git("ls-tree", "-r", commit).splitlines())
    return {path: blob for _mode, kind, blob, path in rows if kind == "blob"}


def blob(path):
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def same_as(folder, commit, only=None):
    """The commit's files (under the `only` prefixes) that differ in `folder` or are missing from it, and
    how many files that is out of."""
    wanted = {path: sha for path, sha in tree(commit).items() if only is None or path.startswith(only)}
    return [path for path, sha in wanted.items() if not (folder / path).is_file() or blob(folder / path) != sha], len(wanted)


def added_to(folder, commit, only=None):
    """The files in `folder` (under the `only` prefixes) that the commit does not have."""
    known = tree(commit)
    files = (str(path.relative_to(folder)) for path in folder.rglob("*")
             if path.is_file() and "__pycache__" not in path.parts and path.name != ".DS_Store")
    return sorted(name for name in files if name not in known and (only is None or name.startswith(only)))


def some(names):
    return ", ".join(names[:12]) + (f" and {len(names) - 12} more" if len(names) > 12 else "")


def which_commit(folder, only=None):
    """The words for which known commit a folder's files are: `main`, the branch checked out, or a tag. A tag
    marks a build that was installed, so the live app's code is found there after `main` has moved on."""
    names = ("main", git("rev-parse", "--abbrev-ref", "HEAD"), *git("tag", "--sort=-creatordate").splitlines())
    for name in dict.fromkeys(names):
        short = git("rev-parse", "--short", f"{name}^{{commit}}")
        if short:
            differ, of = same_as(folder, name, only)
            if of and not differ and not added_to(folder, name, only):
                return f"is commit {short} ({name}), all {of} files"
    differ, of = same_as(folder, "HEAD", only)
    added = added_to(folder, "HEAD", only)
    return (f"is no commit. Against {git('rev-parse', '--short', 'HEAD')}: {len(differ)} of {of} files differ"
            + (f" ({some(differ)})" if differ else "") + f", {len(added)} files are new" + (f" ({some(added)})" if added else ""))


def running(pattern):
    done = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return [int(pid) for pid in done.stdout.split() if int(pid) != os.getpid()]


def main(copy):
    copy = copy.resolve()
    say("OK", f"this Python is {sys.executable} ({sys.version.split()[0]})")
    if not (copy / "run.py").is_file():
        return say("STOP", f"{copy} is not a copy of the code (no run.py)")
    if LIVE in copy.parents or copy == LIVE or LIVE_APP in copy.parents:
        return say("STOP", f"{copy} is inside the live app. Never run or test from there. Make a copy.")
    if (copy / ".git").exists():
        done = subprocess.run(["git", "-C", str(copy), "status", "--short"], capture_output=True, text=True)
        head = subprocess.run(["git", "-C", str(copy), "log", "-1", "--format=%h %s"], capture_output=True, text=True)
        changed = len(done.stdout.splitlines())
        say("WARN" if changed else "OK", f"the copy is a checkout at {head.stdout.strip()[:70]}, {changed} files changed"
            + (": name that in your report, the result is not the commit's" if changed else ""))
    else:
        # an installed app folder holds the app only, no tests or tools
        say("OK", f"the copy {which_commit(copy, None if (copy / 'tests').is_dir() else ('accountdeck/', 'run.py'))}")

    # the live app: named so nothing here gets mistaken for it
    if (LIVE / "app" / "run.py").is_file():
        say("OK", f"the live app's code {which_commit(LIVE / 'app', ('accountdeck/', 'run.py'))}")
    else:
        say("WARN", "no installed app found in Application Support")
    pids = running("run.py --app")
    port = ""
    try:
        port = f", port {json.loads((LIVE / 'runtime.json').read_text())['port']}"
    except Exception:
        pass
    say("OK", f"the live app is running as pid {' '.join(map(str, pids))}{port}: leave it alone" if pids
        else "the live app is not running")

    # demos: only the one you started is yours
    demos = running("run.py --demo")
    say("WARN" if demos else "OK", f"demos running now: {' '.join(map(str, demos)) or 'none'}"
        + (" (not yours unless you started them)" if demos else ""))
    for folder in sorted(glob.glob(os.path.join(tempfile.gettempdir(), "accountdeck2-demo-*"))):
        hours = (time.time() - os.path.getmtime(folder)) / 3600
        say("WARN", f"a demo's temp folder is still there, last used {hours:.0f}h ago, not yours unless you made it: {folder}")

    # what a proof needs
    say("OK" if os.environ.get("ACCOUNTDECK_SAFE_MODE") == "1" else "WARN",
        "ACCOUNTDECK_SAFE_MODE=1 in this shell" if os.environ.get("ACCOUNTDECK_SAFE_MODE") == "1"
        else "ACCOUNTDECK_SAFE_MODE is not set in this shell. The tests and tools set it themselves. "
             "Never start run.py without --demo.")
    try:
        say("OK" if webkit.host() else "STOP", f"the WebKit window for the dashboard: {webkit.host() or 'no swiftc, cannot be built'}")
    except RuntimeError as exc:
        say("STOP", f"the WebKit window did not build: {str(exc)[:300]}")
    chrome = sorted(glob.glob(str(Path.home() / "Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")))
    say("OK" if chrome else "WARN", f"headless Chrome for the second engine: {chrome[-1] if chrome else 'none found, the Chrome pass will skip'}")
    say("OK" if os.access(JSC, os.X_OK) else "WARN", "jsc for JavaScript syntax checks" + ("" if os.access(JSC, os.X_OK) else " is missing"))
    say("OK" if shutil.which("trash") else "WARN", "trash for cleanup" + ("" if shutil.which("trash") else " is missing: move things aside by hand"))
    free = shutil.disk_usage(tempfile.gettempdir()).free / 1e9
    say("OK" if free > 2 else "STOP", f"{free:.0f} GB free in the temp folder")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]))
    print(f"\n{'STOP: fix the lines above before any proof' if stops else 'no STOP: this copy can be driven'}")
    sys.exit(1 if stops else 0)
