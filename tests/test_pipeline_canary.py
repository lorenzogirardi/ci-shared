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


def test_a_commit_is_not_settled_before_its_required_checks_exist():
    """First live run: the agent's own check was done 20 seconds after the push, the CI workflow had not been
    created yet, and the scenario was judged with every required check 'missing'."""
    only_agent = [{"name": "pull-request / change", "status": "completed", "conclusion": "success"}]
    assert not c.settled("certified", only_agent, REQUIRED)
    assert c.settled("certified", only_agent + GREEN, REQUIRED)
    assert c.settled("abandoned", only_agent, REQUIRED)      # an abandonment does not wait for CI


def test_the_road_is_checked_too_not_only_where_it_ended():
    """A defective engine reached the expected files by a verdict of code_defect and a writer that rewrote the test.
    The scenario passed, because nothing looked at the verdict."""
    expect = {**INTENT, "report_contains": ["**test_defect**"], "report_not_contains": ["**code_defect**"]}
    good = "- `tests/test_canary.py::test_the_limit_is_ten`: **test_defect** (high): the stated intent redefines it"
    bad = "- `tests/test_canary.py::test_the_limit_is_ten`: **code_defect** (high): ... [downgraded: no verbatim quote]"
    assert judge(expect, report=good) == []
    problems = judge(expect, report=bad)
    assert any("does not say '**test_defect**'" in p for p in problems) and any("says '**code_defect**'" in p for p in problems)


def test_the_report_is_taken_from_the_engines_comment_and_only_from_a_trusted_account(monkeypatch):
    """Where the road check gets its words: the comment the engine posted, and only from an account we trust,
    so a drive-by comment cannot claim a road the run never took."""
    comments = [
        {"user": {"login": "drive-by"}, "body": "<!-- agent-pr -->\nI took the right road"},
        {"user": {"login": "the-bot"}, "body": "<!-- agent-pr -->\nthe road it really took"},
    ]

    def fake_gh(call, *a, **k):
        path = " ".join(call) if isinstance(call, (list, tuple)) else str(call)
        if "comments" in path:
            return comments
        if "pulls" in path:
            return {"number": 7, "head": {"sha": "abc"}}
        return {"check_runs": []}

    monkeypatch.setattr(c, "gh_json", fake_gh)
    state = c.state_of("owner/repo", 7, {"the-bot"})
    assert "the road it really took" in state["report"]
    assert "I took the right road" not in state["report"]


class TestPushIsRetried:
    """A candidate engine was not released because the third scenario's branch was answered with
    'remote rejected ... (failed)': the canary reported a failure that no agent had anything to do with."""

    def answers(self, monkeypatch, codes):
        calls = []

        def fake(*args, check=True):
            calls.append(args)
            code = codes[min(len(calls), len(codes)) - 1]
            return c.subprocess.CompletedProcess(args, code, "", "" if code == 0 else " ! [remote rejected] HEAD -> x (failed)")

        monkeypatch.setattr(c, "git", fake)
        monkeypatch.setattr(c.time, "sleep", lambda s: None)
        return calls

    def test_a_refused_push_is_tried_again_and_succeeds(self, monkeypatch):
        calls = self.answers(monkeypatch, [1, 1, 0])
        assert c.push("HEAD:refs/heads/x", "tok") is True and len(calls) == 3

    def test_it_gives_up_after_a_few_attempts_and_keeps_the_reason(self, monkeypatch):
        calls = self.answers(monkeypatch, [1])
        assert c.push("HEAD:refs/heads/x", "tok") is False and len(calls) == c.PUSH_ATTEMPTS
        assert "remote rejected" in c.LAST_PUSH_ERROR and "tok" not in c.LAST_PUSH_ERROR
