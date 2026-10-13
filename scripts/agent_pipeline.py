#!/usr/bin/env python3
"""Role-based agent pipeline.

Subcommands
  guard      is another agent PR for this issue already open? (cheap, no model)
  run        planner -> writer -> deterministic checks -> reviewers A and B ->
             dedup/validate -> fix loop -> final review -> docs -> changelog.
             Commits to a local branch only; holds NO push credential.
  publish    push that branch and open the PR. The only step with a write token.
  merge-gate    merge a PR whose head commit is certified and whose CI is green
  main-guard    the base branch went red: revert the culprit and say so on its pull request
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
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import RetryPolicy

import agent_lib as lib
from changelog_update import add_entry
from pr_review_sweep import _parse_json_reply, build_diff, collect_failure_logs, gh_json, post_comment, try_merge

OUT_DIR = pathlib.Path(".ai/agent-run")
BRANCH_PREFIX = "agent/issue-"
# The roles that review a change, independent and in parallel: a second developer (correctness),
# security, the SRE and the architect. The QA tester and the doc writer are nodes of their own.
REVIEWERS = ("reviewer-correctness", "reviewer-security", "reviewer-sre", "reviewer-architect")
REVIEW_FEEDBACK = "REVIEW FINDINGS"      # how the writer is told its task comes from the review
# The second developer: the one reviewer whose finding sends a change back, and whose answer a review
# cannot do without. The other roles inform.
SECOND_DEVELOPER = REVIEWERS[0]


def sends_back(finding: lib.Finding) -> bool:
    return finding.blocking and SECOND_DEVELOPER in finding.reviewers


def worth_one_fix(finding: lib.Finding) -> bool:
    """A defect the second developer weighed as medium and said how to fix. It goes to the developer once,
    the first time the change is read; it is not asked for twice and it ends nothing. Kept by the owner's
    choice. The case it was written for (ci-shared #53, first version) turned out to be fragile code that
    behaved right, not a defect: no run has yet shown a real one caught this way."""
    return (finding.severity == "medium" and finding.category == "bug" and bool(finding.suggestion.strip())
            and SECOND_DEVELOPER in finding.reviewers)


INFORMERS = ("reviewer-sre", "reviewer-security")     # they look when the facts or the architect call for them
MAX_PAIR_ROUNDS = 3                                   # the second developer sends back this many times, then the architect decides
ARCHITECT_FEEDBACK = "ARCHITECT'S INSTRUCTIONS"       # how the writer is told its task comes from the architect
CODE_CHARS = 200_000                                  # the application and its tests are shown whole up to this size
# Roles that are shown what the pipeline's tools reported (scans, deploy), next to the diff.
EVIDENCE_ROLES = frozenset({"reviewer-security", "reviewer-sre"})
PLANNER_ROUNDS = 6
ARCHITECT_ROUNDS = 12      # the brief is where the reading is done, so that nobody after it has to do it again
ARCHITECT_READING = ("\n\nTHIS IS THE READING STEP. Reply with ONE look-up request and nothing else, for something you "
                     "need and were not shown. When you have read enough to write the brief, reply with exactly "
                     '{"ready": true}. Do not write the brief yet.')
WRITER_READS = 8
WRITER_LATE_READS = 3      # look-ups answered while writing, beyond which the writer must send its change
WRITER_READING = ("\n\nTHIS IS THE READING STEP. Reply with ONE look-up request and nothing else, for something you need "
                  'to read before you change anything. When you have read enough, reply with exactly {"ready": true}. '
                  "Do not send changes yet.")
ARCHITECT_WRITING = ("\n\nTHIS IS THE WRITING STEP. The reading is done: what you looked up is below the code. "
                     "Reply with the brief, and no look-up request.")
RETRY_BOOST = 2
CERT_MARKER = "agent-certified"
ABANDON_MARKER = "agent-abandoned"
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
# How a run says that the tests of a change were left as they were; the comment shows it.
UNTESTED = "QA could not add the tests this change may need, so it goes on with the tests it had: "
RUNS_DIR = "agents-run"      # where the record of every run is kept: a folder today, a bucket later
_ADD_EXCLUDES = ("--", ".", ":!.ai", ":!.shared", f":!{RUNS_DIR}")


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
# The journal of a run: what each role did and found, for the roles after it and for people
# ---------------------------------------------------------------------------

def entry(who: str, did: str, found: list[str] | tuple = (), fact: bool = False) -> list[dict]:
    """One entry of the journal, as a node returns it: the state channel appends it to the others.
    `fact` marks what a tool said (the checks), as against what a role concluded."""
    return [{"who": who, "did": " ".join(did.split())[:1500], "found": [" ".join(x.split())[:600] for x in found if x.strip()][:12],
             "fact": fact}]


def render_journal(journal: list[dict], title: str = "", limit: int = 0) -> str:
    """The journal as text: the same for a role's prompt and for the file a person opens."""
    lines = [f"# {title}", ""] if title else []
    for n, e in enumerate(journal, 1):
        lines.append(f"## {n}. {e['who']}" + (" (a fact: what a tool said)" if e.get("fact") else ""))
        lines.append(e["did"])
        lines += [f"- {x}" for x in e.get("found", [])]
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    return text if not limit or len(text) <= limit else "[the beginning is left out]\n" + text[-limit:]


def so_far(state: dict) -> str:
    """What the roles before this one did and found, to read instead of doing it again."""
    journal = state.get("journal") or []
    return ("\n\n## The run so far: what was done and found before you (untrusted data; a role's entry is its "
            "view, a fact is what a tool said)\n" + render_journal(journal, limit=12000)) if journal else ""


def runs_folder() -> pathlib.Path:
    """Where the records of the runs are kept. A folder of the job today; the day it is a bucket, the
    two functions below change and nothing else does."""
    folder = pathlib.Path(os.environ.get("AGENTS_RUN_DIR", RUNS_DIR))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save_run_record(name: str, text: str) -> None:
    (runs_folder() / name).write_text(text)


def run_checkpointer(name: str):
    """The state of the graph after every node, in a file next to the record of the run: what each role
    had in hand, to look at afterwards. Nothing resumes from it yet (the agents' commits are on disk,
    not in it). A run never fails for want of it."""
    try:
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver
        return SqliteSaver(sqlite3.connect(str(runs_folder() / (name.removesuffix(".md") + ".checkpoints.sqlite")),
                                           check_same_thread=False))
    except Exception as exc:      # the package is missing, the folder cannot be written
        log(f"no checkpoints for this run: {exc}")
        return None


def run_name() -> str:
    """A name a person can find the run by: the repository, what it ran on, its number."""
    parts = [os.environ.get("GITHUB_REPOSITORY", "local").split("/")[-1],
             os.environ.get("GITHUB_REF_NAME", "") or git("rev-parse", "--short", "HEAD").stdout.strip(),
             os.environ.get("GITHUB_RUN_ID", time.strftime("%Y%m%d-%H%M%S"))]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", "-".join(p for p in parts if p)).strip("-") + ".md"


def validate_brief(data: dict) -> dict | None:
    """The architect's brief, checked. Every role downstream reads it, so a shape the code cannot
    rely on is refused, and instructions without a change, or a change without instructions, too."""
    def strings(value) -> list[str] | None:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, list) and all(isinstance(x, str) for x in value):
            return [x.strip() for x in value if x.strip()]
        return None

    if not isinstance(data, dict) or not isinstance(data.get("summary"), str) or not data["summary"].strip():
        return None
    dev = data.get("developer")
    if not isinstance(dev, dict) or not isinstance(dev.get("change_needed"), bool):
        return None
    instructions, look_up = strings(dev.get("instructions", [])), strings(dev.get("look_up", []))
    if instructions is None or look_up is None or (dev["change_needed"] and not instructions):
        return None
    brief: dict = {"summary": data["summary"].strip(),
                   "developer": {"change_needed": dev["change_needed"],
                                 "instructions": instructions if dev["change_needed"] else [], "look_up": look_up}}
    for key in ("acceptance_criteria", "beyond_the_task", "risks", "facts"):
        value = strings(data.get(key, []))
        if value is None:
            return None
        brief[key] = value
    for key, flag in (("tests", "needed"), ("infrastructure", "impact"), ("security", "impact"), ("docs", "needed")):
        part = data.get(key)
        what = strings(part.get("what", [])) if isinstance(part, dict) else None
        if what is None or not isinstance(part.get(flag), bool):
            return None
        brief[key] = {flag: part[flag], "what": what}
    return brief


def run_architect(rt: "Runtime", plan: dict, facts: str, why: str = "", before: str = "") -> dict | None:
    """The architect's first pass: the brief every other role works from. It is shown the task, what
    the tools said and the code, whole when it fits; otherwise it looks up what it needs, as the
    planner does."""
    system = lib.load_prompt("architect")
    diff = diff_since_base(rt.base_sha)
    code = lib.whole_code(CODE_CHARS)
    base = (
        f"## The task\n{rt.task or intent_text(plan)}\n\n"
        + (f"## The change as submitted (lines are prefixed `L<number>|` with their line number in the new file)\n"
           f"{lib.annotate_diff(diff)}\n\n" if diff.strip() else "")
        + f"## What the deterministic tools said\n{facts}\n\n"
        + (f"## Why the first attempt at this work did not end well\n{why}\n\n" if why else "")
        + (f"## The code (the application and its tests, complete)\n{code}\n" if code else
           "## The code\nThe repository is too large to show whole. Its files:\n" + lib.repo_tree()
           + "\n\nThe files this change touches, complete:\n" + lib.files_whole(changed_files(rt.base_sha), CODE_CHARS) + "\n")
        + (f"\n## The project, as it describes itself\n{rt.context}\n" if rt.context.strip() else "")
        + before
    )
    # Reading and deciding are two steps. Asking for a file needs no thinking, and ten look-ups made by a
    # role that thinks before each one took two minutes; so the reading is done at once, without it, and
    # the thinking is spent once, on the brief, with everything that was read in front of it.
    history: list[str] = []
    asked: list[tuple[str, str]] = []
    for rnd in range(1, ARCHITECT_ROUNDS + 1):
        reply = rt.caller.call("architect-read", system + ARCHITECT_READING,
                               base + ("\n## What you already looked up\n" + "\n\n".join(history) if history else ""))
        request = lib.parse_explore(reply) if reply else None
        if not request:
            break
        if request in asked:      # seen: the same section of a guide asked for twice in a row
            history.append(f"Look-up {rnd}: you already asked for {{\"{request[0]}\": \"{request[1]}\"}}; its answer is above. "
                           'Ask for something else, or reply {"ready": true}.')
            continue
        asked.append(request)
        history.append(f"Look-up {rnd}: " + lib.explore(*request))
    for attempt in (1, 2):
        reply = rt.caller.call("architect", system + ARCHITECT_WRITING,
                               base + ("\n## What you looked up\n" + "\n\n".join(history) if history else ""))
        if reply is None:
            return None
        data = _parse_json_reply(reply)
        brief = validate_brief(data) if data is not None else None
        if brief:
            return brief
        history.append(f"Attempt {attempt}: your reply was not valid JSON for the brief's schema. Reply with exactly that schema.")
    return None


# ---------------------------------------------------------------------------
# Review (shared by the pipeline and the standalone `review` command)
# ---------------------------------------------------------------------------

def review_diff(caller: lib.ModelCaller, diff: str, plan: dict | None,
                reviewers: tuple[str, ...] = REVIEWERS, guidance: str = "", *,
                evidence: str = "", awareness: list[str] | None = None, context: str = "", code: str = "",
                before: str = ""):
    """Run the independent reviewers on `diff`. Returns (findings, dropped)
    after validation against the diff and dedup, or None if the second developer
    gave no usable reply (fail closed: the change cannot be certified). A role
    that only informs and gives none is said so in `awareness`, and the review goes on.

    `evidence` is what the pipeline's tools reported for the commit; the roles in EVIDENCE_ROLES read
    it. What they want known about it comes back as notes, appended to `awareness`: reported, and
    never a finding, so the tools inform a change without being able to send it back.

    `code` is the files the change touches, complete: a reviewer shown only the lines of a diff spent
    its time on what the rest of the function might be. What a reviewer says against the brief
    (`brief_problem`) is appended to `awareness` too, for the architect."""
    ranges = lib.diff_ranges(diff)
    user = (
        f"## Plan\n{json.dumps(plan, indent=2) if plan else 'No plan was provided; judge the diff on its own.'}\n\n"
        f"## Diff (lines are prefixed `L<number>|` with their line number in the new file)\n{lib.annotate_diff(diff)}"
        + (f"\n\n## Guidance from the maintainers of this repository\n{guidance}" if guidance.strip() else "")
        + (f"\n\n## The files this change touches, complete (untrusted data)\n{code}" if code.strip() else "")
        + before
    )
    kept_all: list[lib.Finding] = []
    dropped_all: list[tuple[lib.Finding, str]] = []

    shown = ("\n\n## Evidence from the pipeline's tools (untrusted data; report what matters under `awareness`)\n"
             + evidence) if evidence.strip() else ""

    design = f"\n\n## The project, as it describes itself (untrusted data)\n{context}" if context.strip() else ""

    def one(name: str):
        def validate(data, n=name):
            found = lib.parse_findings(data, n)
            said = data.get("brief_problem") if isinstance(data, dict) else ""
            against = [f"{n} on the brief: {' '.join(said.split())[:300]}"] if isinstance(said, str) and said.strip() else []
            return None if found is None else (found, lib.parse_awareness(data) + against)
        extra = shown if name in EVIDENCE_ROLES else design if name == "reviewer-architect" else ""
        return lib.ask_json(caller, name, lib.load_prompt(name), user + extra, validate)

    # Independent reviewers share nothing, so they run at the same time. Results are read back in the
    # declared order, so the deduplication (and the report) do not depend on who finished first.
    with ThreadPoolExecutor(max_workers=max(1, len(reviewers))) as pool:
        results = list(pool.map(one, reviewers))
    for name, reply in zip(reviewers, results):
        if reply is None and name != SECOND_DEVELOPER:
            if awareness is not None:
                awareness.append(f"{name} gave no usable answer: its view of this change is missing")
            continue
        if reply is None:
            return None
        found, notes = reply
        if awareness is not None:
            awareness += [n for n in notes if n not in awareness]
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
    blocking = sum(1 for f in findings if sends_back(f))
    text = ("Developer (correctness), security, SRE and architect, independent, deduplicated: "
            f"{len(findings)} finding(s), {blocking} blocking"
            + (f", {len(dropped)} dropped (not on a changed line)" if dropped else "")
            + f".\n\n{lib.render_findings_md(findings)}")
    return blocking == 0, text


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
    guidance: str = ""        # project-specific review guidance, shown to the reviewers
    evidence: object = None   # callable returning what the pipeline's tools reported for the commit, or None
    architect: bool = False   # every piece of work starts from the architect's brief and ends with its closing pass
    task: str = ""            # the work as it came in (kind, title, description), for the architect


class RunState(TypedDict, total=False):
    plan: dict
    iteration: int
    verify_attempts: int
    feedback: str
    applied: dict
    explanation: str
    committed: bool
    rounds: list
    notes: list
    route: str
    outcome: str
    failure_output: str
    verdicts: list
    adjudications: list
    tests_done: int
    applied_kind: str
    own_tests: str           # "tests" or "steward": the change under verification is tests that node has just written
    own_tests_attempts: int
    tests_feedback: str
    spec_tests: list         # test files the writer may not edit: a verdict said the CODE is wrong there
    spec_violations: int
    awareness: list          # what the roles noted about the tools' evidence: reported, never acted on
    fix_from: str            # the commit the developer agent's latest fix starts from
    asked: list              # what the second developer asked it to fix
    cycle: int               # 1, or 2 when the architect started the work again
    close_reason: str        # why the first cycle did not end well, for the architect's second brief
    pair_rounds: int         # how many times the second developer has sent the change back
    decided: bool            # the architect settled a disagreement of the pair: no further review of it
    stage: str               # "qa" while the change under verification is the test steward's; "finish" for what the close sent back
    finishing: bool          # the architect has sent back, once, what its brief asked for and nobody did
    story: list              # what happened, in plain words, by the architect at the end
    journal: Annotated[list, operator.add]   # what each role did and found; a node returns its entry, the channel appends it
    informers: list          # the roles that inform and are called for this change (they run side by side)
    informed: Annotated[list, operator.add]  # what each of them reported; they write here and nowhere else


def is_doc_path(path: str) -> bool:
    name = pathlib.PurePosixPath(path).name
    if name.upper().startswith("CHANGELOG") or path.startswith((".github/", ".shared/", ".ai/", ".claude/")):
        return False
    # Instructions for coding agents are the project's rules, not documentation of the change: an agent
    # that rewrites them to match what it has just done has changed the rules to fit the result.
    if name in ("CLAUDE.md", "AGENTS.md", "GEMINI.md", ".cursorrules"):
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


def commit_subject(explanation: str, limit: int = 72) -> str:
    """The title of an agent's commit: the first sentence of what it did, cut at a word. The whole
    explanation goes in the body: titles cut mid-word at 120 characters could not be read."""
    first = " ".join(explanation.split()).split(". ")[0].rstrip(".")
    if len(first) <= limit:
        return first
    return first[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def diff_since_base(base_sha: str) -> str:
    path = OUT_DIR / "diff.txt"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    build_diff(base_sha, "HEAD", path)
    return path.read_text()


def changed_files(base_sha: str) -> list[str]:
    return [n for n in git("diff", "--name-only", f"{base_sha}...HEAD").stdout.split("\n") if n]


def run_writer(rt: Runtime, plan: dict, feedback: str, before: str = ""):
    """One writer turn: optional read-only exploration, then a validated set
    of changes applied to the working tree. Returns (Applied | None, text).

    `before` is the journal of the run: what the architect and the others already read and found."""
    system = lib.load_prompt("writer")
    brief = plan.get("brief") or {}
    named = sorted({m for i in (brief.get("developer") or {}).get("instructions", [])
                    for m in re.findall(r"[\w./-]+\.\w+", i) if pathlib.Path(m).is_file()})
    base = (
        f"## Plan\n{json.dumps(plan, indent=2)}\n\n## Repository files\n{lib.repo_tree()}\n\n"
        f"## Project context\n{rt.context}\n"
        + (f"\n## The files the brief names, complete (copy the text to replace from here)\n{lib.files_whole(named, 60_000)}\n"
           if named else "")
        + before
        + (f"\n## {feedback}\n" if feedback else "")
    )
    history: list[str] = []
    said_nothing_to_change = False
    # As for the architect: asking for a file needs no thinking, writing the change does. The reading has
    # its own allowance: a writer that read seven files had one round left to write the change.
    for look in range(1, WRITER_READS + 1):
        reply = rt.caller.call("writer-read", system + WRITER_READING,
                               base + ("\n## What you already looked up\n" + "\n\n".join(history) if history else ""))
        request = lib.parse_explore(reply) if reply else None
        if not request:
            break
        history.append(f"Look-up {look}: " + lib.explore(*request))
    rnd = late_reads = 0
    while rnd < rt.writer_rounds:
        rnd += 1
        looked = "\n## What you already looked up\n" + "\n\n".join(history) if history else ""
        reply = rt.caller.call("writer", system, base + looked)
        if reply is None:
            return None, "the model call failed"
        request = lib.parse_explore(reply)
        if request and late_reads < WRITER_LATE_READS:
            # A look-up that comes up while writing is answered and does not cost a round: a writer once spent
            # all eight on reading, for a change of one line. A few of them, then it has to write.
            late_reads += 1
            rnd -= 1
            history.append("Look-up: " + lib.explore(*request))
            continue
        if request:
            history.append(f"Round {rnd}: no more look-ups. With what you have read, send the changes now: the smallest "
                           "change that makes the failing check pass.")
            continue
        data = _parse_json_reply(reply)
        parsed = lib.parse_changes(data) if data is not None else None
        if parsed is None:
            history.append(
                f"Round {rnd}: your reply could not be used. Reply with exactly ONE json object and nothing else: "
                "either one lookup or the changes. No second request, no commentary, no tool-call markup. "
                f"(Changes: max {lib.MAX_CHANGES}, nothing under .github/workflows/, find text must appear exactly once.)"
            )
            continue
        changes, explanation = parsed
        if not changes and not said_nothing_to_change:
            # A reply with no changes is the end only when it is meant: a writer that wanted to read a file
            # first and said so in `explanation`, with no changes, once ended a migration it was half through.
            said_nothing_to_change = True
            history.append(f"Round {rnd}: your reply had no changes. To read a file, send that request alone. If you "
                           "are ready, send the changes. Only if there is truly nothing to change, reply again with "
                           "no changes and say why.")
            continue
        if not changes:
            return None, explanation or "the writer found nothing it could change safely"
        problems = lib.validate_patch(changes, plan)
        if problems:
            history.append(f"Round {rnd}: your patch was refused before it touched anything: " + "; ".join(problems)
                           + ". Propose a patch that respects the plan and the policy.")
            continue
        applied = lib.apply_changes(changes)
        if applied.error:
            history.append(f"Round {rnd}: applying your changes failed: {applied.error}")
            continue
        weakened = lib.weakened_tests("HEAD")
        if weakened:
            # The writer may edit a test that is truly outdated, but never weaken one to get a green run.
            lib.revert(applied)
            history.append(f"Round {rnd}: your patch was refused, it weakens the tests: " + "; ".join(weakened)
                           + ". Make the code satisfy the tests, or correct what a test asserts without removing tests or assertions.")
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


def quote_stands(quote: str, intent: str) -> bool:
    """Is `quote` really taken from the stated intent? Case and whitespace do not count. A model often joins
    two passages with an ellipsis ("now rejects above 25 ... 30 is no longer allowed"); that is still a
    quotation as long as EVERY piece is in the intent word for word, so each piece is checked on its own.
    A paraphrase, however reasonable, is not a quote."""
    whole = _squash(intent)
    pieces = [p.strip(" \"'`") for p in re.split(r"\s*(?:\.{3,}|\u2026|\[\.\.\.\])\s*", _squash(quote))]
    pieces = [p for p in pieces if p]
    if not pieces or max(len(p) for p in pieces) < MIN_QUOTE or any(len(p) < 4 for p in pieces):
        return False
    return all(p in whole for p in pieces)


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
            if not quote_stands(v.get("intent_evidence", ""), intent):
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


def build_graph(rt: Runtime, checkpointer=None):
    def stop(state: RunState, outcome: str, note: str) -> dict:
        if state.get("stage") == "finish":
            # The change already works and was through every role: this was the one try at what the brief
            # asked for and nobody had done. It did not succeed, was taken back, and is reported as missing.
            return {"route": "close", "applied": None, "notes": state.get("notes", []) + [
                "the developer agent was asked once for what the brief still lacked and could not do it: " + note[:400]]}
        if (rt.architect and state.get("cycle", 1) == 1 and outcome in ("escalated", "failed")
                and not getattr(rt.caller, "out_of_time", False)):
            # The work did not end well. The architect looks at it again, once, with the reason in hand,
            # and everything is done again from its new brief. A second cycle that fails is the end.
            return {"route": "architect", "cycle": 2, "feedback": "", "applied": None,
                    "close_reason": note + "\nWhatever the developer changed and did not pass the checks was taken back: "
                                           "the code is as it was submitted, plus what the journal shows as committed.",
                    "verify_attempts": 0, "pair_rounds": 0, "decided": False, "spec_violations": 0, "own_tests_attempts": 0,
                    "fix_from": "", "failure_output": "", "stage": "", "tests_done": 0,
                    "notes": state.get("notes", []) + [note, "the first cycle did not end well: the architect starts a second one"]}
        return {"route": "end", "outcome": outcome, "notes": state.get("notes", []) + [note]}

    def n_architect(state: RunState) -> dict:
        """The first pass: the facts, then the brief every other role works from."""
        logs = state.get("failure_output", "") if rt.start == "ci" and state.get("cycle", 1) == 1 else ""
        if logs:
            ok, output, facts = False, logs, "The checks of this change FAILED in CI. Their logs:\n" + logs[-20000:]
        elif rt.start == "review":
            ok, output, facts = True, "", "No check was run in this job: this is a push to the base branch, whose own pipeline builds and tests it."
        else:
            ok, output = lib.run_verify_isolated(rt.verify_command, rt.verify_timeout)
            log(f"deterministic checks (before the brief): {'passed' if ok else 'FAILED'}")
            facts = "The checks pass (dependencies, lint, tests, the application starts)." if ok else "The checks FAILED. Their output:\n" + output
        seen = entry("checks", "passed" if ok else "FAILED: " + output[-600:], fact=True) if rt.start != "review" else []
        untested = lib.untested_new_functions(diff_since_base(rt.base_sha))
        if untested:
            facts += ("\n\nNew in this change and named by no test file (a fact, from the diff and the tests): "
                      + "; ".join(untested) + ".")
        brief = run_architect(rt, state["plan"], facts, state.get("close_reason", ""), so_far(state))
        if brief is None:
            return {**stop(state, "escalated", "the architect could not produce a usable brief, so nobody knew what to do"), "journal": seen}
        plan = {**state["plan"], "brief": brief}
        notes = state.get("notes", []) + [f"architect: {brief['summary']}"]
        wrote = seen + entry("architect", "wrote the brief: " + brief["summary"], brief.get("facts", []))
        here = {"plan": plan, "stage": "", "pair_rounds": 0, "decided": False, "journal": wrote}
        if not ok:
            # Who is wrong, the code or the test, is still decided on evidence; the writer then has both
            # that verdict and the architect's instructions, which are in the plan it is shown.
            after = handle_failure({**state, "plan": plan, "notes": notes, "journal": (state.get("journal") or []) + wrote}, output, None)
            return {**here, **after, "journal": wrote + after.get("journal", [])}
        if brief["developer"]["change_needed"]:
            return {**here, "notes": notes, "route": "write",
                    "feedback": ARCHITECT_FEEDBACK + ". Make exactly these changes, and only these:\n"
                                + "\n".join(f"- {i}" for i in brief["developer"]["instructions"])
                                + ("\n\nRead first:\n" + "\n".join(f"- {i}" for i in brief["developer"]["look_up"])
                                   if brief["developer"]["look_up"] else "")}
        return {**here, "notes": notes, "route": "pair"}

    def n_write(state: RunState) -> dict:
        applied, text = run_writer(rt, state["plan"], state.get("feedback", ""), so_far(state))
        asked_by = state.get("feedback", "")
        did = entry("developer", (f"changed {', '.join(applied.files)}: {text}" if applied else f"made no change: {text}"))
        if applied is None and asked_by.startswith(REVIEW_FEEDBACK):
            # Asked by the second developer, right after checks that passed, and nothing was changed since:
            # the change works as it is. It goes on with the findings written in the review.
            return {"route": "tests", "journal": did, "notes": state.get("notes", []) + [
                f"writer: {text}", "the developer agent produced no fix for the review's findings; the checks pass, "
                                   "so the findings are reported and the change goes on"]}
        if applied is None and state.get("stage") == "finish":
            return {"route": "close", "journal": did, "notes": state.get("notes", []) + [
                f"the developer agent made no change for what the brief still lacked: {text}"]}
        if applied is None and asked_by.startswith(ARCHITECT_FEEDBACK):
            # Asked by the architect while the checks pass: the change works as it is. Either the developer
            # found it already as the brief asks, or it could not do it; the second developer looks next,
            # unless the architect was settling their disagreement, which is then over.
            return {"route": "tests" if state.get("decided") else "pair", "journal": did, "notes": state.get("notes", []) + [
                f"the developer agent made no change for the architect's instructions: {text}"]}
        if applied is None:
            reportable = state.get("committed") or state.get("rounds")
            return {**stop(state, "escalated" if reportable else "failed", f"writer: {text}"), "journal": did}
        rewritten = sorted(set(applied.files) & set(state.get("spec_tests") or []))
        if rewritten:
            # The verdict was that the code is wrong and these tests are right. Changing what they assert makes
            # the failure go away without fixing anything, and the weakening guard does not see it (same number
            # of tests and assertions, a different expected value). Tests are the specification for the writer too.
            lib.revert(applied)
            again = state.get("spec_violations", 0) + 1
            if again >= 2:
                return stop(state, "escalated", "the writer changed the failing tests instead of the code, twice ("
                            + ", ".join(rewritten) + "); those tests are the specification, so the change is not certified")
            return {"route": "write", "applied": None, "spec_violations": again,
                    "notes": state.get("notes", []) + ["the writer edited a test the verdict had found right; the change was refused: " + ", ".join(rewritten)],
                    "feedback": state.get("feedback", "") + "\n\nREFUSED: your change edited " + ", ".join(rewritten)
                                + ". The verdict is that the CODE is wrong and those tests are right: they are the specification. "
                                "Change the application code so they pass as they are. New tests go in a new file."}
        return {"route": "verify", "applied": {"edited": applied.edited, "created": applied.created},
                "explanation": text, "applied_kind": None, "own_tests": "", "journal": did}

    def adjudicate(state: RunState, output: str):
        """(verdicts, evidence) for the failing tests in `output`, or None when
        the failure is not a test failure (lint, collection) and needs no verdict."""
        tests = lib.parse_failed_tests(output)
        if not tests:
            return None
        evidence = lib.gather_evidence(tests, rt.base_sha)
        if rt.verify_command and all(lib.evidence_hint(e) == "unreproducible" for e in evidence.values()):
            # Entered from a failed CI run, nothing has prepared this job yet: the project's dependencies are
            # installed by the verify command. Run it once (its own result does not matter here) and look again,
            # so that a test this job CAN run is judged on evidence instead of on the log alone.
            lib.run_verify_isolated(rt.verify_command, rt.verify_timeout)
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
        judged: list[dict] = []
        history = list(state.get("adjudications", []))
        verdict_text = ""
        spec_tests: list[str] = []
        result = adjudicate(state, output)
        if result:
            verdicts, _ = result
            classes = {v["classification"] for v in verdicts}
            history.append({"iteration": state["iteration"], "verdicts": verdicts})
            notes.append("failure adjudication: " + "; ".join(f"{v['test']} -> {v['classification']}" for v in verdicts))
            judged = entry("QA", "judged what failed: was the code wrong, or the test?",
                           [f"{v['test']}: {WHO_WAS_WRONG.get(v['classification'], v['classification'])}. {v.get('reason', '')}" for v in verdicts])
            over = attempts > rt.max_verify_retries
            if classes == {"test_defect"} and rt.write_tests and not over:
                return {"route": "steward", "verify_attempts": attempts, "failure_output": output,
                        "verdicts": verdicts, "notes": notes, "adjudications": history, "journal": judged}
            if classes == {"environment"} and not over:
                return {"route": "verify", "verify_attempts": attempts, "notes": notes, "adjudications": history, "journal": judged}
            spec_tests = sorted({v["test"].split("::")[0] for v in verdicts if v["classification"] == "code_defect"})
            verdict_text = ("\n\nADJUDICATION. Tests are the specification, so the code must satisfy them. "
                            "Verdicts:\n" + json.dumps([v for v in verdicts if v["classification"] != "environment"], indent=2))
        elif re.search(r"\b\d+ failed\b", output):
            # Tests failed but none could be named, so nobody judged whether the code or the test is wrong.
            # Said out loud: the health check looks for this sentence.
            notes.append("tests failed but the failing tests could not be identified, so no verdict was given")
        if applied:
            # The writer is shown what it had changed: without it, a change that was right in three files and
            # wrong in a fourth had to be found again from nothing, and the next attempt lost what was right.
            attempted = git("diff", "HEAD").stdout[:12000]
            lib.revert(applied)
            what = "your previous change (reverted)"
            output += ("\n\nYOUR PREVIOUS CHANGE, which was reverted. Send it again with what was missing or wrong "
                       "corrected; keep the parts that were right:\n" + attempted) if attempted.strip() else ""
        else:
            what = "the change as submitted"
        if attempts > rt.max_verify_retries:
            return {**stop(state, "escalated" if state.get("committed") else "failed",
                           f"deterministic checks still failing after {attempts} attempts:\n{output[-1500:]}"),
                    "notes": notes + [f"deterministic checks still failing after {attempts} attempts:\n{output[-1500:]}"],
                    "adjudications": history, "journal": judged}
        return {"route": "write", "verify_attempts": attempts, "applied": None, "notes": notes, "adjudications": history, "journal": judged,
                "spec_tests": spec_tests,
                "feedback": f"FAILED VERIFICATION of {what}. Fix this:\n{output}{verdict_text}"}

    def n_verify(state: RunState) -> dict:
        ok, output = lib.run_verify_isolated(rt.verify_command, rt.verify_timeout)
        log(f"deterministic checks (iteration {state['iteration']}): {'passed' if ok else 'FAILED'}")
        raw = state.get("applied")
        applied = lib.Applied(raw["edited"], raw["created"]) if raw else None
        seen = entry("checks", "passed" if ok else "FAILED: " + output[-600:], fact=True)
        if ok:
            if applied:
                kind = state.get("applied_kind") or (rt.fix_kind if state["iteration"] == 1 else "fix")
                commit_round(f"{kind}(agent): {commit_subject(state['explanation'])}\n\n{state['explanation']}\n\n"
                             f"Iteration {state['iteration']} of the agent pipeline.")
            # The test steward's tests go on to the roles that inform; a change the architect settled is not
            # reviewed again; anything else is the developer's, and the second developer reads it.
            # What the architect sent back at the end, done and checked, returns to it.
            after = ("close" if state.get("stage") == "finish" else "inform" if state.get("stage") == "qa"
                     else "tests" if state.get("decided") else "pair")
            return {"route": after, "journal": seen,
                    "committed": bool(applied) or state.get("committed", False),
                    "verify_attempts": 0, "applied": None, "applied_kind": None, "own_tests": "", "spec_tests": []}
        if state.get("own_tests") and applied:
            # The steward has just written or rewritten these tests and they fail: they assert something the
            # code does not do. A test written a minute ago by a model is not the specification, and the
            # application is never changed to satisfy it (it once removed an app-wide error handler that way).
            # The steward is told what failed and tries again. When it cannot, its tests are gone and the change
            # is as it was when the checks passed: it goes on, and what was not tested is said (rule 4: a new
            # test that does not run says nothing about the application). Once this ended a migration that worked.
            lib.revert(applied)
            tries = state.get("own_tests_attempts", 0) + 1
            if tries >= 2 and state["own_tests"] == "tests":
                return {"route": "inform", "stage": "qa", "applied": None, "applied_kind": None, "own_tests": "",
                        "journal": seen + entry("QA", "could not write a test that runs, twice; its tests were taken out and the change goes on"),
                        "notes": state.get("notes", []) + [UNTESTED + "the tests it wrote did not pass against the change, twice: "
                                                           + " ".join(output[-300:].split())]}
            if tries >= 2:
                return {**stop(state, "escalated", "the tests the steward rewrote do not pass against the change, twice, "
                                                    "so it is not certified"), "journal": seen}
            return {"route": state["own_tests"], "journal": seen, "applied": None, "applied_kind": None, "own_tests": "", "own_tests_attempts": tries,
                    "tests_feedback": output[-3000:],
                    "notes": state.get("notes", []) + ["the steward's new tests failed against the change and were discarded; it was asked again"]}
        after = handle_failure(state, output, applied)
        return {**after, "journal": seen + after.get("journal", [])}

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
        if state.get("tests_feedback"):
            user += ("\n\n## The tests you wrote last time FAILED against this change and were discarded\n"
                     "The application is right and is not going to change. Assert only what this output shows the code "
                     f"really does:\n{state['tests_feedback']}")
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
        return {"route": "verify", "applied": merged(state, applied), "applied_kind": "test", "own_tests": "steward",
                "explanation": f"{base_explanation}; tests updated: {explanation}".strip("; "),
                "notes": state.get("notes", []) + [f"tests updated to the stated intent: {', '.join(applied.files)}"]}

    def usable_changes(role: str, user: str, allowed, check=None):
        """Ask `role` for changes until they can actually be used, at most twice. Returns
        (applied, explanation, None) for an applied change, (None, explanation, None) when the agent says
        none is needed, and (None, "", why) when it could not produce a usable one. The second attempt is
        told exactly what was wrong with the first: an agent that fails is not the same as an agent that
        found nothing to do, and must not be reported as one."""
        why = ""
        for attempt in (1, 2):
            prompt = user if attempt == 1 else (
                f"{user}\n\n## Your previous reply could not be used\n{why}\n"
                "Reply again with changes that avoid exactly that problem. Copy every `find` anchor character for "
                "character from the text you were given; if you cannot, create a new file with `content` instead.")
            parsed = lib.ask_json(rt.caller, role, lib.load_prompt(role), prompt, lambda d: lib.parse_changes(d, allowed=allowed))
            if parsed is None:
                why = "It was not one valid JSON object matching the schema, or it named a file this role may not change."
                continue
            changes, explanation = parsed
            if not changes:
                return None, explanation, None
            applied = lib.apply_changes(changes)
            if applied.error:
                why = f"The changes could not be applied: {applied.error}"
                continue
            problem = check(applied) if check else None
            if problem:
                lib.revert(applied)
                why = problem
                continue
            return applied, explanation, None
        return None, "", why

    def n_tests(state: RunState) -> dict:
        """Proactive: does the change to application code have the tests it needs?"""
        base = {"tests_done": state["iteration"], "route": "inform", "stage": "qa"}
        asked = (state["plan"].get("brief") or {}).get("tests") or {}
        if not rt.write_tests or (not asked.get("needed") and not any(lib.is_source_path(f) for f in changed_files(rt.base_sha))):
            return base
        user = (f"## Mode\nPROACTIVE: decide whether the tests cover this change.\n\n## Stated intent\n{intent_text(state['plan'])}"
                + ("\n\n## What the architect says the tests must check\n" + "\n".join(f"- {w}" for w in asked["what"]) if asked.get("what") else "")
                + f"\n\n## Existing tests\n{lib.test_inventory()}\n\n## How tests are written in this repository\n{lib.test_conventions()}\n\n## Diff\n{diff_since_base(rt.base_sha)[:40000]}")
        untested = lib.untested_new_functions(diff_since_base(rt.base_sha))
        if untested:
            user += ("\n\n## New in this change and named by no test file (a fact, from the diff and the tests)\n"
                     + "\n".join(f"- {u}" for u in untested)
                     + "\nThe existing tests cannot cover what they do not name: these need a test.")
        user += so_far(state)
        if state.get("tests_feedback"):
            user += ("\n\n## The tests you wrote last time FAILED against this change and were discarded\n"
                     "The application is right and is not going to change. Assert only what this output shows the code "
                     f"really does:\n{state['tests_feedback']}")
        def not_weaker(_applied):
            violations = lib.weakened_tests("HEAD")
            return ("The changes weaken the tests, which is never allowed: " + "; ".join(violations)) if violations else None

        notes = list(state.get("notes", []))
        applied, explanation, failure = usable_changes("test-steward", user, lib.is_test_path, not_weaker)
        if failure:
            # The checks pass and nothing of the steward's was kept: the change goes on, and it is said that
            # nobody established whether it has the tests it needs.
            return {**base, "notes": notes + [UNTESTED + f"it gave no usable answer ({failure})"],
                    "journal": entry("QA", f"gave no usable answer about the tests of this change ({failure}); the change goes on")}
        if applied is None:
            return {**base, "notes": notes + [f"tests: {explanation or 'the existing tests already cover this change'}"],
                    "journal": entry("QA", "added no test: " + (explanation or "the existing tests already cover this change"))}
        found = lib.discriminating(rt.base_sha, [f for f in applied.files if f.endswith(".py")])
        if found is not None and found[0] == 0:
            notes.append("the new tests also pass without the change, so they may not test it")
        return {"tests_done": state["iteration"], "route": "verify", "stage": "qa", "applied": {"edited": applied.edited, "created": applied.created},
                "applied_kind": "test", "own_tests": "tests", "explanation": f"tests: {explanation}", "notes": notes + [f"tests added/updated: {', '.join(applied.files)}"],
                "journal": entry("QA", f"added or updated tests in {', '.join(applied.files)}: {explanation}")}

    evidence_read: list[str] = []

    def evidence() -> str:
        # Read once, when the roles that inform look: by then the checks that produce it have had the
        # time the developer, the pair and the test steward took.
        if not evidence_read:
            evidence_read.append(rt.evidence() if callable(rt.evidence) else "")
        return evidence_read[0]

    def record(state: RunState, findings: list[lib.Finding], dropped, stage: str) -> tuple[list[lib.Finding], list, list[str]]:
        in_scope = [f for f in findings if lib.finding_in_scope(f, state["plan"])]
        left_out = len(findings) - len(in_scope)
        rounds = state.get("rounds", []) + [{"iteration": state["iteration"], "stage": stage,
                                             "findings": [f.to_dict() for f in in_scope], "dropped": len(dropped)}]
        note = [f"{left_out} finding(s) concern files the plan declared out of scope and were not acted on"] if left_out else []
        return in_scope, rounds, note

    def n_pair(state: RunState) -> dict:
        """The second developer reads the first one's work: the whole change the first time, then each
        correction alone. It accepts, or sends the change back with what is wrong; the third time it
        sends it back the two do not agree, and the architect decides."""
        said: list[str] = list(state.get("awareness", []))
        guidance = rt.guidance
        if state.get("fix_from"):
            path = OUT_DIR / "fix.diff"
            build_diff(state["fix_from"], "HEAD", path)
            diff, stage = path.read_text(), "fix"
            guidance = ((guidance + "\n\n" if guidance.strip() else "")
                        + "This diff is the correction made for the findings below, and nothing else of the change. "
                        "Judge whether it resolves them and whether it breaks anything.\n" + json.dumps(state.get("asked", []), indent=2))
        else:
            diff, stage = diff_since_base(rt.base_sha), "pair"
        if not diff.strip():
            return {"route": "tests"}
        touched = sorted(set(re.findall(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE)))
        result = review_diff(rt.caller, diff, state["plan"], (SECOND_DEVELOPER,), guidance, awareness=said,
                             code=lib.files_whole(touched, CODE_CHARS // 2), before=so_far(state))
        if result is None:
            return stop(state, "escalated", "the second developer did not return a usable reply, so the change could not be certified")
        findings, rounds, note = record(state, *result, stage)
        notes = state.get("notes", []) + note
        first_reading = stage == "pair" and not state.get("pair_rounds")
        back = [f for f in findings if sends_back(f) or (first_reading and worth_one_fix(f))]
        seen = [f"{f.file}:{f.line} ({f.severity}) {f.problem}" for f in findings]
        if not back:
            return {"route": "tests", "rounds": rounds, "notes": notes, "awareness": said,
                    "journal": entry("second developer", "read the change and accepted it" + (", with notes" if seen else ""), seen)}
        sent = state.get("pair_rounds", 0) + 1
        asked = [f.to_dict() for f in back]
        wrote = entry("second developer", f"sent the change back to the developer (time {sent} of {MAX_PAIR_ROUNDS})", seen)
        if sent >= MAX_PAIR_ROUNDS:
            return {"route": "decide", "rounds": rounds, "notes": notes, "awareness": said, "pair_rounds": sent, "asked": asked,
                    "journal": wrote}
        return {
            "route": "write", "rounds": rounds, "notes": notes, "awareness": said, "pair_rounds": sent, "journal": wrote,
            "iteration": state["iteration"] + 1, "fix_from": git("rev-parse", "HEAD").stdout.strip(), "asked": asked,
            "feedback": REVIEW_FEEDBACK + " to fix (found by the second developer; fix exactly these, minimally):\n"
                        + lib.findings_for_writer(back),
        }

    def n_decide(state: RunState) -> dict:
        """The two developers did not agree in three rounds. The architect settles it, once: the change
        goes on as it is, with the objections reported, or one last change is made and not reviewed."""
        def validate(data: dict):
            wanted = data.get("instructions", [])
            if data.get("decision") not in ("accept", "instruct") or not isinstance(wanted, list):
                return None
            wanted = [x.strip() for x in wanted if isinstance(x, str) and x.strip()]
            return None if data["decision"] == "instruct" and not wanted else (data["decision"], str(data.get("reason", ""))[:400], wanted)

        user = (f"## Your brief\n{json.dumps(state['plan'].get('brief') or state['plan'], indent=2)}\n\n"
                f"## The change as it is now\n{lib.annotate_diff(diff_since_base(rt.base_sha))}\n\n"
                f"## What the second developer still objects to, after {state.get('pair_rounds', 0)} rounds\n"
                f"{json.dumps(state.get('asked', []), indent=2)}" + so_far(state))
        verdict = lib.ask_json(rt.caller, "architect-decide", lib.load_prompt("architect-decide"), user, validate)
        notes = list(state.get("notes", []))
        if verdict is None or verdict[0] == "accept":
            # No answer is an acceptance: the checks pass on the change as it is, and a reviewer's
            # objection does not end a change that works. The objections stay in the report.
            notes.append("the two developers did not agree in three rounds; " + (
                f"the architect accepted the change as it is: {verdict[1]}" if verdict else
                "the architect gave no decision, and the checks pass, so the change goes on with the objections reported"))
            return {"route": "tests", "notes": notes, "decided": True,
                    "journal": entry("architect", "settled the two developers' disagreement: the change goes on as it is. "
                                     + (verdict[1] if verdict else "(no decision was given; the checks pass)"))}
        notes.append(f"the two developers did not agree in three rounds; the architect asked for one last change: {verdict[1]}")
        return {"route": "write", "notes": notes, "decided": True, "iteration": state["iteration"] + 1,
                "journal": entry("architect", "settled the two developers' disagreement: one last change. " + verdict[1], verdict[2]),
                "feedback": ARCHITECT_FEEDBACK + " (the architect's decision between the two developers). Make exactly "
                            "these changes:\n" + "\n".join(f"- {i}" for i in verdict[2])}

    def n_inform(state: RunState) -> dict:
        """Which of the roles that inform are called for this change: the facts (what it touches) oblige,
        the architect may add. They then run side by side, each in its own node."""
        brief = state["plan"].get("brief") or {}
        wanted = lib.roles_required(changed_files(rt.base_sha))
        if (brief.get("infrastructure") or {}).get("impact"):
            wanted.add("reviewer-sre")
        if (brief.get("security") or {}).get("impact"):
            wanted.add("reviewer-security")
        roles = [r for r in INFORMERS if r in wanted] if rt.architect else list(INFORMERS)
        if not roles or not diff_since_base(rt.base_sha).strip():
            return {"informers": [], "notes": state.get("notes", []) + (
                ["SRE and security: nothing in this change touches what they look at"] if rt.architect else []),
                    "journal": entry("SRE and security", "were not called: nothing in this change touches what they look at")}
        evidence()      # read once, here, before the two start: they would otherwise both fetch it
        return {"informers": roles}

    def informer(role: str, who: str):
        def node(state: RunState) -> dict:
            said: list[str] = []
            result = review_diff(rt.caller, diff_since_base(rt.base_sha), state["plan"], (role,), rt.guidance,
                                 evidence=evidence(), awareness=said, before=so_far(state))
            findings, dropped = result if result else ([], [])
            told = said + [f"{f.file}:{f.line} ({f.severity}) {f.problem}" for f in findings]
            return {"informed": [{"cycle": state.get("cycle", 1), "role": role, "said": said, "dropped": len(dropped),
                                  "findings": [f.to_dict() for f in findings]}],
                    "journal": entry(who, "looked at the change and at what the tools reported; it informs, it does not send "
                                     "the change back" + ("" if told else ": nothing to report"), told)}
        return node

    def n_informed(state: RunState) -> dict:
        """What the roles that inform said, gathered into the record of the run."""
        mine = [r for r in state.get("informed", []) if r["cycle"] == state.get("cycle", 1)]
        if not mine:
            return {}
        findings = lib.dedup_findings([lib.Finding(**f) for r in mine for f in r["findings"]])
        kept, rounds, note = record(state, findings, [None] * sum(r["dropped"] for r in mine), "inform")
        known = list(state.get("awareness", []))
        return {"rounds": rounds, "notes": state.get("notes", []) + note,
                "awareness": known + [x for r in mine for x in r["said"] if x not in known]}

    def n_docs(state: RunState) -> dict:
        notes = list(state.get("notes", []))
        diff = diff_since_base(rt.base_sha)
        doc_paths = [p for p in lib.repo_tree(2000).split("\n") if p and is_doc_path(p) and pathlib.Path(p).is_file()]
        # README first: when nothing mentions the change it is the document handed over.
        doc_paths.sort(key=lambda p: (not p.lower().startswith("readme"), p))
        user = (f"## Plan\n{json.dumps(state['plan'], indent=2)}\n\n## Diff\n{diff}\n\n"
                f"## Documentation files\n{lib.relevant_docs(diff, doc_paths)}" + so_far(state))

        def validate(data: dict):
            return lib.parse_changes(data, allowed=is_doc_path)

        applied, explanation, failure = usable_changes("doc-reviewer", user, is_doc_path)
        if failure:
            return stop(state, "escalated", f"the documentation reviewer could not produce a usable answer ({failure}), "
                                             "so the documentation was not checked and the change is not certified")
        if applied is None:
            notes.append(f"docs: {explanation or 'no documentation change needed'}")
        else:
            ok, output = lib.run_verify_isolated(rt.verify_command, rt.verify_timeout)
            if ok:
                commit_round(f"docs(agent): {explanation[:120]}")
                notes.append(f"docs updated: {', '.join(applied.files)}")
            else:
                lib.revert(applied)
                notes.append(f"documentation edits broke the checks and were reverted:\n{output[-800:]}")
        # Changelog: deterministic, so it cannot silently go missing.
        if rt.changelog_path and rt.changelog_path not in changed_files(rt.base_sha):
            path = pathlib.Path(rt.changelog_path)
            ref = f" (#{rt.issue_number})" if rt.issue_number else ""
            summary = state["plan"]["summary"]
            category = "Added" if summary.lower().startswith(("add", "introduce", "implement", "create")) else "Changed"
            path.write_text(add_entry(path.read_text() if path.is_file() else "", category, summary.rstrip(".") + ref))
            commit_round("docs(changelog): record the agent change")
            notes.append(f"changelog entry added to {rt.changelog_path}")
        return {"route": "close", "notes": notes, "journal": entry("documentation", notes[len(state.get("notes", [])):][0]
                                                                   if len(notes) > len(state.get("notes", [])) else "nothing to do")}

    def n_close(state: RunState) -> dict:
        """The architect's last pass. Whether the change works is not its opinion: every commit that
        reached this point went through the checks. It says, for a person who was not there, what
        happened, and what its brief asked for that nobody did; that is reported, and undoes nothing."""
        notes = list(state.get("notes", []))
        brief = state["plan"].get("brief")
        if not rt.architect or not brief:
            return {"route": "end", "outcome": "converged", "notes": notes}

        def validate(data: dict):
            story, missing = data.get("story"), data.get("missing", [])
            if not isinstance(story, list) or not isinstance(missing, list):
                return None
            clean = lambda items: [" ".join(x.split())[:400] for x in items if isinstance(x, str) and x.strip()]  # noqa: E731
            return (clean(story)[:8], clean(missing)[:6]) if clean(story) else None

        record_of_work = {
            "rounds": [{"stage": r["stage"], "findings": [{k: f.get(k) for k in ("severity", "file", "problem", "reviewers")}
                                                          for f in r["findings"]]} for r in state.get("rounds", [])],
            "who_was_wrong_when_a_check_failed": [v for a in state.get("adjudications", []) for v in a["verdicts"]],
            "what_the_tools_reported": state.get("awareness", []),
            "notes": notes,
            "files_changed_by_the_agents": [n for n in git("diff", "--name-only", rt.base_sha, "HEAD").stdout.split("\n") if n],
        }
        user = (f"## The task\n{rt.task or intent_text(state['plan'])}\n\n## Your brief\n{json.dumps(brief, indent=2)}\n\n"
                f"## The record of the work\n{json.dumps(record_of_work, indent=2)[:30000]}" + so_far(state))
        told = lib.ask_json(rt.caller, "architect-close", lib.load_prompt("architect-close"), user, validate)
        if told is None:
            return {"route": "end", "outcome": "converged", "notes": notes}
        story, missing = told
        if missing and not state.get("finishing"):
            # Work the brief asked for and nobody did goes back to the developer, once: it was asked for, it
            # is not an opinion. What is still missing after that is reported, and the change goes on.
            return {"route": "write", "stage": "finish", "finishing": True, "verify_attempts": 0, "applied": None,
                    "iteration": state["iteration"] + 1,
                    "journal": entry("architect", "read the finished work and sent back what its brief asked for and nobody did", missing),
                    "notes": notes + ["the architect sent back to the developer agent what the brief asked for and was not done"],
                    "feedback": ARCHITECT_FEEDBACK + ". The change works and stays as it is. Your brief also asked for the "
                                "following, which was not done. Do exactly these, and nothing else:\n" + "\n".join(f"- {m}" for m in missing)}
        return {"route": "end", "outcome": "converged", "story": story,
                "journal": entry("architect", "closed the work. " + " ".join(story), [f"asked for and not done: {m}" for m in missing]),
                "notes": notes + [f"the brief asked for this and the record does not show it done: {m}" for m in missing]}

    def n_start(state: RunState) -> dict:
        if rt.architect:
            return {"route": "architect"}
        return {"route": "pair" if rt.start == "review" else rt.start}

    graph = StateGraph(RunState)
    # A node whose model call fell is run again by the graph, once (`ModelUnavailable`); a second failure
    # ends the run, and whoever started it says why.
    again_if_the_model_fell = RetryPolicy(max_attempts=2, initial_interval=2.0, retry_on=lib.ModelUnavailable)
    for name, fn in (("start", n_start), ("architect", n_architect), ("write", n_write), ("verify", n_verify),
                     ("pair", n_pair), ("decide", n_decide), ("ci_failure", n_ci_failure), ("steward", n_steward),
                     ("tests", n_tests), ("inform", n_inform), ("sre", informer("reviewer-sre", "SRE")),
                     ("security", informer("reviewer-security", "security")), ("informed", n_informed),
                     ("docs", n_docs), ("close", n_close)):
        graph.add_node(name, fn, retry_policy=again_if_the_model_fell)
    graph.set_entry_point("start")
    route = lambda s: s["route"]  # noqa: E731
    # Any node that can find the work at a dead end can hand it back to the architect, once (see `stop`).
    again = {"architect": "architect", "end": END}
    graph.add_conditional_edges("start", route, {"architect": "architect", "write": "write", "verify": "verify", "pair": "pair", "ci": "ci_failure"})
    graph.add_conditional_edges("architect", route, {"write": "write", "pair": "pair", "steward": "steward", "verify": "verify", **again})
    graph.add_conditional_edges("ci_failure", route, {"write": "write", "steward": "steward", "verify": "verify", **again})
    graph.add_conditional_edges("steward", route, {"verify": "verify", "write": "write", **again})
    graph.add_conditional_edges("write", route, {"verify": "verify", "write": "write", "pair": "pair", "tests": "tests", "close": "close", **again})
    graph.add_conditional_edges("verify", route, {"pair": "pair", "tests": "tests", "inform": "inform", "write": "write",
                                                  "steward": "steward", "verify": "verify", "close": "close", **again})
    graph.add_conditional_edges("pair", route, {"tests": "tests", "write": "write", "decide": "decide", **again})
    graph.add_conditional_edges("decide", route, {"tests": "tests", "write": "write", **again})
    graph.add_conditional_edges("tests", route, {"verify": "verify", "inform": "inform", **again})
    # The SRE and the security role start together, each in its own node, and `informed` gathers them.
    side_by_side = {"reviewer-sre": "sre", "reviewer-security": "security"}
    graph.add_conditional_edges("inform", lambda s: [side_by_side[r] for r in s.get("informers") or []] or "informed",
                                ["sre", "security", "informed"])
    graph.add_edge("sre", "informed")
    graph.add_edge("security", "informed")
    graph.add_edge("informed", "docs")
    graph.add_conditional_edges("docs", route, {"close": "close", **again})
    graph.add_conditional_edges("close", route, {"write": "write", "end": END})
    return graph.compile(checkpointer=checkpointer)


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
                     + ("\n" + render_journal(result["journal"], limit=60000).replace("\n## ", "\n### ") if result.get("journal")
                        else "".join(f"- {n}\n" for n in result.get("notes", [])[:10])))


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
                "adjudications": final.get("adjudications", []),
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
    violations = lib.policy_violations(base_sha)
    if violations:
        outcome, final["notes"] = "abandoned", final.get("notes", []) + ["refusing to publish, policy: " + "; ".join(violations)]
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
    # The pull request is opened with the job's own token: the push token is only required to push (a
    # fine-grained token with Contents alone cannot open one, and the fix then sat on a branch nobody saw).
    # A pull request opened that way starts no workflow, so one more commit, pushed with the push token,
    # starts the checks; the certification is for that commit, the head.
    job_env = {**os.environ, "GH_TOKEN": os.environ.get("JOB_TOKEN") or os.environ.get("GH_TOKEN", "")}
    created = subprocess.run(["gh", "pr", "create", "--repo", args.repo, "--base", args.base_branch, "--head", result["branch"],
                              "--title", result["title"], "--body-file", str(OUT_DIR / "pr-body.md")],
                             capture_output=True, text=True, env=job_env)
    if created.returncode != 0:
        print(f"::error::gh pr create failed: {created.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    url = created.stdout.strip()
    git("config", "user.name", AGENT_AUTHOR)
    git("config", "user.email", "actions@github.com")
    git("commit", "--quiet", "--allow-empty", "-m", "chore(agent): start the checks on this pull request")
    started = subprocess.run(
        ["git", "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {header}",
         "push", "--quiet", "origin", f"HEAD:refs/heads/{result['branch']}"], capture_output=True, text=True)
    if started.returncode != 0:
        print(f"::error::could not push the commit that starts the checks: {started.stderr.replace(header, '***')[:300]}", file=sys.stderr)
        return 1
    head = git("rev-parse", "HEAD").stdout.strip()
    # Certification is bound to this exact commit: the merge gate accepts it only while
    # the PR head is still this sha, and only from a trusted author.
    subprocess.run(["gh", "pr", "comment", url, "--repo", args.repo, "--body", certification_comment(
        head, "the planned change was implemented; the checks, two independent reviews and the final review passed")],
        capture_output=True, text=True, env=job_env)
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


INFRA_STEP_RE = re.compile(
    r"^(Set up job|Initialize containers|Complete job|Post .*|Run actions/(checkout|setup-[a-z]+|cache|upload-artifact|download-artifact)"
    r"|Checkout.*|Set up .*|Harden Runner|Pre Harden Runner|Testing on a k8s Kind Cluster|Create a kind cluster|Run helm/kind-action.*)",
    re.IGNORECASE)


def cmd_ci_infra(args: argparse.Namespace) -> int:
    """A job that fell over before it ran anything of ours (runner start-up, checkout, cluster creation) says
    nothing about the change. Re-run the failed jobs once and let the agent wait for that result, instead of
    spending a repair loop on a failure that is not in the code."""
    run = gh_json([f"repos/{args.repo}/actions/runs/{args.run_id}"]) or {}
    jobs = (gh_json([f"repos/{args.repo}/actions/runs/{args.run_id}/jobs?per_page=100"]) or {}).get("jobs", [])
    failed_steps = [(j["name"], st["name"]) for j in jobs if j.get("conclusion") == "failure"
                    for st in j.get("steps", []) if st.get("conclusion") == "failure"]
    # A failed job with no failed step at all failed in the runner itself.
    bare = [j["name"] for j in jobs if j.get("conclusion") == "failure" and not any(s.get("conclusion") == "failure" for s in j.get("steps", []))]
    infra = bool(failed_steps or bare) and all(INFRA_STEP_RE.match(step) for _, step in failed_steps)
    first_attempt = int(run.get("run_attempt") or 1) < 2
    if infra and first_attempt:
        if args.dry_run:
            log("[dry run] infrastructure failure on " + ", ".join(sorted({job for job, _ in failed_steps} | set(bare))) + ": would re-run it once")
        elif rerun_failed_jobs(args.repo, args.run_id):
            log("infrastructure failure (" + "; ".join(f"{j}: {s}" for j, s in failed_steps) + "): re-running the failed jobs once; "
                "the repair loop waits for that result")
        set_output("infra_retry", "true")
        return 0
    set_output("infra_retry", "false")
    log("not an infrastructure failure, or already re-run once: the repair loop judges it" if (failed_steps or bare) else "no failed job found")
    return 0


def cmd_guard_change(args: argparse.Namespace) -> int:
    """Should this run do nothing? Never reviews its own commits (no loops),
    bookkeeping commits, or a push whose commit belongs to a PR (that PR is
    reviewed on its own)."""
    author = git("log", "-1", "--format=%an").stdout.strip()
    subject = git("log", "-1", "--format=%s").stdout.strip()
    reason = ""
    comments: list[dict] = []
    if getattr(args, "pr", 0) and args.mode in ("pr", "ci"):
        comments = gh_json([f"repos/{args.repo}/issues/{args.pr}/comments?per_page=100"]) or []
        if is_abandoned(comments, args.sha):
            set_output("skip", "true")
            log("skipping: the agent already abandoned this exact commit; a new push starts a new attempt")
            return 0
    if args.mode == "ci":
        # A failed CI run on the agent's own fix is exactly when to try again,
        # but only a bounded number of times in a row.
        authors = git("log", "-n", "20", "--format=%an").stdout.split("\n")
        streak = next((i for i, a in enumerate(authors) if a != AGENT_AUTHOR), len(authors))
        if streak >= args.max_streak:
            reason = f"the last {streak} commits on this branch are the agent's own and CI still fails: stopping"
    elif author == AGENT_AUTHOR:
        # The run that pushed this commit certifies it. If that verdict is missing (the run was cancelled or
        # lost a race before it could post it) nobody else would ever look at this commit: review it, unless
        # the agent has already answered itself too many times in a row.
        authors = git("log", "-n", "20", "--format=%an").stdout.split("\n")
        streak = next((i for i, a in enumerate(authors) if a != AGENT_AUTHOR), len(authors))
        has_verdict = any(f"<!-- {CERT_MARKER}: {args.sha} -->" in (c.get("body") or "") for c in comments)
        if args.mode != "pr" or has_verdict:
            reason = "the head commit was written by this pipeline"
        elif streak >= getattr(args, "max_streak", 3):
            reason = f"the last {streak} commits are the agent's own and none carries a verdict: stopping"
        else:
            log("the head commit is the agent's own but carries no verdict: reviewing it")
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
    # The whole run has a time budget. Past it no model is called, so the graph ends by itself within
    # seconds, as abandoned and with the reason written: a verdict, not a job killed without one.
    minutes = getattr(args, "time_budget_minutes", 0)
    caller.deadline = time.time() + minutes * 60 if minutes else None
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
    evidence_repo = os.environ.get("GITHUB_REPOSITORY", "")
    prefix = getattr(args, "evidence_prefix", "")

    def evidence() -> str:
        """What the tools reported for this commit and, to compare, for the base commit: a finding that
        was already there is not news of this change. With nothing on the base there is no baseline yet."""
        here = lib.fetch_evidence(evidence_repo, head_sha, prefix, getattr(args, "evidence_wait", 0),
                                  own_run=os.environ.get("GITHUB_RUN_ID", ""))
        if not here.strip():
            return here
        before = lib.fetch_evidence(evidence_repo, args.base_sha, prefix, 0)
        return (here + "\n\n## The same tools on the base commit (to compare)\n"
                + (before if before.strip() else "Nothing was found for the base commit: there is no baseline yet, and this run is the first."))

    kind = {"push": "a commit pushed straight to the base branch"}.get(
        args.mode, "a pull request whose checks failed" if start == "ci" else "a pull request")
    rt = Runtime(caller, args.base_sha, pathlib.Path(args.verify_command_file).read_text() if args.verify_command_file else "",
                 args.verify_timeout, args.max_iterations, args.max_verify_retries, args.writer_rounds,
                 context, "", 0, start=start, fix_kind="fix", evidence=evidence, architect=True,
                 task=f"Kind: {kind}\nTitle: {lib.sanitize_untrusted(title, 500)}\n\n{lib.sanitize_untrusted(body, 20000)}")
    # One run of the graph, from the architect's brief to its closing pass. When the work does not end
    # well the graph itself goes back to the architect, once, with the reason (see `stop`).
    # The graph is streamed: after every node the journal of the run is written again to its record, so a
    # run that is cut short leaves what it had done, and one place does the writing.
    record_name, final = run_name(), {}
    lib.tool_context.update(base_sha=args.base_sha, evidence=evidence, runs_dir=str(runs_folder()), run_record=record_name)
    caller.raises = True
    try:
        for state in build_graph(rt, run_checkpointer(record_name)).stream(
                {"plan": plan, "iteration": 1, "verify_attempts": 0, "notes": [], "rounds": [], "journal": [],
                 "failure_output": failure_output},
                config={"recursion_limit": (rt.max_iterations + 1) * (rt.max_verify_retries + 1) * 12 + 60,
                        "configurable": {"thread_id": record_name}}, stream_mode="values"):
            final = state
            save_run_record(record_name, render_journal(state.get("journal") or [], f"{title} ({kind})"))
    except lib.ModelUnavailable as exc:
        # The graph ran the node again and the model still could not be reached. The run ends here and
        # says the cause: a comment that named only the role left without an answer hid a spent key.
        final = {**final, "outcome": "failed", "journal": (final.get("journal") or []) + entry("the model", f"could not be reached, twice: {exc}", fact=True),
                 "notes": final.get("notes", []) + [f"the model could not be reached, twice, so the work stopped: {exc}"]}
        save_run_record(record_name, render_journal(final["journal"], f"{title} ({kind})"))

    outcome = settle(final.get("outcome", "failed"))
    if getattr(caller, "out_of_time", False) and outcome == "abandoned":
        final["notes"] = final.get("notes", []) + [f"the run used its whole time budget of {minutes} minutes"]
    new_commits = int(git("rev-list", "--count", f"{head_sha}..HEAD").stdout.strip() or 0)
    touched_now = [n for n in git("diff", "--name-only", head_sha, "HEAD").stdout.split("\n") if n]
    violations = lib.policy_violations(head_sha)       # only what the agents added, not the author's own commits
    if violations:
        outcome, new_commits = "abandoned", 0
        final["notes"] = final.get("notes", []) + ["refusing to publish, policy: " + "; ".join(violations)]
    if outcome == "converged" and new_commits == 0:
        outcome = "clean"
    rounds = final.get("rounds", [])
    sha7 = head_sha[:7]
    result = {
        "mode": args.mode, "outcome": outcome, "branch": branch, "head_sha": head_sha, "commits": new_commits,
        "final_sha": git("rev-parse", "HEAD").stdout.strip(),
        "plan": final.get("plan") or plan, "notes": final.get("notes", []), "rounds": rounds, "story": final.get("story", []),
        "journal": final.get("journal", []), "run_record": f"{RUNS_DIR}/{record_name}",
        "findings": lib.merge_finding_dicts([f for r in rounds for f in r["findings"]]), "files": touched_now,
        "cost_usd": caller.total_cost_usd(),
        "adjudications": final.get("adjudications", []),
        "awareness": final.get("awareness", []),
        "cost_by_role": caller.cost_by_role(),
        "title": f"fix(agent): address review findings of {sha7}",
    }
    if args.mode == "push" and outcome == "converged":
        plan["summary"] = f"Review of {sha7}: {title[:120]}"
        (OUT_DIR / "pr-body.md").write_text(build_pr_body(plan, final, 0, result["cost_usd"], result["cost_by_role"]))
    write_result(result)
    log(f"agent {args.mode} review: {outcome}, {new_commits} new commit(s), ${result['cost_usd']:.4f}")
    return 0


WHO_WAS_WRONG = {
    "code_defect": "the code was wrong, the test was right",
    "test_defect": "the test was out of date: the change says, in its own words, that this behaviour is meant to change",
    "environment": "neither: the test fails for a reason outside the change",
    "preexisting": "it was failing before this change",
}


def build_change_comment(result: dict, pushed_ok: bool | None) -> str:
    """One comment a person can read. First what happened, in plain words (the architect's closing
    pass writes it); then the brief; the record, with its internal names, is folded underneath."""
    outcome = result["outcome"]
    n = result.get("commits", 0)
    sha = result.get("final_sha", "")
    certified = outcome in ("clean", "converged") and pushed_ok is not False and bool(sha)
    last_note = (result.get("notes") or ["no detail"])[-1].splitlines()[0][:300]
    left = sum(1 for f in result.get("findings") or [] if f["severity"] in lib.BLOCKING)
    status = {
        "clean": "The checks pass and nothing had to be changed.",
        "converged": f"The checks pass. The agents {'added' if pushed_ok else 'could not push'} {n} commit(s) to this branch.",
        "abandoned": f"**The agents abandoned this change** ({last_note}). "
                     "Nothing was merged and the base branch is untouched.",
    }.get(outcome, outcome)
    if left and outcome in ("clean", "converged"):
        status += (f" {left} finding(s) of the review were not resolved: they are in the details, for the people responsible "
                   "for the project, and did not hold the change, because it works.")
    if certified:
        status += f" **Certified at `{sha[:7]}`**: it merges by itself once its CI is green."
    abandoned_head = result.get("head_sha", "") if outcome == "abandoned" else ""
    parts = ["<!-- agent-pr -->" + (f"\n<!-- {CERT_MARKER}: {sha} -->" if certified else "")
             + (f"\n<!-- {ABANDON_MARKER}: {abandoned_head} -->" if abandoned_head else ""), "## Agent review", status]
    if result.get("story"):
        parts.append("### What happened\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(result["story"], 1)))
    brief = (result.get("plan") or {}).get("brief")
    if brief:
        asked = [f"**The architect's brief.** {brief['summary']}"]
        asked += [f"- For the developer: {x}" for x in brief["developer"]["instructions"]]
        for key, flag, who in (("tests", "needed", "QA"), ("infrastructure", "impact", "the SRE"),
                               ("security", "impact", "security"), ("docs", "needed", "the documentation")):
            if brief[key][flag]:
                asked.append(f"- For {who}: " + ("; ".join(brief[key]["what"]) or "to look at this change"))
        asked += [f"- Outside this task, worth knowing: {x}" for x in brief.get("beyond_the_task", [])]
        parts.append("\n".join(asked))
    detail = []
    rounds = result.get("rounds", [])
    if rounds:
        table = ["| Round | Step | Findings | Sent back to the developer | Dropped (not on a changed line) |", "|---|---|---|---|---|"]
        for r in rounds:
            back = sum(1 for f in r["findings"] if f["severity"] in lib.BLOCKING and SECOND_DEVELOPER in (f.get("reviewers") or []))
            table.append(f"| {r['iteration']} | {r['stage']} | {len(r['findings'])} | {back} | {r['dropped']} |")
        detail.append("\n".join(table))
    if result.get("adjudications"):
        lines = [f"- `{v['test']}`: {WHO_WAS_WRONG.get(v['classification'], v['classification'])} "
                 f"(**{v['classification']}**, {v['confidence']}). {v['reason']}"
                 for a in result["adjudications"] for v in a["verdicts"]]
        detail.append("### A check failed: was the code wrong, or the test?\n" + "\n".join(lines))
    if result.get("findings"):
        detail.append("### Findings\n" + lib.render_findings_md([lib.Finding(**f) for f in result["findings"]]))
    if result.get("awareness"):
        detail.append("### What the tools reported\nIt is here to be known; it did not hold the change.\n"
                      + "\n".join(f"- {x}" for x in result["awareness"]))
    if result.get("notes"):
        detail.append("### Notes of the run\n" + "\n".join(f"- {x}" for x in result["notes"]))
    if detail:
        parts.append("<details><summary>Details: findings, what the tools reported, notes</summary>\n\n"
                     + "\n\n".join(detail) + "\n\n</details>")
    parts.append(f"_An architect wrote the brief and closed the work; a developer and a second developer, QA, SRE, security "
                 f"and documentation did the rest. Model cost ${result.get('cost_usd', 0):.4f}._")
    return "\n\n".join(parts)


def cmd_publish_pr(args: argparse.Namespace) -> int:
    result = json.loads((OUT_DIR / "result.json").read_text())
    pushed_ok: bool | None = None
    # A verdict is about one commit. If the branch moved while this run worked (the author pushed, or
    # another agent run did), what it has to say is about a commit that is no longer the head: saying it
    # anyway once overwrote the certification of the newer commit and left the pull request open, green
    # and uncertified, with nothing left to run. The newer commit has its own run.
    try:
        pull = gh_json([f"repos/{args.repo}/pulls/{args.pr}"])
    except (subprocess.CalledProcessError, ValueError):
        pull = None
    current = ((pull if isinstance(pull, dict) else {}).get("head") or {}).get("sha", "")
    if current and result.get("head_sha") and current != result["head_sha"]:
        log(f"stale run: reviewed {result['head_sha'][:7]} but the head is now {current[:7]}; publishing nothing")
        return 0
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
            # Lost the race between the check above and the push: same thing, same answer.
            log("stale run: the branch moved while pushing (" + pushed.stderr.replace(header, "***").strip()[:200] + "); publishing nothing")
            return 0
    comments = gh_json([f"repos/{args.repo}/issues/{args.pr}/comments?per_page=100"]) or []
    existing = next((c for c in comments if "<!-- agent-pr -->" in (c.get("body") or "")), None)
    post_comment(args.repo, args.pr, build_change_comment(result, pushed_ok), existing)
    if result["outcome"] in ("clean", "converged"):
        # A new push by the author, now certified, is not abandoned any more.
        _gh("pr", "edit", str(args.pr), "--repo", args.repo, "--remove-label", ABANDONED_LABEL)  # best effort
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
REVERTED_LABEL = "agent-reverted"


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


def merge_one(args: argparse.Namespace, number: int) -> str:
    """Merge one pull request when, and only when: it is open, not abandoned, its head commit is
    certified, the circuit breaker is closed, and the required CI checks have succeeded on that same
    commit (try_merge). Returns what happened, for the log."""
    pr = gh_json([f"repos/{args.repo}/pulls/{number}"])
    if not pr or pr.get("state") != "open" or pr.get("draft"):
        return "not mergeable: the PR is closed or a draft"
    head = pr["head"]["sha"]
    if ((pr["head"].get("repo") or {}).get("full_name")) != args.repo:
        return "not merging a PR from a fork"
    if ((pr.get("base") or {}).get("ref") or args.base_branch) != args.base_branch:
        # The gate guards one branch. A pull request against another one (the canary's throwaway base, a
        # stacked branch) is not its to merge.
        return f"not merging: its base is {pr['base']['ref']}, this gate merges into {args.base_branch}"
    comments = gh_json([f"repos/{args.repo}/issues/{number}/comments?per_page=100"]) or []
    trusted = {t.strip() for t in args.trusted.split(",") if t.strip()}
    certified = is_certified(comments, head, trusted)
    if is_abandoned(comments, head):
        return "not merging: the agent abandoned this change at this commit"
    if ABANDONED_LABEL in {label["name"] for label in pr.get("labels", [])} and not certified:
        return "not merging: labelled abandoned, and nothing certifies this commit"
    if not certified:
        return f"not merging: nothing certifies {head[:7]} yet (a later run, or the next push, will)"
    reverts = recent_reverts(args.repo, args.base_branch)
    if reverts >= args.max_reverts:
        return f"not merging: circuit breaker open ({reverts} automatic reverts in 24h). Merging resumes by itself as they age out."
    required = tuple(c.strip() for c in args.required_checks.split(",") if c.strip())
    return "merge gate: " + try_merge(args.repo, number, head, args.merge_method, required,
                                      poll_seconds=args.poll_seconds, poll_interval=15)


def is_abandoned(comments: list[dict], head_sha: str) -> bool:
    """The agent gave up on exactly this head commit. Bound to the sha like the certification, so the
    author pushing a new commit starts a fresh attempt instead of staying blocked forever."""
    marker = f"<!-- {ABANDON_MARKER}: {head_sha} -->"
    return any(marker in (c.get("body") or "") for c in comments)


def cmd_merge_gate(args: argparse.Namespace) -> int:
    """Run the merge gate on one pull request, or (--pr 0) on every open one. The sweep over all of them
    is what makes the gate impossible to miss: whichever finishes first, certification or CI, and whatever
    event is lost, the next push or the schedule picks it up. Renovate's PRs belong to the sweep."""
    numbers = [args.pr]
    if not args.pr:
        listed = gh_json([f"repos/{args.repo}/pulls?state=open&per_page=100"]) or []
        numbers = [p["number"] for p in listed if not ((p.get("user") or {}).get("login") or "").lower().startswith("renovate")]
    for number in numbers:
        log(f"PR #{number}: {merge_one(args, number)}")
    if not numbers:
        log("no open pull requests for the merge gate")
    return 0


def failing_job_names(repo: str, run_id: int) -> list[str]:
    jobs = (gh_json([f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"]) or {}).get("jobs", [])
    return [j["name"] for j in jobs if j.get("conclusion") == "failure"]


def rerun_failed_jobs(repo: str, run_id: int) -> bool:
    """Ask GitHub to run only the failed jobs of this run again (a new attempt of the same run)."""
    token = os.environ.get("RERUN_TOKEN") or os.environ.get("GH_TOKEN", "")
    done = subprocess.run(["gh", "api", "-X", "POST", f"repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs"],
                          capture_output=True, text=True, env={**os.environ, "GH_TOKEN": token})
    return done.returncode == 0


_FAILURE_LINE = re.compile(r"(\bFAILED\b|\bERROR\b|Error\b|##\[error\]|^E\s|\d+ failed)")
_LOG_STAMP = re.compile(r"^(?:[^\t\n]*\t)*\d{4}-\d\d-\d\dT[\d:.]+Z\s?")


def failure_digest(logs: str, limit: int = 4000) -> str:
    """The lines of a failed job's log that say what failed. The tail of a log is mostly the runner cleaning
    up; a report made of that tells the reader nothing (it once quoted the runner's own service messages)."""
    lines = [_LOG_STAMP.sub("", line) for line in logs.splitlines()]
    telling = [line for line in lines if _FAILURE_LINE.search(line)]
    return "\n".join(telling or lines)[-limit:].strip()


def cmd_main_guard(args: argparse.Namespace) -> int:
    """The base branch went red after a merge. If a code change plausibly caused
    it, take the change out at once (git revert, pushed with the agent token) and
    say so, with the failure, on the pull request it came from. Reverted is a terminal
    state: no issue, no hand-off. Never reverts a revert, and stops at the breaker."""
    run = gh_json([f"repos/{args.repo}/actions/runs/{args.run_id}"]) or {}
    sha = run.get("head_sha", "")
    if run.get("head_branch") != args.base_branch or run.get("conclusion") != "failure" or not sha:
        log("nothing to guard: not a failed run on the base branch")
        return 0

    def note(text: str) -> int:
        log(("[dry run] " if args.dry_run else "") + text)
        if not args.dry_run:
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
    if any(r["run_number"] > run["run_number"] and r.get("conclusion") == "success" for r in earlier):
        # An agent (or the author) already fixed it forward while this run was being re-run: reverting the
        # old commit now would conflict with, or undo, that repair.
        return note("not reverting: a later run on this branch is green, so it was already repaired")
    previous = next((r for r in earlier if r["run_number"] < run["run_number"]), None)
    if previous is None or previous.get("conclusion") != "success":
        return note("not reverting: the branch was already red before this commit, so it is not the cause")
    if recent_reverts(args.repo, args.base_branch) >= args.max_reverts:
        return note("not reverting: circuit breaker open (too many automatic reverts in 24h)")
    if int(run.get("run_attempt") or 1) < 2:
        # A failure seen once may be a flake (a start-up race, a slow runner). Run only the failed
        # jobs again; this guard is triggered again when that attempt finishes. Only a failure that
        # repeats is treated as caused by the change. A run that passes the second time ends here.
        if args.dry_run:
            return note(f"would re-run the failed jobs ({', '.join(failed)}) once before reverting anything")
        if rerun_failed_jobs(args.repo, args.run_id):
            return note(f"re-running the failed jobs ({', '.join(failed)}) once before reverting anything, to rule out a flake")
        log("could not re-run the failed jobs; judging the first failure")

    token = os.environ.get("AGENT_PUSH_TOKEN", "")
    if not token and not args.dry_run:
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
    if args.dry_run:
        return note(f"would revert {len(suspects)} change(s) since the last green run: " + "; ".join(subjects)
                    + f" (failed jobs: {', '.join(failed)}) and say so on their pull requests")
    message = (f"{REVERT_PREFIX}: {label} (pipeline red)\n\nReverts: " + "; ".join(f"{c[:7]} {t}" for c, t in zip(suspects, subjects))
               + f".\nFailed jobs: {', '.join(failed)}.")
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
    logs = failure_digest(collect_failure_logs(args.repo, sha, 60000) or "") or "(no log excerpt available)"
    listing = "\n".join(f"- `{c[:7]}` {t}" for c, t in zip(suspects, subjects))
    body = (f"The pipeline on `{args.base_branch}` went red after `{sha[:7]}`, so the changes since the last green run were reverted "
            f"automatically:\n\n{listing}\n\nFailed jobs: {', '.join(failed)}.\n\n## Failure\n```\n{logs[-5000:]}\n```\n\n"
            "Reverted is where this change ends: nothing waits for anyone. To try again, open a new pull request with the "
            "change and what the failure above needs; it goes through the same pipeline.")
    # Said where the change came from: on its pull request, or on the commit when it had none. No issue is
    # opened: an issue is a hand-off, and a redo that does not know more than the first attempt fails the same way.
    told = []
    for commit in suspects:
        pulls = gh_json([f"repos/{args.repo}/commits/{commit}/pulls"]) or []
        if pulls:
            number = pulls[0]["number"]
            _gh("api", "-X", "POST", f"repos/{args.repo}/issues/{number}/comments", "-f", f"body={body}", "--silent")
            _gh("api", "-X", "POST", f"repos/{args.repo}/issues/{number}/labels", "-f", f"labels[]={REVERTED_LABEL}", "--silent")
            told.append(f"#{number}")
        else:
            _gh("api", "-X", "POST", f"repos/{args.repo}/commits/{commit}/comments", "-f", f"body={body}", "--silent")
            told.append(commit[:7])
    log(f"reverted {len(suspects)} change(s) since the last green run; said so on: {', '.join(told)}")
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
        p.add_argument("--max-chars", type=int, default=400_000)
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

    ci = sub.add_parser("ci-infra", help="re-run once a CI run that failed in the runner, not in the code")
    ci.add_argument("--repo", required=True)
    ci.add_argument("--run-id", type=int, required=True)
    ci.add_argument("--dry-run", action="store_true")
    ci.set_defaults(fn=cmd_ci_infra)

    gc = sub.add_parser("guard-change")
    gc.add_argument("--mode", choices=("pr", "push", "ci"), required=True)
    gc.add_argument("--max-streak", type=int, default=3,
                    help="mode ci: stop after this many consecutive agent commits on the branch")
    gc.add_argument("--repo", required=True)
    gc.add_argument("--sha", required=True)
    gc.add_argument("--pr", type=int, default=0, help="modes pr/ci: skip a commit the agent already abandoned")
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
    c.add_argument("--time-budget-minutes", type=int, default=15,
                   help="after this long no model is called any more and the run ends abandoned, with the reason")
    c.add_argument("--evidence-prefix", default="ai-context-",
                   help="artifacts of the commit's workflow runs whose name starts with this are shown to the roles as evidence")
    c.add_argument("--evidence-wait", type=int, default=300,
                   help="seconds to wait, at the first review, for the commit's other runs to finish")
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
    mg.add_argument("--pr", type=int, default=0, help="0 = every open pull request (except Renovate's)")
    mg.add_argument("--base-branch", default="main")
    mg.add_argument("--required-checks", default="")
    mg.add_argument("--merge-method", default="squash", choices=("merge", "squash", "rebase"))
    mg.add_argument("--poll-seconds", type=int, default=120)
    mg.add_argument("--trusted", required=True, help="comma-separated logins whose certification comments count")
    mg.add_argument("--max-reverts", type=int, default=3)
    mg.set_defaults(fn=cmd_merge_gate)

    mgd = sub.add_parser("main-guard", help="revert the commit that turned the base branch red, and say so on its pull request")
    mgd.add_argument("--repo", required=True)
    mgd.add_argument("--run-id", type=int, required=True)
    mgd.add_argument("--base-branch", default="main")
    mgd.add_argument("--max-reverts", type=int, default=3)
    mgd.add_argument("--dry-run", action="store_true", help="decide and print, change nothing (no re-run, comment, push or issue)")
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
