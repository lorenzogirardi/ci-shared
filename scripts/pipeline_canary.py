#!/usr/bin/env python3
"""Known changes through the real pipeline, with the outcome checked.

Every defect of the agent loop found so far was found by opening a real pull request and looking at what
the agents did with it: a test asserted more than the code does and the application was rewritten to
satisfy it; a correct verdict was refused over an ellipsis; two runs raced and left a pull request with no
verdict. Nothing in the pipeline looked for any of that. This does, on a schedule, with no model of its own.

For each scenario in the repository's canary file it opens a pull request against a throwaway base branch
(a copy of the real one, so nothing ever lands on it), lets the ordinary workflows run, waits for the
agents' verdict on the final head commit, and then checks facts: the verdict, the required checks, which
files the pull request ended up changing, what a file contains. Then it closes the pull requests and
deletes the branches. Exit 1 if any expectation failed.

The merge gate only merges into the real base branch, so a canary ends "certified and green", not merged;
what happens after a merge is covered by the health check.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import subprocess
import sys
import time

from pr_review_sweep import gh_json, run

CANARY_AUTHOR = "pipeline canary"
AGENT_AUTHOR = "ci-shared agents"


# --- pure: what a scenario expects, checked against what happened ---------------------------------------

def pick_variant(scenario: dict, read) -> dict | None:
    """The first variant whose edits all apply (each `find` exactly once in its file). Variants let a
    scenario alternate, e.g. a limit that goes 10 -> 20 one night and 20 -> 10 the next, so it never
    depends on what an earlier run left behind. A scenario without variants is its own single variant."""
    for variant in scenario.get("variants") or [scenario]:
        if all((read(e["file"]) or "").count(e["find"]) == 1 for e in variant.get("edits", [])):
            return {**{k: v for k, v in scenario.items() if k != "variants"}, **variant}
    return None


def verdict_of(comments: list[dict], head_sha: str, trusted: set[str]) -> str:
    """'certified', 'abandoned' or '' for exactly this commit, from a comment by a trusted account."""
    for kind in ("certified", "abandoned"):
        marker = f"<!-- agent-{kind}: {head_sha} -->"
        if any(marker in (c.get("body") or "") and (c.get("user") or {}).get("login") in trusted for c in comments):
            return kind
    return ""


def settled(verdict: str, checks: list[dict]) -> bool:
    """Nothing more is coming for this commit: it has a verdict and none of its checks is still running."""
    return bool(verdict) and bool(checks) and all(c.get("status") == "completed" for c in checks)


def evaluate(expect: dict, *, verdict: str, checks: list[dict], files: list[str], authors: list[str],
             content_of, required_checks: tuple[str, ...]) -> list[str]:
    """Every way the outcome differs from what the scenario expects, in words. Empty = it passed."""
    problems = []
    want = expect.get("verdict", "certified")
    if verdict != want:
        problems.append(f"verdict is '{verdict or 'none'}', expected '{want}'")
    if want == "certified":
        done = {c.get("name"): c.get("conclusion") for c in checks}
        bad = [name for name in required_checks if done.get(name) != "success"]
        if bad:
            problems.append("required checks not green on the final commit: " + ", ".join(f"{n}={done.get(n) or 'missing'}" for n in bad))
    allowed = tuple(expect.get("allowed_paths") or ())
    if allowed:
        outside = [f for f in files if not f.startswith(allowed)]
        if outside:
            problems.append("the pull request changes files it must not touch: " + ", ".join(outside))
    for prefix in expect.get("changed") or []:
        if not any(f.startswith(prefix) for f in files):
            problems.append(f"nothing under {prefix} was changed, and something had to be")
    for prefix in expect.get("unchanged") or []:
        touched = [f for f in files if f.startswith(prefix)]
        if touched:
            problems.append(f"{', '.join(touched)} differs from the base, and must not")
    if expect.get("agent_commit") and AGENT_AUTHOR not in authors:
        problems.append("the agent pushed no commit, and it had to")
    if expect.get("agent_commit") is False and AGENT_AUTHOR in authors:
        problems.append("the agent pushed a commit, and nothing needed one")
    for item in expect.get("contains") or []:
        if item["text"] not in (content_of(item["file"]) or ""):
            problems.append(f"{item['file']} does not contain {item['text']!r}")
    for item in expect.get("not_contains") or []:
        if item["text"] in (content_of(item["file"]) or ""):
            problems.append(f"{item['file']} contains {item['text']!r}, and must not")
    return problems


def render(results: list[dict]) -> str:
    lines = ["## Pipeline canary", ""]
    for r in results:
        mark = "passed" if not r["problems"] else "FAILED"
        lines.append(f"### {r['name']}: {mark}" + (f" (PR #{r['pr']})" if r.get("pr") else ""))
        lines += [f"- {p}" for p in r["problems"]] or ["- every expectation held"]
        lines.append("")
    return "\n".join(lines)


# --- effects --------------------------------------------------------------------------------------------

def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check)


def push(refspec: str, token: str, *, delete: bool = False) -> bool:
    header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    args = ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {header}", "push", "--quiet", "origin"]
    done = git(*args, *(["--delete"] if delete else []), refspec, check=False)
    return done.returncode == 0


def open_scenario(repo: str, scenario: dict, base_branch: str, base_sha: str, tag: str, token: str) -> dict:
    variant = pick_variant(scenario, lambda f: pathlib.Path(f).read_text() if pathlib.Path(f).is_file() else None)
    if variant is None:
        return {"name": scenario["name"], "problems": ["no variant of this scenario applies to the code on the base branch"]}
    branch = f"canary/{tag}-{scenario['name']}"
    git("checkout", "--quiet", "-B", branch, base_sha)
    for edit in variant["edits"]:
        path = pathlib.Path(edit["file"])
        path.write_text(path.read_text().replace(edit["find"], edit["replace"], 1))
    git("add", "-A")
    git("-c", f"user.name={CANARY_AUTHOR}", "-c", "user.email=actions@github.com", "commit", "--quiet", "-m", variant["title"])
    if not push(f"HEAD:refs/heads/{branch}", token):
        return {"name": scenario["name"], "problems": [f"could not push {branch}"]}
    body = variant.get("body", "") + "\n\n_Opened by the pipeline canary; it is checked and closed automatically and never merged._"
    # Opened with the job token (GH_TOKEN): the push token of the agents is not required to be able to open
    # pull requests. A pull request opened that way starts no workflow, so the workflows are started by the
    # push that follows, made with the push token.
    made = run(["gh", "pr", "create", "--repo", repo, "--base", base_branch, "--head", branch,
                "--title", variant["title"], "--body", body], check=False)
    number = made.stdout.strip().rsplit("/", 1)[-1]
    if made.returncode != 0 or not number.isdigit():
        return {"name": scenario["name"], "branch": branch, "problems": [f"could not open the pull request: {made.stderr.strip()[:200]}"]}
    git("-c", f"user.name={CANARY_AUTHOR}", "-c", "user.email=actions@github.com", "commit", "--quiet", "--allow-empty",
        "-m", "chore: start the workflows on this pull request")
    if not push(f"HEAD:refs/heads/{branch}", token):
        return {"name": scenario["name"], "branch": branch, "pr": int(number), "expect": variant.get("expect", {}),
                "problems": ["could not push the commit that starts the workflows"]}
    return {"name": scenario["name"], "branch": branch, "pr": int(number), "expect": variant.get("expect", {}), "problems": []}


def state_of(repo: str, number: int, trusted: set[str]) -> dict:
    pr = gh_json([f"repos/{repo}/pulls/{number}"]) or {}
    head = (pr.get("head") or {}).get("sha", "")
    comments = gh_json([f"repos/{repo}/issues/{number}/comments?per_page=100"]) or []
    checks = (gh_json([f"repos/{repo}/commits/{head}/check-runs?per_page=100"]) or {}).get("check_runs", []) if head else []
    return {"head": head, "verdict": verdict_of(comments, head, trusted), "checks": checks}


def content_at(repo: str, ref: str, path: str) -> str | None:
    got = run(["gh", "api", f"repos/{repo}/contents/{path}?ref={ref}", "--jq", ".content"], check=False)
    if got.returncode != 0:
        return None
    return base64.b64decode(got.stdout).decode(errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--config", default=".github/canary.json")
    parser.add_argument("--tag", required=True, help="Unique per run; names the branches")
    parser.add_argument("--trusted", default="github-actions[bot]")
    parser.add_argument("--only", default="", help="Comma-separated scenario names; empty = all")
    args = parser.parse_args()

    token = os.environ.get("AGENT_PUSH_TOKEN", "")
    if not token:
        print("::error::AGENT_PUSH_TOKEN is not set: pull requests opened with the job token start no workflow.")
        return 1
    config = json.loads(pathlib.Path(args.config).read_text())
    base = config.get("base", "main")
    required = tuple(config.get("required_checks") or ())
    trusted = {t.strip() for t in args.trusted.split(",") if t.strip()}
    only = {n.strip() for n in args.only.split(",") if n.strip()}
    scenarios = [s for s in config["scenarios"] if not only or s["name"] in only]

    base_sha = git("rev-parse", f"origin/{base}").stdout.strip()
    base_branch = f"canary/{args.tag}-base"
    results: list[dict] = []
    try:
        if not push(f"{base_sha}:refs/heads/{base_branch}", token):
            print(f"::error::could not create {base_branch}")
            return 1
        results = [open_scenario(args.repo, s, base_branch, base_sha, args.tag, token) for s in scenarios]
        deadline = time.time() + int(config.get("timeout_minutes", 45)) * 60
        pending = [r for r in results if r.get("pr")]
        while pending and time.time() < deadline:
            time.sleep(30)
            for r in pending:
                r["state"] = state_of(args.repo, r["pr"], trusted)
            pending = [r for r in pending if not settled(r["state"]["verdict"], r["state"]["checks"])]
        for r in results:
            if not r.get("pr"):
                continue
            state = r.get("state") or state_of(args.repo, r["pr"], trusted)
            if r in pending:
                r["problems"].append(f"no verdict on the final commit after {config.get('timeout_minutes', 45)} minutes: the pull request is stuck")
            compare = gh_json([f"repos/{args.repo}/compare/{base_branch}...{r['branch']}"]) or {}
            commits = gh_json([f"repos/{args.repo}/pulls/{r['pr']}/commits?per_page=100"]) or []
            r["problems"] += evaluate(
                r["expect"], verdict=state["verdict"], checks=state["checks"],
                files=[f["filename"] for f in compare.get("files", [])],
                authors=[((c.get("commit") or {}).get("author") or {}).get("name", "") for c in commits],
                content_of=lambda path, ref=r["branch"]: content_at(args.repo, ref, path), required_checks=required)
    finally:
        for r in results:
            if r.get("pr"):
                run(["gh", "pr", "close", str(r["pr"]), "--repo", args.repo], check=False)
            if r.get("branch"):
                push(f"refs/heads/{r['branch']}", token, delete=True)
        push(f"refs/heads/{base_branch}", token, delete=True)

    report = render(results)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write(report + "\n")
    return 1 if any(r["problems"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
