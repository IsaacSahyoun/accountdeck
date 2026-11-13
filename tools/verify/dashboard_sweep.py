"""Proof run: open the real dashboard page of every demo scenario in headless Chrome and look for script
errors and sections that failed to render. look_sweep.py does the same in the WebKit of the app's window.
Usage: dashboard_sweep.py <python> <code copy> <chrome-headless-shell> [scenario ...]
With ACCOUNTDECK_VERIFY_LOGS set, each page's HTML is kept in that folder."""
import glob, http.cookiejar, json, os, re, shutil, subprocess, sys, tempfile, urllib.error, urllib.request

PY, COPY, CHROME = sys.argv[1:4]
LOGS = os.environ.get("ACCOUNTDECK_VERIFY_LOGS")
sys.path.insert(0, COPY)
os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"
from accountdeck import fixtures  # noqa: E402  (names only)

scenarios = sys.argv[4:] or list(fixtures.SCENARIOS)
tmp = tempfile.gettempdir()
bad = 0
for name in scenarios:
    before = set(glob.glob(os.path.join(tmp, "accountdeck2-demo-*")))
    proc = subprocess.Popen([PY, "-E", "-s", "run.py", "--demo", name, "--port", "0", "--no-browser"],
                            cwd=COPY, env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    profile = tempfile.mkdtemp(prefix="accountdeck-lead-chrome-")
    note, ok = [], True
    try:
        line = proc.stdout.readline().strip()
        url = line.split(": ", 1)[1]
        origin, token = url.split("/#token=")
        page = subprocess.run([CHROME, "--headless", "--dump-dom", "--virtual-time-budget=9000", "--enable-logging=stderr",
                               "--v=0", "--no-first-run", "--disable-gpu", "--window-size=1280,2400",
                               f"--user-data-dir={profile}", url], capture_output=True, text=True, timeout=90)
        dom = page.stdout
        console = [l for l in page.stderr.splitlines() if "CONSOLE" in l]
        errors = [l for l in console if re.search(r"Uncaught|could not|Error|error|Refused|violat", l)]
        still_loading = "Loading your terminal accounts" in dom
        sections = {s: len(re.findall(rf'id="{s}"[^>]*>(.*?)</(?:section|aside|div)>', dom, re.S) and
                          re.search(rf'id="{s}"[^>]*>(.*?)</(?:section|aside|div)>', dom, re.S).group(1).strip())
                    for s in ("accounts", "other-accounts", "overview", "details")}
        note.append(f"dom={len(dom)} console={len(console)} errors={len(errors)} still-loading={still_loading} "
                    + " ".join(f"{k}={v}" for k, v in sections.items()))
        ok &= len(dom) > 5000 and not errors and not still_loading
        for l in errors[:4]:
            note.append("\n      " + l[-300:])
        if LOGS:
            open(os.path.join(LOGS, f"dom-{name}.html"), "w").write(dom)
    except Exception as exc:
        ok = False
        note.append(f"ERROR {type(exc).__name__}: {exc}")
    finally:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                 urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            def post(path, body):
                req = urllib.request.Request(origin + path, data=json.dumps(body).encode(), headers={
                    "Content-Type": "application/json", "X-AccountDeck": "1", "Origin": origin})
                try:
                    return opener.open(req, timeout=15).status
                except urllib.error.HTTPError as e:
                    return e.code
            post("/api/session", {"token": token})
            code = post("/api/stop", {})
            try:
                proc.wait(timeout=15 if code == 200 else 1)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
        except Exception:
            if proc.poll() is None:
                proc.kill(); proc.wait()
        for d in set(glob.glob(os.path.join(tmp, "accountdeck2-demo-*"))) - before:   # only this run's folder
            shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)
    tail = (proc.stdout.read() or "").strip()
    if "Traceback" in tail:
        ok = False
        note.append("TRACEBACK " + tail[-500:])
    bad += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name:20} " + " ".join(note), flush=True)
print(f"\n{len(scenarios) - bad}/{len(scenarios)} dashboards ok")
