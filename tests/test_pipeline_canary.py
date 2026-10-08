"""The canary's judgement is plain code: these are the outcomes it must tell apart."""

import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "scripts"))

import pipeline_canary as c  # noqa: E402

SHA = "59183e7a681f5d029e27ba2d0e7b281042e490a5"
REQUIRED = ("checks", "integration")
GREEN = [{"name": "checks", "status": "completed", "conclusion": "success"},
         {"name": "integration", "status": "completed", "conclusion": "success"}]
INTENT = {"verdict": "certified", "allowed_paths": ["app/canary.py", "tests/"], "changed": ["tests/"],
          "contains": [{"file": "app/canary.py", "text": "CANARY_LIMIT = 20"}]}


def judge(expect, **over):
    args = {"verdict": "certified", "checks": GREEN, "files": ["app/canary.py", "tests/test_canary.py"],
            "authors": ["pipeline canary", "ci-shared agents"],
            "content_of": lambda f: "CANARY_LIMIT = 20\n\ndef within(value):\n    return value <= CANARY_LIMIT\n",
            "required_checks": REQUIRED}
    args.update(over)
    return c.evaluate(expect, **args)


def test_the_expected_outcome_has_no_problems():
    assert judge(INTENT) == []


def test_the_application_rewritten_to_satisfy_a_test_is_caught():
    """flask-test-api #192 and #193: app/main.py and CLAUDE.md changed by a pull request about one limit."""
    problems = judge(INTENT, files=["app/canary.py", "tests/test_canary.py", "app/main.py", "CLAUDE.md"])
    assert any("app/main.py" in p and "CLAUDE.md" in p for p in problems)


def test_a_missing_verdict_or_an_abandonment_is_caught():
    """#195 was abandoned over an ellipsis; #197 was left with no verdict at all."""
    assert any("expected 'certified'" in p for p in judge(INTENT, verdict="abandoned"))
    assert any("'none'" in p for p in judge(INTENT, verdict=""))


def test_a_certification_on_red_checks_is_caught():
    red = [GREEN[0], {"name": "integration", "status": "completed", "conclusion": "failure"}]
    assert any("integration=failure" in p for p in judge(INTENT, checks=red))
    assert any("integration=missing" in p for p in judge(INTENT, checks=[GREEN[0]]))


def test_the_test_that_had_to_be_updated_and_was_not_is_caught():
    """#190: certified without the test the change needed."""
    assert any("tests/" in p for p in judge(INTENT, files=["app/canary.py"]))


def test_code_that_had_to_be_restored_and_was_not_is_caught():
    repair = {"verdict": "certified", "unchanged": ["tests/test_canary.py"], "agent_commit": True,
              "contains": [{"file": "app/canary.py", "text": "value <= CANARY_LIMIT"}],
              "not_contains": [{"file": "app/canary.py", "text": "value < CANARY_LIMIT"}]}
    assert judge(repair, files=["app/canary.py"]) == []
    weakened = judge(repair, files=["app/canary.py", "tests/test_canary.py"], authors=["pipeline canary"],
                     content_of=lambda f: "return value < CANARY_LIMIT")
    assert len(weakened) == 4      # the test was touched, no agent commit, the fix is missing, the bug is still there


def test_an_expected_abandonment_does_not_ask_for_green_checks():
    assert judge({"verdict": "abandoned"}, verdict="abandoned", checks=[]) == []


class TestVerdictAndSettling:
    def comment(self, kind, sha=SHA, login="github-actions[bot]"):
        return {"body": f"<!-- agent-pr -->\n<!-- agent-{kind}: {sha} -->", "user": {"login": login}}

    def test_the_verdict_is_read_for_exactly_this_commit_and_only_from_a_trusted_account(self):
        trusted = {"github-actions[bot]"}
        assert c.verdict_of([self.comment("certified")], SHA, trusted) == "certified"
        assert c.verdict_of([self.comment("abandoned")], SHA, trusted) == "abandoned"
        assert c.verdict_of([self.comment("certified", sha="a" * 40)], SHA, trusted) == ""
        assert c.verdict_of([self.comment("certified", login="someone")], SHA, trusted) == ""

    def test_a_commit_is_settled_only_with_a_verdict_and_no_check_still_running(self):
        running = [GREEN[0], {"name": "image", "status": "in_progress", "conclusion": None}]
        assert c.settled("certified", GREEN)
        assert not c.settled("", GREEN)
        assert not c.settled("certified", running)
        assert not c.settled("certified", [])


class TestVariants:
    SCENARIO = {"name": "intent", "variants": [
        {"title": "raise", "edits": [{"file": "app/canary.py", "find": "CANARY_LIMIT = 10", "replace": "CANARY_LIMIT = 20"}]},
        {"title": "lower", "edits": [{"file": "app/canary.py", "find": "CANARY_LIMIT = 20", "replace": "CANARY_LIMIT = 10"}]}]}

    def test_the_variant_that_fits_the_code_is_chosen_so_the_scenario_alternates(self):
        assert c.pick_variant(self.SCENARIO, lambda f: "CANARY_LIMIT = 10\n")["title"] == "raise"
        assert c.pick_variant(self.SCENARIO, lambda f: "CANARY_LIMIT = 20\n")["title"] == "lower"
        assert c.pick_variant(self.SCENARIO, lambda f: "CANARY_LIMIT = 20\n")["name"] == "intent"

    def test_no_variant_fits(self):
        assert c.pick_variant(self.SCENARIO, lambda f: "something else") is None
        assert c.pick_variant(self.SCENARIO, lambda f: None) is None


def test_the_report_names_what_failed():
    text = c.render([{"name": "repair", "pr": 7, "problems": []}, {"name": "intent", "pr": 8, "problems": ["verdict is 'none'"]}])
    assert "repair: passed (PR #7)" in text and "intent: FAILED (PR #8)" in text and "verdict is 'none'" in text
