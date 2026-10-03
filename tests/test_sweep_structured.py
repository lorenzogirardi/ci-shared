"""The Renovate sweep can use the two independent reviewers, and a machine-
written autofix commit gets reviewed before it can merge."""

import argparse
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import agent_pipeline  # noqa: E402
import pr_review_sweep as sweep  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".ai").mkdir()


def args(**over):
    base = dict(ai_script="x", max_chars=1000, timeout=5, review_guidance_file="", structured_review=True)
    base.update(over)
    return argparse.Namespace(**base)


PR = {"number": 7, "title": "Update dependency x to v2", "body": "notes", "base": {"sha": "b" * 40}}


class TestStructuredVerdict:
    def test_clean_and_dirty_end_with_the_contract_line(self, monkeypatch):
        monkeypatch.setattr(agent_pipeline, "structured_review", lambda *a, **k: (True, "0 findings"))
        assert sweep.split_verdict(sweep.structured_verdict(args(), PR, "diff")) == (True, "0 findings")
        monkeypatch.setattr(agent_pipeline, "structured_review", lambda *a, **k: (False, "1 blocking"))
        assert sweep.split_verdict(sweep.structured_verdict(args(), PR, "diff")) == (False, "1 blocking")

    def test_unusable_reviewers_mean_no_verdict_so_nothing_merges(self, monkeypatch):
        monkeypatch.setattr(agent_pipeline, "structured_review", lambda *a, **k: None)
        assert sweep.structured_verdict(args(), PR, "diff") is None

    def test_guidance_file_reaches_the_reviewers(self, monkeypatch, tmp_path):
        seen = {}
        (tmp_path / "g.txt").write_text("runtime bumps are always critical")
        monkeypatch.setattr(agent_pipeline, "structured_review",
                            lambda caller, diff, title, body, guidance="": seen.update(g=guidance) or (True, "ok"))
        sweep.structured_verdict(args(review_guidance_file="g.txt"), PR, "diff")
        assert seen["g"] == "runtime bumps are always critical"

    def test_review_one_uses_it_only_when_the_flag_is_on(self, monkeypatch):
        monkeypatch.setattr(sweep, "_prepare_diff", lambda pr, a: "diff")
        monkeypatch.setattr(sweep, "structured_verdict", lambda a, pr, d: "STRUCTURED\nVERDICT: CLEAN")
        assert sweep.review_one(PR, args(), pathlib.Path("s")) == "STRUCTURED\nVERDICT: CLEAN"
        monkeypatch.setattr(sweep, "_call_model", lambda *a, **k: "SINGLE")
        monkeypatch.setattr(pathlib.Path, "write_text", lambda *a, **k: 0)
        assert sweep.review_one(PR, args(structured_review=False), pathlib.Path("s")) == "SINGLE"


class TestReviewAfterAutofix:
    @pytest.fixture(autouse=True)
    def _diff(self, monkeypatch):
        monkeypatch.setattr(sweep, "build_diff", lambda base, ref, out: out.write_text("diff"))

    def test_without_the_flag_there_is_no_extra_review(self):
        assert sweep.review_after_autofix(args(structured_review=False), PR) == (None, "")

    def test_clean_review_lets_the_merge_proceed(self, monkeypatch):
        monkeypatch.setattr(sweep, "structured_verdict", lambda a, pr, d: "all good\nVERDICT: CLEAN")
        assert sweep.review_after_autofix(args(), PR) == (None, "all good")

    def test_blocking_finding_stops_the_merge_and_is_reported(self, monkeypatch):
        monkeypatch.setattr(sweep, "structured_verdict", lambda a, pr, d: "bad call\nVERDICT: NEEDS_REVIEW")
        reason, text = sweep.review_after_autofix(args(), PR)
        assert reason == "post-fix review found blocking issues" and text == "bad call"

    def test_unavailable_reviewers_fail_closed(self, monkeypatch):
        monkeypatch.setattr(sweep, "structured_verdict", lambda a, pr, d: None)
        assert sweep.review_after_autofix(args(), PR)[0] == "post-fix review unavailable"
