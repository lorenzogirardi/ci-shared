#!/usr/bin/env python3
"""Role-based agent pipeline.

Subcommands
  guard      is another agent PR for this issue already open? (cheap, no model)
  run        planner -> writer -> deterministic checks -> reviewers A and B ->
             dedup/validate -> fix loop -> final review -> docs -> changelog.
             Commits to a local branch only; holds NO push credential.
  publish    push that branch and open the PR. The only step with a write token.
  merge-gate    merge a PR whose head commit is certified and whose CI is green
  main-guard    the base branch went red: revert the culprit, open a redo issue
  guard-change / change / publish-pr
             the same engine on a change that already exists: a pull request
             (fixes are pushed to its branch) or a push straight to main (a fix
             PR is opened). No planner: the author's description is the intent.
  review     the two independent reviewers on any PR diff (reusable elsewhere)
  docs-plan  documentation architect: a Diataxis-style proposal, read-only

Why `run` and `publish` are separate: `run` executes code a model just wrote
(the deterministic gates), so it must not see a token that can write to the
repository. See agent_lib.run_verify_isolated.

The loop is bounded (max iterations, max verify retries, writer exploration
rounds). When an attempt does not converge it is repeated ONCE with twice the
budget; if that fails too the change is ABANDONED: the base branch is untouched,
the PR (if any) is labelled `agent-abandoned` and, if the agent opened it, closed.
No outcome waits for a person (see TERMINAL_ACTIONS). Merging is done by
`merge-gate`, only for a head commit that is certified and whose CI is green.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime
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
from pr_review_sweep import _parse_json_reply, build_diff, collect_failure_logs, gh_json, post_comment, try_merge

OUT_DIR = pathlib.Path(".ai/agent-run")
BRANCH_PREFIX = "agent/issue-"
REVIEWERS = ("reviewer-correctness", "reviewer-security")
PLANNER_ROUNDS = 6
RETRY_BOOST = 2
CERT_MARKER = "agent-certified"
ABANDONED_LABEL = "agent-abandoned"
# Every way a change can end, and what the system does about it. None of them
# waits for a person; tests/test_terminal_states.py fails if one ever does.
TERMINAL_ACTIONS = {
    "clean": "certify",          # nothing to change: eligible to merge once CI is green
    "converged": "certify",      # fixed: eligible to merge once CI is green
    "abandoned": "abandon",      # label + comment; the agent's own PR is closed; base untouched
    "not_feasible": "abandon",   # the request cannot be implemented as written: commented on the issue
}
# Every commit the pipeline makes carries this author, which is how a run
# recognises its own pushes and does not review them again (no loops).
AGENT_AUTHOR = "ci-shared agents"
_BOT_SUBJECT = re.compile(r"^(Done\s+by Github Actions|docs\(changelog\))", re.IGNORECASE)
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
                reviewers: tuple[str, ...] = REVIEWERS, guidance: str = ""):
    """Run the independent reviewers on `diff`. Returns (findings, dropped)
    after validation against the diff and dedup, or None if a reviewer could
    not produce a usable reply (fail closed: the change cannot be certified)."""
    ranges = lib.diff_ranges(diff)
    user = (
        f"## Plan\n{json.dumps(plan, indent=2) if plan else 'No plan was provided; judge the diff on its own.'}\n\n"
        f"## Diff (lines are prefixed `L<number>|` with their line number in the new file)\n{lib.annotate_diff(diff)}"
        + (f"\n\n## Guidance from the maintainers of this repository\n{guidance}" if guidance.strip() else "")
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


SEVERITY_NOTE = ("Severity mapping: where this guidance says [Critical], use severity critical or high; "
                 "everything else is medium or low.")


def structured_review(caller: lib.ModelCaller, diff: str, title: str, body: str,
                      guidance: str = "") -> tuple[bool, str] | None:
    """Reviewers A and B on a diff for callers that only need a verdict and a
    text (the Renovate sweep). Returns (clean, markdown) or None when a
    reviewer was unusable, in which case nothing is certified."""
    plan = derived_plan(title, lib.sanitize_untrusted(body, 4000))
    note = f"{SEVERITY_NOTE}\n\n{guidance.strip()}" if guidance.strip() else ""
    result = review_diff(caller, diff, plan, guidance=note)
    if result is None:
        return None
    findings, dropped = result
    blocking = sum(1 for f in findings if f.blocking)
    text = ("Reviewer A (correctness/design) and reviewer B (security/operability), independent, deduplicated: "
            f"{len(findings)} finding(s), {blocking} blocking"
            + (f", {len(dropped)} dropped (not on a changed line)" if dropped else "")
            + f".\n\n{lib.render_findings_md(findings)}")
    return blocking == 0, text


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
    start: str = "write"      # "write" (issue), "verify" (existing PR), "review" (a push on main), "ci" (a failed CI run)
    fix_kind: str = "feat"    # conventional-commit type of the agent's commits
    write_tests: bool = True  # the test steward evaluates changes to application code


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
    failure_output: str
    verdicts: list
    adjudications: list
    tests_done: int
    applied_kind: str


def is_doc_path(path: str) -> bool:
    name = pathlib.PurePosixPath(path).name
    if name.upper().startswith("CHANGELOG") or path.startswith((".github/", ".shared/", ".ai/")):
        return False
    return path.lower().endswith((".md", ".rst", ".txt")) or name == ".env.example"


def commit_round(message: str) -> bool:
    git("add", "-A", *_ADD_EXCLUDES)
    if git("diff", "--cached", "--quiet").returncode == 0:
        return False
    git("config", "user.name", AGENT_AUTHOR)
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


# ---------------------------------------------------------------------------
# Failing tests: who is wrong, the code or the test?
# ---------------------------------------------------------------------------

CLASSES = ("code_defect", "test_defect", "environment", "preexisting")
MIN_QUOTE = 12


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def intent_text(plan: dict) -> str:
    """What the change says it is for: the only thing that can justify
    redefining behaviour a test checks."""
    return "\n".join([plan.get("summary", ""), *plan.get("scope", []), *plan.get("acceptance_criteria", [])])


def parse_verdicts(data: dict, tests: list[str]) -> list[dict] | None:
    if not isinstance(data, dict) or not isinstance(data.get("verdicts"), list):
        return None
    out = []
    for item in data["verdicts"]:
        if not isinstance(item, dict) or item.get("test") not in tests or item.get("classification") not in CLASSES:
            continue
        out.append({"test": item["test"], "classification": item["classification"],
                    "confidence": str(item.get("confidence", "medium")),
                    "intent_evidence": item.get("intent_evidence") if isinstance(item.get("intent_evidence"), str) else "",
                    "reason": str(item.get("reason", ""))})
    return out


def apply_rules(verdicts: list[dict], tests: list[str], evidence: dict, intent: str) -> list[dict]:
    """The model proposes, the code decides. Deterministic evidence overrides it
    (a test that passes on re-run is flaky; one that also fails on the base
    commit is not this change's fault), a missing verdict means code_defect, and
    `test_defect` stands only if the model quoted the stated intent verbatim."""
    by_test = {v["test"]: dict(v) for v in verdicts}
    final = []
    for test in tests:
        v = by_test.get(test) or {"test": test, "classification": "code_defect", "confidence": "low",
                                  "intent_evidence": "", "reason": "no verdict was returned for this test"}
        hint = evidence.get(test, {}).get("hint")
        if hint == "flaky":
            v["classification"], v["reason"] = "environment", v["reason"] + " [it passed on re-run: flaky]"
        elif hint == "preexisting":
            v["classification"], v["reason"] = "preexisting", v["reason"] + " [it already fails on the base commit]"
        elif v["classification"] == "test_defect":
            quote = _squash(v.get("intent_evidence", ""))
            if len(quote) < MIN_QUOTE or quote not in _squash(intent):
                v["classification"] = "code_defect"
                v["reason"] += " [downgraded: no verbatim quote of the stated intent justifies changing the test]"
        final.append(v)
    return final


def uncommitted_sources(applied: dict | None, limit: int = 6000) -> str:
    if not applied:
        return ""
    parts = []
    for name in applied["edited"] + applied["created"]:
        path = pathlib.Path(name)
        if path.is_file():
            parts.append(f"### {name}\n{path.read_text(errors='replace')[:limit]}")
    return "\n\n".join(parts)


def build_graph(rt: Runtime):
    def stop(state: RunState, outcome: str, note: str) -> dict:
        return {"route": "end", "outcome": outcome, "notes": state.get("notes", []) + [note]}

    def n_write(state: RunState) -> dict:
        applied, text = run_writer(rt, state["plan"], state.get("feedback", ""))
        if applied is None:
            reportable = state.get("committed") or state.get("rounds")
            return stop(state, "escalated" if reportable else "failed", f"writer: {text}")
        return {"route": "verify", "applied": {"edited": applied.edited, "created": applied.created},
                "explanation": text}

    def adjudicate(state: RunState, output: str):
        """(verdicts, evidence) for the failing tests in `output`, or None when
        the failure is not a test failure (lint, collection) and needs no verdict."""
        tests = lib.parse_failed_tests(output)
        if not tests:
            return None
        evidence = lib.gather_evidence(tests, rt.base_sha)
        intent = intent_text(state["plan"])
        sources = "\n\n".join(
            f"### {path}\n{pathlib.Path(path).read_text(errors='replace')[:4000]}"
            for path in sorted({t.split("::")[0] for t in tests}) if pathlib.Path(path).is_file())
        user = (f"## Stated intent of the change\n{intent}\n\n## Failing tests\n" + "\n".join(tests)
                + f"\n\n## Failing output\n{output[-4000:]}\n\n## Deterministic evidence per failing test\n"
                + json.dumps(evidence, indent=2)
                + f"\n\n## Diff already committed on this branch\n{diff_since_base(rt.base_sha)[:20000]}"
                + (f"\n\n## Change being tested right now (not committed yet)\n{uncommitted_sources(state.get('applied'))}"
                   if state.get("applied") else "")
                + f"\n\n## Source of the failing tests\n{sources}")
        verdicts = lib.ask_json(rt.caller, "failure-adjudicator", lib.load_prompt("failure-adjudicator"), user,
                                lambda d: parse_verdicts(d, tests))
        return apply_rules(verdicts or [], tests, evidence, intent), evidence

    def handle_failure(state: RunState, output: str, applied: lib.Applied | None) -> dict:
        """Where a failed deterministic check goes next. Tests are the
        specification: unless the change's own stated intent redefines a test,
        the code is fixed, never the test."""
        attempts = state.get("verify_attempts", 0) + 1
        notes = list(state.get("notes", []))
        history = list(state.get("adjudications", []))
        verdict_text = ""
        result = adjudicate(state, output)
        if result:
            verdicts, _ = result
            classes = {v["classification"] for v in verdicts}
            history.append({"iteration": state["iteration"], "verdicts": verdicts})
            notes.append("failure adjudication: " + "; ".join(f"{v['test']} -> {v['classification']}" for v in verdicts))
            over = attempts > rt.max_verify_retries
            if classes == {"test_defect"} and rt.write_tests and not over:
                return {"route": "steward", "verify_attempts": attempts, "failure_output": output,
                        "verdicts": verdicts, "notes": notes, "adjudications": history}
            if classes == {"environment"} and not over:
                return {"route": "verify", "verify_attempts": attempts, "notes": notes, "adjudications": history}
            verdict_text = ("\n\nADJUDICATION. Tests are the specification, so the code must satisfy them. "
                            "Verdicts:\n" + json.dumps([v for v in verdicts if v["classification"] != "environment"], indent=2))
        if applied:
            lib.revert(applied)
            what = "your previous change (reverted)"
        else:
            what = "the change as submitted"
        if attempts > rt.max_verify_retries:
            return {**stop(state, "escalated" if state.get("committed") else "failed",
                           f"deterministic checks still failing after {attempts} attempts:\n{output[-1500:]}"),
                    "notes": notes + [f"deterministic checks still failing after {attempts} attempts:\n{output[-1500:]}"],
                    "adjudications": history}
        return {"route": "write", "verify_attempts": attempts, "applied": None, "notes": notes, "adjudications": history,
                "feedback": f"FAILED VERIFICATION of {what}. Fix this:\n{output}{verdict_text}"}

    def n_verify(state: RunState) -> dict:
        ok, output = lib.run_verify_isolated(rt.verify_command, rt.verify_timeout)
        log(f"deterministic checks (iteration {state['iteration']}): {'passed' if ok else 'FAILED'}")
        raw = state.get("applied")
        applied = lib.Applied(raw["edited"], raw["created"]) if raw else None
        if ok:
            if applied:
                kind = state.get("applied_kind") or (rt.fix_kind if state["iteration"] == 1 else "fix")
                commit_round(f"{kind}(agent): {state['explanation'][:120]}\n\nIteration {state['iteration']} of the agent pipeline.")
            needs_tests = rt.write_tests and state.get("tests_done") != state["iteration"]
            return {"route": "tests" if needs_tests else "review",
                    "committed": bool(applied) or state.get("committed", False),
                    "verify_attempts": 0, "applied": None, "applied_kind": None}
        return handle_failure(state, output, applied)

    def n_ci_failure(state: RunState) -> dict:
        return handle_failure(state, state.get("failure_output", ""), None)

    def back_to_code(state: RunState, applied: lib.Applied | None, why: str) -> dict:
        """The tests could not be updated legitimately, so the code is what
        has to change."""
        if applied:
            lib.revert(applied)
        return {"route": "write", "applied": None,
                "notes": state.get("notes", []) + [why],
                "feedback": f"FAILED VERIFICATION. The tests were judged to need updating but that was not possible ({why}). "
                            f"Change the CODE so the tests pass:\n{state.get('failure_output', '')}"}

    def merged(state: RunState, extra: lib.Applied) -> dict:
        raw = state.get("applied") or {"edited": [], "created": []}
        return {"edited": sorted(set(raw["edited"]) | set(extra.edited)), "created": sorted(set(raw["created"]) | set(extra.created))}

    def n_steward(state: RunState) -> dict:
        """Reactive: the adjudicator found the tests wrong, with a quote of the intent."""
        raw = state.get("applied")
        current = lib.Applied(raw["edited"], raw["created"]) if raw else None
        wrong = [v for v in state["verdicts"] if v["classification"] == "test_defect"]
        tests = [v["test"] for v in wrong]
        sources = "\n\n".join(f"### {p}\n{pathlib.Path(p).read_text(errors='replace')[:6000]}"
                               for p in sorted({t.split('::')[0] for t in tests}) if pathlib.Path(p).is_file())
        user = (f"## Mode\nREACTIVE: these tests were judged wrong by the adjudicator.\n\n## Stated intent\n{intent_text(state['plan'])}"
                f"\n\n## Verdicts\n{json.dumps(wrong, indent=2)}\n\n## Failing output\n{state.get('failure_output', '')[-4000:]}"
                f"\n\n## Tests to update\n{sources}\n\n## Diff\n{diff_since_base(rt.base_sha)[:20000]}")
        parsed = lib.ask_json(rt.caller, "test-steward", lib.load_prompt("test-steward"), user,
                              lambda d: lib.parse_changes(d, allowed=lib.is_test_path))
        if parsed is None or not parsed[0]:
            return back_to_code(state, current, "the test steward returned no usable change")
        changes, explanation = parsed
        applied = lib.apply_changes(changes)
        if applied.error:
            return back_to_code(state, current, f"the test change could not be applied: {applied.error}")
        violations = lib.weakened_tests("HEAD")
        if violations:
            lib.revert(applied)
            return back_to_code(state, current, "the test change weakens the tests: " + "; ".join(violations))
        base_explanation = state.get("explanation", "")
        return {"route": "verify", "applied": merged(state, applied), "applied_kind": "test",
                "explanation": f"{base_explanation}; tests updated: {explanation}".strip("; "),
                "notes": state.get("notes", []) + [f"tests updated to the stated intent: {', '.join(applied.files)}"]}

    def n_tests(state: RunState) -> dict:
        """Proactive: does the change to application code have the tests it needs?"""
        base = {"tests_done": state["iteration"], "route": "review"}
        if not any(lib.is_source_path(f) for f in changed_files(rt.base_sha)):
            return base
        user = (f"## Mode\nPROACTIVE: decide whether the tests cover this change.\n\n## Stated intent\n{intent_text(state['plan'])}"
                f"\n\n## Existing tests\n{lib.test_inventory()}\n\n## Diff\n{diff_since_base(rt.base_sha)[:40000]}")
        parsed = lib.ask_json(rt.caller, "test-steward", lib.load_prompt("test-steward"), user,
                              lambda d: lib.parse_changes(d, allowed=lib.is_test_path))
        notes = list(state.get("notes", []))
        if parsed is None:
            return {**base, "notes": notes + ["test steward returned no usable reply; test coverage of the change was not evaluated"]}
        changes, explanation = parsed
        if not changes:
            return {**base, "notes": notes + [f"tests: {explanation or 'the existing tests already cover this change'}"]}
        applied = lib.apply_changes(changes)
        if applied.error:
            return {**base, "notes": notes + [f"test steward changes were not applicable: {applied.error}"]}
        violations = lib.weakened_tests("HEAD")
        if violations:
            lib.revert(applied)
            return {**base, "notes": notes + ["test steward changes refused (they weaken the tests): " + "; ".join(violations)]}
        found = lib.discriminating(rt.base_sha, [f for f in applied.files if f.endswith(".py")])
        if found is not None and found[0] == 0:
            notes.append("the new tests also pass without the change, so they may not test it")
        return {"tests_done": state["iteration"], "route": "verify", "applied": {"edited": applied.edited, "created": applied.created},
                "applied_kind": "test", "explanation": f"tests: {explanation}", "notes": notes + [f"tests added/updated: {', '.join(applied.files)}"]}

    def after_review(state: RunState, findings: list[lib.Finding], dropped, label: str) -> dict:
        blocking = [f for f in findings if f.blocking]
        rounds = state.get("rounds", []) + [{
            "iteration": state["iteration"], "stage": label,
            "findings": [f.to_dict() for f in findings], "dropped": len(dropped),
        }]
        if not blocking:
            # A final review certifies the agent's own changes; if it made none
            # (a clean PR or push), the first review already was the verdict.
            skip_final = label == "final" or not state.get("committed")
            return {"route": "docs" if skip_final else "final", "rounds": rounds}
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

    def n_start(state: RunState) -> dict:
        return {"route": rt.start}

    graph = StateGraph(RunState)
    for name, fn in (("start", n_start), ("write", n_write), ("verify", n_verify), ("review", n_review),
                     ("ci_failure", n_ci_failure), ("steward", n_steward), ("tests", n_tests),
                     ("final", n_final), ("docs", n_docs)):
        graph.add_node(name, fn)
    graph.set_entry_point("start")
    route = lambda s: s["route"]  # noqa: E731
    graph.add_conditional_edges("start", route, {"write": "write", "verify": "verify", "review": "review", "ci": "ci_failure"})
    graph.add_conditional_edges("ci_failure", route, {"write": "write", "steward": "steward", "verify": "verify", "end": END})
    graph.add_conditional_edges("steward", route, {"verify": "verify", "write": "write", "end": END})
    graph.add_conditional_edges("tests", route, {"verify": "verify", "review": "review"})
    graph.add_conditional_edges("write", route, {"verify": "verify", "end": END})
    graph.add_conditional_edges("verify", route, {"review": "review", "tests": "tests", "write": "write", "steward": "steward",
                                                  "verify": "verify", "end": END})
    graph.add_conditional_edges("review", route, {"final": "final", "docs": "docs", "write": "write", "end": END})
    graph.add_conditional_edges("final", route, {"docs": "docs", "write": "write", "end": END})
    graph.add_edge("docs", END)
    return graph.compile()


# ---------------------------------------------------------------------------
# PR body
# ---------------------------------------------------------------------------

def build_pr_body(plan: dict, final: dict, issue_number: int, cost: float, by_role: dict) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) or "- (none)"

    status = "All deterministic checks and every review passed."
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


def settle(outcome: str) -> str:
    """A graph that did not converge ends as `abandoned`, never as a hand-off."""
    return "abandoned" if outcome in ("escalated", "failed") else outcome


def invoke_with_retry(make_runtime, initial: dict) -> dict:
    """Run the graph; if it does not converge, run it ONCE more with twice the
    budget, continuing from whatever the first attempt committed. `make_runtime
    (attempt, previous)` builds the Runtime for each attempt."""
    final: dict = {}
    for attempt in (1, 2):
        rt = make_runtime(attempt, final)
        state = dict(initial)
        if attempt == 2:
            state.update({
                "notes": final.get("notes", []) + ["attempt 1 did not converge; retrying once with twice the budget"],
                "rounds": final.get("rounds", []), "committed": final.get("committed", False),
                "prior_blocking": final.get("prior_blocking", []), "adjudications": final.get("adjudications", []),
                "feedback": "THE PREVIOUS ATTEMPT DID NOT CONVERGE. What happened:\n" + "\n".join(final.get("notes", [])[-5:]),
            })
        limit = (rt.max_iterations + 1) * (rt.max_verify_retries + 1) * 6 + 30
        final = build_graph(rt).invoke(state, config={"recursion_limit": limit})
        if final.get("outcome") == "converged":
            break
    return final


def boosted(rt: Runtime, attempt: int, previous: dict) -> Runtime:
    """The Runtime for attempt 2: twice the budget, and if attempt 1 already
    committed work on an issue, check that work instead of starting from nothing."""
    if attempt == 1:
        return rt
    start = "verify" if rt.start == "write" and previous.get("committed") else rt.start
    return dataclasses.replace(rt, max_iterations=rt.max_iterations * RETRY_BOOST,
                               max_verify_retries=rt.max_verify_retries * RETRY_BOOST,
                               writer_rounds=rt.writer_rounds * RETRY_BOOST, start=start)


def cmd_run(args: argparse.Namespace) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    caller = lib.ModelCaller(args.ai_script, max_chars=args.max_chars, timeout=args.timeout)
    title = pathlib.Path(args.issue_title_file).read_text().strip()
    body = pathlib.Path(args.issue_body_file).read_text()
    context = lib.read_context_files([c.strip() for c in args.context_files.split(",") if c.strip()])
    base_sha = git("rev-parse", "HEAD").stdout.strip()

    plan = run_planner(caller, title, body, context)
    if plan is None:
        write_result({"outcome": "abandoned", "notes": ["the planner did not return a usable plan"]})
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
    final = invoke_with_retry(lambda attempt, previous: boosted(rt, attempt, previous),
                              {"plan": plan, "iteration": 1, "verify_attempts": 0, "notes": [], "rounds": []})

    outcome = settle(final.get("outcome", "failed"))
    touched = changed_files(base_sha)
    if any(n.startswith(".github/workflows/") for n in touched):
        outcome, final["notes"] = "abandoned", final.get("notes", []) + ["refusing to publish: touches .github/workflows/"]
    if outcome != "abandoned" and not touched:
        outcome = "abandoned"
        final["notes"] = final.get("notes", []) + ["the agent produced no change"]
    cost = caller.total_cost_usd()
    result = {"outcome": outcome, "branch": branch, "plan": plan, "notes": final.get("notes", []),
              "rounds": final.get("rounds", []), "files": touched, "cost_usd": cost,
              "final_sha": git("rev-parse", "HEAD").stdout.strip(), "title": plan["summary"][:150]}
    if outcome == "converged":
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

    def commit_comment() -> None:
        if getattr(args, "commit_sha", "") and result.get("findings"):
            text = ("## Agent review of this push\n\n" + lib.render_findings_md([lib.Finding(**f) for f in result["findings"]])
                    + "\n\nNothing is merged automatically.")
            _gh("api", f"repos/{args.repo}/commits/{args.commit_sha}/comments", "-f", f"body={text}")

    if outcome == "clean":
        commit_comment()
        return 0
    if outcome in ("abandoned", "not_feasible"):
        why = "\n".join(f"- {n}" for n in result.get("notes", [])) or "- no detail"
        comment(("The agent could not plan this request" if outcome == "not_feasible"
                 else "The agent abandoned this request after a second attempt with twice the budget; nothing was changed")
                + f":\n{why}")
        commit_comment()
        return 0

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
    created = _gh("pr", "create", "--repo", args.repo, "--base", args.base_branch, "--head", result["branch"],
                  "--title", result["title"], "--body-file", str(OUT_DIR / "pr-body.md"))
    if created.returncode != 0:
        print(f"::error::gh pr create failed: {created.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    url = created.stdout.strip()
    # Certification is bound to this exact commit: the merge gate accepts it only while
    # the PR head is still this sha, and only from a trusted author.
    _gh("pr", "comment", url, "--repo", args.repo, "--body", certification_comment(
        result.get("final_sha") or result.get("head_sha", ""), "the planned change was implemented; the checks, two independent "
        "reviews and the final review passed"))
    commit_comment()
    comment(f"Opened {url}. It merges automatically once its CI is green.")
    log(f"opened {url}")
    return 0


def certification_comment(sha: str, why: str) -> str:
    return (f"<!-- agent-pr -->\n<!-- {CERT_MARKER}: {sha} -->\n## Agent review\n\n"
            f"**Certified at `{sha[:7]}`**: {why}. The pull request merges automatically once its CI is green.")


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


# --- existing changes: a pull request, or a push straight to main ------------

def derived_plan(title: str, body: str) -> dict:
    """A change that already exists has no planner step: the author's own
    description is the intent, and the writer may only fix what reviewers or
    the checks report."""
    return {"feasible": True, "summary": title[:200] or "Change under review", "reason": "",
            "scope": [f"The change as described: {title[:200]}" + (f" - {body.strip()[:400]}" if body.strip() else "")],
            "out_of_scope": ["Anything not needed to fix a failing check or a blocking review finding"],
            "acceptance_criteria": ["The deterministic checks pass", "No blocking review finding remains"],
            "files_hint": [], "risks": []}


def cmd_guard_change(args: argparse.Namespace) -> int:
    """Should this run do nothing? Never reviews its own commits (no loops),
    bookkeeping commits, or a push whose commit belongs to a PR (that PR is
    reviewed on its own)."""
    author = git("log", "-1", "--format=%an").stdout.strip()
    subject = git("log", "-1", "--format=%s").stdout.strip()
    reason = ""
    if args.mode == "ci":
        # A failed CI run on the agent's own fix is exactly when to try again,
        # but only a bounded number of times in a row.
        authors = git("log", "-n", "20", "--format=%an").stdout.split("\n")
        streak = next((i for i, a in enumerate(authors) if a != AGENT_AUTHOR), len(authors))
        if streak >= args.max_streak:
            reason = f"the last {streak} commits on this branch are the agent's own and CI still fails: stopping"
    elif author == AGENT_AUTHOR:
        reason = "the head commit was written by this pipeline"
    elif args.mode == "push":
        if _BOT_SUBJECT.match(subject):
            reason = "bookkeeping commit"
        else:
            prs = gh_json([f"repos/{args.repo}/commits/{args.sha}/pulls"]) or []
            if prs:
                reason = f"the commit belongs to PR #{prs[0]['number']}, which is reviewed there"
    set_output("skip", "true" if reason else "false")
    log(f"skipping: {reason}" if reason else "this change needs a run")
    return 0


def cmd_change(args: argparse.Namespace) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    caller = lib.ModelCaller(args.ai_script, max_chars=args.max_chars, timeout=args.timeout)
    title = pathlib.Path(args.title_file).read_text().strip()
    body = pathlib.Path(args.body_file).read_text()
    context = lib.read_context_files([c.strip() for c in args.context_files.split(",") if c.strip()])
    head_sha = git("rev-parse", "HEAD").stdout.strip()
    plan = derived_plan(title, lib.sanitize_untrusted(body, 4000))

    if args.mode == "push":
        branch = f"agent/push-{head_sha[:7]}"
        git("checkout", "--quiet", "-B", branch, head_sha)
        start = "review"   # the push is already on main; checks on main belong to ai-autofix-main
    else:
        branch, start = "", "verify"
    failure_output = ""
    if getattr(args, "ci_sha", ""):
        # Entered because CI failed: the failing checks' own logs are the first
        # input, read from the run that failed (it may need services this job
        # cannot start, e.g. integration tests).
        start, failure_output = "ci", collect_failure_logs(args.ci_repo, args.ci_sha, 60000)
        if not failure_output.strip():
            write_result({"mode": "pr", "outcome": "clean", "branch": "", "head_sha": head_sha, "commits": 0, "plan": plan,
                          "notes": ["no failing check logs were found for this commit"], "rounds": [], "findings": [],
                          "files": [], "cost_usd": 0.0, "cost_by_role": {}, "title": ""})
            return 0
    rt = Runtime(caller, args.base_sha, pathlib.Path(args.verify_command_file).read_text() if args.verify_command_file else "",
                 args.verify_timeout, args.max_iterations, args.max_verify_retries, args.writer_rounds,
                 context, "", 0, start=start, fix_kind="fix")
    final = invoke_with_retry(lambda attempt, previous: boosted(rt, attempt, previous),
                              {"plan": plan, "iteration": 1, "verify_attempts": 0, "notes": [], "rounds": [],
                               "failure_output": failure_output})

    outcome = settle(final.get("outcome", "failed"))
    new_commits = int(git("rev-list", "--count", f"{head_sha}..HEAD").stdout.strip() or 0)
    touched_now = [n for n in git("diff", "--name-only", head_sha, "HEAD").stdout.split("\n") if n]
    if any(n.startswith(".github/workflows/") for n in touched_now):
        outcome, new_commits = "abandoned", 0
        final["notes"] = final.get("notes", []) + ["refusing to publish: the agent touched .github/workflows/"]
    if outcome == "converged" and new_commits == 0:
        outcome = "clean"
    rounds = final.get("rounds", [])
    sha7 = head_sha[:7]
    result = {
        "mode": args.mode, "outcome": outcome, "branch": branch, "head_sha": head_sha, "commits": new_commits,
        "final_sha": git("rev-parse", "HEAD").stdout.strip(),
        "plan": plan, "notes": final.get("notes", []), "rounds": rounds,
        "findings": rounds[-1]["findings"] if rounds else [], "files": touched_now, "cost_usd": caller.total_cost_usd(),
        "adjudications": final.get("adjudications", []),
        "cost_by_role": caller.cost_by_role(),
        "title": f"fix(agent): address review findings of {sha7}",
    }
    if args.mode == "push" and outcome == "converged":
        plan["summary"] = f"Review of {sha7}: {title[:120]}"
        (OUT_DIR / "pr-body.md").write_text(build_pr_body(plan, final, 0, result["cost_usd"], result["cost_by_role"]))
    write_result(result)
    log(f"agent {args.mode} review: {outcome}, {new_commits} new commit(s), ${result['cost_usd']:.4f}")
    return 0


def build_change_comment(result: dict, pushed_ok: bool | None) -> str:
    outcome = result["outcome"]
    n = result.get("commits", 0)
    sha = result.get("final_sha", "")
    certified = outcome in ("clean", "converged") and pushed_ok is not False and bool(sha)
    last_note = (result.get("notes") or ["no detail"])[-1].splitlines()[0][:300]
    status = {
        "clean": "No blocking findings, and the deterministic checks pass. Nothing was changed.",
        "converged": f"The agent fixed this and {'pushed' if pushed_ok else 'could not push'} {n} commit(s) to the branch. "
                     "Checks and every review pass.",
        "abandoned": f"**The agent abandoned this change** after a second attempt with twice the budget ({last_note}). "
                     "Nothing was merged and the base branch is untouched.",
    }.get(outcome, outcome)
    if certified:
        status += f" **Certified at `{sha[:7]}`**: it merges automatically once its CI is green."
    parts = ["<!-- agent-pr -->" + (f"\n<!-- {CERT_MARKER}: {sha} -->" if certified else ""), "## Agent review", status]
    rounds = result.get("rounds", [])
    if rounds:
        table = ["| Iteration | Stage | Findings | Blocking | Dropped (not on a changed line) |", "|---|---|---|---|---|"]
        for r in rounds:
            blocking = sum(1 for f in r["findings"] if f["severity"] in lib.BLOCKING)
            table.append(f"| {r['iteration']} | {r['stage']} | {len(r['findings'])} | {blocking} | {r['dropped']} |")
        parts.append("\n".join(table))
    if result.get("adjudications"):
        lines = [f"- `{v['test']}`: **{v['classification']}** ({v['confidence']}): {v['reason']}"
                 for a in result["adjudications"] for v in a["verdicts"]]
        parts.append("### Who was wrong, the code or the test?\n" + "\n".join(lines))
    if result.get("findings"):
        parts.append("### Findings at the last round\n" + lib.render_findings_md([lib.Finding(**f) for f in result["findings"]]))
    if result.get("notes"):
        parts.append("### Notes\n" + "\n".join(f"- {x}" for x in result["notes"]))
    parts.append(f"_Reviewer A (correctness/design) and reviewer B (security/operability), independent. "
                 f"Model cost ${result.get('cost_usd', 0):.4f}._")
    return "\n\n".join(parts)


def cmd_publish_pr(args: argparse.Namespace) -> int:
    result = json.loads((OUT_DIR / "result.json").read_text())
    pushed_ok: bool | None = None
    if result["outcome"] == "converged" and result.get("commits"):
        token = os.environ.get("AGENT_PUSH_TOKEN", "")
        if not token:
            print("::error::AGENT_PUSH_TOKEN is not set: fixes pushed as GITHUB_TOKEN would not re-run the PR checks.",
                  file=sys.stderr)
            return 1
        header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        pushed = subprocess.run(
            ["git", "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {header}",
             "push", "--quiet", "origin", f"HEAD:refs/heads/{args.head_ref}"], capture_output=True, text=True)
        pushed_ok = pushed.returncode == 0
        if not pushed_ok:
            result["notes"] = result.get("notes", []) + [
                "the fixes could not be pushed (the branch moved while the agent worked): "
                + pushed.stderr.replace(header, "***").strip()[:200]]
    comments = gh_json([f"repos/{args.repo}/issues/{args.pr}/comments?per_page=100"]) or []
    existing = next((c for c in comments if "<!-- agent-pr -->" in (c.get("body") or "")), None)
    post_comment(args.repo, args.pr, build_change_comment(result, pushed_ok), existing)
    if result["outcome"] == "abandoned":
        # Terminal, with no hand-off: the PR is labelled so the merge gate never touches it, and
        # a PR the agent itself opened is closed. A person's own PR is left open and unmerged.
        _gh("pr", "edit", str(args.pr), "--repo", args.repo, "--add-label", ABANDONED_LABEL)  # best effort
        if args.head_ref.startswith("agent/"):
            _gh("pr", "close", str(args.pr), "--repo", args.repo)
    return 0


# --- merge authority and the safety net after the merge ----------------------

CODE_JOBS = ("build", "quality-gate", "k8s-check", "docker")   # failures a code change can cause
REVERT_PREFIX = "revert(agent)"


def recent_reverts(repo: str, branch: str, hours: int = 24) -> int:
    """How many automatic reverts landed on `branch` lately. This is the circuit
    breaker: stateless (it reads the history, nothing to keep in sync), and it
    stops automatic merging when the pipeline keeps shipping things that break."""
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    commits = gh_json([f"repos/{repo}/commits?sha={branch}&since={since}&per_page=100"]) or []
    return sum(1 for c in commits if ((c.get("commit") or {}).get("message") or "").startswith(REVERT_PREFIX))


def is_certified(comments: list[dict], head_sha: str, trusted: set[str]) -> bool:
    """Certified = a comment by a trusted account carries the marker for exactly
    this head commit. Anyone can post a comment, so the author is checked; a new
    push changes the sha and the certification no longer applies."""
    marker = f"<!-- {CERT_MARKER}: {head_sha} -->"
    return any(marker in (c.get("body") or "") and (c.get("user") or {}).get("login") in trusted for c in comments)


def cmd_merge_gate(args: argparse.Namespace) -> int:
    """Merge a pull request when, and only when: it is open, not abandoned, its
    head commit is certified, the circuit breaker is closed, and the required
    CI checks have succeeded on that same commit (try_merge)."""
    pr = gh_json([f"repos/{args.repo}/pulls/{args.pr}"])
    if not pr or pr.get("state") != "open" or pr.get("draft"):
        log("not mergeable: the PR is closed or a draft")
        return 0
    head = pr["head"]["sha"]
    if ((pr["head"].get("repo") or {}).get("full_name")) != args.repo:
        log("not merging a PR from a fork")
        return 0
    if ABANDONED_LABEL in {label["name"] for label in pr.get("labels", [])}:
        log("not merging: the agent abandoned this change")
        return 0
    comments = gh_json([f"repos/{args.repo}/issues/{args.pr}/comments?per_page=100"]) or []
    trusted = {t.strip() for t in args.trusted.split(",") if t.strip()}
    if not is_certified(comments, head, trusted):
        log(f"not merging: nothing certifies {head[:7]} yet (a later run, or the next push, will)")
        return 0
    reverts = recent_reverts(args.repo, args.base_branch)
    if reverts >= args.max_reverts:
        log(f"not merging: circuit breaker open ({reverts} automatic reverts in 24h). Merging resumes by itself as they age out.")
        return 0
    required = tuple(c.strip() for c in args.required_checks.split(",") if c.strip())
    outcome = try_merge(args.repo, args.pr, head, args.merge_method, required,
                        poll_seconds=args.poll_seconds, poll_interval=15)
    log(f"merge gate: {outcome}")
    return 0


def failing_job_names(repo: str, run_id: int) -> list[str]:
    jobs = (gh_json([f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"]) or {}).get("jobs", [])
    return [j["name"] for j in jobs if j.get("conclusion") == "failure"]


def cmd_main_guard(args: argparse.Namespace) -> int:
    """The base branch went red after a merge. If a code change plausibly caused
    it, take the change out at once (git revert, pushed with the agent token) and
    open an issue labelled `agent` so the whole pipeline redoes it, this time
    knowing why it broke. Never reverts a revert, and stops at the breaker."""
    run = gh_json([f"repos/{args.repo}/actions/runs/{args.run_id}"]) or {}
    sha = run.get("head_sha", "")
    if run.get("head_branch") != args.base_branch or run.get("conclusion") != "failure" or not sha:
        log("nothing to guard: not a failed run on the base branch")
        return 0

    def note(text: str) -> int:
        log(text)
        _gh("api", f"repos/{args.repo}/commits/{sha}/comments", "-f", f"body=Main guard: {text}")
        return 0

    subject = ((gh_json([f"repos/{args.repo}/commits/{sha}"]) or {}).get("commit") or {}).get("message", "").splitlines()[:1]
    subject = subject[0] if subject else sha[:7]
    if subject.startswith(REVERT_PREFIX):
        return note("the automatic revert is itself red; not reverting a revert")
    failed = failing_job_names(args.repo, args.run_id)
    if not set(failed) & set(CODE_JOBS):
        return note(f"not reverting: the failed jobs ({', '.join(failed) or 'none'}) are not ones a code change causes "
                    "(supply chain / scanners)")
    earlier = (gh_json([f"repos/{args.repo}/actions/workflows/{run['workflow_id']}/runs?branch={args.base_branch}"
                        "&status=completed&per_page=10"]) or {}).get("workflow_runs", [])
    previous = next((r for r in earlier if r["run_number"] < run["run_number"]), None)
    if previous is None or previous.get("conclusion") != "success":
        return note("not reverting: the branch was already red before this commit, so it is not the cause")
    if recent_reverts(args.repo, args.base_branch) >= args.max_reverts:
        return note("not reverting: circuit breaker open (too many automatic reverts in 24h)")

    token = os.environ.get("AGENT_PUSH_TOKEN", "")
    if not token:
        print("::error::AGENT_PUSH_TOKEN is not set: cannot push the revert", file=sys.stderr)
        return 1
    header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    git("config", "user.name", AGENT_AUTHOR)
    git("config", "user.email", "actions@github.com")
    # Back to the last green state. Several commits can land between two pipeline runs, and the tip
    # is not necessarily the culprit, so every change since the last green run is taken out
    # (the pipeline's own bookkeeping commits are left alone: reverting them would only churn).
    suspects = [c for c in git("rev-list", "--reverse", f"{previous['head_sha']}..{sha}").stdout.split()
                if not _BOT_SUBJECT.match(git("log", "-1", "--format=%s", c).stdout.strip())]
    if not suspects:
        return note("not reverting: nothing but bookkeeping commits landed since the last green run")
    subjects = [git("log", "-1", "--format=%s", c).stdout.strip() for c in suspects]
    label = subjects[-1] if len(suspects) == 1 else f"{len(suspects)} commits since the last green run"
    message = (f"{REVERT_PREFIX}: {label} (pipeline red)\n\nReverts: " + "; ".join(f"{c[:7]} {t}" for c, t in zip(suspects, subjects))
               + f".\nFailed jobs: {', '.join(failed)}.\nAn issue labelled `agent` redoes the change.")
    for attempt in range(1, 4):
        git("fetch", "--quiet", "origin", args.base_branch)
        git("checkout", "--quiet", "-B", args.base_branch, f"origin/{args.base_branch}")
        if any(git("revert", "--no-commit", c).returncode != 0 for c in reversed(suspects)):
            git("revert", "--abort")
            git("reset", "--hard", f"origin/{args.base_branch}")
            return note("could not revert cleanly (later commits depend on it); leaving the branch as it is")
        git("commit", "--quiet", "-m", message)
        pushed = subprocess.run(["git", "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {header}",
                                 "push", "--quiet", "origin", f"HEAD:refs/heads/{args.base_branch}"],
                                capture_output=True, text=True)
        if pushed.returncode == 0:
            break
        if attempt == 3:
            print(f"::error::revert push rejected: {pushed.stderr.replace(header, '***')[:300]}", file=sys.stderr)
            return 1
    logs = collect_failure_logs(args.repo, sha, 6000) or "(no log excerpt available)"
    listing = "\n".join(f"- `{c[:7]}` {t}" for c, t in zip(suspects, subjects))
    body = (f"The pipeline went red after `{sha[:7]}`, so the changes since the last green run were reverted automatically:\n\n{listing}\n\n"
            f"Failed jobs: {', '.join(failed)}.\n\n## Failure\n```\n{logs[-5000:]}\n```\n\n"
            "Implement these changes again. They must not break the failing checks above.")
    title = f"Redo: {subjects[-1]}" if len(suspects) == 1 else f"Redo: {len(suspects)} reverted changes"
    created = _gh("issue", "create", "--repo", args.repo, "--title", title[:200], "--body", body, "--label", "agent")
    if created.returncode != 0:     # the label may not exist yet: still record the work to redo
        created = _gh("issue", "create", "--repo", args.repo, "--title", title[:200], "--body", body)
    log(f"reverted {len(suspects)} change(s) since the last green run; redo issue: "
        f"{created.stdout.strip() or created.stderr.strip()[:200]}")
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

    gc = sub.add_parser("guard-change")
    gc.add_argument("--mode", choices=("pr", "push", "ci"), required=True)
    gc.add_argument("--max-streak", type=int, default=3,
                    help="mode ci: stop after this many consecutive agent commits on the branch")
    gc.add_argument("--repo", required=True)
    gc.add_argument("--sha", required=True)
    gc.set_defaults(fn=cmd_guard_change)

    c = sub.add_parser("change", help="review and fix an existing PR or a push to main")
    model_args(c)
    c.add_argument("--mode", choices=("pr", "push"), required=True)
    c.add_argument("--base-sha", required=True)
    c.add_argument("--title-file", required=True)
    c.add_argument("--body-file", required=True)
    c.add_argument("--context-files", default="CLAUDE.md,README.md")
    c.add_argument("--verify-command-file", default="")
    c.add_argument("--verify-timeout", type=int, default=600)
    c.add_argument("--max-iterations", type=int, default=3)
    c.add_argument("--max-verify-retries", type=int, default=3)
    c.add_argument("--writer-rounds", type=int, default=8)
    c.add_argument("--ci-repo", default="", help="with --ci-sha: read the failing checks' logs of this commit")
    c.add_argument("--ci-sha", default="")
    c.set_defaults(fn=cmd_change)

    pp = sub.add_parser("publish-pr")
    pp.add_argument("--repo", required=True)
    pp.add_argument("--pr", type=int, required=True)
    pp.add_argument("--head-ref", required=True)
    pp.set_defaults(fn=cmd_publish_pr)

    p = sub.add_parser("publish")
    p.add_argument("--commit-sha", default="")
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

    mg = sub.add_parser("merge-gate", help="merge a certified PR whose CI is green")
    mg.add_argument("--repo", required=True)
    mg.add_argument("--pr", type=int, required=True)
    mg.add_argument("--base-branch", default="main")
    mg.add_argument("--required-checks", default="")
    mg.add_argument("--merge-method", default="squash", choices=("merge", "squash", "rebase"))
    mg.add_argument("--poll-seconds", type=int, default=120)
    mg.add_argument("--trusted", required=True, help="comma-separated logins whose certification comments count")
    mg.add_argument("--max-reverts", type=int, default=3)
    mg.set_defaults(fn=cmd_merge_gate)

    mgd = sub.add_parser("main-guard", help="revert the commit that turned the base branch red, and open a redo issue")
    mgd.add_argument("--repo", required=True)
    mgd.add_argument("--run-id", type=int, required=True)
    mgd.add_argument("--base-branch", default="main")
    mgd.add_argument("--max-reverts", type=int, default=3)
    mgd.set_defaults(fn=cmd_main_guard)

    d = sub.add_parser("docs-plan")
    model_args(d)
    d.add_argument("--repo", default="")
    d.add_argument("--post-issue", action="store_true")
    d.set_defaults(fn=cmd_docs_plan)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
