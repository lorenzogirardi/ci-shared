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
    MAX_FIND_CHARS,
    MAX_FIX_EDITS,
    _autofix_report,
    apply_fix,
    build_context,
    checks_state,
    comment_body,
    error_region,
    find_matching_paths,
    grep_matching_lines,
    list_directory,
    may_auto_merge,
    parse_find_request,
    parse_fix,
    parse_grep_request,
    parse_list_request,
    parse_read_request,
    resolve_readable_dir,
    resolve_readable_path,
    split_verdict,
    touches_workflow_files,
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


class TestRunVerify:
    """run_verify is what makes autofix agentic: it proves an edit works, in
    this job, before anything is pushed — instead of finding out a CI round
    trip later, the way the first version worked."""

    def test_empty_command_means_no_local_verification(self):
        # Preserves the original behavior when a consumer configures nothing.
        assert pr_review_sweep.run_verify("", timeout=5) == (True, "")

    def test_passing_command_reports_ok(self):
        ok, detail = pr_review_sweep.run_verify("exit 0", timeout=5)
        assert ok is True and detail == ""

    def test_failing_command_reports_the_real_output(self):
        ok, detail = pr_review_sweep.run_verify(
            "echo 'ResolutionImpossible: pydantic conflict' >&2; exit 1", timeout=5)
        assert ok is False
        assert "ResolutionImpossible" in detail

    def test_timeout_is_reported_as_a_failure_not_a_crash(self):
        ok, detail = pr_review_sweep.run_verify("sleep 5", timeout=1)
        assert ok is False
        assert "exceeded" in detail


class TestAutofixGuardrails:
    """These are the rules that stand between model output and a pushed commit.

    Enforced in code rather than in the prompt: a prompt is a request, and the
    whole point here is that a wrong or adversarial reply must not become a
    commit.
    """

    def _reply(self, edits, explanation="because"):
        import json as _json
        return "```json\n" + _json.dumps({"explanation": explanation, "edits": edits}) + "\n```"

    def test_accepts_a_well_formed_manifest_edit(self):
        parsed = parse_fix(self._reply(
            [{"file": "requirements.txt", "find": "pydantic==2.11.7",
              "replace": "pydantic==2.13.4"}]))
        assert parsed is not None
        edits, explanation = parsed
        assert edits[0]["file"] == "requirements.txt"
        assert explanation == "because"

    def test_reads_bare_json_without_a_fence(self):
        import json as _json
        parsed = parse_fix(_json.dumps({"explanation": "x", "edits": []}))
        assert parsed == ([], "x")

    @pytest.mark.parametrize("path", [
        ".github/workflows/pipeline.yml",       # push would be rejected regardless
        ".github/workflows/nested/reused.yml",  # same, one level deeper
        "../../etc/passwd",                     # traversal
        "/etc/passwd",                          # absolute
    ])
    def test_rejects_workflow_files_and_path_games(self, path):
        assert parse_fix(self._reply(
            [{"file": path, "find": "a", "replace": "b"}])) is None

    @pytest.mark.parametrize("path", [
        "requirements.txt",       # still fine, unrestricted now covers it too
        "app/main.py",            # application code -- allowed since the real
        "app/mcp/tools.py",       # test suite gates correctness, not file type
        "tests/test_storage.py",  # a fix can legitimately touch its own test
        "setup.py",
    ])
    def test_accepts_any_file_outside_workflows(self, path):
        """A dependency major bump can break at the API level, not just at
        install time (real incident: mcp 2.x renamed FastMCP to MCPServer,
        breaking app/mcp/tools.py) -- a pin revert alone can't fix that, so
        autofix is not restricted to manifests. What gates a wrong fix is the
        real test suite in required_checks, not a file-type allowlist."""
        assert parse_fix(self._reply(
            [{"file": path, "find": "a", "replace": "b"}])) is not None

    def test_rejects_malformed_or_oversized_replies(self):
        assert parse_fix("not json at all") is None
        assert parse_fix('```json\n{"edits": []}\n```') is None       # no explanation
        assert parse_fix('```json\n{"explanation": "x"}\n```') is None  # no edits
        # A no-op edit would produce an empty commit.
        assert parse_fix(self._reply(
            [{"file": "requirements.txt", "find": "a", "replace": "a"}])) is None
        # Too many edits stops "minimal fix" turning into a rewrite.
        assert parse_fix(self._reply(
            [{"file": "requirements.txt", "find": f"x{i}", "replace": "y"}
             for i in range(MAX_FIX_EDITS + 1)])) is None
        # An oversized anchor is not a targeted change.
        assert parse_fix(self._reply(
            [{"file": "requirements.txt", "find": "x" * (MAX_FIND_CHARS + 1),
              "replace": "y"}])) is None

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


class TestAutofixReporting:
    def test_a_pushed_fix_is_declared_as_unreviewed_machine_output(self):
        text = _autofix_report("pushed", "bumped pydantic")
        assert "not reviewed by a human" in text
        assert "bumped pydantic" in text
        # The comment must not imply the fix is validated — CI decides after.
        assert "merges only if" in text

    @pytest.mark.parametrize("outcome", ["declined", "rejected", "failed", "skipped"])
    def test_a_non_push_says_the_pr_still_needs_a_human(self, outcome):
        assert "needs a human" in _autofix_report(outcome, "some reason")


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


@pytest.mark.parametrize(
    "review, expected_clean",
    [
        ("All good.\n\nVERDICT: CLEAN", True),
        ("- [Critical] bad thing\n\nVERDICT: NEEDS_REVIEW", False),
        # The reason this is an exact last-line match and not a grep for
        # "[Critical]": a clean review that *mentions* the tag in prose must
        # still read as clean.
        ("No [Critical] issues found.\n\nVERDICT: CLEAN", True),
        # Fail safe: a model that ignores the format is never treated as clean.
        ("Looks fine to me.", False),
        ("", False),
        # Only the LAST line counts; the string appearing earlier means nothing.
        ("VERDICT: CLEAN appears mid-text\n\nVERDICT: NEEDS_REVIEW", False),
        ("ok\n\nVERDICT: CLEAN\n\n", True),
        # Near-misses are not the contract, so they are not clean.
        ("ok\n\nVERDICT: clean", False),
        ("ok\n\n**VERDICT: CLEAN**", False),
    ],
)
def test_split_verdict_clean_flag(review, expected_clean):
    is_clean, _ = split_verdict(review)
    assert is_clean is expected_clean


@pytest.mark.parametrize("tag", ["VERDICT: CLEAN", "VERDICT: NEEDS_REVIEW"])
def test_verdict_line_is_stripped_from_body(tag):
    _, body = split_verdict(f"Some findings here.\n\n{tag}")
    assert body == "Some findings here."
    assert "VERDICT" not in body


def test_unrecognized_last_line_is_left_in_body():
    _, body = split_verdict("Some findings here.")
    assert body == "Some findings here."


def test_comment_body_carries_marker_and_sha_for_dedup():
    sha = "0123456789abcdef0123456789abcdef01234567"
    body = comment_body("AI Review", "findings", sha, is_clean=True, merge_outcome="merged")
    assert "<!-- ai-review-sweep -->" in body
    # The sweep skips a PR when this exact line already matches its head SHA.
    assert f"<!-- reviewed-sha: {sha} -->" in body
    assert "## AI Review" in body
    assert "findings" in body


def test_comment_reports_what_happened_not_what_was_intended():
    sha = "a" * 40
    assert "clean — merged" in comment_body("h", "x", sha, is_clean=True, merge_outcome="merged")
    # A clean review whose merge GitHub refused (e.g. the diff touches
    # .github/workflows/) must not claim it merged.
    refused = comment_body("h", "x", sha, is_clean=True, merge_outcome="failed")
    assert "refused" in refused
    assert "merged" not in refused
    # auto_merge off: clean, with no claim either way.
    assert "clean" in comment_body("h", "x", sha, is_clean=True, merge_outcome="not attempted")
    assert "needs a human" in comment_body("h", "x", sha, is_clean=False)
    # CI is the gate: a clean review with red CI must say so, not claim a merge.
    red = comment_body("h", "x", sha, is_clean=True, merge_outcome="checks failing")
    assert "CI is failing" in red and "merged" not in red


def test_triage_comment_attributes_the_finding_to_ci_not_the_model():
    sha = "c" * 40
    body = comment_body("AI Review — CI failure", "what failed…", sha,
                        is_clean=False, merge_outcome="checks failing")
    assert "CI is failing" in body
    # "needs a human" would credit the model with a call CI actually made.
    assert "needs a human" not in body
    # Marked distinctly so a later sweep can tell a stale CI verdict from a
    # real review finding, and re-review once CI goes green at the same SHA.
    assert "<!-- verdict: ci-failure -->" in body


def test_review_finding_and_ci_failure_are_marked_differently():
    sha = "d" * 40
    review_finding = comment_body("h", "x", sha, is_clean=False)
    assert "<!-- verdict: needs-review -->" in review_finding
    assert "<!-- verdict: ci-failure -->" not in review_finding


def test_verdict_marker_lets_the_next_sweep_retry_a_pending_merge():
    """A clean review whose CI hadn't finished must be retryable without
    re-running the model, so the marker records the verdict, not just the SHA."""
    sha = "b" * 40
    pending = comment_body("h", "x", sha, is_clean=True, merge_outcome="checks pending")
    assert "<!-- verdict: clean -->" in pending
    assert "<!-- verdict: clean -->" not in comment_body("h", "x", sha, is_clean=False)


class TestAutoMergeAuthors:
    """Lets --authors sweep (review/autofix) a wider set of PRs than
    --auto-merge is allowed to touch -- e.g. the repo owner's own PRs get
    autofixed on red CI but are never merged unattended, only Renovate's are."""

    def test_no_restriction_when_auto_merge_authors_is_empty(self):
        """Unchanged behavior for callers that never set --auto-merge-authors."""
        assert may_auto_merge(True, set(), "anyone-at-all") is True

    def test_auto_merge_off_blocks_regardless_of_author(self):
        assert may_auto_merge(False, set(), "renovate[bot]") is False
        assert may_auto_merge(False, {"renovate[bot]"}, "renovate[bot]") is False

    def test_listed_author_is_allowed(self):
        assert may_auto_merge(True, {"renovate[bot]"}, "renovate[bot]") is True

    def test_unlisted_author_is_blocked(self):
        """The whole point: a human's PR can be reviewed/autofixed but never merged."""
        assert may_auto_merge(True, {"renovate[bot]"}, "lorenzogirardi") is False


class TestWorkflowFileMerge:
    """GITHUB_TOKEN can never merge a change to .github/workflows/ -- GitHub
    requires the `workflow` OAuth scope for that regardless of any
    `permissions:` block. Real incident: Renovate's own action-version bumps
    (actions/upload-artifact, step-security/harden-runner) reviewed clean and
    green, then GitHub refused the merge outright."""

    def _fake_diff(self, monkeypatch, files, returncode=0):
        result = subprocess.CompletedProcess(
            args=["gh"], returncode=returncode, stdout="\n".join(files), stderr="",
        )
        monkeypatch.setattr(pr_review_sweep, "run", lambda *a, **k: result)

    def test_detects_a_workflow_file_in_the_diff(self, monkeypatch):
        self._fake_diff(monkeypatch, ["requirements.txt", ".github/workflows/pipeline.yml"])
        assert touches_workflow_files("o/r", 1) is True

    def test_manifest_only_diff_is_not_flagged(self, monkeypatch):
        self._fake_diff(monkeypatch, ["requirements.txt", "pyproject.toml"])
        assert touches_workflow_files("o/r", 1) is False

    def test_a_failed_diff_call_does_not_block_the_merge_path(self, monkeypatch):
        """If we can't tell, don't invent a reason to skip -- fail open to the
        normal CI-gated path rather than stalling every PR on a `gh` hiccup."""
        self._fake_diff(monkeypatch, [], returncode=1)
        assert touches_workflow_files("o/r", 1) is False

    def test_try_merge_skips_workflow_file_prs_without_checking_ci(self, monkeypatch):
        monkeypatch.setattr(pr_review_sweep, "touches_workflow_files", lambda repo, number: True)

        def fail_if_called(*a, **k):
            raise AssertionError("must not check CI or attempt a merge for a workflow-file PR")

        monkeypatch.setattr(pr_review_sweep, "checks_state", fail_if_called)
        monkeypatch.setattr(pr_review_sweep, "run", fail_if_called)

        outcome = try_merge("o/r", 1, "sha", "squash")
        assert outcome == "workflow-file"

    def test_workflow_file_outcome_reads_as_clean_but_unmerged(self):
        body = comment_body("h", "x", "c" * 40, is_clean=True, merge_outcome="workflow-file")
        assert "needs a human to merge" in body
        # Still marked clean, so a later sweep retries the merge (cheaply, no
        # model call) instead of treating a workflow-only bump as a finding.
        assert "<!-- verdict: clean -->" in body


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


class TestBuildContext:
    """Real incident: keeping only the last round's result meant a model
    that read file A, then file B, then needed A again had no way to know
    it had already seen it -- it just re-read A a third time and burned
    the whole attempt budget without ever proposing an edit."""

    def test_all_rounds_appear_when_they_fit(self):
        history = ["Round 1: saw A", "Round 2: saw B", "Round 3: saw C"]
        context = build_context(history, budget=1000)
        assert "Round 1: saw A" in context
        assert "Round 2: saw B" in context
        assert "Round 3: saw C" in context

    def test_rounds_stay_in_original_order(self):
        history = ["Round 1: saw A", "Round 2: saw B"]
        context = build_context(history, budget=1000)
        assert context.index("Round 1") < context.index("Round 2")

    def test_drops_the_oldest_whole_entry_when_over_budget(self):
        history = ["Round 1: " + "x" * 50, "Round 2: " + "y" * 50, "Round 3: " + "z" * 50]
        # Each entry is 59 chars; 120 fits the last two but not all three.
        context = build_context(history, budget=120)
        assert "Round 1" not in context
        assert "Round 2" in context
        assert "Round 3" in context

    def test_never_truncates_a_single_entry_mid_content(self):
        """A half-shown file reads as a shorter, wrong file -- worse than
        not showing it at all."""
        history = ["Round 1: " + "x" * 500]
        context = build_context(history, budget=50)
        assert context == history[0]  # kept whole, even though it's over budget alone

    def test_empty_history_is_empty_context(self):
        assert build_context([]) == ""
