#!/usr/bin/env python3
"""Sweep open pull requests: review the unreviewed ones, optionally merge clean ones.

Driven by a scheduled workflow rather than PR events. A `schedule` run has no
PR actor, no fork, and no bot-author special-casing, so it always gets the
repo's full GITHUB_TOKEN -- none of the restrictions that apply to
`pull_request` runs authored by dependabot[bot]/renovate[bot] exist here, and
no `pull_request_target` escape hatch is needed. A sweep also picks up PRs
that were already open before the workflow existed, which an event trigger
can never do retroactively.

Dedup: each PR gets ONE sweep comment, updated in place, carrying a hidden
`reviewed-sha:` line. A PR whose current head SHA already appears there is
skipped, so re-running the sweep costs nothing and never double-posts.

Everything that talks to GitHub goes through `gh` (already authenticated in
Actions via GH_TOKEN); the model call goes through openrouter_ai.py.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys

MARKER = "<!-- ai-review-sweep -->"
CLEAN_VERDICT = "VERDICT: CLEAN"
DIRTY_VERDICT = "VERDICT: NEEDS_REVIEW"

# Paths that must never reach the model, or are pointless to review.
DIFF_EXCLUDES = [
    ".env", ".env.*", "**/.env", "**/.env.*",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.cer", "*.crt", "*.jks", "*.keystore",
    ".git/**", "node_modules/**", "**/node_modules/**",
    "vendor/**", "dist/**", "build/**",
    "*.min.js", "*.map", "*.pyc",
    "**/*.pdf", "**/*.zip", "**/*.tar", "**/*.gz",
    "**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.gif",
    "**/*.ico", "**/*.woff", "**/*.woff2", "**/*.ttf",
]


def run(cmd: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def gh_json(args: list[str]) -> object:
    return json.loads(run(["gh", "api", *args]).stdout or "null")


def list_open_prs(repo: str) -> list[dict]:
    """Open PRs, oldest first so a backlog drains deterministically."""
    raw = gh_json([f"repos/{repo}/pulls?state=open&sort=created&direction=asc&per_page=100"])
    return raw or []


def existing_sweep_comment(repo: str, number: int) -> dict | None:
    comments = gh_json([f"repos/{repo}/issues/{number}/comments"]) or []
    ours = [c for c in comments if MARKER in (c.get("body") or "")]
    return ours[-1] if ours else None


def build_diff(base_sha: str, local_ref: str, out_path: pathlib.Path) -> None:
    cmd = ["git", "diff", f"{base_sha}...{local_ref}", "--"]
    cmd += [f":(exclude){p}" for p in DIFF_EXCLUDES]
    result = run(cmd, check=False)
    text = result.stdout or ""
    if not text.strip():
        text = "No reviewable changes in this PR.\n"
    out_path.write_text(text)


def review_one(pr: dict, args: argparse.Namespace, system_file: pathlib.Path) -> str | None:
    """Return the review text, or None if the model call failed."""
    number = pr["number"]
    local_ref = f"pr-{number}"
    fetched = run(
        ["git", "fetch", "--quiet", "origin", f"pull/{number}/head:{local_ref}"],
        check=False,
    )
    if fetched.returncode != 0:
        print(f"Could not fetch head of PR #{number}, skipping.", file=sys.stderr)
        return None

    diff_path = pathlib.Path(".ai/diff.txt")
    build_diff(pr["base"]["sha"], local_ref, diff_path)

    # The PR title is untrusted input: it is written into a prompt file as
    # data, never interpolated into a shell command.
    user_path = pathlib.Path(".ai/review-user.txt")
    user_path.write_text(
        f"PR #{number} title: {pr['title']}\n\n"
        f"Diff (max {args.max_chars} chars):\n"
        + diff_path.read_text()[: args.max_chars]
    )

    proc = run(
        [
            sys.executable, args.ai_script,
            "--system-file", str(system_file),
            "--prompt-file", str(user_path),
            "--max-chars", str(args.max_chars),
            "--timeout", str(args.timeout),
        ],
        check=False,
    )
    if proc.returncode != 0:
        print(
            f"Model call failed for PR #{number}: {proc.stderr.strip()[:400]} "
            "— leaving it for the next sweep.",
            file=sys.stderr,
        )
        return None
    return proc.stdout


def split_verdict(review: str) -> tuple[bool, str]:
    """(is_clean, review_without_the_verdict_line).

    Exact match on the dedicated last line, never a substring search over
    prose -- a sentence like "no [Critical] issues found" must not read as
    dirty, and a model that forgets the line must not read as clean.
    """
    stripped = review.rstrip()
    lines = stripped.splitlines()
    last = lines[-1].strip() if lines else ""
    is_clean = last == CLEAN_VERDICT
    if last in (CLEAN_VERDICT, DIRTY_VERDICT):
        stripped = "\n".join(lines[:-1]).rstrip()
    return is_clean, stripped


def comment_body(heading: str, review: str, head_sha: str, is_clean: bool, will_merge: bool) -> str:
    if is_clean:
        verdict = "clean — merging" if will_merge else "clean"
    else:
        verdict = "needs a human"
    return (
        f"{MARKER}\n"
        f"<!-- reviewed-sha: {head_sha} -->\n\n"
        f"## {heading}\n\n"
        f"{review}\n\n"
        f"_Verdict: {verdict}. Reviewed commit `{head_sha[:7]}`._\n"
    )


def post_comment(repo: str, number: int, body: str, existing: dict | None) -> None:
    path = pathlib.Path(".ai/comment.md")
    path.write_text(body)
    if existing:
        run(["gh", "api", "-X", "PATCH",
             f"repos/{repo}/issues/comments/{existing['id']}",
             "-F", f"body=@{path}", "--silent"])
    else:
        run(["gh", "api", "-X", "POST",
             f"repos/{repo}/issues/{number}/comments",
             "-F", f"body=@{path}", "--silent"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Review open PRs and optionally merge clean ones.")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--system-file", required=True, help="Rendered system prompt")
    parser.add_argument("--ai-script", required=True, help="Path to openrouter_ai.py")
    parser.add_argument("--authors", default="", help="Comma-separated logins; empty = all authors")
    parser.add_argument("--max-prs", type=int, default=10)
    parser.add_argument("--max-chars", type=int, default=140_000)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--heading", default="AI Review (sweep)")
    parser.add_argument("--auto-merge", action="store_true")
    parser.add_argument("--merge-method", default="squash", choices=["merge", "squash", "rebase"])
    args = parser.parse_args()

    pathlib.Path(".ai").mkdir(exist_ok=True)
    system_file = pathlib.Path(args.system_file)
    authors = {a.strip() for a in args.authors.split(",") if a.strip()}

    reviewed = merged = skipped = 0
    for pr in list_open_prs(args.repo):
        if reviewed >= args.max_prs:
            print(f"Reached --max-prs={args.max_prs}, stopping.")
            break

        number, head_sha = pr["number"], pr["head"]["sha"]
        author = (pr.get("user") or {}).get("login", "")
        if authors and author not in authors:
            continue

        existing = existing_sweep_comment(args.repo, number)
        if existing and f"reviewed-sha: {head_sha}" in (existing.get("body") or ""):
            print(f"PR #{number} already reviewed at {head_sha[:7]} — skipping.")
            skipped += 1
            continue

        print(f"::group::Reviewing PR #{number} ({author}): {pr['title']}")
        review = review_one(pr, args, system_file)
        if review is None:
            print("::endgroup::")
            continue

        is_clean, body_text = split_verdict(review)
        will_merge = is_clean and args.auto_merge
        post_comment(args.repo, number,
                     comment_body(args.heading, body_text, head_sha, is_clean, will_merge),
                     existing)
        reviewed += 1

        if will_merge:
            done = run(["gh", "pr", "merge", str(number),
                        f"--{args.merge_method}", "--repo", args.repo], check=False)
            if done.returncode == 0:
                print(f"Merged PR #{number}.")
                merged += 1
            else:
                print(f"::warning::Review was clean but merging PR #{number} failed: "
                      f"{done.stderr.strip()[:300]}")
        print("::endgroup::")

    summary = (
        "## PR review sweep\n\n"
        f"- reviewed: {reviewed}\n"
        f"- merged: {merged}\n"
        f"- skipped (already reviewed at current head): {skipped}\n"
    )
    print(summary)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
