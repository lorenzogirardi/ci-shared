#!/usr/bin/env python3
"""LangGraph core for the autofix propose/explore/verify loop.

Turns (logs, diff) into (outcome, detail, changed files, explanation) without
knowing anything about git branches, PRs, or how a fix gets landed -- that is
the caller's job. `pr_review_sweep.py::autofix_one` pushes to an existing PR
branch; `main_autofix.py` pushes a new branch and opens a PR. This used to be
a plain `for attempt in range(...)` loop inside `autofix_one` itself; pulling
it out into a langgraph StateGraph makes the propose/explore/verify/retry
topology explicit instead of implicit in nested `continue`/`return` branches,
and lets both callers share one core.

Deliberately still parses a JSON fence out of prose instead of using native
tool-calling: the model in production use (hy3-free, via the OpenCode Zen
gateway) has unverified function-calling support, and the existing
prompt-driven protocol is already proven in production -- betting the whole
loop on native tool-calling would trade a known-working mechanism for an
unverified one for no functional gain here. All the parsing/validation/git
functions below are imported unchanged from pr_review_sweep.py.
"""

from __future__ import annotations

import argparse
import pathlib
from typing import TypedDict

from langgraph.graph import END, StateGraph

from pr_review_sweep import (
    AUTOFIX_SYSTEM,
    MAX_READ_CHARS,
    _call_model,
    apply_fix,
    build_context,
    find_matching_paths,
    grep_matching_lines,
    list_directory,
    parse_find_request,
    parse_fix,
    parse_grep_request,
    parse_list_request,
    parse_read_request,
    resolve_readable_path,
    run,
    run_verify,
)


class AutofixResult(TypedDict):
    outcome: str  # "ready" | "declined" | "rejected" | "skipped" | "exhausted"
    detail: str
    changed: list[str]
    explanation: str


class AutofixState(TypedDict, total=False):
    header: str  # one-line identifying context, e.g. "PR #123 title: bump pydantic"
    logs: str
    diff: str
    max_chars: int
    ai_script: str
    timeout: int
    identifier: int  # threaded into _call_model's log lines only
    history: list[str]
    attempt: int
    max_attempts: int
    verify_command: str
    verify_timeout: int
    last_verify_output: str
    route: str  # "explore" | "apply" | "give_up" | "done" | "retry"
    pending_kind: str  # "read" | "find" | "grep" | "list"
    pending_value: str
    edits: list[dict]
    explanation: str
    changed: list[str]
    outcome: str | None
    detail: str


def _model_args(state: AutofixState) -> argparse.Namespace:
    return argparse.Namespace(
        ai_script=state["ai_script"], max_chars=state["max_chars"], timeout=state["timeout"]
    )


def _budget_line(attempt_number: int, max_attempts: int) -> str:
    """Tells the model how much budget is left -- without this it has no
    reason to stop exploring and commit to an edit before the loop gives up.
    Real incident this addresses: a migration correctly found and fixed a
    renamed class in one file, then kept exploring instead of wrapping up,
    and ran out of rounds before a second call site of the same rename ever
    got an edit proposed for it.
    """
    remaining = max_attempts - attempt_number
    line = f"This is round {attempt_number} of {max_attempts} available."
    if remaining <= 2:
        line += (
            f" Only {remaining} round(s) left after this one -- if you have enough "
            "to propose a concrete edit, do that now rather than exploring further."
        )
    return line


def propose(state: AutofixState) -> dict:
    pathlib.Path(".ai").mkdir(parents=True, exist_ok=True)
    system_path = pathlib.Path(".ai/autofix-system.txt")
    system_path.write_text(AUTOFIX_SYSTEM)
    user_path = pathlib.Path(".ai/autofix-user.txt")
    context = build_context(state["history"])
    attempt = state["attempt"] + 1
    user_path.write_text(
        f"{state['header']}\n\n"
        f"{_budget_line(attempt, state['max_attempts'])}\n\n"
        f"## Failing CI output\n{state['logs']}\n\n"
        f"## Diff\n{state['diff'][: state['max_chars'] // 2]}\n"
        + (f"\n## What you already know from previous rounds\n{context}\n" if context else "")
    )
    reply = _call_model(_model_args(state), system_path, user_path, state["identifier"])

    if reply is None:
        detail = "model call failed"
        if state["attempt"] > 0:
            detail += f" after {state['attempt']} verified-failing attempt(s)"
        return {"attempt": attempt, "route": "give_up", "outcome": "skipped", "detail": detail}

    print(f"autofix attempt {attempt}/{state['max_attempts']}: {reply[:1000]}")

    for kind, parser in (
        ("read", parse_read_request),
        ("find", parse_find_request),
        ("grep", parse_grep_request),
        ("list", parse_list_request),
    ):
        value = parser(reply)
        if value is not None:
            return {"attempt": attempt, "route": "explore", "pending_kind": kind, "pending_value": value}

    parsed = parse_fix(reply)
    if parsed is None:
        return {
            "attempt": attempt,
            "route": "give_up",
            "outcome": "rejected",
            "detail": "model reply was malformed or outside the allowed scope",
        }
    edits, explanation = parsed
    if not edits:
        # A refusal is a valid answer, and better than a guessed edit.
        return {
            "attempt": attempt,
            "route": "give_up",
            "outcome": "declined",
            "detail": explanation or "the model found no concrete fix",
        }
    return {"attempt": attempt, "route": "apply", "edits": edits, "explanation": explanation}


def explore(state: AutofixState) -> dict:
    kind, value, attempt = state["pending_kind"], state["pending_value"], state["attempt"]
    history = list(state["history"])
    budget = state["max_attempts"]

    if kind == "read":
        resolved = resolve_readable_path(value)
        if resolved is None:
            history.append(
                f"Round {attempt}: you asked to read {value!r}, but it doesn't exist, "
                "isn't somewhere readable (this repo checkout, or an installed Python "
                "package), or isn't a resolvable module name. Use {\"find\": \"name\"} to "
                "search for the real path instead of guessing one."
            )
        else:
            content = resolved.read_text(errors="replace")[:MAX_READ_CHARS]
            history.append(f"Round {attempt}: you read {value} ({resolved}):\n{content}")
        print(
            f"autofix attempt {attempt}/{budget}: read {value!r} "
            f"({'found' if resolved else 'not found'})"
        )
    elif kind == "find":
        matches = find_matching_paths(value)
        history.append(
            (f"Round {attempt}: you searched for {value!r}. Matches:\n" + "\n".join(matches))
            if matches
            else f"Round {attempt}: you searched for {value!r}. No matches in this repo "
            "checkout or the installed Python packages."
        )
        print(f"autofix attempt {attempt}/{budget}: find {value!r} ({len(matches)} match(es))")
    elif kind == "grep":
        hits = grep_matching_lines(value)
        history.append(
            (f"Round {attempt}: you searched file contents for {value!r}. Matches:\n" + "\n".join(hits))
            if hits
            else f"Round {attempt}: you searched file contents for {value!r}. No matches "
            "in this repo checkout or the installed Python packages."
        )
        print(f"autofix attempt {attempt}/{budget}: grep {value!r} ({len(hits)} match(es))")
    else:  # "list"
        entries = list_directory(value)
        history.append(
            f"Round {attempt}: you asked to list {value!r}, but it doesn't exist or "
            "isn't somewhere listable (this repo checkout, or an installed Python package)."
            if entries is None
            else f"Round {attempt}: contents of {value}:\n" + ("\n".join(entries) if entries else "(empty)")
        )
        print(
            f"autofix attempt {attempt}/{budget}: list {value!r} "
            f"({'found' if entries is not None else 'not found'})"
        )
    return {"history": history}


def apply_and_verify(state: AutofixState) -> dict:
    attempt, edits, explanation = state["attempt"], state["edits"], state["explanation"]
    changed, error = apply_fix(edits)
    if error:
        return {"route": "give_up", "outcome": "rejected", "detail": error}

    ok, verify_output = run_verify(state["verify_command"], state["verify_timeout"])
    print(
        f"autofix attempt {attempt}/{state['max_attempts']}: verify "
        f"{'passed' if ok else 'failed'} on {', '.join(changed)}"
    )
    if ok:
        return {
            "route": "done",
            "outcome": "ready",
            "changed": changed,
            "explanation": explanation,
            "detail": (
                f"{explanation} (edited {', '.join(changed)}, verified locally in {attempt} attempt(s))"
            ),
        }

    # Verification failed: undo this attempt before the next round proposes another.
    run(["git", "checkout", "--", *changed], check=False)
    history = state["history"] + [
        f"Round {attempt}: you tried:\n{explanation}\n\nBut local verification then failed:\n{verify_output}"
    ]
    return {"route": "retry", "history": history, "last_verify_output": verify_output}


def give_up(state: AutofixState) -> dict:
    if state.get("outcome"):
        return {}
    last = state.get("last_verify_output", "")
    return {
        "outcome": "exhausted",
        "detail": (
            f"used all {state['max_attempts']} attempt(s) (proposed fixes and file reads "
            f"combined), none passed verification. Last failure:\n{last[-1500:]}"
        ),
    }


def _budget_exhausted(state: AutofixState) -> bool:
    return state["attempt"] >= state["max_attempts"]


def _route_from_propose(state: AutofixState) -> str:
    return state["route"]


def _route_after_explore(state: AutofixState) -> str:
    return "give_up" if _budget_exhausted(state) else "propose"


def _route_after_apply(state: AutofixState) -> str:
    if state["route"] in ("give_up", "done"):
        return state["route"]
    return "give_up" if _budget_exhausted(state) else "propose"  # "retry"


def build_graph():
    graph = StateGraph(AutofixState)
    graph.add_node("propose", propose)
    graph.add_node("explore", explore)
    graph.add_node("apply_and_verify", apply_and_verify)
    graph.add_node("give_up", give_up)

    graph.set_entry_point("propose")
    graph.add_conditional_edges(
        "propose", _route_from_propose,
        {"explore": "explore", "apply": "apply_and_verify", "give_up": "give_up"},
    )
    graph.add_conditional_edges("explore", _route_after_explore, {"propose": "propose", "give_up": "give_up"})
    graph.add_conditional_edges(
        "apply_and_verify", _route_after_apply,
        {"propose": "propose", "give_up": "give_up", "done": END},
    )
    graph.add_edge("give_up", END)
    return graph.compile()


_GRAPH = None


def _get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


def run_autofix_graph(
    *,
    header: str,
    logs: str,
    diff: str,
    verify_command: str,
    verify_timeout: int,
    max_attempts: int,
    ai_script: str,
    max_chars: int,
    timeout: int,
    identifier: int = 0,
) -> AutofixResult:
    """Run the propose/explore/verify loop to completion and return the outcome.

    `header` replaces the old "PR #N title: ..." line -- callers without a PR
    (a broken push to main) pass whatever one-line context makes sense instead.
    `identifier` is only used in the model-call's own log lines.
    """
    initial: AutofixState = {
        "header": header,
        "logs": logs,
        "diff": diff,
        "verify_command": verify_command,
        "verify_timeout": verify_timeout,
        "max_attempts": max_attempts,
        "ai_script": ai_script,
        "max_chars": max_chars,
        "timeout": timeout,
        "identifier": identifier,
        "history": [],
        "attempt": 0,
        "last_verify_output": "",
    }
    # Each round costs at most 2 supersteps (propose -> explore|apply); pad generously
    # so a large --max-autofix-attempts never trips langgraph's own recursion guard.
    final = _get_graph().invoke(initial, config={"recursion_limit": max_attempts * 4 + 10})
    return {
        "outcome": final["outcome"],
        "detail": final["detail"],
        "changed": final.get("changed", []),
        "explanation": final.get("explanation", ""),
    }
