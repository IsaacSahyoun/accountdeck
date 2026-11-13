"""Drive one demo's dashboard step by step in the WebKit of the app's own window, and keep the evidence.
Fake data only: the demo is started here, on its own temp data, and stopped at the end.

Usage: drive.py <python> <code copy> <scenario> <proof folder> '<JSON list of steps>'
       drive.py <python> <code copy> <scenario> <proof folder> <steps.json>      (a file is easier to quote)

Steps, one key each (plus the options named):
  {"more": "Beta", "client": "claude"}   open the More menu of that account's row
  {"click": "Rename…"}                   click the button with exactly these words; "in": "menu", "sheet" or a
                                         CSS selector narrows where to look, and so does "row" with "client"
  {"type": "Work account"}               type into the field that has the keyboard
  {"press": "Enter"}                     Enter, Escape, ArrowDown, ArrowUp, Home, End, Tab or " "
  {"wait": "<JavaScript>"}               wait until it is true, 15 seconds at most; "seconds": 40 waits longer
  {"js": "<JavaScript>"}                 print its value
  {"row": "Beta", "client": "claude"}    print the words of that account's row
  {"words": "sheet"}                     print the words in the menu, the sheet or a CSS selector
  {"accounts": true}                     print what the app itself says: client, name, in use (/api/state)
  {"in_use": "Gamma", "client": "claude"}  fail unless the app itself says that account is the one in use
  {"shot": "before"}                     save <proof folder>/before.png, the whole page
  {"dark": true}                         dark or light

Clients: claude_cli (Claude Code), claude (Claude Desktop), codex, muse. Clicks are real mouse events and
fail when something covers the button, as they would for a person. A click waits 15 seconds for its button.
`words` and `row` print screen-reader words too, like "(done)" after a step. `accounts` checks nothing:
`in_use` is the step that fails. The run fails on a step that fails and on anything the page's console
shows in red. The last line is "ok" or "FAILED".

The demo runs from <code copy>. The helpers that find rows and buttons come from this repo's own
tests/test_dashboard.py, so on a copy of an older commit a row or a button may not be found. This tool
drives WebKit only, and it removes the demo's fake disk when it ends."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_run import Demo  # noqa: E402
from tests import test_dashboard as page  # noqa: E402

PLACES = {"menu": page.MENU, "sheet": page.SHEET, None: "document"}
WIDTH, HEIGHT = 1180, 820


def account(demo, step):
    """The account a step names, as the app describes it now."""
    accounts = json.loads(demo.call("/api/state")[1])["accounts"]
    name = step.get("more") or step.get("row") or step.get("in_use")
    found = [a for a in accounts if a["label"] == name and a["provider"] == step.get("client", a["provider"])]
    if len(found) != 1:
        raise AssertionError(f"{len(found)} accounts are named {name!r}; say which with \"client\"")
    return found[0]


def run(step, window, demo, proof):
    if "more" in step:
        window.click(page.more(account(demo, step)))
        return window.wait(f"{page.MENU}.matches(':popover-open') && [...{page.MENU}.children].map((n) => n.textContent.trim())")
    if "click" in step:
        place = step.get("in")
        inside = page.row(account(demo, step)) if "row" in step else PLACES.get(place) or f"document.querySelector({json.dumps(place)})"
        return window.click(page.button(step["click"], inside), f"the button {step['click']!r}")
    if "type" in step:
        return window.type(step["type"])
    if "press" in step:
        return window.press(step["press"])
    if "wait" in step:
        return window.wait(step["wait"], seconds=step.get("seconds", page.WAIT))
    if "js" in step:
        return window.js(step["js"])
    if "words" in step:
        place = PLACES.get(step["words"]) or f"document.querySelector({json.dumps(step['words'])})"
        return window.wait(f"{place}.innerText").replace("\n", " | ")
    if "row" in step:
        return window.wait(f"{page.row(account(demo, step))}.innerText").replace("\n", " | ")
    if "in_use" in step:
        if not account(demo, step).get("active"):
            raise AssertionError(f"the app does not say {step['in_use']!r} is in use")
        return "the app says so"
    if "accounts" in step:
        return [(a["provider"], a["label"], "in use" if a.get("active") else "") for a in
                json.loads(demo.call("/api/state")[1])["accounts"]]
    if "shot" in step:
        tall = max(HEIGHT, window.js("document.documentElement.scrollHeight"))
        window.window.call("size", width=WIDTH, height=tall)
        window.window.call("shot", path=str(proof / f"{step['shot']}.png"))
        window.window.call("size", width=WIDTH, height=HEIGHT)
        return str(proof / f"{step['shot']}.png")
    if "dark" in step:
        return window.window.call("dark", on=bool(step["dark"]))["ok"]
    raise AssertionError(f"no such step: {step}")


def main(python, copy, scenario, proof, steps):
    proof.mkdir(parents=True, exist_ok=True)
    undo, failed = [], 0
    demo = Demo(python, copy, scenario)
    try:
        code, body = demo.call("/api/session", {"token": demo.token})
        state = json.loads(demo.call("/api/state")[1])
        if code != 200 or state.get("mode") != "demo" or not state.get("safe_mode"):
            raise AssertionError(f"this is not a demo in safe mode: mode={state.get('mode')} safe_mode={state.get('safe_mode')}")
        print(f"demo '{scenario}' of {copy}: mode={state['mode']} safe_mode={state['safe_mode']} version={state.get('version')}")
        window = page.AppWindow(lambda function, *args: undo.append((function, args)))
        port, token = demo.origin.rsplit(":", 1)[1], demo.token
        window.open(SimpleNamespace(server_port=int(port), token=token))
        for number, step in enumerate(steps, 1):
            try:
                print(f"{number}. {json.dumps(step, ensure_ascii=False)} -> {run(step, window, demo, proof)}")
            except AssertionError as exc:
                failed += 1
                print(f"{number}. {json.dumps(step, ensure_ascii=False)} FAILED: {exc}")
                break
        problems = window.problems
        print(f"console: {problems or 'clean'}")
        failed += bool(problems)
    finally:
        for function, args in reversed(undo):
            function(*args)
        left = demo.stop()
        if "Traceback" in left:
            failed += 1
            print("the demo crashed:\n" + left[-1200:])
    print("FAILED" if failed else "ok")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 6:
        sys.exit(__doc__)
    steps = sys.argv[5]
    if not steps.lstrip().startswith("["):
        steps = Path(steps).read_text(encoding="utf-8")
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4]), json.loads(steps)))
