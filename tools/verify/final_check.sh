#!/bin/zsh
# The whole final check, on fake data only, against a clean copy of the code.
# Usage: final_check.sh <clean code copy> <log folder> [under-load]
#   COPY=$(mktemp -d); git archive <commit> | tar -x -C "$COPY"; final_check.sh "$COPY" ~/.accountdeck/proof/<task>
# Read every file it writes: a line that says FAIL, ERROR, SKIPPED or "exit 1" is a failed check. Nothing here stops
# at the first failure, so one run shows everything that is wrong.
set -u
COPY="$1"; LOGS="$2"; HERE="${0:A:h}"
PY="$HOME/Library/Application Support/AccountDeck2/runtime/python/bin/python3.14"
CHROME=$(find "$HOME/Library/Caches/ms-playwright" -name chrome-headless-shell -type f | head -1)
JSC=/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc
mkdir -p "$LOGS"
export ACCOUNTDECK_SAFE_MODE=1
cd "$COPY" || exit 2

"$PY" -E -s "$HERE/doctor.py" "$COPY" > "$LOGS/doctor.txt" 2>&1 || { cat "$LOGS/doctor.txt"; exit 2; }

# The suite twice: the dashboard in the WebKit of the app's own window, then in headless Chrome.
suite() {   # suite <name> [VAR=value]
  env ${2:-X=} "$PY" -E -s -m unittest discover -s tests -t . > "$LOGS/$1-full.txt" 2>&1; local code=$?
  { grep -E "^(FAIL|ERROR):" "$LOGS/$1-full.txt"; tail -4 "$LOGS/$1-full.txt"
    # A dashboard test skips when its browser engine is missing, and unittest still says OK.
    tail -1 "$LOGS/$1-full.txt" | grep -q "skipped=" && echo "SKIPPED: tests did not run in this pass, see $1-full.txt"
    echo "exit $code"; } > "$LOGS/$1.txt"
}
suite final-suite
suite chrome-suite ACCOUNTDECK_TEST_BROWSER=chrome

{
  echo "== policy";            "$PY" -E -s "$HERE/policy_probe.py" "$COPY"
  echo "== refill";            "$PY" -E -s "$HERE/refill_probe.py" "$COPY"
  echo "== remove signed-in";  "$PY" -E -s "$HERE/remove_probe.py" "$COPY" signed-in
  echo "== remove signed-out"; "$PY" -E -s "$HERE/remove_probe.py" "$COPY" signed-out
  echo "== renewal";           "$PY" -E -s "$HERE/switch_renewal_probe.py" "$COPY"
  echo "== wait";              "$PY" -E -s "$HERE/wait_probe.py" "$COPY"
} > "$LOGS/final-probes.txt" 2>&1

{
  for parallel in 6 12 64; do
    echo "== burst $parallel parallel"; "$PY" -E -s "$HERE/static_burst.py" "$PY" "$COPY" $parallel 5
  done
} > "$LOGS/final-burst.txt" 2>&1

"$PY" -E -s "$HERE/demo_sweep.py" "$PY" "$COPY" > "$LOGS/final-demo-sweep.txt" 2>&1
"$PY" -E -s "$HERE/dashboard_sweep.py" "$PY" "$COPY" "$CHROME" > "$LOGS/final-dashboard-sweep.txt" 2>&1
# Every demo dashboard in the app's WebKit window: pictures, the words on the page, and the words it cuts off.
"$PY" -E -s "$HERE/look_sweep.py" shoot "$PY" "$COPY" "$LOGS/look" > "$LOGS/final-look.txt" 2>&1

{
  echo "python files that compile: $(find "$COPY" -name '*.py' -not -path '*/__pycache__/*' | while read f; do "$PY" -E -s -c 'import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")' "$f" && echo ok; done | grep -c ok) of $(find "$COPY" -name '*.py' -not -path '*/__pycache__/*' | wc -l | tr -d ' ')"
  echo "javascript files that parse: $(find "$COPY/accountdeck/web" -name '*.js' | while read f; do "$JSC" -e "checkSyntax('$f')" && echo ok; done | grep -c ok) of $(find "$COPY/accountdeck/web" -name '*.js' | wc -l | tr -d ' ')"
  for script in "$COPY"/*.sh "$COPY"/*.command; do [ -f "$script" ] && bash -n "$script" && echo "shell syntax ok: ${script:t}"; done
  for script in "$COPY"/tools/verify/*.sh(N); do zsh -n "$script" && echo "shell syntax ok: ${script:t}"; done
  swiftc -typecheck "$COPY/tests/webkit_host.swift" && echo "swift checks: webkit_host.swift"
  echo "scanner: $("$PY" -E -s "$COPY/tools/slop_scan.py" 2>&1 | grep '^==' | tr '\n' ' ')"
} > "$LOGS/final-syntax.txt" 2>&1

# A third pass of the suite with every core busy, to catch tests that depend on timing.
if [ "${3:-}" = "under-load" ]; then
  # Each busy loop runs only while this script is its parent, so a run that is cut off leaves none behind.
  pids=(); for n in {1..$(sysctl -n hw.ncpu)}; do "$PY" -I -c "import os
while os.getppid() == $$:
    for _ in range(200000): pass" & pids+=($!); done
  suite loaded-suite
  kill $pids 2>/dev/null
fi
echo done
