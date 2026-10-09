#!/usr/bin/env python3
"""Does the pipeline itself still work? A check nobody has to remember to run.

The agent loop removes the person who would have noticed that something silently stopped. Each check
here is one of those silences, all seen for real:

- a commit reached the base branch and no build ever started for it (a merge made with a token that
  starts no workflow: three dependency bumps went unbuilt and unverified for hours);
- a pull request was certified although one of the agents could not do its job (the test steward lost
  its reply, the change merged without the test it had asked for);
- one of the agent workflows fails, so the reviews, repairs or merges it owns do not happen;
- a pull request sits open with no verdict on its head commit and nothing left to run (two agent runs
  raced, the second overwrote the first one's certification: green, uncertified, waiting for nobody).

Everything is read from the GitHub API and decided in code; no model is involved. The first case is also
repaired (the build is started on the branch) and so is the last (the pull request is abandoned, which is
a terminal state: a new push starts a new attempt). The command exits 1 when anything is found, so the run is red.
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
    "could not produce a usable answer",
    "could not be identified, so no verdict was given",
    # Wording of engine versions before an agent's failure blocked the certification.
    "returned no usable reply",
    "were not applicable",
)
LEGACY_NOTES = ("returned no usable reply", "were not applicable")
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


def failing_agent_workflows(runs: list[dict], names: tuple[str, ...], last_success: dict | None = None) -> list[dict]:
    """Agent workflows that are failing NOW: a failed run with no successful run of the same workflow after
    it. A workflow that failed yesterday and has passed since is working; reporting it for two more days
    only teaches the reader to ignore a red health check. `last_success` maps a workflow id to the creation
    time of its latest successful run."""
    last_success = last_success or {}
    failing = []
    for r in runs:
        if r.get("conclusion") != "failure" or r.get("name") not in names or never_started(r):
            continue
        recovered = last_success.get(r.get("workflow_id"))
        if recovered and recovered > (r.get("created_at") or ""):
            continue
        failing.append({"name": r.get("name", ""), "url": r.get("html_url", ""), "created": r.get("created_at", "")})
    return failing


def never_started(run_: dict) -> bool:
    """A pull request opened with the job token (the canary's, the agent's own fix) makes GitHub record a run
    that fails at once without a single job; the real run is the one started by the push that follows. That
    phantom is not an agent workflow failing."""
    return (run_.get("event") == "pull_request"
            and ((run_.get("triggering_actor") or run_.get("actor") or {}).get("login") == "github-actions[bot]"))


def stuck_pull_requests(pulls: list[dict], comments_of, checks_of, now: dt.datetime, minutes: int,
                        skip_authors: tuple[str, ...] = ()) -> list[dict]:
    """Open pull requests whose head commit has no verdict (neither certified nor abandoned) although
    nothing has happened on it for `minutes`. Every path of the pipeline ends in a verdict, so a commit
    without one after that long is not being worked on: it is waiting for a person who is not there.
    `comments_of(number)` and `checks_of(sha)` fetch lazily, only for candidates."""
    stuck = []
    for pr in pulls:
        head = (pr.get("head") or {}).get("sha", "")
        if pr.get("draft") or not head or (pr.get("user") or {}).get("login") in skip_authors:
            continue
        if ((pr.get("head") or {}).get("repo") or {}).get("full_name") != ((pr.get("base") or {}).get("repo") or {}).get("full_name"):
            continue  # a fork: the agents never work on it
        checks = checks_of(head)
        if any(c.get("status") != "completed" for c in checks):
            continue
        times = [parse_time(c["completed_at"]) for c in checks if c.get("completed_at")] or [parse_time(pr.get("updated_at") or now.isoformat())]
        idle = (now - max(times)).total_seconds() / 60
        if idle < minutes:
            continue
        bodies = [c.get("body") or "" for c in comments_of(pr["number"])]
        if any(f"<!-- agent-certified: {head} -->" in b or f"<!-- agent-abandoned: {head} -->" in b for b in bodies):
            continue
        stuck.append({"pr": pr["number"], "sha": head, "idle_minutes": int(idle), "title": pr.get("title", "")})
    return stuck


def abandon(repo: str, number: int, sha: str, idle: int) -> bool:
    body = (f"<!-- agent-abandoned: {sha} -->\nNo agent verdict was recorded for commit `{sha[:7]}` and nothing has run on it for "
            f"{idle} minutes, so the pipeline health check abandons it: nothing was merged and the base branch is untouched. "
            "A new push starts a new attempt.")
    posted = run(["gh", "api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}", "--silent"], check=False)
    run(["gh", "api", "-X", "POST", f"repos/{repo}/issues/{number}/labels", "-f", "labels[]=agent-abandoned", "--silent"], check=False)
    return posted.returncode == 0


def render(unbuilt: list[dict], degraded: list[dict], failing: list[dict], started: bool, stuck: list[dict] | None = None,
           abandoned: bool = False) -> str:
    stuck = stuck or []
    lines = ["## Pipeline health", ""]
    if not (unbuilt or degraded or failing or stuck):
        return "\n".join(lines + ["Nothing found: every commit on the base branch is covered by a build, no certification "
                                  "hides an agent that failed, no agent workflow is failing and no pull request is stuck."])
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
    if stuck:
        lines += [f"### {len(stuck)} pull request(s) with no verdict and nothing left to run", ""]
        lines += [f"- PR #{s['pr']} at `{s['sha'][:7]}`, idle for {s['idle_minutes']} minutes: {s['title']}" for s in stuck]
        lines += ["", "They were abandoned (a new push starts a new attempt)." if abandoned else "They were NOT abandoned.", ""]
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
    parser.add_argument("--stuck-minutes", type=int, default=90, help="A pull request with no verdict on its head and idle this long is stuck")
    parser.add_argument("--abandon-stuck", action="store_true", help="Abandon stuck pull requests (label and marker) instead of only reporting them")
    parser.add_argument("--skip-authors", default="renovate[bot]", help="Comma-separated authors whose pull requests another workflow owns")
    args = parser.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    commits = gh_json([f"repos/{args.repo}/commits?sha={args.branch}&since={since}&per_page=100"]) or []
    builds = (gh_json([f"repos/{args.repo}/actions/workflows/{args.build_workflow}/runs?branch={args.branch}&per_page=100"]) or {}).get("workflow_runs", [])
    comments = gh_json([f"repos/{args.repo}/issues/comments?since={since}&per_page=100&sort=updated&direction=desc"]) or []
    recent = (gh_json([f"repos/{args.repo}/actions/runs?status=failure&created=>={since}&per_page=100"]) or {}).get("workflow_runs", [])
    names = tuple(n.strip() for n in args.agent_workflows.split(",") if n.strip())

    unbuilt = unbuilt_commits(commits, builds, now, args.grace_minutes)
    # Reported for a day: it cannot be repaired after the fact, only noticed.
    day = (now - dt.timedelta(hours=min(args.hours, 24))).strftime("%Y-%m-%dT%H:%M:%SZ")
    degraded = degraded_certifications([c for c in comments if (c.get("updated_at") or c.get("created_at") or day) >= day])
    last_success = {}
    for workflow_id in {r.get("workflow_id") for r in recent if r.get("conclusion") == "failure" and r.get("name") in names}:
        latest = (gh_json([f"repos/{args.repo}/actions/workflows/{workflow_id}/runs?status=success&per_page=1"]) or {}).get("workflow_runs", [])
        if latest:
            last_success[workflow_id] = latest[0].get("created_at", "")
    failing = failing_agent_workflows(recent, names, last_success)

    pulls = gh_json([f"repos/{args.repo}/pulls?state=open&base={args.branch}&per_page=100"]) or []
    stuck = stuck_pull_requests(
        pulls,
        lambda number: gh_json([f"repos/{args.repo}/issues/{number}/comments?per_page=100"]) or [],
        lambda sha: (gh_json([f"repos/{args.repo}/commits/{sha}/check-runs?per_page=100"]) or {}).get("check_runs", []),
        now, args.stuck_minutes, tuple(a.strip() for a in args.skip_authors.split(",") if a.strip()))
    abandoned = bool(stuck) and args.abandon_stuck and all([abandon(args.repo, s["pr"], s["sha"], s["idle_minutes"]) for s in stuck])

    started = False
    if unbuilt and args.start_missing_build:
        done = run(["gh", "workflow", "run", args.build_workflow, "--repo", args.repo, "--ref", args.branch], check=False)
        started = done.returncode == 0
        if not started:
            print(f"::error::could not start {args.build_workflow}: {done.stderr.strip()[:300]}")

    report = render(unbuilt, degraded, failing, started, stuck, abandoned)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write(report + "\n")
    return 1 if (unbuilt or degraded or failing or stuck) else 0


if __name__ == "__main__":
    sys.exit(main())
