#!/usr/bin/env python3
"""Building blocks shared by the role-based agents (planner, writer, reviewers,
documentation agents).

Everything that turns model text into something the pipeline acts on lives
here and is strict: a reply that does not match its schema is discarded, never
interpreted generously. The roles themselves are only prompts
(prompts/agents/*.md) plus the schema validators below.

Stdlib only, except that the callers in agent_pipeline.py use langgraph.
"""

from __future__ import annotations

import dataclasses
import difflib
import json
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable

from ai_sanitize import _HARD_SECRET_RE, redact
from pr_review_sweep import (
    AUTOFIX_BLOCKED_PREFIX,
    MAX_FIND_CHARS,
    MAX_READ_CHARS,
    _parse_json_reply,
    apply_fix,
    find_matching_paths,
    grep_matching_lines,
    list_directory,
    parse_find_request,
    parse_grep_request,
    parse_list_request,
    parse_read_request,
    resolve_readable_path,
)

PROMPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "prompts" / "agents"

SEVERITIES = ("critical", "high", "medium", "low")
BLOCKING = frozenset({"critical", "high"})
MAX_FINDINGS = 30
MAX_CHANGES = 8
MAX_CONTENT_CHARS = 30_000
BLOCKED_PREFIXES = (AUTOFIX_BLOCKED_PREFIX, ".git/")
# Files an agent must never write, whatever the plan says: credentials and keys.
_PROTECTED_NAME_RE = re.compile(r"(^|/)(\.env(\..*)?|id_(rsa|ed25519|ecdsa)(\.pub)?|[^/]*\.(pem|key|p12|pfx|jks|keystore))$")
MAX_PATCH_FILES = 60
MAX_PATCH_LINES = 3000


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text()


# ---------------------------------------------------------------------------
# Model access
# ---------------------------------------------------------------------------

class ModelCaller:
    """One place that talks to openrouter_ai.py, so every role call is logged,
    costed and saved under .ai/agents/ for traceability.

    The API key is read from the environment here and handed to the model
    subprocess only. Nothing else in this process tree needs it -- in
    particular not the verify_command, see run_verify_isolated().
    """

    def __init__(self, ai_script: str, *, max_chars: int = 140_000, timeout: int = 180,
                 work_dir: str = ".ai/agents") -> None:
        self.ai_script = ai_script
        self.max_chars = max_chars
        self.timeout = timeout
        self.work_dir = pathlib.Path(work_dir)
        self.calls = 0
        self.usage: list[dict] = []

    def call(self, role: str, system: str, user: str) -> str | None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.calls += 1
        stem = self.work_dir / f"{self.calls:03d}-{role}"
        system_path, user_path = stem.with_suffix(".system.txt"), stem.with_suffix(".user.txt")
        usage_path = stem.with_suffix(".usage.json")
        system_path.write_text(system)
        user_path.write_text(user)
        proc = subprocess.run(
            [sys.executable, self.ai_script,
             "--system-file", str(system_path), "--prompt-file", str(user_path),
             "--max-chars", str(self.max_chars), "--timeout", str(self.timeout),
             "--usage-file", str(usage_path)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print(f"::warning::model call for role {role!r} failed: {proc.stderr.strip()[:400]}", file=sys.stderr)
            return None
        stem.with_suffix(".reply.txt").write_text(proc.stdout)
        if usage_path.is_file():
            try:
                self.usage.append({"role": role, **json.loads(usage_path.read_text())})
            except (ValueError, OSError):
                pass
        return proc.stdout

    def total_cost_usd(self) -> float:
        return round(sum(float(u.get("cost_usd") or 0) for u in self.usage), 6)

    def cost_by_role(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for u in self.usage:
            out[u["role"]] = round(out.get(u["role"], 0.0) + float(u.get("cost_usd") or 0), 6)
        return out


def ask_json(caller: ModelCaller, role: str, system: str, user: str,
             validate: Callable[[dict], object | None], *, retries: int = 1):
    """Ask, parse a JSON object out of the reply, validate it. On a malformed
    reply re-ask once with the schema reminder before giving up, because a
    flash-class model drops a brace often enough that one retry is cheap
    insurance. Returns whatever `validate` returns, or None."""
    reply = caller.call(role, system, user)
    for attempt in range(retries + 1):
        if reply is None:
            return None
        data = _parse_json_reply(reply)
        parsed = validate(data) if data is not None else None
        if parsed is not None:
            return parsed
        if attempt == retries:
            return None
        reply = caller.call(
            role, system,
            user + "\n\nYour previous reply was not one valid JSON object matching the required "
                   "schema. Reply again with ONE fenced json block that matches it exactly, nothing else.",
        )
    return None


# ---------------------------------------------------------------------------
# Untrusted text and repository context
# ---------------------------------------------------------------------------

def sanitize_untrusted(text: str, limit: int = 20_000) -> str:
    """Issue bodies and PR text are data: redact secret-shaped strings and cap
    the size before they reach a prompt."""
    return redact(text or "")[:limit]


def repo_tree(max_files: int = 500) -> str:
    proc = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
    files = [f for f in proc.stdout.splitlines() if f and not f.startswith((".shared/", ".ai/"))]
    shown = files[:max_files]
    suffix = f"\n... ({len(files) - max_files} more files)" if len(files) > max_files else ""
    return "\n".join(shown) + suffix


def read_context_files(names: list[str], per_file: int = 6000) -> str:
    parts = []
    for name in names:
        path = pathlib.Path(name)
        if path.is_file():
            parts.append(f"### {name}\n{path.read_text(errors='replace')[:per_file]}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Read-only exploration (shared with the autofix protocol)
# ---------------------------------------------------------------------------

def parse_explore(reply: str) -> tuple[str, str] | None:
    for kind, parser in (("read", parse_read_request), ("find", parse_find_request),
                         ("grep", parse_grep_request), ("list", parse_list_request)):
        value = parser(reply)
        if value is not None:
            return kind, value
    return None


def explore(kind: str, value: str) -> str:
    """Run one read/find/grep/list request and return the text to feed back."""
    if kind == "read":
        resolved = resolve_readable_path(value)
        if resolved is None:
            return f"You asked to read {value!r}, but it does not exist or is not readable. Use find to locate it."
        return f"You read {value}:\n{resolved.read_text(errors='replace')[:MAX_READ_CHARS]}"
    if kind == "find":
        matches = find_matching_paths(value)
        return (f"Files matching {value!r}:\n" + "\n".join(matches)) if matches else f"No files match {value!r}."
    if kind == "grep":
        hits = grep_matching_lines(value)
        return (f"Lines matching {value!r}:\n" + "\n".join(hits)) if hits else f"No lines match {value!r}."
    entries = list_directory(value)
    if entries is None:
        return f"{value!r} does not exist or is not listable."
    return f"Contents of {value}:\n" + ("\n".join(entries) if entries else "(empty)")


# ---------------------------------------------------------------------------
# Findings: strict schema, validation against the diff, dedup
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    severity: str
    file: str
    line: int
    category: str
    evidence: str
    problem: str
    suggestion: str
    reviewers: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return self.severity in BLOCKING

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _safe_path(path: object) -> bool:
    return isinstance(path, str) and bool(path) and ".." not in path.split("/") and not path.startswith("/")


def parse_findings(data: dict | None, reviewer: str) -> list[Finding] | None:
    """Findings from a reviewer reply, or None when the reply is malformed.

    Strict on purpose: a finding that cannot be located (no file, no line) or
    has no evidence cannot be validated against the diff and would only be
    noise handed to the writer.
    """
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        return None
    out: list[Finding] = []
    for item in data["findings"][:MAX_FINDINGS]:
        if not isinstance(item, dict):
            return None
        severity = str(item.get("severity", "")).lower()
        line = item.get("line")
        text = {k: item.get(k) for k in ("evidence", "problem", "suggestion")}
        if (severity not in SEVERITIES or not _safe_path(item.get("file"))
                or not isinstance(line, int) or isinstance(line, bool) or line < 1
                or not all(isinstance(v, str) and v.strip() for v in text.values())):
            return None
        category = item.get("category")
        out.append(Finding(
            severity=severity, file=item["file"], line=line,
            category=category.strip().lower() if isinstance(category, str) and category.strip() else "general",
            evidence=text["evidence"].strip(), problem=text["problem"].strip(),
            suggestion=text["suggestion"].strip(), reviewers=[reviewer],
        ))
    return out


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def diff_ranges(diff: str) -> dict[str, list[tuple[int, int]]]:
    """New-file line ranges covered by each hunk, per file. A finding is only
    valid if its line falls inside one of them. Consumes each hunk by its
    declared line counts, so a removed line that happens to start with `-- `
    is never mistaken for a file header."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    old_left = new_left = 0
    for raw in diff.splitlines():
        if old_left > 0 or new_left > 0:
            if raw.startswith("+"):
                new_left -= 1
            elif raw.startswith("-"):
                old_left -= 1
            elif raw.startswith("\\"):
                pass
            else:
                old_left -= 1
                new_left -= 1
            continue
        if raw.startswith("+++ "):
            name = raw[4:].strip()
            current = name[2:] if name.startswith("b/") else None
            if current:
                ranges.setdefault(current, [])
            continue
        match = _HUNK_RE.match(raw)
        if match and current:
            old_left = int(match.group(2)) if match.group(2) is not None else 1
            new_len = int(match.group(4)) if match.group(4) is not None else 1
            new_left = new_len
            start = int(match.group(3))
            ranges[current].append((start, start + max(new_len, 1) - 1))
    return ranges


def annotate_diff(diff: str) -> str:
    """The diff with `L<n>|` in front of every added or context line, n being
    its line number in the new file. Models are bad at deriving line numbers
    from hunk headers; handing them the numbers removes the most common way a
    correct finding ends up pointing at the wrong line."""
    out: list[str] = []
    old_left = new_left = new_line = 0
    for raw in diff.splitlines():
        if old_left > 0 or new_left > 0:
            if raw.startswith("+"):
                out.append(f"L{new_line}|{raw}")
                new_line += 1
                new_left -= 1
            elif raw.startswith("-"):
                out.append(raw)
                old_left -= 1
            elif raw.startswith("\\"):
                out.append(raw)
            else:
                out.append(f"L{new_line}|{raw}")
                new_line += 1
                old_left -= 1
                new_left -= 1
            continue
        match = _HUNK_RE.match(raw)
        if match:
            old_left = int(match.group(2)) if match.group(2) is not None else 1
            new_left = int(match.group(4)) if match.group(4) is not None else 1
            new_line = int(match.group(3))
        out.append(raw)
    return "\n".join(out)


def validate_findings(findings: list[Finding],
                      ranges: dict[str, list[tuple[int, int]]]) -> tuple[list[Finding], list[tuple[Finding, str]]]:
    """Keep findings whose file is in the diff and whose line is inside a
    changed hunk; return the rest with the reason they were dropped."""
    kept: list[Finding] = []
    dropped: list[tuple[Finding, str]] = []
    for f in findings:
        spans = ranges.get(f.file)
        if spans is None:
            dropped.append((f, "file is not part of the diff"))
        elif not any(a <= f.line <= b for a, b in spans):
            dropped.append((f, "line is outside the changed hunks"))
        else:
            kept.append(f)
    return kept, dropped


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def dedup_findings(findings: list[Finding]) -> list[Finding]:
    """Merge findings that point at the same place about the same thing, most
    severe first, remembering which reviewers raised it. Two reviewers
    agreeing is a stronger signal than either alone."""
    merged: list[Finding] = []
    for f in sorted(findings, key=lambda x: SEVERITIES.index(x.severity)):
        for m in merged:
            same_place = m.file == f.file and abs(m.line - f.line) <= 3
            same_topic = (m.category == f.category
                          or difflib.SequenceMatcher(None, _norm(m.problem), _norm(f.problem)).ratio() >= 0.6)
            if same_place and same_topic:
                for r in f.reviewers:
                    if r not in m.reviewers:
                        m.reviewers.append(r)
                break
        else:
            merged.append(dataclasses.replace(f, reviewers=list(f.reviewers)))
    return merged


def render_findings_md(findings: list[Finding]) -> str:
    if not findings:
        return "_No findings._"
    lines = []
    for f in findings:
        lines.append(
            f"- **[{f.severity}]** `{f.file}:{f.line}` ({f.category}; {', '.join(f.reviewers) or 'n/a'})\n"
            f"  - Problem: {f.problem}\n  - Evidence: {f.evidence}\n  - Suggested fix: {f.suggestion}"
        )
    return "\n".join(lines)


def findings_for_writer(findings: list[Finding]) -> str:
    return json.dumps([f.to_dict() for f in findings], indent=2)


# ---------------------------------------------------------------------------
# Writer output: edits and new files, applied all-or-nothing
# ---------------------------------------------------------------------------

def parse_changes(data: dict | None, *, allowed: Callable[[str], bool] | None = None
                  ) -> tuple[list[dict], str] | None:
    """(changes, explanation) from a writer reply, or None if unusable.

    A change is either {file, find, replace} (edit an existing file; the
    anchor must be unique, enforced at apply time) or {file, content} (create
    a new file). Anything outside `allowed`, under .github/workflows/, or
    oversized invalidates the whole reply.
    """
    if not isinstance(data, dict):
        return None
    raw, explanation = data.get("changes"), data.get("explanation")
    if not isinstance(raw, list) or not isinstance(explanation, str) or len(raw) > MAX_CHANGES:
        return None
    changes: list[dict] = []
    for item in raw:
        if not isinstance(item, dict) or not _safe_path(item.get("file")):
            return None
        path = item["file"]
        if is_protected_path(path) or (allowed is not None and not allowed(path)):
            return None
        if "content" in item:
            content = item["content"]
            if not isinstance(content, str) or not content or len(content) > MAX_CONTENT_CHARS:
                return None
            changes.append({"file": path, "content": content})
        else:
            find, replace = item.get("find"), item.get("replace")
            if not isinstance(find, str) or not isinstance(replace, str):
                return None
            if not find or find == replace or len(find) > MAX_FIND_CHARS or len(replace) > MAX_CONTENT_CHARS:
                return None
            changes.append({"file": path, "find": find, "replace": replace})
    return changes, explanation


@dataclass
class Applied:
    edited: list[str]
    created: list[str]
    error: str | None = None

    @property
    def files(self) -> list[str]:
        return self.edited + self.created


def apply_changes(changes: list[dict]) -> Applied:
    """Apply edits (via apply_fix, which composes edits to one file and is
    all-or-nothing) and then create new files. Nothing is written if any
    change is invalid."""
    creates = [c for c in changes if "content" in c]
    edits = [c for c in changes if "content" not in c]
    seen: set[str] = set()
    for c in creates:
        if c["file"] in seen or pathlib.Path(c["file"]).exists():
            return Applied([], [], f"{c['file']} already exists or is created twice (edit it with find/replace)")
        seen.add(c["file"])
    edited: list[str] = []
    if edits:
        edited, error = apply_fix(edits)
        if error:
            return Applied([], [], error)
    for c in creates:
        path = pathlib.Path(c["file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(c["content"])
    return Applied(edited, [c["file"] for c in creates])


def revert(applied: Applied) -> None:
    if applied.edited:
        subprocess.run(["git", "checkout", "--", *applied.edited], capture_output=True)
    for name in applied.created:
        pathlib.Path(name).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Deterministic verification, isolated from secrets
# ---------------------------------------------------------------------------

# Allow-list, not deny-list: a new secret added to the job's env tomorrow must
# not become visible to code under test by default.
_SAFE_ENV = frozenset({
    "PATH", "HOME", "LANG", "TMPDIR", "TEMP", "TERM", "CI", "LD_LIBRARY_PATH", "PKG_CONFIG_PATH",
    "pythonLocation", "Python_ROOT_DIR", "Python2_ROOT_DIR", "Python3_ROOT_DIR",
    "RUNNER_OS", "RUNNER_ARCH", "RUNNER_TOOL_CACHE", "RUNNER_TEMP", "AGENT_TOOLSDIRECTORY",
    "PIP_DISABLE_PIP_VERSION_CHECK", "VIRTUAL_ENV",
})


def scrubbed_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV or k.startswith("LC_")}
    env["CI"] = "true"
    return env


def run_verify_isolated(command: str, timeout: int) -> tuple[bool, str]:
    """Run the deterministic gates (lint, tests, smoke test) as a process that
    cannot see the model API key or any GitHub token.

    The code under test was written by a model moments ago; pytest and pip
    execute it. If it could read the job's environment it could exfiltrate the
    secrets. The job also checks out with persist-credentials: false, so there
    is no token in .git/config either.
    """
    if not command.strip():
        return True, ""
    try:
        proc = subprocess.run(["bash", "-c", command], capture_output=True, text=True,
                              timeout=timeout, env=scrubbed_env())
    except subprocess.TimeoutExpired:
        return False, f"verify command exceeded {timeout}s and was killed"
    if proc.returncode == 0:
        return True, ""
    return False, (proc.stdout + "\n" + proc.stderr).strip()[-4000:]


# ---------------------------------------------------------------------------
# Failing tests: deterministic evidence, and a guard against weakening tests
# ---------------------------------------------------------------------------

# Only real test ids (with `::`): a collection error such as `ERROR tests/x.py - SyntaxError` is
# a broken file, not a failing test, and has nothing to adjudicate.
_NODEID_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+\.py::\S+)", re.MULTILINE)
SOURCE_SUFFIXES = (".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".php", ".cs", ".c", ".cpp", ".h")


def parse_failed_tests(output: str) -> list[str]:
    """pytest node ids named in a failure report (`FAILED path::test - msg`)."""
    seen: list[str] = []
    for match in _NODEID_RE.finditer(output):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


def is_test_path(path: str) -> bool:
    name = pathlib.PurePosixPath(path).name
    return (path.startswith(("tests/", "test/")) or "/tests/" in path
            or name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py")


def is_source_path(path: str) -> bool:
    """Application code: what a change must be covered by tests for."""
    return (path.endswith(SOURCE_SUFFIXES) and not is_test_path(path)
            and not path.startswith((".github/", ".shared/", ".ai/", "docs/")))


def test_signals(text: str) -> dict[str, int]:
    return {
        "tests": len(re.findall(r"^\s*(?:async\s+)?def\s+test_", text, re.MULTILINE)),
        "asserts": len(re.findall(r"\bassert\b|\bpytest\.raises\b|\.assert_\w+\(", text)),
        "skips": len(re.findall(r"\bskip\b|\bxfail\b|skipif", text)),
    }


def weakened_tests(ref: str = "HEAD") -> list[str]:
    """Ways the working tree makes the tests weaker than at `ref`: a test file
    deleted, fewer tests, fewer assertions, or more skip/xfail. This is what
    stops a failing test from being "fixed" by gutting it."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], capture_output=True, text=True).stdout

    violations: list[str] = []
    for path in git("diff", "--name-only", "--diff-filter=D", ref).split("\n"):
        if path and is_test_path(path):
            violations.append(f"{path} was deleted")
    for path in git("diff", "--name-only", "--diff-filter=M", ref).split("\n"):
        if not path or not is_test_path(path) or not pathlib.Path(path).is_file():
            continue
        before, after = test_signals(git("show", f"{ref}:{path}")), test_signals(pathlib.Path(path).read_text())
        for key, label in (("tests", "fewer tests"), ("asserts", "fewer assertions")):
            if after[key] < before[key]:
                violations.append(f"{path}: {label} ({before[key]} -> {after[key]})")
        if after["skips"] > before["skips"]:
            violations.append(f"{path}: more skip/xfail markers ({before['skips']} -> {after['skips']})")
    return violations


def run_pytest(args: list[str], cwd: str | pathlib.Path, timeout: int = 300) -> tuple[str, str]:
    """('pass' | 'fail' | 'absent' | 'skipped' | 'error', output) for one pytest
    invocation, in the same secret-free environment as the verify command."""
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *args],
                              capture_output=True, text=True, timeout=timeout, cwd=str(cwd), env=scrubbed_env())
    except subprocess.TimeoutExpired:
        return "error", f"pytest exceeded {timeout}s"
    out = (proc.stdout + "\n" + proc.stderr).strip()
    if proc.returncode == 0:
        return ("skipped" if "skipped" in out and "passed" not in out else "pass"), out[-1500:]
    return {1: "fail", 5: "absent"}.get(proc.returncode, "error"), out[-1500:]


def evidence_hint(evidence: dict) -> str:
    """What the deterministic runs say about one failing test, before any model
    gets an opinion: flaky / unreproducible / new_test / preexisting / regression."""
    head = evidence.get("head", [])
    if any(h == "pass" for h in head):
        return "flaky"
    if not head or all(h in ("error", "skipped", "absent") for h in head):
        return "unreproducible"
    base = evidence.get("base")
    return {"absent": "new_test", "fail": "preexisting", "pass": "regression"}.get(base, "unknown")


def gather_evidence(nodeids: list[str], base_sha: str, *, repeats: int = 2, timeout: int = 300) -> dict[str, dict]:
    """Re-run each failing test on the current tree (is it flaky?) and on the
    base commit (did it pass before this change?). Only possible for tests that
    can run in this job; CI-only tests come back 'unreproducible' and are judged
    on their output and the change's stated intent instead."""
    result: dict[str, dict] = {}
    for nid in nodeids[:5]:
        result[nid] = {"head": [run_pytest([nid], ".", timeout)[0] for _ in range(repeats)], "base": "unknown"}
    runnable = [n for n, e in result.items() if any(h in ("fail", "pass") for h in e["head"])]
    if runnable and base_sha:
        import tempfile
        tmp = tempfile.mkdtemp(prefix="agent-base-")
        added = subprocess.run(["git", "worktree", "add", "--detach", tmp, base_sha], capture_output=True, text=True)
        if added.returncode == 0:
            try:
                for nid in runnable:
                    path = nid.split("::")[0]
                    exists = subprocess.run(["git", "cat-file", "-e", f"{base_sha}:{path}"], capture_output=True).returncode == 0
                    # A test file that does not exist on the base was added by this change.
                    result[nid]["base"] = run_pytest([nid], tmp, timeout)[0] if exists else "absent"
            finally:
                subprocess.run(["git", "worktree", "remove", "--force", tmp], capture_output=True)
    for nid, ev in result.items():
        ev["hint"] = evidence_hint(ev)
    return result


def discriminating(base_sha: str, files: list[str], timeout: int = 300) -> tuple[int, int] | None:
    """Do the given test files fail on the BASE source? A test written for a
    change should fail without it. Returns (tests that failed there, that passed
    there), or None when this cannot be measured here."""
    import tempfile
    if not files or not base_sha:
        return None
    tmp = tempfile.mkdtemp(prefix="agent-base-")
    if subprocess.run(["git", "worktree", "add", "--detach", tmp, base_sha], capture_output=True).returncode != 0:
        return None
    try:
        for f in files:
            dest = pathlib.Path(tmp) / f
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(pathlib.Path(f).read_text())
        outcome, out = run_pytest(["--tb=no", *files], tmp, timeout)
        if outcome in ("error", "absent", "skipped"):
            return None
        failed = re.search(r"(\d+) failed", out)
        passed = re.search(r"(\d+) passed", out)
        return (int(failed.group(1)) if failed else 0), (int(passed.group(1)) if passed else 0)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", tmp], capture_output=True)


def test_inventory(max_files: int = 60) -> str:
    """The test files and the test functions in them, so the steward knows what
    is already covered without reading every file."""
    files = [f for f in subprocess.run(["git", "ls-files"], capture_output=True, text=True).stdout.split("\n")
             if f.endswith(".py") and is_test_path(f)][:max_files]
    lines = []
    for f in files:
        names = re.findall(r"^\s*(?:async\s+)?def\s+(test_\w+)", pathlib.Path(f).read_text(errors="replace"), re.MULTILINE)
        lines.append(f"{f}: " + ", ".join(names[:40]))
    return "\n".join(lines) or "(no test files)"


# ---------------------------------------------------------------------------
# Patch policy: checked on what the writer PROPOSES, and again on what is about to be published
# ---------------------------------------------------------------------------

def is_protected_path(path: str) -> bool:
    return path.startswith(BLOCKED_PREFIXES) or bool(_PROTECTED_NAME_RE.search(path))


def contains_secret(text: str) -> bool:
    """A credential-shaped string (token, key, JWT). Only the patterns that are almost always real secrets:
    config-style assignments such as `password = "x"` are normal in tests and docs."""
    return any(pattern.search(text) for pattern in _HARD_SECRET_RE)


_PATHISH_RE = re.compile(r"[\w.\-]+(?:/[\w.\-]+)+/?|[\w\-]+/(?![\w.\-])|[\w\-]+\.[a-z]{1,5}\b")


def out_of_scope_paths(plan: dict) -> list[str]:
    """Path-like tokens the plan itself declared out of scope."""
    found: list[str] = []
    for item in plan.get("out_of_scope", []) or []:
        found += [m.group(0).rstrip("/") for m in _PATHISH_RE.finditer(item)]
    return found


def validate_patch(changes: list[dict], plan: dict) -> list[str]:
    """Reasons a PROPOSED patch must not be applied. Deterministic, before anything touches the tree: a
    secret in the new text, a file the plan declared out of scope. (Protected paths and sizes are already
    refused when the reply is parsed.) The writer is told the reasons and proposes again."""
    problems: list[str] = []
    forbidden = out_of_scope_paths(plan)
    for change in changes:
        path = change["file"]
        text = change.get("content") if "content" in change else change.get("replace", "")
        if contains_secret(text or ""):
            problems.append(f"{path}: the new text contains something shaped like a credential; secrets never go into code")
        for token in forbidden:
            if path == token or path.startswith(token + "/") or pathlib.PurePosixPath(path).name == token:
                problems.append(f"{path}: the plan declared {token!r} out of scope")
                break
    return problems


def policy_violations(from_sha: str, max_files: int = MAX_PATCH_FILES, max_lines: int = MAX_PATCH_LINES) -> list[str]:
    """Final deterministic gate on everything the agents added since `from_sha`, run before anything is
    published (the reviewers are models; this is not). Protected files, binary files, a credential-shaped
    string in an added line, a patch too large to be a reasoned change."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], capture_output=True, text=True).stdout

    violations: list[str] = []
    files = lines = 0
    for row in git("diff", "--numstat", f"{from_sha}..HEAD").splitlines():
        added, removed, path = row.split("\t", 2)
        files += 1
        if added == "-":
            violations.append(f"{path}: a binary file was added or changed")
        else:
            lines += int(added) + int(removed)
        if is_protected_path(path):
            violations.append(f"{path}: protected path (workflows, credentials, keys)")
    if files > max_files:
        violations.append(f"{files} files changed (limit {max_files})")
    if lines > max_lines:
        violations.append(f"{lines} lines changed (limit {max_lines})")
    added_text = "\n".join(ln[1:] for ln in git("diff", "-U0", f"{from_sha}..HEAD").splitlines()
                           if ln.startswith("+") and not ln.startswith("+++"))
    if contains_secret(added_text):
        violations.append("an added line contains something shaped like a credential")
    return violations
