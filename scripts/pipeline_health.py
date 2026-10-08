#!/usr/bin/env python3
"""Does the pipeline itself still work? A check nobody has to remember to run.

The agent loop removes the person who would have noticed that something silently stopped. Each check
here is one of those silences, all seen for real:

- a commit reached the base branch and no build ever started for it (a merge made with a token that
  starts no workflow: three dependency bumps went unbuilt and unverified for hours);
- a pull request was certified although one of the agents could not do its job (the test steward lost
  its reply, the change merged without the test it had asked for);
- one of the agent workflows fails, so the reviews, repairs or merges it owns do not happen.

Everything is read from the GitHub API and decided in code; no model is involved. The first case is also
repaired: the build is started on the branch. The command exits 1 when anything is found, so the run is red.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys

from pr_review_sweep import gh_json, run

# Commits the pipeline adds to the base branch by itself (image tag bump, changelog). Made with the job
# token, so no workflow starts for them, by design.
BOOKKEEPING = re.compile(r"^(Done\s+by Github Actions|docs\(changelog\))", re.IGNORECASE)

# What the engine writes in its report when an agent could not do its job. The engine and this check use
# the same list (agent_pipeline imports it), so a new wording cannot go unnoticed here.
DEGRADED_NOTES = (
    "returned no usable reply",
    "were not applicable",
    "was not evaluated",
    "were not checked",
    "could not be identified, so no verdict was given",
)
CERTIFIED_RE = re.compile(r"<!--\s*agent-certified:\s*[0-9a-f]{7,40}\s*-->")
AGENT_COMMENT = "<!-- agent-pr -->"


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def unbuilt_commits(commits: list[dict], runs: list[dict], now: dt.datetime, grace_minutes: int) -> list[dict]:
    """Commits on the base branch that no build covers. `commits` newest first.

    A commit is covered by a run on itself, or by a run on a later commit that is still going or succeeded
    (the image built there contains it). The pipeline's own bookkeeping commits never need one. A commit
    younger than the grace period is left alone: its run may not have been created yet."""
    by_sha = {r.get("head_sha"): r for r in reversed(runs)}  # newest run of each commit wins
    missing, covered_by_later = [], False
    for commit in commits:
        sha = commit.get("sha", "")
        message = ((commit.get("commit") or {}).get("message") or "").splitlines()[0] if commit.get("commit") else ""
        when = parse_time(((commit.get("commit") or {}).get("committer") or {}).get("date") or now.isoformat())
        run_ = by_sha.get(sha)
        if run_ is not None:
            if run_.get("status") != "completed" or run_.get("conclusion") == "success":
                covered_by_later = True
            continue
        if covered_by_later or BOOKKEEPING.match(message):
            continue
        if (now - when).total_seconds() < grace_minutes * 60:
            continue
        missing.append({"sha": sha, "subject": message, "date": when.isoformat()})
    return missing


def degraded_certifications(comments: list[dict]) -> list[dict]:
    """Agent reports that certify a commit while saying one of the agents could not do its job."""
    found = []
    for comment in comments:
        body = comment.get("body") or ""
        if AGENT_COMMENT not in body or not CERTIFIED_RE.search(body):
            continue
        hits = [note for note in DEGRADED_NOTES if note in body]
        if hits:
            number = (comment.get("issue_url") or "").rstrip("/").rsplit("/", 1)[-1]
            found.append({"pr": number, "notes": hits, "url": comment.get("html_url", "")})
    return found


def failing_agent_workflows(runs: list[dict], names: tuple[str, ...]) -> list[dict]:
    """Runs of the agent workflows themselves that ended in failure: what they own did not happen."""
    return [{"name": r.get("name", ""), "url": r.get("html_url", ""), "created": r.get("created_at", "")}
            for r in runs if r.get("conclusion") == "failure" and r.get("name") in names]


def render(unbuilt: list[dict], degraded: list[dict], failing: list[dict], started: bool) -> str:
    lines = ["## Pipeline health", ""]
    if not (unbuilt or degraded or failing):
        return "\n".join(lines + ["Nothing found: every commit on the base branch is covered by a build, no certification "
                                  "hides an agent that failed, and no agent workflow is failing."])
    if unbuilt:
        lines += [f"### {len(unbuilt)} commit(s) on the base branch with no build", ""]
        lines += [f"- `{c['sha'][:7]}` {c['subject']}" for c in unbuilt]
        lines += ["", "The build was started on the branch." if started else "The build could NOT be started.", ""]
    if degraded:
        lines += [f"### {len(degraded)} certification(s) given although an agent could not do its job", ""]
        lines += [f"- PR #{d['pr']}: {', '.join(d['notes'])} ({d['url']})" for d in degraded]
        lines.append("")
    if failing:
        lines += [f"### {len(failing)} failed run(s) of the agent workflows", ""]
        lines += [f"- {f['name']} at {f['created']} ({f['url']})" for f in failing]
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--build-workflow", required=True, help="File name of the workflow that builds the base branch, e.g. pipeline.yml")
    parser.add_argument("--agent-workflows", default="", help="Comma-separated NAMES of the agent workflows whose failed runs are reported")
    parser.add_argument("--hours", type=int, default=48, help="How far back to look")
    parser.add_argument("--grace-minutes", type=int, default=20)
    parser.add_argument("--start-missing-build", action="store_true", help="Start the build workflow on the branch when commits have none")
    args = parser.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    commits = gh_json([f"repos/{args.repo}/commits?sha={args.branch}&since={since}&per_page=100"]) or []
    builds = (gh_json([f"repos/{args.repo}/actions/workflows/{args.build_workflow}/runs?branch={args.branch}&per_page=100"]) or {}).get("workflow_runs", [])
    comments = gh_json([f"repos/{args.repo}/issues/comments?since={since}&per_page=100&sort=updated&direction=desc"]) or []
    recent = (gh_json([f"repos/{args.repo}/actions/runs?status=failure&created=>={since}&per_page=100"]) or {}).get("workflow_runs", [])
    names = tuple(n.strip() for n in args.agent_workflows.split(",") if n.strip())

    unbuilt = unbuilt_commits(commits, builds, now, args.grace_minutes)
    degraded = degraded_certifications(comments)
    failing = failing_agent_workflows(recent, names)

    started = False
    if unbuilt and args.start_missing_build:
        done = run(["gh", "workflow", "run", args.build_workflow, "--repo", args.repo, "--ref", args.branch], check=False)
        started = done.returncode == 0
        if not started:
            print(f"::error::could not start {args.build_workflow}: {done.stderr.strip()[:300]}")

    report = render(unbuilt, degraded, failing, started)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write(report + "\n")
    return 1 if (unbuilt or degraded or failing) else 0


if __name__ == "__main__":
    sys.exit(main())
