"""Tests for the verdict parsing that gates auto-merge.

This is the load-bearing bit of the sweep: a wrong answer here either merges
a PR the model flagged, or blocks one it cleared. No network, no `gh`.
"""

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import pr_review_sweep  # noqa: E402
from pr_review_sweep import (  # noqa: E402
    apply_fix,
    checks_state,
    error_region,
    find_matching_paths,
    grep_matching_lines,
    list_directory,
    parse_find_request,
    parse_grep_request,
    parse_list_request,
    parse_read_request,
    resolve_readable_dir,
    resolve_readable_path,
    try_merge,
)


def test_error_region_keeps_the_error_and_drops_the_middle_noise():
    # Error early, then a long stretch of noise, so the error window and the
    # always-kept tail do not overlap and the elision is observable.
    log = "\n".join(
        ["ERROR: Cannot install -r requirements.txt", "conflicting dependencies"]
        + [f"noise {i}" for i in range(400)]
    )
    region = error_region(log, 50_000)
    assert "ERROR: Cannot install -r requirements.txt" in region
    assert "conflicting dependencies" in region
    assert "noise 200" not in region  # the middle is dropped
    assert "..." in region  # and the elision is visible


class TestAutofixGuardrails:
    """These are the rules that stand between model output and a pushed commit.

    Enforced in code rather than in the prompt: a prompt is a request, and the
    whole point here is that a wrong or adversarial reply must not become a
    commit.
    """

    def _reply(self, edits, explanation="because"):
        import json as _json
        return "```json\n" + _json.dumps({"explanation": explanation, "edits": edits}) + "\n```"


    def test_applies_a_unique_anchor(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "requirements.txt").write_text("fastapi==1\npydantic==2.11.7\n")
        changed, error = apply_fix([{"file": "requirements.txt",
                                     "find": "pydantic==2.11.7",
                                     "replace": "pydantic==2.13.4"}])
        assert error is None and changed == ["requirements.txt"]
        assert (tmp_path / "requirements.txt").read_text() == "fastapi==1\npydantic==2.13.4\n"

    def test_two_edits_to_the_same_file_both_survive(self, tmp_path, monkeypatch):
        """Real incident, found running against flask-test-api PR #118: a
        renamed import plus the renamed constructor call it enables, both in
        the same file, in the same round -- exactly what a rename-propagation
        fix needs (see AUTOFIX_SYSTEM's grep-after-rename guidance). Before
        this fix, each edit's replacement was computed from the file's
        ORIGINAL content independently, so writing them in order let the
        second edit silently overwrite the first instead of composing with
        it -- the import reverted to the old name while only the constructor
        call changed, leaving an undefined-name error identical to not
        having fixed the import at all."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "tools.py").write_text(
            "from mcp.server.fastmcp import FastMCP\n\nmcp = FastMCP(\"pytbak\")\n"
        )
        changed, error = apply_fix([
            {"file": "tools.py", "find": "from mcp.server.fastmcp import FastMCP",
             "replace": "from mcp.server.mcpserver import MCPServer"},
            {"file": "tools.py", "find": 'mcp = FastMCP("pytbak")',
             "replace": 'mcp = MCPServer("pytbak")'},
        ])
        assert error is None
        assert changed == ["tools.py"]  # one file touched, not one entry per edit
        assert (tmp_path / "tools.py").read_text() == (
            "from mcp.server.mcpserver import MCPServer\n\nmcp = MCPServer(\"pytbak\")\n"
        )

    def test_second_edit_to_a_file_is_checked_against_the_first_edits_result(self, tmp_path, monkeypatch):
        """An anchor that only becomes ambiguous (or only becomes valid)
        after an earlier edit in the same batch must be judged against the
        file as it stands mid-batch, not the original content."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "a.py").write_text("x = 1\n")
        changed, error = apply_fix([
            {"file": "a.py", "find": "x = 1", "replace": "x = 1\nx = 1"},
            {"file": "a.py", "find": "x = 1", "replace": "y = 2"},
        ])
        assert changed == []
        assert "appears 2 times" in error
        assert (tmp_path / "a.py").read_text() == "x = 1\n"  # nothing written on error

    def test_refuses_an_ambiguous_anchor(self, tmp_path, monkeypatch):
        """An anchor matching twice is how a 'small' edit hits the wrong line."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "requirements.txt").write_text("x==1\nx==1\n")
        changed, error = apply_fix([{"file": "requirements.txt",
                                     "find": "x==1", "replace": "x==2"}])
        assert changed == []
        assert "appears 2 times" in error
        assert (tmp_path / "requirements.txt").read_text() == "x==1\nx==1\n"

    def test_writes_nothing_when_any_edit_in_the_batch_is_invalid(self, tmp_path, monkeypatch):
        """All-or-nothing: a half-applied fix is worse than none."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "requirements.txt").write_text("a==1\n")
        changed, error = apply_fix([
            {"file": "requirements.txt", "find": "a==1", "replace": "a==2"},
            {"file": "Dockerfile", "find": "FROM x", "replace": "FROM y"},  # missing
        ])
        assert changed == [] and "Dockerfile" in error
        assert (tmp_path / "requirements.txt").read_text() == "a==1\n"


def test_ansi_codes_are_stripped_from_logs():
    """CI logs are full of colour codes; they waste prompt budget and obscure
    the text. (They also make `gh api` refuse to emit the body at all without
    --allow-escape-sequences, which is why job_log passes that flag.)"""
    coloured = "\x1b[31m##[error]it broke\x1b[0m\n\x1b[1mdetail\x1b[0m"
    assert pr_review_sweep._ANSI_RE.sub("", coloured) == "##[error]it broke\ndetail"


def test_error_region_always_keeps_the_tail():
    """A traceback usually sits right before the process exits."""
    log = "\n".join([f"line {i}" for i in range(500)])
    region = error_region(log, 10_000)
    assert "line 499" in region


def test_error_region_respects_the_char_budget():
    log = "\n".join([f"##[error]failure {i}" for i in range(5000)])
    assert len(error_region(log, 2_000)) <= 2_000


def _fake_check_runs(monkeypatch, runs):
    monkeypatch.setattr(pr_review_sweep, "gh_json", lambda _args: {"check_runs": runs})


def test_checks_state_green_when_all_completed_successfully(monkeypatch):
    _fake_check_runs(monkeypatch, [
        {"name": "checks", "status": "completed", "conclusion": "success"},
        # skipped/neutral are not failures — a conditional job that didn't run
        # must not block a merge forever.
        {"name": "optional", "status": "completed", "conclusion": "skipped"},
    ])
    assert checks_state("o/r", "sha")[0] == "green"


def test_checks_state_failing_blocks_merge(monkeypatch):
    _fake_check_runs(monkeypatch, [
        {"name": "checks", "status": "completed", "conclusion": "success"},
        {"name": "smoke", "status": "completed", "conclusion": "failure"},
    ])
    state, detail = checks_state("o/r", "sha")
    assert state == "failing"
    assert "smoke" in detail


def test_checks_state_pending_is_not_green(monkeypatch):
    """Merging mid-run would defeat the gate; the next sweep retries."""
    _fake_check_runs(monkeypatch, [
        {"name": "checks", "status": "in_progress", "conclusion": None},
    ])
    assert checks_state("o/r", "sha")[0] == "pending"


def test_no_checks_is_not_treated_as_green(monkeypatch):
    """A repo with no CI must not get unattended merges by default."""
    _fake_check_runs(monkeypatch, [])
    assert checks_state("o/r", "sha")[0] == "none"


def test_only_skipped_checks_is_not_green(monkeypatch):
    """Regression: this merged real PRs with nothing tested.

    A Renovate PR's only check run was the review workflow reporting
    `skipped` (it skips bot authors). "Nothing failed" read as green, so the
    sweep merged it without a single test having run.
    """
    _fake_check_runs(monkeypatch, [
        {"name": "review", "status": "completed", "conclusion": "skipped"},
    ])
    state, detail = checks_state("o/r", "sha")
    assert state == "none"
    assert "no check actually ran" in detail


def test_required_check_must_be_present_and_successful(monkeypatch):
    _fake_check_runs(monkeypatch, [
        {"name": "review", "status": "completed", "conclusion": "skipped"},
        {"name": "checks", "status": "completed", "conclusion": "success"},
    ])
    assert checks_state("o/r", "sha", ("checks",))[0] == "green"
    # A required check that never ran is pending, not green — and not failing
    # either, since a fresh push may not have registered it yet.
    assert checks_state("o/r", "sha", ("checks", "e2e"))[0] == "pending"


def test_required_check_failing_blocks_even_if_others_pass(monkeypatch):
    _fake_check_runs(monkeypatch, [
        {"name": "checks", "status": "completed", "conclusion": "failure"},
        {"name": "workflows", "status": "completed", "conclusion": "success"},
    ])
    state, detail = checks_state("o/r", "sha", ("checks", "workflows"))
    assert state == "failing"
    assert "checks" in detail


def test_required_check_skipped_is_not_success(monkeypatch):
    """Skipping the gate is not passing it, even when named as required."""
    _fake_check_runs(monkeypatch, [
        {"name": "checks", "status": "completed", "conclusion": "skipped"},
    ])
    assert checks_state("o/r", "sha", ("checks",))[0] == "failing"


class TestAutofixRead:
    """The model can ask to see a real file instead of guessing an API from
    an error message alone -- real incident: a migration guessed a renamed
    class's constructor kwarg wrong, because nothing showed it the class's
    actual signature."""

    def test_parses_a_read_request(self):
        assert parse_read_request('```json\n{"read": "app/mcp/tools.py"}\n```') == "app/mcp/tools.py"

    def test_bare_json_without_a_fence(self):
        assert parse_read_request('{"read": "setup.py"}') == "setup.py"

    @pytest.mark.parametrize("text", [
        "not json at all",
        '{"edits": []}',          # a real edit reply, not a read request
        '{"read": ""}',           # empty path
        '{"read": 3}',            # wrong type
    ])
    def test_non_read_replies_return_none(self, text):
        assert parse_read_request(text) is None

    def test_resolves_a_file_inside_the_repo_checkout(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "app" / "main.py"
        target.parent.mkdir()
        target.write_text("x = 1\n")
        resolved = resolve_readable_path("app/main.py")
        assert resolved == target.resolve()

    def test_refuses_a_path_outside_the_repo_and_site_packages(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        outside = tmp_path.parent / "secret.txt"
        outside.write_text("nope\n")
        assert resolve_readable_path(str(outside)) is None

    def test_refuses_traversal_that_escapes_the_repo_root(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        (tmp_path / "outside.txt").write_text("nope\n")
        monkeypatch.chdir(repo)
        assert resolve_readable_path("../outside.txt") is None

    def test_refuses_a_nonexistent_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert resolve_readable_path("does/not/exist.py") is None

    def test_resolves_an_installed_package_file(self, tmp_path, monkeypatch):
        """The whole point: reading the real, installed library source, not
        just files inside the repo checkout -- resolved from a cwd that
        has nothing to do with where the package actually lives."""
        monkeypatch.chdir(tmp_path)
        resolved = resolve_readable_path(pytest.__file__)
        assert resolved is not None

    def test_resolves_a_dotted_module_name_to_its_real_file(self, tmp_path, monkeypatch):
        """Real incident: the model correctly guessed the module name
        (mcp.server.mcpserver) but had no way to turn that into a real path
        on this specific runner, and its absolute-path guess missed."""
        monkeypatch.chdir(tmp_path)
        resolved = resolve_readable_path("json.decoder")
        assert resolved is not None
        assert resolved.name == "decoder.py"

    def test_an_unresolvable_module_name_is_not_a_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert resolve_readable_path("this.does.not.exist.anywhere") is None


class TestAutofixExplore:
    """find/grep/list -- so a reply can locate something real instead of
    inventing an absolute path or guessing which file holds what it needs.
    Real incident: a migration attempt guessed a GitHub-runner toolcache
    path for an installed library and it didn't exist on that runner."""

    def _reply(self, obj):
        import json as _json
        return "```json\n" + _json.dumps(obj) + "\n```"

    def test_parses_each_request_kind(self):
        assert parse_find_request(self._reply({"find": "mcpserver"})) == "mcpserver"
        assert parse_grep_request(self._reply({"grep": "class MCPServer"})) == "class MCPServer"
        assert parse_list_request(self._reply({"list": "app/mcp"})) == "app/mcp"
        # Each parser only recognizes its own key.
        assert parse_find_request(self._reply({"grep": "x"})) is None
        assert parse_grep_request(self._reply({"read": "x"})) is None
        assert parse_list_request(self._reply({"find": "x"})) is None

    def _isolate_roots(self, monkeypatch, tmp_path):
        """Scope find/grep to just tmp_path -- otherwise they also scan this
        machine's real site-packages/stdlib, which is real and correct
        behavior but makes a substring test non-deterministic across
        environments."""
        monkeypatch.setattr(pr_review_sweep, "readable_roots", lambda: [tmp_path.resolve()])

    def test_find_matches_by_filename_under_the_repo_checkout(self, tmp_path, monkeypatch):
        self._isolate_roots(monkeypatch, tmp_path)
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "mcpserver.py").write_text("x = 1\n")
        (tmp_path / "app" / "unrelated.py").write_text("y = 2\n")
        matches = find_matching_paths("mcpserver")
        assert len(matches) == 1
        assert matches[0].endswith("mcpserver.py")

    def test_find_skips_pycache(self, tmp_path, monkeypatch):
        self._isolate_roots(monkeypatch, tmp_path)
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "mcpserver.cpython-314.pyc").write_text("junk")
        assert find_matching_paths("mcpserver") == []

    def test_grep_finds_a_real_line_with_file_and_line_number(self, tmp_path, monkeypatch):
        self._isolate_roots(monkeypatch, tmp_path)
        target = tmp_path / "tools.py"
        target.write_text("first line\nclass MCPServer:\n    pass\n")
        hits = grep_matching_lines("class MCPServer")
        assert len(hits) == 1
        assert hits[0].startswith(f"{target}:2:")
        assert "class MCPServer" in hits[0]

    def test_grep_falls_back_to_literal_text_on_a_bad_regex(self, tmp_path, monkeypatch):
        self._isolate_roots(monkeypatch, tmp_path)
        (tmp_path / "a.py").write_text("weird(name\n")
        # "(" alone is an invalid regex; must not raise, must still match literally.
        hits = grep_matching_lines("(name")
        assert len(hits) == 1

    def test_resolve_readable_dir_accepts_the_repo_root(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "app").mkdir()
        assert resolve_readable_dir("app") == (tmp_path / "app").resolve()

    def test_resolve_readable_dir_refuses_outside_both_roots(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        (tmp_path / "outside").mkdir()
        monkeypatch.chdir(repo)
        assert resolve_readable_dir("../outside") is None

    def test_resolve_readable_dir_refuses_a_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "a_file.py").write_text("x = 1\n")
        assert resolve_readable_dir("a_file.py") is None

    def test_list_directory_marks_subdirectories(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "sub").mkdir()
        (tmp_path / "app" / "tools.py").write_text("x = 1\n")
        entries = list_directory("app")
        assert entries == ["sub/", "tools.py"]

    def test_list_directory_excludes_pycache(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "__pycache__").mkdir()
        (tmp_path / "app" / "tools.py").write_text("x = 1\n")
        assert list_directory("app") == ["tools.py"]

    def test_list_directory_of_a_nonexistent_path_is_none(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert list_directory("does/not/exist") is None


class TestMergeStartsThePushWorkflows:
    """A merge made with the job's own GITHUB_TOKEN starts no workflow on the base branch (no image build, no
    deploy check, no changelog, a blind guard). Real incident: PRs #183, #185 and #186 of flask-test-api."""


    def _merge(self, monkeypatch, update_token):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"], seen["env"] = cmd, kwargs.get("env")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(pr_review_sweep, "run", fake_run)
        monkeypatch.setattr(pr_review_sweep, "checks_state", lambda *a, **k: ("green", "ok"))
        monkeypatch.setenv("GH_TOKEN", "job-token")
        if update_token:
            monkeypatch.setenv("UPDATE_TOKEN", update_token)
        else:
            monkeypatch.delenv("UPDATE_TOKEN", raising=False)
        assert try_merge("o/r", 1, "sha", "squash", ("checks",)) == "merged"
        return seen

    def test_merges_with_the_push_token(self, monkeypatch):
        seen = self._merge(monkeypatch, "push-token")
        assert seen["cmd"][:3] == ["gh", "pr", "merge"]
        assert seen["env"]["GH_TOKEN"] == "push-token"

    def test_without_a_push_token_the_job_token_is_used_as_before(self, monkeypatch):
        assert self._merge(monkeypatch, "")["env"] is None
