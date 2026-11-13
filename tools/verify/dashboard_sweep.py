"""Proof run: open the real dashboard page of every demo scenario in headless Chrome. A page fails on a script
error, and when its two account lists are not the demo's accounts: one left out, one drawn twice, or one in
the wrong list. look_sweep.py takes the pictures, in the WebKit of the app's window.
Usage: dashboard_sweep.py <python> <code copy> <chrome-headless-shell> [scenario ...]
With ACCOUNTDECK_VERIFY_LOGS set, each page's HTML is kept in that folder."""
import html.parser, json, os, re, shutil, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from demo_run import Demo, end_on_kill  # noqa: E402

LISTS = {"accounts": ("claude_cli",), "other-accounts": ("claude", "codex", "muse")}    # the clients each list draws


class Rows(html.parser.HTMLParser):
    """The account rows in a page's HTML: {list id: [account id, ...]} for every `li.row` inside a list."""

    def __init__(self, dom):
        super().__init__()
        self.open, self.rows = [], {name: [] for name in LISTS}
        self.feed(dom)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.open.append((tag, attrs.get("id")))
        inside = next((name for _tag, name in reversed(self.open) if name in LISTS), None)
        if inside and tag == "li" and "row" in (attrs.get("class") or "").split():
            self.rows[inside].append(attrs.get("data-id"))

    def handle_endtag(self, tag):
        for at in range(len(self.open) - 1, -1, -1):        # a tag with no end, like <input>, closes with its parent
            if self.open[at][0] == tag:
                del self.open[at:]
                break


def not_drawn(dom, accounts):
    """What is wrong with the page's account lists, as words. Empty when each list holds its clients'
    accounts, each one once."""
    drawn, wrong = Rows(dom).rows, []
    for name, clients in LISTS.items():
        if f'id="{name}"' not in dom:
            wrong.append(f"no #{name} list")
        want = sorted(str(a["id"]) for a in accounts if a.get("provider") in clients)
        got = sorted(map(str, drawn[name]))
        if want != got:
            labels = {str(a["id"]): a.get("label") for a in accounts}
            missing = [labels[i] for i in want if i not in got]
            extra = [labels.get(i, i) for i in got if got.count(i) > 1 or i not in want]
            wrong.append(f"#{name} drew {len(got)} of {len(want)} rows" + (f", missing {missing}" if missing else "")
                         + (f", not its own or twice {sorted(set(map(str, extra)))}" if extra else ""))
    return wrong


def sweep(python, copy, chrome, name, logs=None):
    """(ok, words) for one scenario."""
    note, ok = [], True
    try:
        demo = Demo(python, copy, name)
    except RuntimeError as exc:
        return False, f"ERROR {exc}"
    profile = tempfile.mkdtemp(prefix="accountdeck-lead-chrome-")
    try:
        page = subprocess.run([chrome, "--headless", "--dump-dom", "--virtual-time-budget=9000", "--enable-logging=stderr",
                               "--v=0", "--no-first-run", "--disable-gpu", "--window-size=1280,2400",
                               f"--user-data-dir={profile}", demo.url], capture_output=True, text=True, timeout=90)
        dom = page.stdout
        console = [line for line in page.stderr.splitlines() if "CONSOLE" in line]
        errors = [line for line in console if re.search(r"Uncaught|could not|Error|error|Refused|violat", line)]
        still_loading = "Loading your terminal accounts" in dom
        demo.call("/api/session", {"token": demo.token})
        code, body = demo.call("/api/state")
        accounts = json.loads(body)["accounts"] if code == 200 else None
        wrong = not_drawn(dom, accounts) if accounts is not None else [f"no state to compare with ({code})"]
        note.append(f"dom={len(dom)} console={len(console)} errors={len(errors)} still-loading={still_loading} "
                    + (f"rows={len(accounts)}/{len(accounts)}" if not wrong else "ROWS " + "; ".join(wrong)))
        ok &= len(dom) > 5000 and not errors and not still_loading and not wrong
        for line in errors[:4]:
            note.append("\n      " + line[-300:])
        if logs:
            open(os.path.join(logs, f"dom-{name}.html"), "w").write(dom)
    except Exception as exc:
        ok = False
        note.append(f"ERROR {type(exc).__name__}: {exc}")
    finally:
        tail = demo.stop()
        shutil.rmtree(profile, ignore_errors=True)
    if "Traceback" in tail:
        ok = False
        note.append("TRACEBACK " + tail[-500:])
    return ok, " ".join(note)


def main(python, copy, chrome, scenarios):
    sys.path.insert(0, copy)
    os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"
    from accountdeck import fixtures  # noqa: E402  (names only)
    scenarios, bad = scenarios or list(fixtures.SCENARIOS), 0
    for name in scenarios:
        ok, words = sweep(python, copy, chrome, name, os.environ.get("ACCOUNTDECK_VERIFY_LOGS"))
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name:20} {words}", flush=True)
    print(f"\n{len(scenarios) - bad}/{len(scenarios)} dashboards ok")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    end_on_kill()
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:])
