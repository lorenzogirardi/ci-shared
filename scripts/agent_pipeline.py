#!/usr/bin/env python3
"""Role-based agent pipeline.

Subcommands
  guard      is another agent PR for this issue already open? (cheap, no model)
  run        planner -> writer -> deterministic checks -> reviewers A and B ->
             dedup/validate -> fix loop -> final review -> docs -> changelog.
             Commits to a local branch only; holds NO push credential.
  publish    push that branch and open the PR. The only step with a write token.
  review     the two independent reviewers on any PR diff (reusable elsewhere)
  docs-plan  documentation architect: a Diataxis-style proposal, read-only

Why `run` and `publish` are separate: `run` executes code a model just wrote
(the deterministic gates), so it must not see a token that can write to the
repository. See agent_lib.run_verify_isolated.

The loop is bounded (max iterations, max verify retries, writer exploration
rounds). When it does not converge it stops and says so: the PR is opened as a
draft titled "[needs human]" with the unresolved findings. Nothing here merges.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, StateGraph

import agent_lib as lib
from changelog_update import add_entry
from pr_review_sweep import _parse_json_reply, build_diff, gh_json, post_comment

OUT_DIR = pathlib.Path(".ai/agent-run")
BRANCH_PREFIX = "agent/issue-"
REVIEWERS = ("reviewer-correctness", "reviewer-security")
PLANNER_ROUNDS = 6
_ADD_EXCLUDES = ("--", ".", ":!.ai", ":!.shared")


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

def validate_plan(data: dict) -> dict | None:
    if not isinstance(data, dict) or not isinstance(data.get("feasible"), bool):
        return None
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        return None
    lists: dict[str, list[str]] = {}
    for key in ("scope", "out_of_scope", "acceptance_criteria", "files_hint", "risks"):
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            return None
        lists[key] = value
    reason = data.get("reason", "")
    if not isinstance(reason, str):
        return None
    if data["feasible"]:
        if not lists["scope"] or not lists["acceptance_criteria"]:
            return None
        lists["files_hint"] = [p for p in lists["files_hint"] if not p.startswith(".github/workflows")]
    elif not reason.strip():
        return None
    return {"feasible": data["feasible"], "summary": data["summary"].strip(), "reason": reason, **lists}


def run_planner(caller: lib.ModelCaller, title: str, body: str, context: str) -> dict | None:
    system = lib.load_prompt("planner")
    base = (
        f"## Issue\nTitle: {lib.sanitize_untrusted(title, 500)}\n\n"
        f"{lib.sanitize_untrusted(body)}\n\n## Repository files\n{lib.repo_tree()}\n\n"
        f"## Project context\n{context}\n"
    )
    history: list[str] = []
    for rnd in range(1, PLANNER_ROUNDS + 1):
        reply = caller.call("planner", system, base + ("\n## What you already looked up\n" + "\n\n".join(history) if history else ""))
        if reply is None:
            return None
        request = lib.parse_explore(reply)
        if request:
            history.append(f"Round {rnd}: " + lib.explore(*request))
            continue
        data = _parse_json_reply(reply)
        plan = validate_plan(data) if data is not None else None
        if plan:
            return plan
        history.append(f"Round {rnd}: your reply was not valid JSON for the plan schema. Reply again with exactly that schema.")
    return None


# ---------------------------------------------------------------------------
# Review (shared by the pipeline and the standalone `review` command)
# ---------------------------------------------------------------------------

def review_diff(caller: lib.ModelCaller, diff: str, plan: dict | None,
                reviewers: tuple[str, ...] = REVIEWERS):
    """Run the independent reviewers on `diff`. Returns (findings, dropped)
    after validation against the diff and dedup, or None if a reviewer could
    not produce a usable reply (fail closed: the change cannot be certified)."""
    ranges = lib.diff_ranges(diff)
    user = (
        f"## Plan\n{json.dumps(plan, indent=2) if plan else 'No plan was provided; judge the diff on its own.'}\n\n"
        f"## Diff (lines are prefixed `L<number>|` with their line number in the new file)\n{lib.annotate_diff(diff)}"
    )
    kept_all: list[lib.Finding] = []
    dropped_all: list[tuple[lib.Finding, str]] = []
    for name in reviewers:
        found = lib.ask_json(caller, name, lib.load_prompt(name), user, lambda d, n=name: lib.parse_findings(d, n))
        if found is None:
            return None
        kept, dropped = lib.validate_findings(found, ranges)
        kept_all += kept
        dropped_all += dropped
    return lib.dedup_findings(kept_all), dropped_all


def final_review(caller: lib.ModelCaller, diff: str, plan: dict, prior: list[dict]):
    ranges = lib.diff_ranges(diff)
    user = (
        f"## Plan\n{json.dumps(plan, indent=2)}\n\n## Earlier findings (blocking, from previous rounds)\n"
        f"{json.dumps(prior, indent=2) if prior else '[]'}\n\n"
        f"## Current diff (lines are prefixed `L<number>|` with their line number in the new file)\n"
        f"{lib.annotate_diff(diff)}"
    )
    found = lib.ask_json(caller, "final-reviewer", lib.load_prompt("final-reviewer"), user,
                         lambda d: lib.parse_findings(d, "final-reviewer"))
    if found is None:
        return None
    kept, dropped = lib.validate_findings(found, ranges)
    return lib.dedup_findings(kept), dropped


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

@dataclass
class Runtime:
    caller: lib.ModelCaller
    base_sha: str
    verify_command: str
    verify_timeout: int
    max_iterations: int
    max_verify_retries: int
    writer_rounds: int
    context: str
    changelog_path: str
    issue_number: int


class RunState(TypedDict, total=False):
    plan: dict
    iteration: int
    verify_attempts: int
    feedback: str
    applied: dict
    explanation: str
    committed: bool
    prior_blocking: list
    rounds: list
    notes: list
    route: str
    outcome: str


def is_doc_path(path: str) -> bool:
    name = pathlib.PurePosixPath(path).name
    if name.upper().startswith("CHANGELOG") or path.startswith((".github/", ".shared/", ".ai/")):
        return False
    return path.lower().endswith((".md", ".rst", ".txt")) or name == ".env.example"


def commit_round(message: str) -> bool:
    git("add", "-A", *_ADD_EXCLUDES)
    if git("diff", "--cached", "--quiet").returncode == 0:
        return False
    git("config", "user.name", "ci-shared agents")
    git("config", "user.email", "actions@github.com")
    done = git("commit", "--quiet", "-m", message)
    if done.returncode != 0:
        raise RuntimeError(f"commit failed: {(done.stderr or done.stdout).strip()[:200]}")
    return True


def diff_since_base(base_sha: str) -> str:
    path = OUT_DIR / "diff.txt"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    build_diff(base_sha, "HEAD", path)
    return path.read_text()


def changed_files(base_sha: str) -> list[str]:
    return [n for n in git("diff", "--name-only", f"{base_sha}...HEAD").stdout.split("\n") if n]


def run_writer(rt: Runtime, plan: dict, feedback: str):
    """One writer turn: optional read-only exploration, then a validated set
    of changes applied to the working tree. Returns (Applied | None, text)."""
    system = lib.load_prompt("writer")
    base = (
        f"## Plan\n{json.dumps(plan, indent=2)}\n\n## Repository files\n{lib.repo_tree()}\n\n"
        f"## Project context\n{rt.context}\n"
        + (f"\n## {feedback}\n" if feedback else "")
    )
    history: list[str] = []
    for rnd in range(1, rt.writer_rounds + 1):
        reply = rt.caller.call("writer", system, base + ("\n## What you already looked up\n" + "\n\n".join(history) if history else ""))
        if reply is None:
            return None, "the model call failed"
        request = lib.parse_explore(reply)
        if request:
            history.append(f"Round {rnd}: " + lib.explore(*request))
            continue
        data = _parse_json_reply(reply)
        parsed = lib.parse_changes(data) if data is not None else None
        if parsed is None:
            history.append(
                f"Round {rnd}: your reply was malformed or outside the allowed scope (max {lib.MAX_CHANGES} "
                "changes, nothing under .github/workflows/, find text must appear exactly once). Reply again."
            )
            continue
        changes, explanation = parsed
        if not changes:
            return None, explanation or "the writer found nothing it could change safely"
        applied = lib.apply_changes(changes)
        if applied.error:
            history.append(f"Round {rnd}: applying your changes failed: {applied.error}")
            continue
        return applied, explanation
    return None, "the writer used all its rounds without producing a valid change"


def build_graph(rt: Runtime):
    def stop(state: RunState, outcome: str, note: str) -> dict:
        return {"route": "end", "outcome": outcome, "notes": state.get("notes", []) + [note]}

    def n_write(state: RunState) -> dict:
        applied, text = run_writer(rt, state["plan"], state.get("feedback", ""))
        if applied is None:
            return stop(state, "escalated" if state.get("committed") else "failed", f"writer: {text}")
        return {"route": "verify", "applied": {"edited": applied.edited, "created": applied.created},
                "explanation": text}

    def n_verify(state: RunState) -> dict:
        ok, output = lib.run_verify_isolated(rt.verify_command, rt.verify_timeout)
        log(f"deterministic checks (iteration {state['iteration']}): {'passed' if ok else 'FAILED'}")
        applied = lib.Applied(state["applied"]["edited"], state["applied"]["created"])
        if ok:
            kind = "feat" if state["iteration"] == 1 else "fix"
            commit_round(f"{kind}(agent): {state['explanation'][:120]}\n\nIteration {state['iteration']} of the agent pipeline.")
            return {"route": "review", "committed": True, "verify_attempts": 0}
        lib.revert(applied)
        attempts = state.get("verify_attempts", 0) + 1
        if attempts > rt.max_verify_retries:
            return stop(state, "escalated" if state.get("committed") else "failed",
                        f"deterministic checks still failing after {attempts} attempts:\n{output[-1500:]}")
        return {"route": "write", "verify_attempts": attempts,
                "feedback": f"FAILED VERIFICATION of your previous change (reverted). Fix this:\n{output}"}

    def after_review(state: RunState, findings: list[lib.Finding], dropped, label: str) -> dict:
        blocking = [f for f in findings if f.blocking]
        rounds = state.get("rounds", []) + [{
            "iteration": state["iteration"], "stage": label,
            "findings": [f.to_dict() for f in findings], "dropped": len(dropped),
        }]
        if not blocking:
            return {"route": "docs" if label == "final" else "final", "rounds": rounds}
        if state["iteration"] >= rt.max_iterations:
            return {**stop(state, "escalated", f"{len(blocking)} blocking finding(s) remain after {state['iteration']} iteration(s)"),
                    "rounds": rounds}
        return {
            "route": "write", "rounds": rounds, "iteration": state["iteration"] + 1,
            "prior_blocking": state.get("prior_blocking", []) + [f.to_dict() for f in blocking],
            "feedback": "REVIEW FINDINGS to fix (each is blocking; fix exactly these, minimally):\n"
                        + lib.findings_for_writer(blocking),
        }

    def n_review(state: RunState) -> dict:
        result = review_diff(rt.caller, diff_since_base(rt.base_sha), state["plan"])
        if result is None:
            return stop(state, "escalated", "a reviewer did not return a usable reply, so the change could not be certified")
        return after_review(state, *result, "review")

    def n_final(state: RunState) -> dict:
        result = final_review(rt.caller, diff_since_base(rt.base_sha), state["plan"], state.get("prior_blocking", []))
        if result is None:
            return stop(state, "escalated", "the final reviewer did not return a usable reply")
        return after_review(state, *result, "final")

    def n_docs(state: RunState) -> dict:
        notes = list(state.get("notes", []))
        docs = "\n\n".join(
            f"### {p}\n{pathlib.Path(p).read_text(errors='replace')[:5000]}"
            for p in lib.repo_tree(2000).split("\n") if p and is_doc_path(p) and pathlib.Path(p).is_file()
        )[:40_000]
        user = (f"## Plan\n{json.dumps(state['plan'], indent=2)}\n\n## Diff\n{diff_since_base(rt.base_sha)}\n\n"
                f"## Documentation files\n{docs}")

        def validate(data: dict):
            return lib.parse_changes(data, allowed=is_doc_path)

        parsed = lib.ask_json(rt.caller, "doc-reviewer", lib.load_prompt("doc-reviewer"), user, validate)
        if parsed is None:
            notes.append("documentation reviewer returned no usable reply; docs were not checked")
        else:
            changes, explanation = parsed
            if changes:
                applied = lib.apply_changes(changes)
                if applied.error:
                    notes.append(f"documentation edits were not applicable: {applied.error}")
                else:
                    ok, output = lib.run_verify_isolated(rt.verify_command, rt.verify_timeout)
                    if ok:
                        commit_round(f"docs(agent): {explanation[:120]}")
                        notes.append(f"docs updated: {', '.join(applied.files)}")
                    else:
                        lib.revert(applied)
                        notes.append(f"documentation edits broke the checks and were reverted:\n{output[-800:]}")
            else:
                notes.append(f"docs: {explanation or 'no documentation change needed'}")
        # Changelog: deterministic, so it cannot silently go missing.
        if rt.changelog_path and rt.changelog_path not in changed_files(rt.base_sha):
            path = pathlib.Path(rt.changelog_path)
            ref = f" (#{rt.issue_number})" if rt.issue_number else ""
            summary = state["plan"]["summary"]
            category = "Added" if summary.lower().startswith(("add", "introduce", "implement", "create")) else "Changed"
            path.write_text(add_entry(path.read_text() if path.is_file() else "", category, summary.rstrip(".") + ref))
            commit_round("docs(changelog): record the agent change")
            notes.append(f"changelog entry added to {rt.changelog_path}")
        return {"route": "end", "outcome": "converged", "notes": notes}

    graph = StateGraph(RunState)
    for name, fn in (("write", n_write), ("verify", n_verify), ("review", n_review),
                     ("final", n_final), ("docs", n_docs)):
        graph.add_node(name, fn)
    graph.set_entry_point("write")
    route = lambda s: s["route"]  # noqa: E731
    graph.add_conditional_edges("write", route, {"verify": "verify", "end": END})
    graph.add_conditional_edges("verify", route, {"review": "review", "write": "write", "end": END})
    graph.add_conditional_edges("review", route, {"final": "final", "write": "write", "end": END})
    graph.add_conditional_edges("final", route, {"docs": "docs", "write": "write", "end": END})
    graph.add_edge("docs", END)
    return graph.compile()


# ---------------------------------------------------------------------------
# PR body
# ---------------------------------------------------------------------------

def build_pr_body(plan: dict, final: dict, issue_number: int, cost: float, by_role: dict) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) or "- (none)"

    outcome = final["outcome"]
    status = ("All deterministic checks and every review passed." if outcome == "converged"
              else "**The agent did not converge. A person needs to look at this.**")
    parts = [
        f"{status}\n\nImplements #{issue_number}: {plan['summary']}" if issue_number else f"{status}\n\n{plan['summary']}",
        f"## Scope\n{bullets(plan['scope'])}",
        f"## Out of scope\n{bullets(plan['out_of_scope'])}",
        f"## Acceptance criteria\n{bullets(plan['acceptance_criteria'])}",
    ]
    rounds = final.get("rounds", [])
    if rounds:
        table = ["| Iteration | Stage | Findings | Blocking | Dropped (not in diff) |", "|---|---|---|---|---|"]
        for r in rounds:
            blocking = sum(1 for f in r["findings"] if f["severity"] in lib.BLOCKING)
            table.append(f"| {r['iteration']} | {r['stage']} | {len(r['findings'])} | {blocking} | {r['dropped']} |")
        parts.append("## Review rounds\n" + "\n".join(table))
        last = [f for f in rounds[-1]["findings"]]
        if last:
            parts.append("## Findings at the last round\n"
                         + lib.render_findings_md([lib.Finding(**f) for f in last]))
    if final.get("notes"):
        parts.append("## Notes\n" + bullets(final["notes"]))
    parts.append(
        "## Pipeline\nplanner -> writer -> deterministic checks (no secrets in their environment) -> "
        "reviewer A (correctness/design) + reviewer B (security/operability) -> dedup/validation -> fix loop "
        f"-> final reviewer -> documentation reviewer -> changelog.\n\nModel cost: ${cost:.4f} "
        f"({', '.join(f'{k} ${v:.4f}' for k, v in by_role.items()) or 'n/a'}).\n\n"
        "Written by an automated agent pipeline. **It is not merged automatically.**"
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def cmd_guard(args: argparse.Namespace) -> int:
    prs = gh_json([f"repos/{args.repo}/pulls?state=open&per_page=100"]) or []
    prefix = f"{BRANCH_PREFIX}{args.issue}-"
    clash = next((p for p in prs if str((p.get("head") or {}).get("ref", "")).startswith(prefix)), None)
    set_output("skip", "true" if clash else "false")
    log(f"an agent PR for issue #{args.issue} is already open: {clash['html_url']}" if clash
        else f"no open agent PR for issue #{args.issue}")
    return 0


def write_result(result: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "result.json").write_text(json.dumps(result, indent=2))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"## Agent pipeline\n\n- outcome: **{result['outcome']}**\n"
                     + "".join(f"- {n}\n" for n in result.get("notes", [])[:10]))


def cmd_run(args: argparse.Namespace) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    caller = lib.ModelCaller(args.ai_script, max_chars=args.max_chars, timeout=args.timeout)
    title = pathlib.Path(args.issue_title_file).read_text().strip()
    body = pathlib.Path(args.issue_body_file).read_text()
    context = lib.read_context_files([c.strip() for c in args.context_files.split(",") if c.strip()])
    base_sha = git("rev-parse", "HEAD").stdout.strip()

    plan = run_planner(caller, title, body, context)
    if plan is None:
        write_result({"outcome": "failed", "notes": ["the planner did not return a usable plan"]})
        return 0
    (OUT_DIR / "plan.json").write_text(json.dumps(plan, indent=2))
    if not plan["feasible"]:
        write_result({"outcome": "not_feasible", "plan": plan, "notes": [plan["reason"]]})
        return 0

    branch = f"{BRANCH_PREFIX}{args.issue}-{args.run_number}"
    git("checkout", "--quiet", "-B", branch, base_sha)
    rt = Runtime(caller, base_sha, pathlib.Path(args.verify_command_file).read_text() if args.verify_command_file else "",
                 args.verify_timeout, args.max_iterations, args.max_verify_retries, args.writer_rounds,
                 context, args.changelog, args.issue)
    limit = (args.max_iterations + 1) * (args.max_verify_retries + 1) * 6 + 30
    final = build_graph(rt).invoke({"plan": plan, "iteration": 1, "verify_attempts": 0, "notes": [], "rounds": []},
                                   config={"recursion_limit": limit})

    outcome = final.get("outcome", "failed")
    touched = changed_files(base_sha)
    if any(n.startswith(".github/workflows/") for n in touched):
        outcome, final["notes"] = "failed", final.get("notes", []) + ["refusing to publish: touches .github/workflows/"]
    if outcome != "failed" and not touched:
        outcome = "failed"
    cost = caller.total_cost_usd()
    result = {"outcome": outcome, "branch": branch, "plan": plan, "notes": final.get("notes", []),
              "rounds": final.get("rounds", []), "files": touched, "cost_usd": cost,
              "title": f"{'[needs human] ' if outcome == 'escalated' else ''}{plan['summary'][:150]}"}
    if outcome in ("converged", "escalated"):
        (OUT_DIR / "pr-body.md").write_text(build_pr_body(plan, final, args.issue, cost, caller.cost_by_role()))
    write_result(result)
    log(f"agent pipeline: {outcome}, {len(touched)} file(s), ${cost:.4f}")
    return 0


def _gh(*cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *cmd], capture_output=True, text=True)


def cmd_publish(args: argparse.Namespace) -> int:
    result = json.loads((OUT_DIR / "result.json").read_text())
    token = os.environ.get("AGENT_PUSH_TOKEN", "")
    outcome, issue = result["outcome"], args.issue

    def comment(text: str) -> None:
        if issue:
            _gh("issue", "comment", str(issue), "--repo", args.repo, "--body", text)

    if outcome in ("failed", "not_feasible"):
        why = "\n".join(f"- {n}" for n in result.get("notes", [])) or "- no detail"
        comment(("The agent could not plan this request" if outcome == "not_feasible"
                 else "The agent pipeline did not produce a change") + f":\n{why}")
        return 0 if outcome == "not_feasible" else 1

    if not token:
        print("::error::AGENT_PUSH_TOKEN is not set: a fix PR created as GITHUB_TOKEN would not trigger the "
              "PR checks, so it could never be verified.", file=sys.stderr)
        return 1
    header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    pushed = subprocess.run(
        ["git", "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {header}",
         "push", "--quiet", "origin", f"HEAD:refs/heads/{result['branch']}"],
        capture_output=True, text=True)
    if pushed.returncode != 0:
        print(f"::error::push rejected: {pushed.stderr.replace(header, '***')[:300]}", file=sys.stderr)
        return 1
    cmd = ["pr", "create", "--repo", args.repo, "--base", args.base_branch, "--head", result["branch"],
           "--title", result["title"], "--body-file", str(OUT_DIR / "pr-body.md")]
    if outcome == "escalated":
        cmd.append("--draft")
    created = _gh(*cmd)
    if created.returncode != 0:
        print(f"::error::gh pr create failed: {created.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    url = created.stdout.strip()
    for label in (["needs-human"] if outcome == "escalated" else []):
        _gh("pr", "edit", url, "--repo", args.repo, "--add-label", label)  # best effort: label may not exist
    comment(f"Opened {url} ({'draft, needs a person: the loop did not converge' if outcome == 'escalated' else 'checks and reviews passed'}).")
    log(f"opened {url}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    caller = lib.ModelCaller(args.ai_script, max_chars=args.max_chars, timeout=args.timeout)
    path = pathlib.Path(".ai/review-diff.txt")
    path.parent.mkdir(exist_ok=True)
    build_diff(args.base_sha, args.head_sha, path)
    result = review_diff(caller, path.read_text(), None)
    marker = "<!-- agent-review -->"
    if result is None:
        body = f"{marker}\n\n## Agent review\n\nA reviewer did not return a usable reply; nothing was certified."
    else:
        findings, dropped = result
        blocking = sum(1 for f in findings if f.blocking)
        body = (f"{marker}\n\n## Agent review\n\nReviewer A (correctness/design) and reviewer B (security/operability), "
                f"independent, deduplicated. {len(findings)} finding(s), {blocking} blocking"
                f"{f', {len(dropped)} dropped (not on a changed line)' if dropped else ''}.\n\n"
                f"{lib.render_findings_md(findings)}\n\nModel cost: ${caller.total_cost_usd():.4f}")
    comments = gh_json([f"repos/{args.repo}/issues/{args.pr}/comments?per_page=100"]) or []
    existing = next((c for c in comments if marker in (c.get("body") or "")), None)
    if args.post:
        post_comment(args.repo, args.pr, body, existing)
    else:
        print(body)
    return 0


# --- documentation architect -------------------------------------------------

QUADRANTS = ("tutorial", "how-to", "reference", "explanation")
DOC_SUFFIXES = (".md", ".rst", ".txt", ".html")
_ROUTE_RE = re.compile(r"@\w+\.(get|post|put|patch|delete|websocket|route)\(")
_CLI_RE = re.compile(r"argparse\.ArgumentParser\(|typer\.|@click\.command|add_typer\(")
_ENV_RE = re.compile(r"os\.(environ|getenv)|BaseSettings")


def is_architect_doc(path: str) -> bool:
    name = pathlib.PurePosixPath(path).name
    if name.upper().startswith("CHANGELOG") or path.startswith((".github/", ".claude/", ".shared/", ".ai/", "graphify-out/")):
        return False
    if name in ("requirements.txt", "LICENSE.txt") or name.startswith("requirements"):
        return False
    if path.lower().endswith(".html"):
        return path.startswith("docs/")  # an app template is not documentation
    return path.lower().endswith(DOC_SUFFIXES) or name == ".env.example"


def collect_doc_inputs(files: list[str]) -> tuple[list[str], str, str]:
    docs = [f for f in files if is_architect_doc(f)]
    blocks = []
    for d in docs:
        text = pathlib.Path(d).read_text(errors="replace")
        heads = [ln for ln in text.splitlines() if ln.startswith("#")][:40]
        blocks.append(f"### {d} ({len(text)} chars)\n{chr(10).join(text.splitlines()[:25])}\n[headings]\n" + "\n".join(heads))
    signals: list[str] = []
    for f in files:
        if not f.endswith((".py", ".yml", ".yaml", ".toml", ".cfg")) or f.startswith((".shared/", ".ai/")):
            continue
        for n, line in enumerate(pathlib.Path(f).read_text(errors="replace").splitlines(), 1):
            if f.endswith(".py") and (_ROUTE_RE.search(line) or _CLI_RE.search(line) or _ENV_RE.search(line)):
                signals.append(f"{f}:{n}: {line.strip()[:140]}")
    workflows = [f for f in files if f.startswith(".github/workflows/")]
    signals.append("workflows: " + ", ".join(workflows))
    return docs, "\n\n".join(blocks)[:90_000], "\n".join(signals[:600])


def _existing(paths: object, universe: set[str]) -> list[str]:
    return [p for p in paths if isinstance(p, str) and p in universe] if isinstance(paths, list) else []


def validate_docs_proposal(data: dict, docs: set[str], files: set[str]) -> tuple[dict, list[str]]:
    """Keep only what the repository can back up; report what was dropped."""
    warnings: list[str] = []
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str) or not isinstance(data.get("inventory"), list):
        raise ValueError("proposal does not match the schema")
    out: dict = {"summary": data["summary"], "inventory": [], "gaps": [], "mapping": [], "duplicates": [],
                 "obsolete": [], "new_docs": [], "open_questions": [q for q in data.get("open_questions", []) if isinstance(q, str)]}
    for item in data["inventory"]:
        if isinstance(item, dict) and item.get("path") in docs and item.get("quadrant") in QUADRANTS + ("unclear",):
            out["inventory"].append({"path": item["path"], "quadrant": item["quadrant"],
                                     "ambiguous": bool(item.get("ambiguous")) or item["quadrant"] == "unclear",
                                     "reason": str(item.get("reason", ""))})
        else:
            warnings.append(f"inventory entry dropped (unknown file or quadrant): {str(item)[:100]}")
    for item in data.get("gaps", []) if isinstance(data.get("gaps"), list) else []:
        ev = _existing(item.get("evidence") if isinstance(item, dict) else None, files)
        if ev and item.get("verification") in ("code", "human") and isinstance(item.get("topic"), str):
            out["gaps"].append({**{k: item.get(k, "") for k in ("topic", "quadrant", "why")}, "evidence": ev,
                                "verification": item["verification"]})
        else:
            warnings.append(f"gap dropped (no existing evidence file): {str(item)[:100]}")
    for item in data.get("new_docs", []) if isinstance(data.get("new_docs"), list) else []:
        ev = _existing(item.get("evidence") if isinstance(item, dict) else None, files)
        if (ev and item.get("quadrant") in QUADRANTS and item.get("verification") in ("code", "human")
                and isinstance(item.get("path"), str) and not item["path"].upper().endswith("CHANGELOG.MD")):
            out["new_docs"].append({"path": item["path"], "quadrant": item["quadrant"],
                                    "title": str(item.get("title", "")), "evidence": ev, "verification": item["verification"]})
        else:
            warnings.append(f"new document proposal dropped (insufficient evidence): {str(item)[:100]}")
    for item in data.get("mapping", []) if isinstance(data.get("mapping"), list) else []:
        if isinstance(item, dict) and item.get("from") in docs and item.get("action") in ("keep", "move", "merge", "split", "rewrite") \
                and isinstance(item.get("to"), str):
            out["mapping"].append({k: str(item.get(k, "")) for k in ("from", "to", "action", "notes")})
        else:
            warnings.append(f"mapping entry dropped: {str(item)[:100]}")
    for item in data.get("duplicates", []) if isinstance(data.get("duplicates"), list) else []:
        paths = _existing(item.get("paths") if isinstance(item, dict) else None, docs)
        if len(paths) >= 2:
            out["duplicates"].append({"paths": paths, "note": str(item.get("note", ""))})
    for item in data.get("obsolete", []) if isinstance(data.get("obsolete"), list) else []:
        ev = _existing(item.get("evidence") if isinstance(item, dict) else None, files)
        if isinstance(item, dict) and item.get("path") in docs and ev:
            out["obsolete"].append({"path": item["path"], "evidence": ev, "note": str(item.get("note", ""))})
        else:
            warnings.append(f"obsolete claim dropped (no existing evidence file): {str(item)[:100]}")
    structure = data.get("structure") if isinstance(data.get("structure"), dict) else {}
    out["structure"] = {
        "directories": [d for d in structure.get("directories", [])
                        if isinstance(d, dict) and d.get("quadrant") in QUADRANTS and isinstance(d.get("path"), str)],
        "navigation": [n for n in structure.get("navigation", []) if isinstance(n, dict) and isinstance(n.get("entries"), list)],
    }
    return out, warnings


def render_docs_report(p: dict, docs: list[str], warnings: list[str]) -> str:
    classified = {i["path"] for i in p["inventory"]}
    verif = {"code": "verifiable from the code", "human": "needs human confirmation"}
    lines = ["<!-- docs-architect-proposal -->", "# Documentation architecture proposal (Diataxis)", "",
             "> Plan only. No document was created, moved or rewritten. Any later change must come as a patch or pull "
             "request reviewed by a person; nothing is merged automatically. The changelog is out of scope here.", "",
             p["summary"], "", "## Inventory", "", "| Document | Quadrant | Ambiguous | Why |", "|---|---|---|---|"]
    lines += [f"| `{i['path']}` | {i['quadrant']} | {'yes' if i['ambiguous'] else 'no'} | {i['reason']} |" for i in p["inventory"]]
    missed = [d for d in docs if d not in classified]
    if missed:
        lines += ["", "Not classified by the agent (needs a person): " + ", ".join(f"`{d}`" for d in missed)]
    lines += ["", "## Proposed structure", ""]
    lines += [f"- `{d['path']}/` ({d['quadrant']}): {d.get('purpose', '')}" for d in p["structure"]["directories"]] or ["- (none proposed)"]
    lines += ["", "### Navigation map", ""]
    for section in p["structure"]["navigation"]:
        lines.append(f"- **{section.get('section', '')}**")
        lines += [f"  - [{e.get('title', '')}]({e.get('path', '')})" for e in section["entries"] if isinstance(e, dict)]
    lines += ["", "## Mapping of existing documents", "", "| From | Action | To | Notes |", "|---|---|---|---|"]
    lines += [f"| `{m['from']}` | {m['action']} | `{m['to']}` | {m['notes']} |" for m in p["mapping"]]
    lines += ["", "## Gaps", ""]
    lines += [f"- **{g['topic']}** ({g['quadrant']}, {verif[g['verification']]}): {g['why']} Evidence: "
              + ", ".join(f"`{e}`" for e in g["evidence"]) for g in p["gaps"]] or ["- none found"]
    lines += ["", "## Proposed new documents (only where the evidence is enough)", ""]
    lines += [f"- `{n['path']}` ({n['quadrant']}, {verif[n['verification']]}): {n['title']}. Evidence: "
              + ", ".join(f"`{e}`" for e in n["evidence"]) for n in p["new_docs"]] or ["- none"]
    lines += ["", "## Duplicates", ""] + ([f"- {', '.join(f'`{x}`' for x in d['paths'])}: {d['note']}" for d in p["duplicates"]] or ["- none"])
    obsolete = [f"- `{o['path']}`: {o['note']} Evidence: " + ", ".join(f"`{e}`" for e in o["evidence"]) for o in p["obsolete"]]
    lines += ["", "## Possibly obsolete", ""] + (obsolete or ["- none"])
    lines += ["", "## Open questions for a person", ""] + ([f"- {q}" for q in p["open_questions"]] or ["- none"])
    if warnings:
        lines += ["", "## Checks applied", "", f"{len(warnings)} item(s) from the model were discarded because the repository does not back them up:"]
        lines += [f"- {w}" for w in warnings[:20]]
    return "\n".join(lines) + "\n"


def cmd_docs_plan(args: argparse.Namespace) -> int:
    files = [f for f in git("ls-files").stdout.split("\n") if f and not f.startswith((".shared/", ".ai/"))]
    docs, doc_text, signals = collect_doc_inputs(files)
    if not docs:
        print("no documentation files found")
        return 0
    caller = lib.ModelCaller(args.ai_script, max_chars=args.max_chars, timeout=args.timeout)
    user = ("## Repository files\n" + "\n".join(files[:800]) + f"\n\n## Existing documentation (head and headings)\n{doc_text}"
            f"\n\n## Signals extracted from the code\n{signals}")
    box: dict = {}

    def validate(data: dict):
        try:
            box["p"], box["w"] = validate_docs_proposal(data, set(docs), set(files))
        except ValueError:
            return None
        return box["p"] if box["p"]["inventory"] else None

    if lib.ask_json(caller, "docs-architect", lib.load_prompt("docs-architect"), user, validate) is None:
        print("::error::the documentation architect did not return a usable proposal", file=sys.stderr)
        return 1
    dirty = [ln for ln in git("status", "--porcelain").stdout.split("\n") if ln and not ln[3:].startswith((".ai/", ".shared/"))]
    if dirty:
        print(f"::error::the working tree changed during a plan-only run: {dirty[:5]}", file=sys.stderr)
        return 1
    out = pathlib.Path(".ai/docs-architect")
    out.mkdir(parents=True, exist_ok=True)
    report = render_docs_report(box["p"], docs, box["w"]) + f"\nModel cost: ${caller.total_cost_usd():.4f}\n"
    (out / "proposal.md").write_text(report)
    (out / "proposal.json").write_text(json.dumps(box["p"], indent=2))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report)
    if args.post_issue:
        title = "Documentation architecture proposal"
        found = json.loads(_gh("issue", "list", "--repo", args.repo, "--state", "open", "--search",
                               f'"{title}" in:title', "--json", "number,title").stdout or "[]")
        match = next((i for i in found if i["title"] == title), None)
        body = report[:60_000]
        if match:
            _gh("issue", "edit", str(match["number"]), "--repo", args.repo, "--body", body)
        else:
            _gh("issue", "create", "--repo", args.repo, "--title", title, "--body", body)
    print(f"proposal written to {out}/proposal.md")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def model_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--ai-script", required=True)
        p.add_argument("--max-chars", type=int, default=140_000)
        p.add_argument("--timeout", type=int, default=180)

    g = sub.add_parser("guard")
    g.add_argument("--repo", required=True)
    g.add_argument("--issue", type=int, required=True)
    g.set_defaults(fn=cmd_guard)

    r = sub.add_parser("run")
    model_args(r)
    r.add_argument("--issue", type=int, default=0)
    r.add_argument("--run-number", type=int, required=True)
    r.add_argument("--issue-title-file", required=True)
    r.add_argument("--issue-body-file", required=True)
    r.add_argument("--context-files", default="CLAUDE.md,README.md")
    r.add_argument("--verify-command-file", default="")
    r.add_argument("--verify-timeout", type=int, default=600)
    r.add_argument("--max-iterations", type=int, default=3)
    r.add_argument("--max-verify-retries", type=int, default=3)
    r.add_argument("--writer-rounds", type=int, default=8)
    r.add_argument("--changelog", default="CHANGELOG.md")
    r.set_defaults(fn=cmd_run)

    p = sub.add_parser("publish")
    p.add_argument("--repo", required=True)
    p.add_argument("--issue", type=int, default=0)
    p.add_argument("--base-branch", default="main")
    p.set_defaults(fn=cmd_publish)

    rv = sub.add_parser("review")
    model_args(rv)
    rv.add_argument("--repo", required=True)
    rv.add_argument("--pr", type=int, required=True)
    rv.add_argument("--base-sha", required=True)
    rv.add_argument("--head-sha", required=True)
    rv.add_argument("--post", action="store_true")
    rv.set_defaults(fn=cmd_review)

    d = sub.add_parser("docs-plan")
    model_args(d)
    d.add_argument("--repo", default="")
    d.add_argument("--post-issue", action="store_true")
    d.set_defaults(fn=cmd_docs_plan)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
