"""Fixed scanner for the refactor (standard library only). It lists four kinds of leftover code so that a
person or an agent edits from a list instead of from a hunch:

  unused      a function, class or module constant whose name appears nowhere but its own definition
  tests-only  the same, when only tests/ mention it
  long        a function longer than LONG_LINES lines
  repeated    a block of REPEAT_LINES or more code lines that appears in more than one place

Usage: python tools/slop_scan.py [--json] [--kind unused|tests-only|long|repeated] [path prefix ...]
The repo root is the parent of this file's folder. Names are matched as whole words in every source file, so
a name used through getattr("name") counts as used. A finding is a candidate, not a verdict: read before
deleting."""
import ast
import collections
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
LONG_LINES = 80
REPEAT_LINES = 6
REPEAT_CHARS = 240
PRODUCT = ("accountdeck", "run.py")
JS_EXPORT = re.compile(r"^export\s+(?:async\s+)?(?:function\*?|const|let|class)\s+([A-Za-z_$][\w$]*)", re.M)
JS_LOCAL = re.compile(r"^(?:async\s+)?function\*?\s+([A-Za-z_$][\w$]*)", re.M)


def files(suffixes, roots):
    for root in roots:
        path = ROOT / root
        found = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for item in found:
            if item.suffix in suffixes and "__pycache__" not in item.parts:
                yield item


def rel(path):
    return str(path.relative_to(ROOT))


def words(paths):
    counts = collections.Counter()
    for path in paths:
        counts.update(re.findall(r"[A-Za-z_$][\w$]*", path.read_text(encoding="utf-8", errors="replace")))
    return counts


def python_definitions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name, node.lineno, node.end_lineno, type(node).__name__
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in targets:
            if isinstance(target, ast.Name):
                yield target.id, node.lineno, node.end_lineno, "constant"


def scan_names():
    product_py = list(files({".py"}, PRODUCT))
    test_py = list(files({".py"}, ("tests", "tools")))
    other = list(files({".js", ".html", ".sh"}, ("accountdeck/web", "install.sh", "uninstall.sh")))
    in_product, in_tests, elsewhere = words(product_py), words(test_py), words(other)
    unused, tests_only = [], []
    for path in product_py:
        for name, line, _end, kind in python_definitions(path):
            if name.startswith("__") and name.endswith("__"):
                continue
            if in_product[name] + elsewhere[name] > 1:
                continue
            item = {"where": f"{rel(path)}:{line}", "name": name, "what": kind}
            (tests_only if in_tests[name] else unused).append(item)
    js = list(files({".js"}, ("accountdeck/web",)))
    js_words = words(js + list(files({".html"}, ("accountdeck/web",))))
    for path in js:
        text = path.read_text(encoding="utf-8")
        for pattern, kind in ((JS_EXPORT, "js export"), (JS_LOCAL, "js function")):
            for match in pattern.finditer(text):
                if js_words[match.group(1)] <= 1:
                    unused.append({"where": f"{rel(path)}:{text.count(chr(10), 0, match.start()) + 1}",
                                   "name": match.group(1), "what": kind})
    return unused, tests_only


def scan_long():
    found = []
    for path in files({".py"}, PRODUCT):
        for name, line, end, kind in python_definitions(path):
            if kind != "ClassDef" and kind != "constant" and end - line + 1 > LONG_LINES:
                found.append({"where": f"{rel(path)}:{line}", "name": name, "lines": end - line + 1})
    return sorted(found, key=lambda item: -item["lines"])


def code_lines(path):
    """(line number, text) of each line that is code: no blanks, no comment-only lines."""
    comment = "#" if path.suffix in (".py", ".sh") else "//"
    for number, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        text = " ".join(raw.split())
        if text and not text.startswith(comment):
            yield number, text


def scan_repeated():
    seen = collections.defaultdict(list)
    lines_of = {}
    for path in list(files({".py"}, PRODUCT)) + list(files({".js"}, ("accountdeck/web",))):
        lines = lines_of[rel(path)] = list(code_lines(path))
        for index in range(len(lines) - REPEAT_LINES + 1):
            window = "\n".join(text for _, text in lines[index:index + REPEAT_LINES])
            if len(window) >= REPEAT_CHARS:
                seen[hashlib.sha1(window.encode()).hexdigest()].append((rel(path), index))
    # Join windows that overlap into one block per group of places.
    blocks, taken = [], set()
    for places in seen.values():
        if len(places) < 2 or all(place in taken for place in places):
            continue
        length = REPEAT_LINES
        while True:
            nxt = [(name, index + length) for name, index in places]
            if any(index >= len(lines_of[name]) for name, index in nxt):
                break
            if len({lines_of[name][index][1] for name, index in nxt}) != 1:
                break
            length += 1
        for name, index in places:
            taken.update((name, index + step) for step in range(length - REPEAT_LINES + 1))
        blocks.append({"lines": length, "places": [f"{name}:{lines_of[name][index][0]}" for name, index in places]})
    return sorted(blocks, key=lambda block: -block["lines"])


def main(argv):
    as_json = "--json" in argv
    kind = argv[argv.index("--kind") + 1] if "--kind" in argv else None
    prefixes = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] != "--kind")]
    unused, tests_only = scan_names()
    report = {"unused": unused, "tests-only": tests_only, "long": scan_long(), "repeated": scan_repeated()}
    if prefixes:
        def wanted(item):
            places = item.get("places") or [item["where"]]
            return any(place.startswith(tuple(prefixes)) for place in places)
        report = {name: [item for item in items if wanted(item)] for name, items in report.items()}
    if kind:
        report = {kind: report[kind]}
    if as_json:
        print(json.dumps(report, indent=1))
        return
    for name, items in report.items():
        print(f"== {name} ({len(items)})")
        for item in items:
            if name == "repeated":
                print(f"  {item['lines']:>3} lines  " + "  ==  ".join(item["places"]))
            elif name == "long":
                print(f"  {item['lines']:>3} lines  {item['where']}  {item['name']}")
            else:
                print(f"  {item['where']}  {item['name']}  ({item['what']})")


if __name__ == "__main__":
    main(sys.argv[1:])
