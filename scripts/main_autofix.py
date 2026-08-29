#!/usr/bin/env python3
"""Repair a broken push to a protected branch (e.g. main) by opening a PR.

Companion to pr_review_sweep.py's `autofix_one`, for the case that has no PR
at all: a push straight to main broke CI. There is no existing branch to push
a verified fix to, and a fix must never land on main without going through
the same gate as any other change -- so this script checks out a NEW branch
from the broken commit, runs the same langgraph propose/explore/verify loop
(autofix_core.py) used for PRs, and on success opens a PR instead of pushing
directly. That PR then goes through the caller repo's normal per-PR checks
(e.g. pr-checks.yml) and can be picked up by the scheduled sweep like any
other PR -- nothing here merges anything.

Called from a job that runs only `if: failure()` on the same push's own
pipeline, so `collect_failure_logs` can read this exact run's failed checks
without needing a PR number to look them up by.
"""

from __future__ import annotations

import argparse
import os
import pathlib

from pr_review_sweep import _autofix_report, build_diff, collect_failure_logs, run, run_verify

from autofix_core import run_autofix_graph


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair a broken push by opening a fix PR.")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--head-sha", required=True, help="The broken commit")
    parser.add_argument("--base-sha", required=True, help="Last known-good commit, for the diff")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--run-number", type=int, required=True, help="github.run_number, for naming")
    parser.add_argument("--ai-script", required=True, help="Path to openrouter_ai.py")
    parser.add_argument("--max-chars", type=int, default=140_000)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--verify-command-file", default="")
    parser.add_argument("--verify-timeout", type=int, default=180)
    parser.add_argument("--max-autofix-attempts", type=int, default=10)
    args = parser.parse_args()

    pathlib.Path(".ai").mkdir(exist_ok=True)

    logs = collect_failure_logs(args.repo, args.head_sha, args.max_chars // 2)
    if not logs.strip():
        print("No failure logs found for this commit yet -- nothing to work from.")
        return 0

    diff_path = pathlib.Path(".ai/diff.txt")
    build_diff(args.base_sha, args.head_sha, diff_path)
    diff = diff_path.read_text()

    branch = f"autofix/main-{args.run_number}"
    if run(["git", "checkout", "--quiet", "-B", branch, args.head_sha], check=False).returncode != 0:
        print(f"::error::could not check out a new branch from {args.head_sha}")
        return 1

    verify_command = ""
    if args.verify_command_file:
        vf = pathlib.Path(args.verify_command_file)
        if vf.is_file():
            verify_command = vf.read_text()

    # Same reason as autofix_one: put the actually-new dependency state on
    # disk before the model's first prompt, discarding the (expected) failure.
    run_verify(verify_command, args.verify_timeout)

    result = run_autofix_graph(
        header=(
            f"Push to {args.base_branch} at {args.head_sha[:7]} broke CI "
            "(no PR -- this was a direct push)"
        ),
        logs=logs,
        diff=diff,
        verify_command=verify_command,
        verify_timeout=args.verify_timeout,
        max_attempts=args.max_autofix_attempts,
        ai_script=args.ai_script,
        max_chars=args.max_chars,
        timeout=args.timeout,
        identifier=args.run_number,
    )
    print(f"autofix {result['outcome']}: {result['detail']}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(f"## Main-branch autofix\n\n- outcome: {result['outcome']}\n- {result['detail']}\n")

    if result["outcome"] != "ready":
        # main was already red; this just means no fix was found. Nothing
        # pushed, nothing to clean up -- the branch checked out above is
        # local to this job's throwaway workspace.
        return 1

    run(["git", "config", "user.name", "ci-shared autofix"], check=False)
    run(["git", "config", "user.email", "actions@github.com"], check=False)
    run(["git", "add", *result["changed"]])
    title = f"fix: automated repair for red {args.base_branch} build (run {args.run_number})"
    message = (
        f"{title}\n\n{result['explanation']}\n\n"
        "Written by the ci-shared CI autofix and pushed unreviewed (verified "
        "locally). Opened as a PR rather than pushed to the protected branch "
        "directly -- the normal per-PR checks decide whether it merges."
    )
    committed = run(["git", "commit", "--quiet", "-m", message], check=False)
    if committed.returncode != 0:
        reason = (committed.stderr or committed.stdout or "").strip()[:200]
        print(f"::error::commit failed: {reason or 'no output from git'}")
        return 1

    pushed = run(["git", "push", "--quiet", "-u", "origin", f"HEAD:refs/heads/{branch}"], check=False)
    if pushed.returncode != 0:
        print(f"::error::push rejected: {pushed.stderr.strip()[:200]}")
        return 1

    body_path = pathlib.Path(".ai/pr-body.md")
    body_path.write_text(
        _autofix_report("pushed", result["detail"])
        + f"\n\nOpened automatically because `{args.head_sha[:7]}` broke `{args.base_branch}`."
    )
    created = run(
        [
            "gh", "pr", "create",
            "--repo", args.repo,
            "--base", args.base_branch,
            "--head", branch,
            "--title", title,
            "--body-file", str(body_path),
        ],
        check=False,
    )
    if created.returncode != 0:
        print(f"::error::gh pr create failed: {created.stderr.strip()[:300]}")
        return 1

    print(f"Opened a PR from {branch} against {args.base_branch}: {created.stdout.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
