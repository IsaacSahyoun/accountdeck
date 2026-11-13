#!/bin/bash
# Installs or upgrades AccountDeck 2 for this Mac user (Apple silicon).
#
#     bash install.sh [--no-launch] [--start-at-login] [--prefix DIR] [--downloads DIR]
#
# Run it from the unpacked AccountDeck2-mac package. A brand-new Mac needs nothing beforehand: the
# script downloads its own Python and the two packages AccountDeck uses, each pinned and checked
# against its SHA-256, and never calls /usr/bin/python3, pip3 or git (on a Mac without the developer
# tools those only show an install dialog).
#
# What it creates (DIR replaces your home folder with --prefix, which tests use):
#   ~/Library/Application Support/AccountDeck2/runtime   Python 3.14.8 + cffi + cryptography
#   ~/Library/Application Support/AccountDeck2/app       the AccountDeck 2 code (never under ~/Documents,
#                                                        which iCloud may sync or evict)
#   ~/Applications/AccountDeck 2.app                     the app: Spotlight, Launchpad and Finder find it. Its
#                                                        Contents/MacOS/python3 is a copy of the runtime's
#                                                        python3.14 signed (ad hoc) with the app's bundle id:
#                                                        macOS only lets a process whose code identity matches
#                                                        its bundle id send notifications
#   ~/Library/LaunchAgents/local.accountdeck2.plist      only with --start-at-login
# Everything else in ~/Library/Application Support/AccountDeck2 is your data, and is never touched.
#
# Options:
#   --no-launch        do not open AccountDeck 2 at the end
#   --start-at-login   open AccountDeck 2 when you log in (a LaunchAgent; switch it off in the dashboard)
#   --prefix DIR       install below DIR instead of your home folder, for tests: the app, its support
#                      folder and the LaunchAgents folder all go there, launchctl and the Finder
#                      registration are skipped, and the app is not opened. Do not open that app by hand:
#                      it would run on your real data and logins.
#   --downloads DIR    take the pinned files from DIR when they are there (still checked against their
#                      SHA-256) instead of downloading them: an offline or repeated test install
#
# Running it again upgrades: the runtime is replaced only when the pins changed (or it fails its
# self-test), the app code only when the files changed, and your data stays.
set -euo pipefail
umask 022

BUNDLE_ID=local.accountdeck2
APP_NAME="AccountDeck 2"
EXECUTABLE=AccountDeck2
MIN_MACOS=14                    # Sonoma's WebKit supports the dashboard's dialogs and CSS
PY_VERSION=3.14.8
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
# What install.sh copies from a package or checkout into the app folder: everything except these.
EXCLUDES=(.git .gitignore '.venv*' .ruff_cache .pytest_cache .handoff .claude .agents .codex .aws node_modules package.json package-lock.json
          __pycache__ '*.pyc' .DS_Store third_party docs tests tools dist AGENTS.md CLAUDE.md VALIDATION.md
          'Install AccountDeck.command' 'Uninstall AccountDeck.command' 'START HERE.txt'
          '*.sqlite*' '*.db' '*.dpapi' '*.adeck' '*.log*' vault logs runtime runtime.json service.lock
          startup-error.txt install.json .source-sha256 'notes*' '.env*' '*.pem' '*.key')

say() { printf '%s\n' "$*"; }
die() { printf 'AccountDeck 2 was not installed: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---- pinned runtime: Apple silicon only ----
PY_URL=https://github.com/astral-sh/python-build-standalone/releases/download/20261003/cpython-3.14.8%2B20261003-aarch64-apple-darwin-install_only.tar.gz
PY_SHA=d15291f940cfecd2e54010d5e37d2e03aa192f076a65d26ab741372fff2dabfe
CFFI_URL=https://files.pythonhosted.org/packages/b3/a9/9db617d05d7367c1ad0ab00b3aa6e6f9281edd689b4ee9ea0e5a84e89c97/cffi-2.1.1-cp314-cp314-macosx_11_0_arm64.whl
CFFI_SHA=661c298b4821edebead0c91edd2b00374d67ad7c5a1f7a91d4442633b79d6a72
CRYPTO_URL=https://files.pythonhosted.org/packages/ba/19/797e2aaac9df6a66f1550f49979dc1b1e39ecd2077501c30efa81e8d5d67/cryptography-50.0.1-cp311-abi3-macosx_11_0_arm64.whl
CRYPTO_SHA=b8f852c65863251b9e3a1b8c150ce21e59b522dbb6a7d4bc80e680d38388e986

# The runtime self-test: a real AES-GCM round trip, which needs cffi's _cffi_backend, not only the import.
SELF_TEST='import os, sys
assert sys.version_info >= (3, 14), sys.version
import ctypes, fcntl, sqlite3, ssl, plistlib
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
key = AESGCM.generate_key(256); nonce = os.urandom(12)
box = AESGCM(key).encrypt(nonce, b"AccountDeck 2 self-test", b"aad")
assert AESGCM(key).decrypt(nonce, box, b"aad") == b"AccountDeck 2 self-test"
try:
    AESGCM(key).decrypt(nonce, box[:-1] + bytes([box[-1] ^ 1]), b"aad")
except InvalidTag:
    pass
else:
    raise AssertionError("a changed ciphertext was accepted")
print("runtime self-test passed:", sys.version.split()[0], "cryptography", __import__("cryptography").__version__)'

# Runs a runtime's python with a clean environment: no PYTHON*/PIP* variables, no user site, HOME in
# the work folder so nothing (pip's cache included) is written to the real home.
clean_python() {
    local python=$1; shift
    env -i PATH=/usr/bin:/bin HOME="$WORK" LANG=en_US.UTF-8 PIP_CONFIG_FILE=/dev/null PIP_NO_INPUT=1 \
        "$python" -I "$@"
}

self_test() { clean_python "$1" -c "$SELF_TEST"; }

fetch() {   # fetch URL SHA256 OUT
    local url=$1 sha=$2 out=$3 actual
    if [ -n "$DOWNLOADS" ] && [ -f "$DOWNLOADS/${url##*/}" ]; then
        cp "$DOWNLOADS/${url##*/}" "$out"
    else
        curl -fsSL --proto '=https' --tlsv1.2 --retry 2 --connect-timeout 30 -o "$out" "$url" \
            || die "could not download ${url##*/}. Check the internet connection and run install.sh again."
    fi
    actual=$(shasum -a 256 "$out" | cut -d ' ' -f 1)
    [ "$actual" = "$sha" ] || die "${url##*/} did not match its pinned SHA-256 (got $actual). Nothing was changed."
}

tree_hash() {   # one SHA-256 over every file's path and content below $1
    (cd "$1" && find . -type f ! -name install.json ! -name .source-sha256 -print0 | LC_ALL=C sort -z \
        | xargs -0 shasum -a 256 | shasum -a 256 | cut -d ' ' -f 1)
}

sq() {   # a single-quoted shell word
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

app_running() {   # a process of this user runs this install's run.py (started by the app or the agent)
    pgrep -U "$(id -u)" -f -- "$APPDIR/run.py" >/dev/null 2>&1
}

cleanup() {
    local status=$?
    trap - EXIT
    [ -n "${WORK:-}" ] && rm -rf "$WORK"
    exit "$status"
}

DOWNLOADS=""

main() {
    local launch=1 at_login=0 prefix="" manage=true
    while [ $# -gt 0 ]; do
        case "$1" in
        --no-launch) launch=0 ;;
        --start-at-login|--start-on-login) at_login=1 ;;
        --prefix) [ $# -ge 2 ] && [ -n "$2" ] || die "--prefix needs a folder."; prefix=$2; shift ;;
        --prefix=*) prefix=${1#--prefix=}; [ -n "$prefix" ] || die "--prefix needs a folder." ;;
        --downloads) [ $# -ge 2 ] && [ -d "$2" ] || die "--downloads needs an existing folder."; DOWNLOADS=$2; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option $1 (see bash install.sh --help)." ;;
        esac
        shift
    done

    # ---- this Mac ----
    [ "$(uname -s)" = Darwin ] || die "this installer is for macOS."
    [ "$(id -u)" -ne 0 ] || die "run it as yourself, not with sudo."
    local version major
    version=$(sw_vers -productVersion)
    major=${version%%.*}
    [ "$major" -ge "$MIN_MACOS" ] 2>/dev/null || die "Please update to macOS Sonoma ($MIN_MACOS) or newer. This Mac has macOS $version."
    # Native shells identify arm64 directly. Under Rosetta, hw.optional.arm64 still reports 1.
    if [ "$(uname -m)" != arm64 ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != 1 ]; then
        die "This download needs Apple silicon (M1 or newer). Intel Macs are not supported."
    fi

    # ---- where things go ----
    local src base
    src=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
    [ -f "$src/run.py" ] && [ -f "$src/accountdeck/__init__.py" ] && [ -f "$src/make_icon.py" ] \
        || die "run install.sh from the unpacked AccountDeck2-mac package folder."
    if [ -n "$prefix" ]; then
        mkdir -p "$prefix"
        base=$(cd "$prefix" && pwd -P)
        manage=false
        launch=0    # an app below a prefix has no data folder of its own: opened, it would run on the real data and logins
    else
        base=${HOME:-}
        [ -n "$base" ] && [ -d "$base" ] || die "your home folder was not found."
    fi
    APPS="$base/Applications"
    SUPPORT="$base/Library/Application Support/AccountDeck2"
    AGENTS_DIR="$base/Library/LaunchAgents"
    LOGS="$base/Library/Logs/AccountDeck2"
    RT="$SUPPORT/runtime"
    APPDIR="$SUPPORT/app"
    APP="$APPS/$APP_NAME.app"
    case "$src/" in "$APPDIR/"*|"$RT/"*) die "run install.sh from the package folder, not from the installed copy." ;; esac

    mkdir -p "$SUPPORT" "$APPS"
    chmod 700 "$SUPPORT"            # the data folder: private to you
    trap cleanup EXIT
    # Everything is built in a work folder of this run's own, so two installs started at once never write into
    # each other's files. An install that is killed leaves its folder behind; uninstall.sh removes those.
    WORK=$(mktemp -d "$SUPPORT/.install-XXXXXX")
    say "Installing $APP_NAME for $(id -un) (macOS $version) into $base"

    # ---- the app code: staged, compared with the installed copy ----
    local app_changed=0 new_hash pattern
    excludes=()
    APP_NEW="$WORK/app"
    mkdir "$APP_NEW"
    for pattern in "${EXCLUDES[@]}"; do excludes+=(--exclude "$pattern"); done
    (cd "$src" && tar -cf - "${excludes[@]}" .) | (cd "$APP_NEW" && tar -xf -)
    new_hash=$(tree_hash "$APP_NEW")
    if [ -f "$APPDIR/.source-sha256" ] && [ "$(cat "$APPDIR/.source-sha256")" = "$new_hash" ]; then
        say "App code: unchanged."
    else
        printf '%s\n' "$new_hash" > "$APP_NEW/.source-sha256"
        app_changed=1
    fi

    # ---- the runtime: kept when its pins match and it passes the self-test ----
    local pins rt_changed=0 crypto_whl cffi_whl
    pins=$(printf '%s %s\n' "$PY_URL" "$PY_SHA" "$CFFI_URL" "$CFFI_SHA" "$CRYPTO_URL" "$CRYPTO_SHA")
    if [ -x "$RT/python/bin/python3.14" ] && [ -f "$RT/PINS" ] && [ "$(cat "$RT/PINS")" = "$pins" ] \
            && self_test "$RT/python/bin/python3.14" >/dev/null 2>&1; then
        say "Runtime: Python $PY_VERSION with cryptography, unchanged."
    else
        say "Downloading Python $PY_VERSION, cffi and cryptography (about 30 MB)..."
        cffi_whl="$WORK/${CFFI_URL##*/}"           # wheels keep their real names: pip reads the tags
        crypto_whl="$WORK/${CRYPTO_URL##*/}"
        fetch "$PY_URL" "$PY_SHA" "$WORK/python.tar.gz"
        fetch "$CFFI_URL" "$CFFI_SHA" "$cffi_whl"
        fetch "$CRYPTO_URL" "$CRYPTO_SHA" "$crypto_whl"
        RT_NEW="$WORK/runtime"
        mkdir "$RT_NEW"
        tar -xzf "$WORK/python.tar.gz" -C "$RT_NEW"
        [ -x "$RT_NEW/python/bin/python3.14" ] || die "the Python download did not unpack as expected."
        clean_python "$RT_NEW/python/bin/python3.14" -m pip install --no-index --no-deps --no-cache-dir \
            --disable-pip-version-check --no-warn-script-location --quiet "$cffi_whl" "$crypto_whl" \
            || die "the packages could not be installed into the downloaded Python."
        self_test "$RT_NEW/python/bin/python3.14" >/dev/null || die "the downloaded runtime failed its self-test."
        printf '%s\n' "$pins" > "$RT_NEW/PINS"
        rt_changed=1
    fi

    if { [ "$app_changed" = 1 ] || [ "$rt_changed" = 1 ]; } && [ -d "$APPDIR" ] && app_running; then
        die "$APP_NAME is running. Choose Quit in its menu bar menu, then run install.sh again. Nothing was changed."
    fi

    if [ "$rt_changed" = 1 ]; then
        rm -rf "$RT.old"
        [ -d "$RT" ] && mv "$RT" "$RT.old"
        mv "$RT_NEW" "$RT"
        rm -rf "$RT.old"
    fi
    PY="$RT/python/bin/python3.14"
    self_test "$PY" || die "the installed runtime failed its self-test."

    if [ "$app_changed" = 1 ]; then
        rm -rf "$APPDIR.old"
        [ -d "$APPDIR" ] && mv "$APPDIR" "$APPDIR.old"
        mv "$APP_NEW" "$APPDIR"
        rm -rf "$APPDIR.old"
        say "App code: installed $(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$APPDIR/accountdeck/__init__.py")."
    fi

    # The install record autostart_mac reads (accountdeck/platform/autostart_mac.py, MANIFEST_VERSION).
    clean_python "$PY" -c 'import json, sys
launcher, agents, logs, manage, target = sys.argv[1:]
record = {"version": 1, "launcher": launcher, "launch_agents": agents, "logs": logs, "manage": manage == "true"}
with open(target + ".tmp", "w", encoding="utf-8") as handle:
    json.dump(record, handle, indent=2)
    handle.write("\n")
' "$APP/Contents/MacOS/$EXECUTABLE" "$AGENTS_DIR" "$LOGS" "$manage" "$APPDIR/install.json"
    mv "$APPDIR/install.json.tmp" "$APPDIR/install.json"

    # ---- the app bundle: built aside, swapped in only when it differs ----
    local app_version icns bundle_changed=0
    app_version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$APPDIR/accountdeck/__init__.py")
    [ -n "$app_version" ] || die "the app version was not found."
    BUNDLE_NEW="$WORK/bundle"
    mkdir -p "$BUNDLE_NEW/Contents/MacOS" "$BUNDLE_NEW/Contents/Resources"
    icns="$APPDIR/AccountDeck2.icns"
    if [ ! -f "$icns" ] || [ "$(head -c 4 "$icns")" != icns ]; then
        say "Drawing the app icon..."
        icns="$WORK/AccountDeck2.icns"
        clean_python "$PY" "$APPDIR/make_icon.py" --icns "$icns" >/dev/null
    fi
    cp "$icns" "$BUNDLE_NEW/Contents/Resources/AccountDeck2.icns"
    printf 'APPL????' > "$BUNDLE_NEW/Contents/PkgInfo"
    clean_python "$PY" -c 'import plistlib, sys
bundle_id, name, executable, version, minimum, target = sys.argv[1:]
info = {
    "CFBundleIdentifier": bundle_id, "CFBundleName": name, "CFBundleDisplayName": name,
    "CFBundleExecutable": executable, "CFBundleIconFile": "AccountDeck2", "CFBundlePackageType": "APPL",
    "CFBundleInfoDictionaryVersion": "6.0", "CFBundleDevelopmentRegion": "en",
    "CFBundleShortVersionString": version, "CFBundleVersion": version,
    "LSMinimumSystemVersion": minimum + ".0", "LSArchitecturePriority": ["arm64"],
    "LSApplicationCategoryType": "public.app-category.utilities",
    "LSUIElement": True,                    # the menu bar item is the app: no Dock icon, no app menu
    "LSMultipleInstancesProhibited": True,  # opening it again reaches the running copy (it opens the dashboard)
    "NSHighResolutionCapable": True, "NSSupportsAutomaticGraphicsSwitching": True,
}
with open(target, "wb") as handle:
    plistlib.dump(info, handle, sort_keys=False)
' "$BUNDLE_ID" "$APP_NAME" "$EXECUTABLE" "$app_version" "$MIN_MACOS" "$BUNDLE_NEW/Contents/Info.plist"
    plutil -lint "$BUNDLE_NEW/Contents/Info.plist" >/dev/null || die "the app's Info.plist is not valid."
    # The process that runs is this copy of the runtime's python, signed with the bundle id: macOS checks that
    # the running code's identity matches the bundle before it allows notifications. pyvenv.cfg points the
    # copy at the runtime's standard library and packages.
    cp -p "$PY" "$BUNDLE_NEW/Contents/MacOS/python3"
    codesign --force --sign - --identifier "$BUNDLE_ID" "$BUNDLE_NEW/Contents/MacOS/python3" >/dev/null 2>&1 \
        || die "the app's python could not be signed."
    printf 'home = %s\ninclude-system-site-packages = true\nversion = %s\n' "$RT/python/bin" "$PY_VERSION" \
        > "$BUNDLE_NEW/Contents/pyvenv.cfg"
    cat > "$BUNDLE_NEW/Contents/MacOS/$EXECUTABLE" <<LAUNCHER
#!/bin/sh
# $APP_NAME launcher, written by install.sh. It execs this bundle's python3 (same process, so the app keeps
# its identity, $BUNDLE_ID), and run.py --app opens the dashboard of the running service or becomes the
# service with the menu bar item.
APPROOT=$(sq "$APPDIR")
HERE=\$(cd "\$(dirname "\$0")" && pwd) || exit 1
cd "\$APPROOT" || exit 1
case "\${1-}" in -psn_*) shift ;; esac
case "\${1-}" in --app) ;; *) set -- --app "\$@" ;; esac
# -E -s, not -I: -I also drops run.py's folder from sys.path, and then accountdeck does not import.
exec "\$HERE/python3" -E -s "\$APPROOT/run.py" "\$@"
LAUNCHER
    chmod 755 "$BUNDLE_NEW/Contents/MacOS/$EXECUTABLE"
    "$BUNDLE_NEW/Contents/MacOS/python3" -E -s -c 'import cryptography.hazmat.primitives.ciphers.aead' 2>/dev/null \
        || die "the app's python does not find its runtime."
    xattr -dr com.apple.quarantine "$BUNDLE_NEW" "$RT" "$APPDIR" 2>/dev/null || true
    if [ -d "$APP" ] && diff -r "$APP" "$BUNDLE_NEW" >/dev/null 2>&1; then
        say "App: $APP unchanged."
    else
        rm -rf "$APP.old"
        [ -e "$APP" ] && mv "$APP" "$APP.old"
        mv "$BUNDLE_NEW" "$APP"
        rm -rf "$APP.old"
        bundle_changed=1
        say "App: $APP"
        if [ "$manage" = true ] && [ -x "$LSREGISTER" ]; then
            "$LSREGISTER" -f "$APP" >/dev/null 2>&1 || true    # Finder and Spotlight pick up the name and icon
        fi
    fi

    rm -rf "$WORK"; WORK=""

    if [ "$at_login" = 1 ]; then
        (cd "$APPDIR" && "$PY" -E -s "$APPDIR/run.py" --set-autostart on) \
            || die "the app is installed, but Open at login could not be switched on."
        [ "$manage" = true ] || say "Open at login: the LaunchAgent was written to $AGENTS_DIR (not loaded: --prefix)."
    fi

    if [ "$launch" = 1 ]; then
        if [ "$bundle_changed" = 1 ] && app_running; then
            say "$APP_NAME is running the previous app: choose Quit in its menu bar menu and open it again."
        else
            open "$APP" || die "the app is installed at $APP but could not be opened."
        fi
    fi
    say "$APP_NAME is installed. Your data in $SUPPORT was kept. To remove the app: bash $(sq "$APPDIR/uninstall.sh")"
}

main "$@"
exit 0
