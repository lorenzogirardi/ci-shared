"""Tests for the verdict parsing that gates auto-merge.

This is the load-bearing bit of the sweep: a wrong answer here either merges
a PR the model flagged, or blocks one it cleared. No network, no `gh`.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from pr_review_sweep import comment_body, split_verdict  # noqa: E402


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
    body = comment_body("AI Review", "findings", sha, is_clean=True, will_merge=True)
    assert "<!-- ai-review-sweep -->" in body
    # The sweep skips a PR when this exact line already matches its head SHA.
    assert f"<!-- reviewed-sha: {sha} -->" in body
    assert "## AI Review" in body
    assert "findings" in body


def test_comment_body_states_the_verdict_it_acted_on():
    sha = "a" * 40
    assert "clean — merging" in comment_body("h", "x", sha, is_clean=True, will_merge=True)
    assert "clean" in comment_body("h", "x", sha, is_clean=True, will_merge=False)
    assert "needs a human" in comment_body("h", "x", sha, is_clean=False, will_merge=False)
