"""The risk level is the owner's dial, 1 to 10. At 1, the default, nothing ever asks: no person in
the loop. At 10 every certified change waits for the label. In between, a change waits when its risk
score, computed from facts, reaches 11 minus the level."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import agent_lib as lib  # noqa: E402
import agent_pipeline as ap  # noqa: E402

SHA = "a" * 40


class TestTheDial:
    @pytest.mark.parametrize("score", range(1, 11))
    def test_level_1_never_asks_and_level_10_always_asks(self, score):
        assert lib.needs_confirmation(1, score) is False
        assert lib.needs_confirmation(10, score) is True

    def test_in_between_it_asks_from_a_threshold(self):
        assert [s for s in range(1, 11) if lib.needs_confirmation(5, s)] == [6, 7, 8, 9, 10]
        assert [s for s in range(1, 11) if lib.needs_confirmation(2, s)] == [9, 10]


class TestTheScoreComesFromFacts:
    def test_a_small_change_in_one_place_is_the_lowest_risk(self):
        assert lib.risk_score(["app/canary.py"], 2)[0] == 1

    def test_what_the_service_is_built_or_deployed_with_raises_it(self):
        score, facts = lib.risk_score(["Dockerfile"], 1)
        assert score == 3 and "Dockerfile" in facts[-1]

    def test_size_spread_and_areas_add_up_and_stay_within_ten(self):
        files = [f"{area}/f{i}.py" for area in ("app", "tests", "helm", "docs") for i in range(5)]
        assert lib.risk_score(files, 5000)[0] == 10
        assert lib.risk_score(["app/a.py", "app/b.py", "app/c.py"], 150)[0] == 4      # 1 + size 2 + spread 1

    def test_three_areas_raise_the_score_but_staying_in_one_does_not(self):
        assert lib.risk_score(["a/x.py", "a/y.py", "a/z.py"], 10)[0] == 2
        assert lib.risk_score(["a/x.py", "b/y.py", "c/z.py"], 10)[0] == 3


def result(ask: bool) -> dict:
    return {"outcome": "clean", "commits": 0, "final_sha": SHA, "notes": [], "rounds": [],
            "risk": {"score": 7, "facts": ["120 changed line(s)"], "level": 5 if ask else 1, "ask": ask}}


class TestAskingHoldsTheMergeUntilTheLabel:
    def test_the_comment_always_shows_the_risk_and_asks_only_when_told_to(self):
        plain, asking = ap.build_change_comment(result(False), None), ap.build_change_comment(result(True), None)
        assert "**Risk 7/10**" in plain and "merges automatically" in plain and ap.CONFIRM_MARKER not in plain
        assert f"<!-- {ap.CONFIRM_MARKER}: {SHA} -->" in asking and ap.CONFIRMED_LABEL in asking
        assert f"<!-- {ap.CERT_MARKER}: {SHA} -->" in asking            # still certified: the label is all that is missing

    @pytest.mark.parametrize("labels, merges", [([], False), ([ap.CONFIRMED_LABEL], True)])
    def test_the_gate_waits_for_the_label(self, monkeypatch, labels, merges):
        pr = {"state": "open", "draft": False, "labels": [{"name": n} for n in labels],
              "head": {"sha": SHA, "repo": {"full_name": "o/r"}}, "base": {"ref": "main"}}
        comment = {"body": ap.build_change_comment(result(True), None), "user": {"login": "bot"}}
        monkeypatch.setattr(ap, "gh_json", lambda a: pr if a[0].endswith("/pulls/5") else [comment] if "comments" in a[0] else [])
        monkeypatch.setattr(ap, "try_merge", lambda *a, **k: "merged")
        args = type("A", (), {"repo": "o/r", "base_branch": "main", "trusted": "bot", "max_reverts": 3,
                              "required_checks": "", "merge_method": "squash", "poll_seconds": 0})()
        outcome = ap.merge_one(args, 5)
        assert (outcome == "merge gate: merged") is merges
        assert merges or ap.CONFIRMED_LABEL in outcome

    def test_without_the_marker_the_gate_is_as_before(self, monkeypatch):
        pr = {"state": "open", "draft": False, "labels": [], "head": {"sha": SHA, "repo": {"full_name": "o/r"}},
              "base": {"ref": "main"}}
        comment = {"body": ap.build_change_comment(result(False), None), "user": {"login": "bot"}}
        monkeypatch.setattr(ap, "gh_json", lambda a: pr if a[0].endswith("/pulls/5") else [comment] if "comments" in a[0] else [])
        monkeypatch.setattr(ap, "try_merge", lambda *a, **k: "merged")
        args = type("A", (), {"repo": "o/r", "base_branch": "main", "trusted": "bot", "max_reverts": 3,
                              "required_checks": "", "merge_method": "squash", "poll_seconds": 0})()
        assert ap.merge_one(args, 5) == "merge gate: merged"
