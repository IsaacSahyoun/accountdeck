"""Builds and audits a review folder for AccountDeck. It copies committed code only and runs none of it.

  review.py new <name> <base commit | -> <head commit> <intent.md> [context file ...]
  review.py audit <name>

`new` makes ~/.accountdeck/reviews/<name>/ (never reused) with:
  code/           the whole repo at <head>
  base/           the whole repo at <base>            (left out when <base> is -, a whole-app review)
  product.patch   what the change did to the app      tests.patch   what it did to the tests
  diffstat.txt    commits.txt
  BRIEF.md        the reviewer brief, with your intent file and the attack list in it
  out/            one answer file per reviewer        lead/         your proof runs
  logs/manifest.before   a checksum of every file the reviewers may read

`audit` says whether the copies are untouched, whether a file was added, and whether each answer file is
whole: over 1 KB, with its Coverage section, ending in the line "END OF REVIEW". It passes only when three
answers are whole. Run it before you read an answer. ACCOUNTDECK_REVIEWS moves the reviews folder, for a
trial."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys

SKILL = Path(__file__).resolve().parents[1]
REPO = SKILL.parents[2]
ROOT = Path(os.environ.get("ACCOUNTDECK_REVIEWS") or Path.home() / ".accountdeck" / "reviews")
READ = ("code", "base")                     # what a reviewer reads and must leave as it is
SEATS = 3                                   # a review is this many reviewers who each finished
LAST_LINE = "END OF REVIEW"                 # the brief has a reviewer write it when the answer is done


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True).stdout


def unpack(commit, folder):
    folder.mkdir()
    archive = subprocess.run(["git", "-C", str(REPO), "archive", commit], capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(folder)], input=archive, check=True)


def manifest(folder):
    lines = []
    for part in READ:
        for path in sorted((folder / part).rglob("*")) if (folder / part).is_dir() else []:
            if path.is_file():
                lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(folder)}")
    return lines


def new(name, base, head, intent, context):
    folder = ROOT / name
    if folder.exists():
        sys.exit(f"{folder} exists. A review folder is never reused: pick a new name.")
    intent_text = Path(intent).read_text(encoding="utf-8").strip()
    short = git("rev-parse", "--short", head).strip()
    folder.mkdir(parents=True)
    for part in ("out", "logs", "lead"):
        (folder / part).mkdir()
    unpack(head, folder / "code")
    contents = [f"- `code/`: the whole repo at commit {short}. Give locations relative to `code/`, as `<path>:<line>`."]
    if base != "-":
        unpack(base, folder / "base")
        product = git("diff", base, head, "--", ".", ":!tests")
        tests = git("diff", base, head, "--", "tests")
        (folder / "product.patch").write_text(product, encoding="utf-8")
        (folder / "tests.patch").write_text(tests, encoding="utf-8")
        (folder / "diffstat.txt").write_text(git("diff", "--stat", base, head), encoding="utf-8")
        (folder / "commits.txt").write_text(git("log", "--format=%h %s%n%n%b", f"{base}..{head}"), encoding="utf-8")
        contents += [
            f"- `base/`: the whole repo before the change, commit {git('rev-parse', '--short', base).strip()}.",
            f"- `product.patch`: everything the change did to the app ({git('diff', '--shortstat', base, head, '--', '.', ':!tests').strip()}). "
            f"`tests.patch`: what it did to the tests ({git('diff', '--shortstat', base, head, '--', 'tests').strip()}).",
            "- `diffstat.txt` and `commits.txt`: the file list and the commit messages. A commit message says what its "
            "author believed. Check it against the code.",
        ]
    else:
        contents.append("- This is a review of the whole app, so there is no `base/` and no patch.")
    for file in context:
        copy = folder / Path(file).name
        copy.write_bytes(Path(file).read_bytes())
        contents.append(f"- `{copy.name}`: context from the lead.")
    brief = (SKILL / "reviewer-brief.md").read_text(encoding="utf-8")
    attacks = (SKILL / "attacks.md").read_text(encoding="utf-8")
    attacks = attacks[attacks.index("\n## ") + 1:].strip()            # the list, without the note to builders above it
    for slot, text in (("{FOLDER}", str(folder) + "/"), ("{CONTENTS}", "\n".join(contents)), ("{INTENT}", intent_text),
                       ("{ATTACKS}", ("\n" + attacks).replace("\n## ", "\n### ").strip())):
        brief = brief.replace(slot, text)
    (folder / "BRIEF.md").write_text(brief, encoding="utf-8")
    (folder / "logs" / "manifest.before").write_text("\n".join(manifest(folder)) + "\n", encoding="utf-8")
    print(f"{folder}\n  {len(manifest(folder))} files for the reviewers, brief of {len(brief.splitlines())} lines")
    print("Send each reviewer: Read " + str(folder / "BRIEF.md") + " and follow it. Your answer file is "
          + str(folder / "out") + "/<model>.md")


def audit(name):
    folder = ROOT / name
    before = (folder / "logs" / "manifest.before").read_text(encoding="utf-8").splitlines()
    after = manifest(folder)
    (folder / "logs" / "manifest.after").write_text("\n".join(after) + "\n", encoding="utf-8")
    changed = sorted(set(before) ^ set(after))
    print(f"copies: {len(after)} files, " + ("untouched" if not changed else f"CHANGED, this run does not count:\n  "
                                              + "\n  ".join(changed[:20])))
    bad, whole_answers = bool(changed), 0
    for answer in sorted((folder / "out").glob("*.md")):
        text = answer.read_text(encoding="utf-8", errors="replace")
        size, ended = answer.stat().st_size, text.rstrip().rpartition("\n")[2].strip() == LAST_LINE
        note = ("under 1 KB: likely a usage-limit message, not a review" if size < 1024
                else "no Coverage section: cut off" if "## Coverage" not in text
                else f"no last line \"{LAST_LINE}\": cut off, or its reviewer is still writing" if not ended else "ok")
        bad |= note != "ok"
        whole_answers += note == "ok"
        print(f"{answer.name}: {size / 1024:.0f} KB, {text.count(chr(10) + '### ')} findings, {note}")
    if whole_answers < SEATS:
        print(f"{whole_answers} of {SEATS} reviews are whole: this review is not done")
    return 1 if bad or whole_answers < SEATS else 0


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "new":
        sys.exit(new(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6:]))
    if len(sys.argv) == 3 and sys.argv[1] == "audit":
        sys.exit(audit(sys.argv[2]))
    sys.exit(__doc__)
