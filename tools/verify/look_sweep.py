"""Pictures of every demo dashboard as the app's own window draws it (WebKit, 1180 wide, light and dark),
and the pixels that differ between two sets of them. Fake data only.

  look_sweep.py shoot <python> <code copy> <out folder> [scenario ...]
  look_sweep.py compare <folder a> <folder b> <out folder>

Shoot the code before a change and after it, then compare: every difference is either meant or a bug.
Both sets start the demo clock at the same moment, so the same account shows the same numbers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_run import Demo  # noqa: E402
from tests import webkit  # noqa: E402

NOW = "2026-11-12T15:00:00"
WIDTH, HEIGHT = 1180, 820                       # the app window's first size (dashboard_window.DEFAULT_SIZE)
DRAWN = ("document.querySelectorAll('.section-title').length > 0"
         " && !document.body.innerText.includes('Loading your terminal accounts')")
# Words the page cuts off: a box that hides what does not fit and holds more than fits. "dots" ends in an
# ellipsis. "cut" ends in the middle of a letter: either the page never asked for an ellipsis, or it asked
# on a flex or grid box, where browsers do not draw one. Text hidden on purpose for screen readers
# (.sr-only) is not counted.
CUT = """[...document.querySelectorAll('body *')].filter((e) => {
  const style = getComputedStyle(e);
  return e.getClientRects().length && !e.closest('.sr-only') && ['hidden', 'clip'].includes(style.overflowX)
    && e.scrollWidth > e.clientWidth + 1 && e.textContent.trim();
}).map((e) => { const style = getComputedStyle(e);
  const dots = style.textOverflow === 'ellipsis' && !/flex|grid/.test(style.display);
  return `${dots ? 'dots' : 'cut '} ${String(e.className || e.tagName).slice(0, 40)}: `
    + e.textContent.trim().replace(/\\s+/g, ' ').slice(0, 70); })"""


def drawn(window, url):
    """Loads the page until it is drawn. The 2.0.0 server drops requests at random, so a page that came up
    without one of its files is loaded again. Returns the number of loads it took."""
    for load in range(1, 9):
        window.problems()
        window.call("load", url=url)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if window.js(DRAWN):
                if not any("could not load" in line for line in window.problems()):
                    return load
                break
            time.sleep(0.05)
    raise RuntimeError("the page never drew")


def shoot(python, copy, out, scenarios):
    sys.path.insert(0, copy)
    os.environ["ACCOUNTDECK_SAFE_MODE"] = "1"
    from accountdeck import fixtures               # the scenario names of that copy
    out.mkdir(parents=True, exist_ok=True)
    window, bad = webkit.AppWindow("app"), 0
    try:
        for name in scenarios or list(fixtures.SCENARIOS):
            demo, note = Demo(python, copy, name, NOW), ""
            try:
                loads = drawn(window, demo.url)
                time.sleep(1.0)                    # meters and banners ease in
                for theme in ("light", "dark"):
                    window.call("dark", on=theme == "dark")
                    window.call("size", width=WIDTH, height=HEIGHT)
                    tall = max(HEIGHT, window.js("document.documentElement.scrollHeight"))
                    window.call("size", width=WIDTH, height=tall)
                    window.call("shot", path=str(out / f"{name}-{theme}.png"))
                window.call("size", width=WIDTH, height=HEIGHT)
                (out / f"{name}.txt").write_text(window.js("document.body.innerText") + "\n", encoding="utf-8")
                cut = window.js(CUT)
                (out / f"{name}-cut.txt").write_text("".join(line + "\n" for line in cut), encoding="utf-8")
                problems = window.problems()
                note = (f"loads={loads} tall={tall} cut={sum(line.startswith('cut') for line in cut)} "
                        f"dots={sum(line.startswith('dots') for line in cut)} console={len(problems)} "
                        + " | ".join(problems[:3]))
                bad += bool(problems)
            except Exception as exc:
                bad += 1
                note = f"ERROR {type(exc).__name__}: {exc}"
            finally:
                left = demo.stop()
                window.call("dark", on=False)
                window.call("size", width=WIDTH, height=HEIGHT)
            if "Traceback" in left:
                bad += 1
                note += " TRACEBACK " + left[-400:]
            print(f"{name:20} {note}", flush=True)
    finally:
        window.close()
    print(f"\n{bad} scenarios with a problem")
    return 1 if bad else 0


def compare(a, b, out):
    out.mkdir(parents=True, exist_ok=True)
    names = sorted(p.name for p in a.glob("*.png") if (b / p.name).exists())
    only = sorted({p.name for p in a.glob("*.png")} ^ {p.name for p in b.glob("*.png")})
    differ = 0
    for name in names:
        done = subprocess.run([webkit.host(), "diff", str(a / name), str(b / name), str(out / name)],
                              capture_output=True, text=True)
        report = json.loads(done.stdout)
        differ += bool(report.get("different"))
        print(f"{name:34} different={report.get('different'):>8} a={report.get('a')} b={report.get('b')} "
              f"bands={len(report.get('bands', []))}", flush=True)
    for name in only:
        print(f"{name:34} in one set only")
    print(f"\n{differ} of {len(names)} pictures differ; the differing bands are in {out} (first set above second)")
    return 1 if differ or only else 0


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "shoot":
        sys.exit(shoot(sys.argv[2], sys.argv[3], Path(sys.argv[4]), sys.argv[5:]))
    if len(sys.argv) == 5 and sys.argv[1] == "compare":
        sys.exit(compare(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])))
    sys.exit(__doc__)
