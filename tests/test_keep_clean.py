"""Keeps the code clean after the 2026-11-12 refactor. The scanner in tools/slop_scan.py lists names nothing
uses, functions over 80 lines and repeated blocks. Every item it reports must be on the keep list,
tools/slop_keep.txt, with a reason. A new item fails this test: delete it, merge it, split it, or add it to
the list and say why. An entry the scanner no longer reports fails too, so the list only gets shorter."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
KEEP = ROOT / "tools" / "slop_keep.txt"


def scanner():
    spec = importlib.util.spec_from_file_location("slop_scan", ROOT / "tools" / "slop_scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def found():
    """What the scanner reports now, as (kind, key). Keys carry no line numbers, so moving code is free."""
    scan = scanner()
    unused, tests_only = scan.scan_names()
    items = set()
    for kind, rows in (("unused", unused), ("tests-only", tests_only), ("long", scan.scan_long())):
        items.update((kind, f"{row['where'].rsplit(':', 1)[0]}::{row['name']}") for row in rows)
    for block in scan.scan_repeated():
        items.add(("repeated", " == ".join(sorted({place.rsplit(":", 1)[0] for place in block["places"]}))))
    return items


def kept():
    """The keep list: one `kind | key | reason` per line; blank lines and # comments are skipped."""
    entries = {}
    for number, line in enumerate(KEEP.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|", 2)]
        if len(parts) != 3 or not all(parts):
            raise AssertionError(f"tools/slop_keep.txt line {number} needs `kind | key | reason`")
        entries[(parts[0], parts[1])] = parts[2]
    return entries


class KeepCleanTest(unittest.TestCase):
    def test_everything_the_scanner_reports_is_on_the_keep_list(self):
        new = sorted(found() - set(kept()))
        self.assertEqual(new, [], "new leftover code: fix it, or add it to tools/slop_keep.txt with a reason")

    def test_the_keep_list_has_no_entry_the_scanner_no_longer_reports(self):
        gone = sorted(set(kept()) - found())
        self.assertEqual(gone, [], "these were cleaned up: remove their lines from tools/slop_keep.txt")


if __name__ == "__main__":
    unittest.main()
