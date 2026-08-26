"""Tests for the verdict parsing that gates auto-merge.

This is the load-bearing bit of the sweep: a wrong answer here either merges
a PR the model flagged, or blocks one it cleared. No network, no `gh`.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import pr_review_sweep  # noqa: E402
from pr_review_sweep import checks_state, comment_body, split_verdict  # noqa: E402


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


def test_verdict_marker_lets_the_next_sweep_retry_a_pending_merge():
    """A clean review whose CI hadn't finished must be retryable without
    re-running the model, so the marker records the verdict, not just the SHA."""
    sha = "b" * 40
    pending = comment_body("h", "x", sha, is_clean=True, merge_outcome="checks pending")
    assert "<!-- verdict: clean -->" in pending
    assert "<!-- verdict: clean -->" not in comment_body("h", "x", sha, is_clean=False)
