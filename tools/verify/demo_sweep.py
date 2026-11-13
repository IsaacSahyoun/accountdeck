"""Proof run: start every demo scenario on synthetic data, read its API and pages, stop it cleanly.
Usage: demo_sweep.py <python> <code copy> [scenario ...]"""
import glob, http.cookiejar, json, os, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request

PY, COPY = sys.argv[1], sys.argv[2]
sys.path.insert(0, COPY)
os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"
from accountdeck import fixtures  # noqa: E402  (names only)

scenarios = sys.argv[3:] or list(fixtures.SCENARIOS)
tmp = tempfile.gettempdir()
web = os.path.join(COPY, "accountdeck", "web")
# The page loads its files from /static/<path>; the icon and the index have their own routes.
static = ["/", "/icon.svg"] + ["/static/" + os.path.relpath(os.path.join(d, f), web)
                               for d, _, fs in os.walk(web) for f in fs if f not in ("index.html", "icon.svg")]
rows, bad = [], 0
for name in scenarios:
    before = set(glob.glob(os.path.join(tmp, "accountdeck2-demo-*")))
    env = dict(os.environ, ACCOUNTDECK_SAFE_MODE="1")
    proc = subprocess.Popen([PY, "-E", "-s", "run.py", "--demo", name, "--port", "0", "--no-browser"],
                            cwd=COPY, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    note, ok = [], True
    try:
        line = proc.stdout.readline().strip()
        if "#token=" not in line:
            raise RuntimeError("no demo URL: " + line[:200] + proc.stdout.read()[:1500])
        origin, token = line.split(": ", 1)[1].split("/#token=")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        def call(path, body=None):
            headers, data = {"Accept": "application/json"}, None
            if body is not None:
                data = json.dumps(body).encode()
                headers.update({"Content-Type": "application/json", "X-AccountDeck": "1", "Origin": origin})
            try:
                with opener.open(urllib.request.Request(origin + path, data=data, headers=headers), timeout=20) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()
        # Before a session: the API must refuse.
        code, _ = call("/api/state")
        note.append(f"state-without-session={code}")
        ok &= code in (401, 403)
        code, _ = call("/api/session", {"token": "wrong-token"})
        note.append(f"wrong-token={code}")
        ok &= code in (401, 403)
        code, _ = call("/api/session", {"token": token})
        ok &= code == 200
        code, body = call("/api/state")
        state = json.loads(body) if code == 200 else {}
        ok &= code == 200 and isinstance(state, dict)
        accounts = state.get("accounts")
        note.append(f"state={code} ready={state.get('ready')} accounts={len(accounts) if isinstance(accounts, list) else accounts!r}")
        first = accounts[0].get("id") if isinstance(accounts, list) and accounts else None
        if first:
            code, _ = call("/api/history?id=" + urllib.parse.quote(str(first)))
            note.append(f"history={code}")
            ok &= code == 200
        code, _ = call("/api/diagnostics")
        note.append(f"diagnostics={code}")
        ok &= code == 200
        # A POST from another origin, or with no origin, must be refused even with a good session.
        for label, origin_header in (("other-origin", "http://evil.example"), ("no-origin", None)):
            headers = {"Content-Type": "application/json", "X-AccountDeck": "1"}
            if origin_header:
                headers["Origin"] = origin_header
            try:
                with opener.open(urllib.request.Request(origin + "/api/settings", data=b'{"patch": {}}', headers=headers), timeout=20) as r:
                    code = r.status
            except urllib.error.HTTPError as e:
                code = e.code
            note.append(f"{label}={code}")
            ok &= code == 403
        missing = [p for p in static if call(p)[0] != 200]
        note.append(f"pages={len(static) - len(missing)}/{len(static)}")
        ok &= not missing
        if missing:
            note.append("missing=" + ",".join(missing[:5]))
        outside = [call(p)[0] for p in ("/static/../../run.py", "/static/%2e%2e/%2e%2e/run.py",
                                        "/static/..%2f..%2frun.py", "/static//etc/hosts", "/static/js/../../../run.py")]
        note.append("outside-web=" + "/".join(map(str, outside)))
        ok &= all(c in (400, 403, 404) for c in outside)
        code, body = call("/api/stop", {})
        note.append(f"stop={code}")
        if code == 409:   # an operation is running in this scenario: say so, then end the process ourselves
            note.append("busy:" + body.decode("utf-8", "replace")[:80])
        try:
            rc = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill(); rc = proc.wait(); note.append("killed-after-timeout")
            ok &= code == 409
        note.append(f"exit={rc}")
        left = set(glob.glob(os.path.join(tmp, "accountdeck2-demo-*"))) - before
        note.append(f"temp-left={len(left)}")
        for d in left:   # only folders this run created
            subprocess.run(["/bin/rm", "-rf", d])
    except Exception as exc:
        ok = False
        note.append(f"ERROR {type(exc).__name__}: {exc}")
        if proc.poll() is None:
            proc.kill(); proc.wait()
    tail = (proc.stdout.read() or "").strip()
    if "Traceback" in tail:
        ok = False
        note.append("TRACEBACK " + tail[-600:])
    bad += not ok
    rows.append(f"{'ok  ' if ok else 'FAIL'} {name:20} " + " ".join(note))
    print(rows[-1], flush=True)
print(f"\n{len(scenarios) - bad}/{len(scenarios)} scenarios ok")
