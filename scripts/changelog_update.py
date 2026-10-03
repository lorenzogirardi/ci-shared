#!/usr/bin/env python3
"""Keep a Keep-a-Changelog file up to date, deterministically (no model).

Two entry points:

  add        append one entry under `## [Unreleased]` (used by the agent
             pipeline for the change it just made)
  from-push  add one entry per commit of a push to the default branch that did
             not already update the changelog itself

Why deterministic: "updated on every merge" is a guarantee, and a guarantee
should not depend on a model call succeeding. The entry text is the commit
subject, which for a squash merge is the PR title.

Idempotent: an entry is skipped when its PR number or short SHA already
appears in the file, so re-running a workflow never duplicates a line.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

CATEGORIES = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security", "Dependencies")
HEADER = (
    "# Changelog\n\n"
    "All notable changes to this project are documented in this file.\n\n"
    "The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).\n"
    "Entries are added automatically on every merge to the default branch.\n\n"
)
UNRELEASED = "## [Unreleased]"

# Commits the pipeline itself makes: they must never produce an entry, or the
# changelog would log its own bookkeeping.
_SKIP_SUBJECT = re.compile(r"^(Done\s+by Github Actions|docs\(changelog\))", re.IGNORECASE)
_PR_REF = re.compile(r"\(#(\d+)\)\s*$")
_CONVENTIONAL = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?(?P<bang>!)?:\s*(?P<rest>.+)$")


def category_for(subject: str) -> str:
    """Map a commit subject to a changelog category."""
    lowered = subject.lower()
    if lowered.startswith("update dependency") or lowered.startswith("update ") and " action" in lowered:
        return "Dependencies"
    match = _CONVENTIONAL.match(subject)
    if not match:
        return "Changed"
    kind = match.group("type")
    if kind == "feat":
        return "Added"
    if kind == "fix":
        return "Fixed"
    if kind == "security":
        return "Security"
    if kind in ("deps", "build") and ("bump" in lowered or "dependenc" in lowered):
        return "Dependencies"
    return "Changed"


def clean_subject(subject: str) -> str:
    match = _CONVENTIONAL.match(subject)
    text = match.group("rest") if match else subject
    text = _PR_REF.sub("", text).strip()
    return text[:1].upper() + text[1:] if text else text


def _ensure_file(text: str) -> str:
    if UNRELEASED not in text:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else HEADER) + UNRELEASED + "\n"
    return text if text.endswith("\n") else text + "\n"


def add_entry(text: str, category: str, entry: str) -> str:
    """Return `text` with `- entry` appended to the right category of the
    Unreleased section, creating the section and category in canonical order."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}")
    text = _ensure_file(text)
    lines = text.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.strip() == UNRELEASED)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    heading = f"### {category}"
    # find the category inside the Unreleased block
    cat_at = next((i for i in range(start + 1, end) if lines[i].strip() == heading), None)
    bullet = f"- {entry}"
    if cat_at is not None:
        stop = next((i for i in range(cat_at + 1, end) if lines[i].startswith("### ")), end)
        insert_at = stop
        while insert_at > cat_at + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, bullet)
        return "\n".join(lines)
    # new category: place it before the first existing category that sorts after it
    order = {c: n for n, c in enumerate(CATEGORIES)}
    insert_at = end
    for i in range(start + 1, end):
        m = re.match(r"^### (\w+)", lines[i])
        if m and order.get(m.group(1), 99) > order[category]:
            insert_at = i
            break
    if insert_at == end:
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines[insert_at:insert_at] = ["", heading, bullet] + ([""] if end < len(lines) else [])
    else:
        lines[insert_at:insert_at] = [heading, bullet, ""]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out)


def already_logged(text: str, ref: str | None, short_sha: str | None) -> bool:
    if ref and re.search(rf"(?<!\d)#{re.escape(ref)}(?!\d)", text):
        return True
    return bool(short_sha and short_sha in text)


def format_entry(subject: str, pr: str | None, short_sha: str, repo_url: str) -> str:
    text = clean_subject(subject)
    if pr:
        return f"{text} ([#{pr}]({repo_url}/pull/{pr}))" if repo_url else f"{text} (#{pr})"
    return f"{text} ([`{short_sha}`]({repo_url}/commit/{short_sha}))" if repo_url else f"{text} (`{short_sha}`)"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def commits_in_push(before: str, after: str) -> list[str]:
    """Oldest-first SHAs of a push. A brand new branch (all-zero `before`) is
    treated as just its tip commit."""
    if not before or set(before) == {"0"}:
        return [after]
    return _git("rev-list", "--no-merges", "--reverse", f"{before}..{after}").split()


def entries_for_push(path: pathlib.Path, before: str, after: str, repo_url: str) -> int:
    text = path.read_text() if path.is_file() else ""
    added = 0
    for sha in commits_in_push(before, after):
        subject = _git("log", "-1", "--format=%s", sha).strip()
        if _SKIP_SUBJECT.match(subject):
            continue
        if str(path) in _git("show", "--name-only", "--format=", sha).split("\n"):
            # The change updated the changelog itself (e.g. an agent PR):
            # its entry is already there, adding another would duplicate it.
            continue
        match = _PR_REF.search(subject)
        pr, short = (match.group(1) if match else None), sha[:7]
        if already_logged(text, pr, short):
            continue
        text = add_entry(text, category_for(subject), format_entry(subject, pr, short, repo_url))
        added += 1
    if added:
        path.write_text(text)
    return added


def main() -> int:
    parser = argparse.ArgumentParser(description="Update CHANGELOG.md deterministically.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="append one entry to Unreleased")
    add.add_argument("--file", default="CHANGELOG.md")
    add.add_argument("--category", required=True, choices=CATEGORIES)
    add.add_argument("--entry", required=True)
    push = sub.add_parser("from-push", help="one entry per commit of a push")
    push.add_argument("--file", default="CHANGELOG.md")
    push.add_argument("--before", default="")
    push.add_argument("--after", required=True)
    push.add_argument("--repo-url", default="")
    args = parser.parse_args()

    path = pathlib.Path(args.file)
    if args.cmd == "add":
        text = path.read_text() if path.is_file() else ""
        path.write_text(add_entry(text, args.category, args.entry))
        print(f"added to {path}")
        return 0
    count = entries_for_push(path, args.before, args.after, args.repo_url.rstrip("/"))
    print(f"{count} changelog entr{'y' if count == 1 else 'ies'} added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
