#!/usr/bin/env python3
"""What is left of the dependency sweep: helpers the graph and the scripts around it still use.

The sweep was a second engine for the dependency bot's pull requests (review, triage, repair, merge).
The graph in agent_pipeline.py does all of that now, and the sweep is gone. This file keeps what the
others import from it: reading GitHub through `gh`, the logs of failed checks, the diff of a change,
the comment and the merge, and the read-only look-ups (read, find, grep, list) with the rules for what
may be read. Its name is kept so that nothing importing it has to change.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import sysconfig
import time


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


def run(cmd: list[str], *, check: bool = True, capture: bool = True,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, env=env)


def push_token_env() -> dict[str, str] | None:
    """The environment for an action that must start the workflows of the branch it lands on.

    GitHub does not start workflows for events made with the job's own GITHUB_TOKEN, so a merge made
    with it produces a commit on the base branch that no push workflow ever sees: no image build, no
    deploy check, no changelog, and the guard on that branch stays blind. The push token (a PAT or
    app token) does not have that limit. Without one, the job token is used, as before."""
    token = os.environ.get("UPDATE_TOKEN")
    return {**os.environ, "GH_TOKEN": token} if token else None


def gh_json(args: list[str]) -> object:
    return json.loads(run(["gh", "api", *args]).stdout or "null")


def checks_state(repo: str, head_sha: str,
                 required: tuple[str, ...] = ()) -> tuple[str, str]:
    """('green'|'failing'|'pending'|'none', human-readable detail).

    A model reading a diff cannot know whether the code still builds, installs,
    or serves a request -- a CI run does. So the merge gate is the CI result;
    the review verdict only decides whether a human should look at it.

    `required` names the checks that must have actually SUCCEEDED. Without it
    this gate is close to vacuous, and was: an earlier version accepted "at
    least one check run exists and none failed", so a Renovate PR whose only
    check was the review workflow reporting `skipped` (it skips bot authors)
    counted as green and got merged with nothing tested at all. "Nothing
    objected" is not the same as "something verified".

    Absent a required check, the state is 'pending', not 'failing': on a fresh
    push the run may simply not have registered yet, and the next sweep will
    look again.
    """
    runs = gh_json([f"repos/{repo}/commits/{head_sha}/check-runs"]) or {}
    checks = runs.get("check_runs", []) if isinstance(runs, dict) else []
    if not checks:
        return "none", "no check runs reported for this commit"

    by_name = {c.get("name"): c for c in checks}

    if required:
        missing = [name for name in required if name not in by_name]
        incomplete = [name for name in required
                      if name in by_name and by_name[name].get("status") != "completed"]
        failing = [name for name in required
                   if name in by_name and by_name[name].get("status") == "completed"
                   and by_name[name].get("conclusion") != "success"]
        if failing:
            return "failing", f"required check(s) not successful: {', '.join(sorted(failing))}"
        if missing or incomplete:
            waiting = sorted(missing + incomplete)
            return "pending", f"required check(s) not finished: {', '.join(waiting)}"
        return "green", f"required check(s) passed: {', '.join(required)}"

    pending = [c["name"] for c in checks if c.get("status") != "completed"]
    if pending:
        return "pending", f"still running: {', '.join(sorted(pending)[:5])}"

    bad = [c["name"] for c in checks
           if c.get("conclusion") not in ("success", "neutral", "skipped")]
    if bad:
        return "failing", f"failing: {', '.join(sorted(bad)[:5])}"

    # Without a required list, insist on at least one genuine success: a lone
    # skipped check proves nothing was run, let alone that it passed.
    succeeded = [c["name"] for c in checks if c.get("conclusion") == "success"]
    if not succeeded:
        return "none", "no check actually ran (all skipped or neutral)"
    return "green", f"{len(succeeded)} check(s) passed"


def failed_check_runs(repo: str, head_sha: str) -> list[dict]:
    runs = gh_json([f"repos/{repo}/commits/{head_sha}/check-runs"]) or {}
    checks = runs.get("check_runs", []) if isinstance(runs, dict) else []
    return [c for c in checks
            if c.get("status") == "completed"
            and c.get("conclusion") not in ("success", "neutral", "skipped")]


def error_region(log: str, max_chars: int) -> str:
    """The part of a CI log worth sending: error lines plus their context.

    A job log is mostly setup noise; the failure is a handful of lines. Anchor
    on the error markers, keep a window around each, and always keep the tail —
    the traceback or resolver output that explains the failure usually sits
    right before the process exits.
    """
    lines = log.splitlines()
    markers = ("##[error]", "ERROR:", "Traceback (most recent call last)",
               "error:", "FAILED", "AssertionError")
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if any(m in line for m in markers):
            keep.update(range(max(0, i - 30), min(len(lines), i + 10)))
    keep.update(range(max(0, len(lines) - 60), len(lines)))

    out: list[str] = []
    previous = -1
    for i in sorted(keep):
        if previous >= 0 and i > previous + 1:
            out.append("...")
        out.append(lines[i])
        previous = i
    text = "\n".join(out)
    return text[-max_chars:] if len(text) > max_chars else text


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def job_log(repo: str, job_id: int) -> str | None:
    """A job's log, de-coloured, or None when there is none to fetch."""
    # --allow-escape-sequences is required, not cosmetic: CI logs are full of
    # ANSI colour codes and without the flag `gh api` refuses to emit the body
    # at all and exits non-zero, which reads exactly like "no log exists".
    result = run(
        ["gh", "api", "--allow-escape-sequences", f"repos/{repo}/actions/jobs/{job_id}/logs"],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return _ANSI_RE.sub("", result.stdout)


def collect_failure_logs(repo: str, head_sha: str, max_chars: int) -> str:
    """Error output from every failed job on this commit."""
    parts: list[str] = []
    budget = max_chars
    for check in failed_check_runs(repo, head_sha):
        job_id = check.get("id")
        name = check.get("name", "unknown")
        if not job_id or budget <= 0:
            continue
        # A check run posted by an app rather than an Actions job has no log
        # endpoint; that 404 is expected, not an error worth failing over.
        log = job_log(repo, job_id)
        if log is None:
            parts.append(f"### {name}\n(no job log available for this check)\n")
            continue
        region = error_region(log, min(budget, max_chars // 2))
        parts.append(f"### {name}\n```\n{region}\n```\n")
        budget -= len(region)
    return "\n".join(parts)


def build_diff(base_sha: str, local_ref: str, out_path: pathlib.Path) -> None:
    cmd = ["git", "diff", f"{base_sha}...{local_ref}", "--"]
    cmd += [f":(exclude){p}" for p in DIFF_EXCLUDES]
    result = run(cmd, check=False)
    text = result.stdout or ""
    if not text.strip():
        text = "No reviewable changes in this PR.\n"
    out_path.write_text(text)


# No file-type allowlist: this PR author is a dependency bot and the fix is
# gated on the real test suite passing before merge (required_checks), not on
# which files the model touched. The one hard exclusion is workflow files --
# not a scope choice but a fact about the push credential: GitHub rejects any
# write to .github/workflows/** without the separate `workflow` OAuth scope,
# so an edit there would fail at push time after burning a model call.
AUTOFIX_BLOCKED_PREFIX = ".github/workflows/"

MAX_FIND_CHARS = 2000
MAX_READ_CHARS = 20_000


_DOTTED_MODULE_RE = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)*$")     # `mcp.server.x`, and a top-level package: `mcp`


APP_PYTHON_ENV = "AGENT_APP_PYTHON"
_app_paths: dict[str, dict[str, str]] = {}


def app_python() -> str:
    """The interpreter the APPLICATION runs on. The engine has its own: a library the engine needs had no
    build for the Python an image moved to, and with one interpreter for both no agent job could start
    (flask-test-api #316, Python 3.15). The job names the application's in AGENT_APP_PYTHON; without it
    they are the same one, as in a local run."""
    return os.environ.get(APP_PYTHON_ENV) or sys.executable


def app_paths() -> dict[str, str]:
    """Where the application's interpreter keeps its libraries, asked of that interpreter once."""
    exe = app_python()
    if exe == sys.executable:
        return sysconfig.get_paths()
    if exe not in _app_paths:
        try:
            done = subprocess.run([exe, "-c", "import json, sys, sysconfig; p = sysconfig.get_paths(); p['syspath'] = sys.path; print(json.dumps(p))"],
                                  capture_output=True, text=True, timeout=30)
            _app_paths[exe] = json.loads(done.stdout) if done.returncode == 0 else {}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            _app_paths[exe] = {}
    return _app_paths[exe]


def app_env(env: dict[str, str]) -> dict[str, str]:
    """`env`, with the application's interpreter first on PATH: `python`, `pip` and what pip installs
    (pytest, flake8) are then the application's, whatever the engine runs on."""
    exe = app_python()
    if exe == sys.executable:
        return env
    folder = str(pathlib.Path(exe).parent)
    out = dict(env)
    out["PATH"] = folder + os.pathsep + out.get("PATH", "")
    # An interpreter from the runner's tool cache finds its own shared library through this.
    lib = str(pathlib.Path(folder).parent / "lib")
    out["LD_LIBRARY_PATH"] = lib + (os.pathsep + out["LD_LIBRARY_PATH"] if out.get("LD_LIBRARY_PATH") else "")
    for key in ("pythonLocation", "Python_ROOT_DIR", "Python3_ROOT_DIR", "VIRTUAL_ENV"):
        out.pop(key, None)      # they name the engine's interpreter
    home = pathlib.Path(folder).parent
    if (home / "pyvenv.cfg").is_file():
        out["VIRTUAL_ENV"] = str(home)
    return out


def readable_roots() -> list[pathlib.Path]:
    """Everywhere a read/find/grep/list request may look: this repo's
    checkout, the installed third-party packages of the application's
    interpreter, and its standard library -- so a break rooted in stdlib
    behavior can be read too, not just third-party dependencies."""
    roots = [pathlib.Path.cwd().resolve()]
    paths = app_paths()
    for key in ("purelib", "platlib", "stdlib", "platstdlib"):
        if paths.get(key):
            roots.append(pathlib.Path(paths[key]).resolve())
    return roots


def module_file(dotted: str) -> pathlib.Path | None:
    """The file of a module, found by its name in this repository and under the application's libraries,
    without importing anything: `a.b.c` is `a/b/c.py` or `a/b/c/__init__.py`. Importing would run the
    package's code in the engine's interpreter, which may not be the one it was installed for."""
    parts = dotted.split(".")
    for root in readable_roots():
        for candidate in (root.joinpath(*parts).with_suffix(".py"), root.joinpath(*parts, "__init__.py")):
            if candidate.is_file():
                return candidate
    return None


def _parse_json_reply(text: str) -> dict | None:
    """The model's own final answer, tolerant of it thinking out loud first.

    Real incident: a reply wrote one malformed JSON block, caught its own
    mistake mid-message ("Wait, that's wrong -- let me issue it correctly"),
    and then wrote a second, correct block. Taking the FIRST fenced block
    would parse the abandoned attempt instead of the correction. A model
    emitting more than one fenced block is almost always superseding an
    earlier mistake, not leaving a stray example after the real answer, so
    the LAST one wins.
    """
    matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    blob = matches[-1] if matches else text.strip()
    try:
        # strict=False: a model that writes a whole file into a JSON string almost always leaves the
        # line breaks in it raw. The content is intact; refusing it throws away a correct answer.
        data = json.loads(blob, strict=False)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _parse_single_key_request(text: str, key: str) -> str | None:
    """The string value of `key` from a single-key JSON reply, or None if
    this reply isn't that shape. Shared by read/find/grep/list requests --
    checked before parse_fix() so none of them are forced through the edits
    schema, where they'd just fail validation.
    """
    data = _parse_json_reply(text)
    if data is None:
        return None
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def parse_read_request(text: str) -> str | None:
    return _parse_single_key_request(text, "read")


def parse_find_request(text: str) -> str | None:
    return _parse_single_key_request(text, "find")


def parse_grep_request(text: str) -> str | None:
    return _parse_single_key_request(text, "grep")


def parse_list_request(text: str) -> str | None:
    return _parse_single_key_request(text, "list")


_SKIP_DIR_NAMES = {"__pycache__", ".git"}
_BINARY_SUFFIXES = {
    ".pyc", ".so", ".dylib", ".dll", ".png", ".jpg", ".jpeg", ".gif", ".ico",
    ".whl", ".zip", ".tar", ".gz", ".woff", ".woff2", ".ttf", ".pdf",
}


def find_matching_paths(pattern: str, max_results: int = 20) -> list[str]:
    """Filenames containing `pattern`, under the same roots a read may use.

    A real filesystem search, not a guess -- the failure this replaced was
    the model inventing a plausible-looking absolute path (a GitHub-hosted
    runner's toolcache layout, close but not exact) instead of being able to
    find out where a file actually is.
    """
    needle = pattern.lower()
    matches: list[str] = []
    for root in readable_roots():
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if _SKIP_DIR_NAMES & set(p.parts):
                continue
            if p.is_file() and needle in p.name.lower():
                matches.append(str(p))
                if len(matches) >= max_results:
                    return matches
    return matches


def grep_matching_lines(pattern: str, max_matches: int = 30, max_files: int = 8000) -> list[str]:
    """`path:line: text` for every line matching `pattern` (regex, falling
    back to a literal substring if it doesn't compile), under the same
    roots a read may use -- finding code by what it says, not just by a
    filename guess.
    """
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    results: list[str] = []
    scanned = 0
    for root in readable_roots():
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if _SKIP_DIR_NAMES & set(p.parts) or not p.is_file() or p.suffix in _BINARY_SUFFIXES:
                continue
            scanned += 1
            if scanned > max_files:
                return results
            try:
                text = p.read_text(errors="ignore")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    results.append(f"{p}:{lineno}: {line.strip()[:300]}")
                    if len(results) >= max_matches:
                        return results
    return results


def resolve_readable_dir(path: str) -> pathlib.Path | None:
    """Same containment rule as resolve_readable_path, for a directory."""
    try:
        target = pathlib.Path(path).resolve()
    except (OSError, ValueError):
        return None
    if not target.is_dir():
        return None
    for root in readable_roots():
        try:
            target.relative_to(root)
            return target
        except ValueError:
            continue
    return None


def list_directory(path: str, max_entries: int = 200) -> list[str] | None:
    """Immediate contents of a directory the model may look inside, or None
    if it doesn't exist or is outside the two allowed roots. Directories get
    a trailing "/" so a reply can tell them apart from files without a
    follow-up request."""
    resolved = resolve_readable_dir(path)
    if resolved is None:
        return None
    entries = sorted(
        p.name + "/" if p.is_dir() else p.name
        for p in resolved.iterdir()
        if p.name not in _SKIP_DIR_NAMES
    )
    return entries[:max_entries]


def resolve_readable_path(path: str) -> pathlib.Path | None:
    """A path the model may actually read, or None if it doesn't exist or
    resolves outside what's safe to show it.

    Also accepts a dotted Python module name (e.g. "mcp.server.mcpserver")
    and resolves it via `importlib` to that module's real file -- without
    this, the only way to read an installed library is to already know its
    exact absolute path on this specific runner, which is not something to
    expect a reply to guess correctly.

    Two roots only for the path form: this repo checkout, and the
    interpreter's installed packages. Anything else -- absolute paths
    elsewhere on the runner, traversal out of both roots -- returns None;
    `.resolve()` collapses `..` before the containment check runs, so a
    traversal attempt is judged on where it actually lands, not on the
    string itself.
    """
    if _DOTTED_MODULE_RE.match(path) and not pathlib.Path(path).is_file():
        found = module_file(path)
        if found is None and app_python() == sys.executable:
            # One interpreter for the engine and the application: what the name search misses (a
            # namespace package, an editable install) the import machinery still finds.
            try:
                spec = importlib.util.find_spec(path)
            except (ImportError, ValueError, ModuleNotFoundError):
                spec = None
            if spec is not None and spec.origin and spec.origin not in ("built-in", "frozen"):
                found = pathlib.Path(spec.origin)
        if found is not None:
            path = str(found)
    try:
        target = pathlib.Path(path).resolve()
    except (OSError, ValueError):
        return None
    if not target.is_file():
        return None
    for root in readable_roots():
        try:
            target.relative_to(root)
            return target
        except ValueError:
            continue
    return None


def apply_fix(edits: list[dict]) -> tuple[list[str], str | None]:
    """(files changed, error). Applies nothing at all if any edit is invalid.

    Edits to the same file compose, in order -- each is checked and applied
    against the result of the previous edit to that file, not against the
    file's original on-disk content independently. Real bug this fixes:
    reading fresh-per-edit meant two edits to the same file (e.g. a renamed
    import plus the renamed constructor call it enables) each computed their
    replacement from the ORIGINAL content, so writing them in edit order let
    the second write silently overwrite the first edit instead of layering
    on top of it -- an import fix and a call-site fix in the same round,
    exactly what a rename-propagation fix needs, would arrive as a diff with
    only the LAST edit surviving.
    """
    order: list[pathlib.Path] = []
    contents: dict[pathlib.Path, str] = {}
    for edit in edits:
        path = pathlib.Path(edit["file"])
        if path not in contents:
            if not path.is_file():
                return [], f"{edit['file']} does not exist"
            contents[path] = path.read_text()
            order.append(path)
        content = contents[path]
        occurrences = content.count(edit["find"])
        if occurrences != 1:
            # Ambiguous anchors are how a "small" edit silently changes the
            # wrong line; require the model to be exact instead.
            return [], f"{edit['file']}: anchor appears {occurrences} times, expected exactly 1"
        contents[path] = content.replace(edit["find"], edit["replace"], 1)

    for path in order:
        path.write_text(contents[path])
    return [str(p) for p in order], None


def wait_for_settled_checks(repo: str, head_sha: str, required: tuple[str, ...],
                             poll_seconds: int, poll_interval: int) -> tuple[str, str]:
    """Poll checks_state until it leaves 'none'/'pending', bounded by poll_seconds.

    Without this, a single sweep pass always defers a commit whose checks
    haven't registered or finished yet to the next scheduled run -- even one
    that would go green thirty seconds later, well within this job's own
    lifetime. Bounded so one slow PR can't hang the whole sweep.
    """
    deadline = time.monotonic() + poll_seconds
    state, detail = checks_state(repo, head_sha, required)
    while state in ("none", "pending") and time.monotonic() < deadline:
        time.sleep(poll_interval)
        state, detail = checks_state(repo, head_sha, required)
    return state, detail


def try_merge(repo: str, number: int, head_sha: str, method: str,
              required: tuple[str, ...] = (), *,
              poll_seconds: int = 0, poll_interval: int = 15) -> str:
    """Attempt the merge, gated on CI. Returns a merge_outcome string.

    CI is the gate, not the review verdict: a model reading a diff cannot know
    whether the code still installs and serves requests, and two bumps it
    waved through as clean broke main precisely there.
    """
    if poll_seconds:
        state, detail = wait_for_settled_checks(repo, head_sha, required, poll_seconds, poll_interval)
    else:
        state, detail = checks_state(repo, head_sha, required)
    if state != "green":
        print(f"Not merging PR #{number}: {detail}.")
        return {"failing": "checks failing",
                "pending": "checks pending",
                "none": "no checks"}[state]

    done = run(["gh", "pr", "merge", str(number), f"--{method}", "--repo", repo], check=False, env=push_token_env())
    if done.returncode == 0:
        print(f"Merged PR #{number} ({detail}).")
        return "merged"
    print(f"::warning::Review of PR #{number} was clean and CI green, but the merge was "
          f"refused: {done.stderr.strip()[:300]}")
    return "failed"


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
