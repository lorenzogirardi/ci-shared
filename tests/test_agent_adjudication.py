"""Who is wrong, the code or the test? Deterministic evidence, the rules that
override the model, the guard against weakening tests, and the loop that
routes each verdict -- all with real pytest runs in throwaway git repos and a
scripted model."""

import json
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import agent_lib as lib  # noqa: E402
import agent_pipeline as ap  # noqa: E402


def fence(obj) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


def git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True, check=True).stdout.strip()


def commit_all(msg):
    git("add", "-A")
    git("commit", "-qm", msg)
    return git("rev-parse", "HEAD")


GOOD = "def add(a, b):\n    return a + b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n    assert add(0, 0) == 0\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        git(*cmd)
    (tmp_path / ".gitignore").write_text(".ai/\n.shared/\n__pycache__/\n.pytest_cache/\n")
    (tmp_path / "calc.py").write_text(GOOD)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(TEST)
    base = commit_all("init")
    git("checkout", "-qb", "feature")
    return base


class TestPaths:
    def test_failed_test_ids_are_parsed(self):
        out = "FAILED tests/test_a.py::test_x[asyncio] - assert 1 == 2\nERROR tests/b.py::test_y - boom\nFAILED tests/test_a.py::test_x[asyncio] - dup"
        assert lib.parse_failed_tests(out) == ["tests/test_a.py::test_x[asyncio]", "tests/b.py::test_y"]
        assert lib.parse_failed_tests("flake8: E9 something") == []

    def test_path_kinds(self):
        assert lib.is_test_path("tests/test_x.py") and lib.is_test_path("app/tests/helper.py") and lib.is_test_path("conftest.py")
        assert not lib.is_test_path("app/main.py")
        assert lib.is_source_path("app/main.py") and not lib.is_source_path("tests/test_x.py")
        assert not lib.is_source_path("docs/x.py") and not lib.is_source_path("README.md")


class TestWeakeningGuard:
    def write(self, text):
        pathlib.Path("tests/test_calc.py").write_text(text)

    def test_legitimate_update_is_allowed(self, repo):
        self.write(TEST.replace("== 3", "== 4"))
        assert lib.weakened_tests("HEAD") == []

    def test_adding_tests_is_allowed(self, repo):
        self.write(TEST + "\n\ndef test_more():\n    assert add(2, 2) == 4\n")
        assert lib.weakened_tests("HEAD") == []

    @pytest.mark.parametrize("text,needle", [
        ("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", "fewer assertions"),
        ("from calc import add\n", "fewer tests"),
        (TEST.replace("def test_add", "@pytest.mark.skip\ndef test_add"), "skip/xfail"),
    ])
    def test_weakening_is_caught(self, repo, text, needle):
        self.write(text)
        assert any(needle in v for v in lib.weakened_tests("HEAD"))

    def test_deleting_a_test_file_is_caught(self, repo):
        pathlib.Path("tests/test_calc.py").unlink()
        assert any("deleted" in v for v in lib.weakened_tests("HEAD"))


class TestEvidence:
    def test_regression_flaky_preexisting_and_new_test_are_told_apart(self, repo):
        base = repo
        # preexisting: fails on base too -- committed on main before branching is not possible now,
        # so build it on the branch point: recreate base with an always-failing test.
        git("checkout", "-q", "main")
        pathlib.Path("tests/test_pre.py").write_text("def test_pre():\n    assert False\n")
        base = commit_all("preexisting failure")
        git("checkout", "-qb", "feature2")
        pathlib.Path("calc.py").write_text("def add(a, b):\n    return a - b\n")                       # regression
        pathlib.Path("tests/test_flaky.py").write_text(
            "import pathlib\n\n\ndef test_flaky():\n    p = pathlib.Path('.count')\n    n = int(p.read_text()) if p.exists() else 0\n"
            "    p.write_text(str(n + 1))\n    assert n % 2 == 1\n")                                   # fails then passes
        pathlib.Path("tests/test_new.py").write_text("def test_new():\n    assert False\n")            # added by this change
        commit_all("change")
        ids = ["tests/test_calc.py::test_add", "tests/test_pre.py::test_pre", "tests/test_flaky.py::test_flaky",
               "tests/test_new.py::test_new"]
        ev = lib.gather_evidence(ids, base)
        assert ev["tests/test_calc.py::test_add"]["hint"] == "regression"
        assert ev["tests/test_pre.py::test_pre"]["hint"] == "preexisting"
        assert ev["tests/test_flaky.py::test_flaky"]["hint"] == "flaky"
        assert ev["tests/test_new.py::test_new"]["hint"] == "new_test"

    def test_tests_that_cannot_run_here_are_unreproducible(self, repo):
        pathlib.Path("tests/test_live.py").write_text("import pytest\n\n\ndef test_live():\n    pytest.skip('needs a live service')\n")
        commit_all("live")
        ev = lib.gather_evidence(["tests/test_live.py::test_live"], repo)
        assert ev["tests/test_live.py::test_live"]["hint"] == "unreproducible"

    def test_new_tests_that_pass_without_the_change_are_not_discriminating(self, repo):
        pathlib.Path("tests/test_trivial.py").write_text("def test_true():\n    assert True\n")
        pathlib.Path("tests/test_real.py").write_text("from calc import add\n\n\ndef test_real():\n    assert add(1, 2) == 4\n")
        commit_all("tests")
        assert lib.discriminating(repo, ["tests/test_trivial.py"]) == (0, 1)
        failed, _ = lib.discriminating(repo, ["tests/test_real.py"])
        assert failed == 1


class TestRules:
    INTENT = "Make add multiply its arguments\nadd(2, 3) returns 6"

    def verdict(self, **over):
        v = {"test": "t::a", "classification": "test_defect", "confidence": "high",
             "intent_evidence": "Make add multiply its arguments", "reason": "r"}
        v.update(over)
        return v

    def test_a_verbatim_quote_of_the_intent_lets_the_test_be_changed(self):
        out = ap.apply_rules([self.verdict()], ["t::a"], {}, self.INTENT)
        assert out[0]["classification"] == "test_defect"

    @pytest.mark.parametrize("quote", ["", "short", "Make add subtract its arguments"])
    def test_no_valid_quote_means_the_code_is_wrong(self, quote):
        out = ap.apply_rules([self.verdict(intent_evidence=quote)], ["t::a"], {}, self.INTENT)
        assert out[0]["classification"] == "code_defect" and "downgraded" in out[0]["reason"]

    def test_quote_matching_ignores_case_and_whitespace(self):
        out = ap.apply_rules([self.verdict(intent_evidence="make  ADD multiply\nits arguments")], ["t::a"], {}, self.INTENT)
        assert out[0]["classification"] == "test_defect"

    def test_evidence_overrides_the_model(self):
        v = self.verdict(classification="code_defect")
        assert ap.apply_rules([v], ["t::a"], {"t::a": {"hint": "flaky"}}, "")[0]["classification"] == "environment"
        assert ap.apply_rules([v], ["t::a"], {"t::a": {"hint": "preexisting"}}, "")[0]["classification"] == "preexisting"

    def test_a_test_without_a_verdict_defaults_to_code_defect(self):
        assert ap.apply_rules([], ["t::a"], {}, "")[0]["classification"] == "code_defect"

    def test_parse_verdicts_drops_invented_tests_and_classes(self):
        data = {"verdicts": [self.verdict(), self.verdict(test="invented"), self.verdict(classification="maybe")]}
        assert [v["test"] for v in ap.parse_verdicts(data, ["t::a"])] == ["t::a"]


class FakeCaller:
    def __init__(self, replies):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.log = []
        self.max_chars = 1000
        self.usage = []

    def call(self, role, system, user):
        self.log.append((role, user))
        queue = self.replies.get(role)
        if not queue:
            return None
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def total_cost_usd(self):
        return 0.0

    def cost_by_role(self):
        return {}


CLEAN = fence({"summary": "ok", "findings": []})
NODOC = fence({"explanation": "none", "changes": []})
NOTESTS = fence({"explanation": "covered", "changes": []})


def run(repo_base, caller, title, start="verify", failure="", max_verify_retries=2):
    plan = ap.derived_plan(title, "")
    rt = ap.Runtime(caller, repo_base, f"{sys.executable} -m pytest -q tests", 120, 3, max_verify_retries, 4, "", "", 0,
                    start=start, fix_kind="fix")
    return rt, ap.build_graph(rt).invoke(
        {"plan": plan, "iteration": 1, "verify_attempts": 0, "notes": [], "rounds": [], "failure_output": failure},
        config={"recursion_limit": 300})


def reviewers_ok():
    return {"reviewer-correctness": [CLEAN], "reviewer-security": [CLEAN], "final-reviewer": [CLEAN], "doc-reviewer": [NODOC]}


def subjects(base):
    return git("log", "--format=%s", f"{base}..HEAD").splitlines()


class TestLoopRouting:
    def break_code(self):
        pathlib.Path("calc.py").write_text("def add(a, b):\n    return a * b\n")
        commit_all("feat: change add")

    def fix_code(self):
        return fence({"explanation": "restore the sum", "changes": [{"file": "calc.py", "find": "a * b", "replace": "a + b"}]})

    def test_code_defect_goes_to_the_writer_with_the_verdict(self, repo):
        self.break_code()
        caller = FakeCaller({
            "failure-adjudicator": [fence({"verdicts": [{"test": "tests/test_calc.py::test_add", "classification": "code_defect",
                                                           "confidence": "high", "intent_evidence": "", "reason": "sum expected"}]})],
            "writer": [self.fix_code()], "test-steward": [NOTESTS], **reviewers_ok()})
        _, final = run(repo, caller, "feat: change add")
        assert final["outcome"] == "converged"
        writer_prompt = [u for r, u in caller.log if r == "writer"][0]
        assert "ADJUDICATION" in writer_prompt and "Tests are the specification" in writer_prompt
        assert pathlib.Path("tests/test_calc.py").read_text() == TEST     # the test was never touched

    def test_test_defect_with_a_quote_updates_the_test_and_keeps_the_code(self, repo):
        self.break_code()
        title = "make add multiply its arguments"
        updated = fence({"explanation": "add now multiplies", "changes": [
            {"file": "tests/test_calc.py", "find": "assert add(1, 2) == 3", "replace": "assert add(1, 2) == 2"}]})
        caller = FakeCaller({
            "failure-adjudicator": [fence({"verdicts": [{"test": "tests/test_calc.py::test_add", "classification": "test_defect",
                                                           "confidence": "high", "intent_evidence": "make add multiply its arguments",
                                                           "reason": "the intent redefines add"}]})],
            "test-steward": [updated, NOTESTS], **reviewers_ok()})
        _, final = run(repo, caller, title)
        assert final["outcome"] == "converged"
        assert "== 2" in pathlib.Path("tests/test_calc.py").read_text()
        assert pathlib.Path("calc.py").read_text().count("a * b") == 1       # the code change stands
        assert any(s.startswith("test(agent)") for s in subjects(repo))
        assert "writer" not in [r for r, _ in caller.log]

    def test_a_test_the_steward_rewrote_and_that_fails_never_changes_the_application(self, repo):
        """flask-test-api PR #192: asked to move a test to the new intended behaviour, the steward also asserted
        something the application did not do; the writer then removed an app-wide handler to make it pass."""
        self.break_code()
        code_before = pathlib.Path("calc.py").read_text()
        verdict = fence({"verdicts": [{"test": "tests/test_calc.py::test_add", "classification": "test_defect", "confidence": "high",
                                       "intent_evidence": "make add multiply its arguments", "reason": "the intent redefines add"}]})
        over = fence({"explanation": "x", "changes": [
            {"file": "tests/test_calc.py", "find": "assert add(1, 2) == 3", "replace": "assert add(1, 2) == 2\n    assert add(2, 2) == 5"}]})
        right = fence({"explanation": "add now multiplies", "changes": [
            {"file": "tests/test_calc.py", "find": "assert add(1, 2) == 3", "replace": "assert add(1, 2) == 2"}]})
        bend = fence({"explanation": "make it pass", "changes": [{"file": "calc.py", "find": "a * b", "replace": "5"}]})
        caller = FakeCaller({"failure-adjudicator": [verdict], "test-steward": [over, right, NOTESTS], "writer": [bend], **reviewers_ok()})
        _, final = run(repo, caller, "make add multiply its arguments")
        assert final["outcome"] == "converged"
        assert pathlib.Path("calc.py").read_text() == code_before
        assert "writer" not in [r for r, _ in caller.log]
        assert "== 5" not in pathlib.Path("tests/test_calc.py").read_text()
        second = [u for r, u in caller.log if r == "test-steward"][1]
        assert "FAILED against this change" in second

    def test_test_defect_without_a_quote_is_downgraded_and_the_code_is_fixed(self, repo):
        self.break_code()
        caller = FakeCaller({
            "failure-adjudicator": [fence({"verdicts": [{"test": "tests/test_calc.py::test_add", "classification": "test_defect",
                                                           "confidence": "high", "intent_evidence": "it seems fine",
                                                           "reason": "looks reasonable"}]})],
            "writer": [self.fix_code()], "test-steward": [NOTESTS], **reviewers_ok()})
        _, final = run(repo, caller, "feat: change add")
        assert final["outcome"] == "converged"
        assert pathlib.Path("tests/test_calc.py").read_text() == TEST
        assert final["adjudications"][0]["verdicts"][0]["classification"] == "code_defect"

    def test_a_steward_that_guts_the_test_is_refused_and_the_code_is_changed(self, repo):
        self.break_code()
        gut = fence({"explanation": "relax", "changes": [{"file": "tests/test_calc.py", "find": "    assert add(0, 0) == 0\n", "replace": ""}]})
        caller = FakeCaller({
            "failure-adjudicator": [fence({"verdicts": [{"test": "tests/test_calc.py::test_add", "classification": "test_defect",
                                                           "confidence": "high", "intent_evidence": "make add multiply its arguments",
                                                           "reason": "r"}]})],
            "test-steward": [gut, NOTESTS], "writer": [self.fix_code()], **reviewers_ok()})
        _, final = run(repo, caller, "make add multiply its arguments")
        assert final["outcome"] == "converged"
        assert pathlib.Path("tests/test_calc.py").read_text() == TEST
        assert any("weakens the tests" in n for n in final["notes"])

    def test_a_flaky_failure_is_rerun_not_blamed_on_anyone(self, repo):
        pathlib.Path("tests/test_flaky.py").write_text("def test_f():\n    assert True\n")
        commit_all("add flaky test")
        flag = pathlib.Path(".ok").resolve()
        script = f'if [ -f {flag} ]; then exit 0; else touch {flag}; echo "FAILED tests/test_flaky.py::test_f - boom"; exit 1; fi'
        plan = ap.derived_plan("a change", "")
        caller = FakeCaller({"failure-adjudicator": [fence({"verdicts": []})], "test-steward": [NOTESTS], **reviewers_ok()})
        rt = ap.Runtime(caller, repo, script, 60, 3, 2, 4, "", "", 0, start="verify", fix_kind="fix")
        final = ap.build_graph(rt).invoke({"plan": plan, "iteration": 1, "verify_attempts": 0, "notes": [], "rounds": []},
                                          config={"recursion_limit": 300})
        assert final["outcome"] == "converged"
        assert final["adjudications"][0]["verdicts"][0]["classification"] == "environment"
        assert "writer" not in [r for r, _ in caller.log]

    def test_entering_from_a_failed_ci_run_starts_at_the_adjudication(self, repo):
        self.break_code()
        failure = "FAILED tests/test_calc.py::test_add - assert 2 == 3"
        caller = FakeCaller({
            "failure-adjudicator": [fence({"verdicts": [{"test": "tests/test_calc.py::test_add", "classification": "code_defect",
                                                           "confidence": "high", "intent_evidence": "", "reason": "r"}]})],
            "writer": [self.fix_code()], "test-steward": [NOTESTS], **reviewers_ok()})
        _, final = run(repo, caller, "feat: change add", start="ci", failure=failure)
        assert final["outcome"] == "converged"
        assert [r for r, _ in caller.log][0] == "failure-adjudicator"

    def test_lint_style_failures_need_no_verdict(self, repo):
        pathlib.Path("calc.py").write_text("def add(a, b):\n    return a +\n")      # syntax error, no FAILED line
        commit_all("feat: broken")
        fix = fence({"explanation": "fix syntax", "changes": [{"file": "calc.py", "find": "return a +\n", "replace": "return a + b\n"}]})
        caller = FakeCaller({"writer": [fix], "test-steward": [NOTESTS], **reviewers_ok()})
        _, final = run(repo, caller, "feat: broken")
        assert final["outcome"] == "converged" and "failure-adjudicator" not in [r for r, _ in caller.log]


class TestProactiveSteward:
    def test_new_application_code_gets_tests(self, repo):
        pathlib.Path("calc.py").write_text(GOOD + "\n\ndef sub(a, b):\n    return a - b\n")
        commit_all("feat: add sub")
        new_test = fence({"explanation": "cover sub", "changes": [{"file": "tests/test_sub.py", "content":
                          "from calc import sub\n\n\ndef test_sub():\n    assert sub(5, 3) == 2\n"}]})
        caller = FakeCaller({"test-steward": [new_test, NOTESTS], **reviewers_ok()})
        _, final = run(repo, caller, "feat: add sub")
        assert final["outcome"] == "converged" and pathlib.Path("tests/test_sub.py").is_file()
        assert any(s.startswith("test(agent)") for s in subjects(repo))
        assert not any("also pass without the change" in n for n in final["notes"])   # it fails on the base: it tests the change

    def test_a_trivially_passing_new_test_is_called_out(self, repo):
        pathlib.Path("calc.py").write_text(GOOD + "\n\ndef sub(a, b):\n    return a - b\n")
        commit_all("feat: add sub")
        trivial = fence({"explanation": "cover", "changes": [{"file": "tests/test_t.py", "content": "def test_t():\n    assert True\n"}]})
        caller = FakeCaller({"test-steward": [trivial, NOTESTS], **reviewers_ok()})
        _, final = run(repo, caller, "feat: add sub")
        assert any("also pass without the change" in n for n in final["notes"])

    def test_a_change_without_application_code_does_not_call_the_steward(self, repo):
        pathlib.Path("README.md").write_text("docs\n")
        commit_all("docs: readme")
        caller = FakeCaller(reviewers_ok())
        _, final = run(repo, caller, "docs: readme")
        assert final["outcome"] == "converged" and "test-steward" not in [r for r, _ in caller.log]

    def test_a_steward_that_deletes_tests_is_refused(self, repo):
        pathlib.Path("calc.py").write_text(GOOD + "\n\ndef sub(a, b):\n    return a - b\n")
        commit_all("feat: add sub")
        evil = fence({"explanation": "x", "changes": [{"file": "tests/test_calc.py", "find": "    assert add(0, 0) == 0\n", "replace": ""}]})
        caller = FakeCaller({"test-steward": [evil], **reviewers_ok()})
        _, final = run(repo, caller, "feat: add sub")
        assert pathlib.Path("tests/test_calc.py").read_text() == TEST
        # It tried twice, was refused twice: the tests of this change were never established, so no certification.
        assert final["outcome"] != "converged"
        assert any("weaken the tests" in n and "not certified" in n for n in final["notes"])

    def test_a_steward_whose_first_reply_is_unusable_gets_told_why_and_tries_again(self, repo):
        """flask-test-api PR #190: an anchor that is not in the file. It used to end as a note under a certification."""
        pathlib.Path("calc.py").write_text(GOOD + "\n\ndef sub(a, b):\n    return a - b\n")
        commit_all("feat: add sub")
        bad = fence({"explanation": "x", "changes": [{"file": "tests/test_calc.py", "find": "not in the file", "replace": "y"}]})
        good = fence({"explanation": "sub is tested", "changes": [{"file": "tests/test_sub.py",
                      "content": "from calc import sub\n\n\ndef test_sub():\n    assert sub(3, 1) == 2\n"}]})
        caller = FakeCaller({"test-steward": [bad, good], **reviewers_ok()})
        _, final = run(repo, caller, "feat: add sub")
        assert pathlib.Path("tests/test_sub.py").is_file()
        second = [user for role, user in caller.log if role == "test-steward"][1]
        assert "Your previous reply could not be used" in second and "appears 0 times" in second

    def test_a_failing_test_the_steward_just_wrote_never_changes_the_application(self, repo):
        """flask-test-api PR #193: the steward asserted a response field the app did not return; the test was
        treated as the specification and the writer changed the app-wide error handler to satisfy it."""
        pathlib.Path("calc.py").write_text(GOOD + "\n\ndef sub(a, b):\n    return a - b\n")
        commit_all("feat: add sub")
        code_before = pathlib.Path("calc.py").read_text()
        wrong = fence({"explanation": "x", "changes": [{"file": "tests/test_sub.py",
                       "content": "from calc import sub\n\n\ndef test_sub():\n    assert sub(3, 1) == 99\n"}]})
        right = fence({"explanation": "sub is tested", "changes": [{"file": "tests/test_sub.py",
                       "content": "from calc import sub\n\n\ndef test_sub():\n    assert sub(3, 1) == 2\n"}]})
        bend = fence({"explanation": "make the test pass", "changes": [{"file": "calc.py", "find": "return a - b", "replace": "return 99"}]})
        caller = FakeCaller({"test-steward": [wrong, right], "writer": [bend], **reviewers_ok()})
        _, final = run(repo, caller, "feat: add sub")
        assert pathlib.Path("calc.py").read_text() == code_before
        assert "writer" not in [role for role, _ in caller.log]
        assert "== 2" in pathlib.Path("tests/test_sub.py").read_text()
        second = [user for role, user in caller.log if role == "test-steward"][1]
        assert "FAILED against this change" in second

    def test_steward_tests_that_fail_twice_block_the_certification_and_leave_the_code_alone(self, repo):
        pathlib.Path("calc.py").write_text(GOOD + "\n\ndef sub(a, b):\n    return a - b\n")
        commit_all("feat: add sub")
        code_before = pathlib.Path("calc.py").read_text()
        wrong = fence({"explanation": "x", "changes": [{"file": "tests/test_sub.py",
                       "content": "from calc import sub\n\n\ndef test_sub():\n    assert sub(3, 1) == 99\n"}]})
        caller = FakeCaller({"test-steward": [wrong], **reviewers_ok()})
        _, final = run(repo, caller, "feat: add sub")
        assert final["outcome"] != "converged"
        assert pathlib.Path("calc.py").read_text() == code_before
        assert not pathlib.Path("tests/test_sub.py").exists()
        assert any("not certified" in n for n in final["notes"])

    def test_a_steward_that_never_answers_blocks_the_certification(self, repo):
        pathlib.Path("calc.py").write_text(GOOD + "\n\ndef sub(a, b):\n    return a - b\n")
        commit_all("feat: add sub")
        caller = FakeCaller({"test-steward": ["not json at all"], **reviewers_ok()})
        _, final = run(repo, caller, "feat: add sub")
        assert final["outcome"] != "converged"
        assert any("test steward could not produce a usable answer" in n for n in final["notes"])


class TestGuardCi:
    def streak_args(self, mode="ci", n=3):
        return type("A", (), {"mode": mode, "repo": "o/r", "sha": "x", "max_streak": n})()

    def test_ci_retries_until_the_streak_limit(self, repo, monkeypatch, tmp_path):
        out = tmp_path / "o.txt"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        for i in range(2):
            pathlib.Path(f"f{i}").write_text("x")
            git("add", "-A")
            git("-c", f"user.name={ap.AGENT_AUTHOR}", "commit", "-qm", f"fix(agent): {i}")
        ap.cmd_guard_change(self.streak_args(n=3))
        assert out.read_text().strip() == "skip=false"           # 2 agent commits < 3: try again
        out.unlink()
        pathlib.Path("f9").write_text("x")
        git("add", "-A")
        git("-c", f"user.name={ap.AGENT_AUTHOR}", "commit", "-qm", "fix(agent): 9")
        ap.cmd_guard_change(self.streak_args(n=3))
        assert out.read_text().strip() == "skip=true"            # 3 in a row and still red: stop

    def test_a_human_commit_resets_the_streak(self, repo, monkeypatch, tmp_path):
        out = tmp_path / "o.txt"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        pathlib.Path("a").write_text("x")
        git("add", "-A")
        git("-c", f"user.name={ap.AGENT_AUTHOR}", "commit", "-qm", "fix(agent): a")
        pathlib.Path("b").write_text("x")
        commit_all("human change")
        ap.cmd_guard_change(self.streak_args(n=1))
        assert out.read_text().strip() == "skip=false"
