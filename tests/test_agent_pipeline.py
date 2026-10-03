"""The orchestration: plan -> write -> checks -> two reviewers -> fix loop ->
final review -> docs -> changelog, against a real throwaway git repository
and a scripted model."""

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


PLAN = {"feasible": True, "summary": "Add the greeting module", "scope": ["greeting module"],
        "out_of_scope": ["api"], "acceptance_criteria": ["greeting() returns hello"],
        "files_hint": ["greet.py"], "risks": [], "reason": ""}


def write_change(content="def greeting():\n    return 'hello'\n"):
    return fence({"explanation": "add greet", "changes": [{"file": "greet.py", "content": content}]})


def no_findings():
    return fence({"summary": "ok", "findings": []})


def blocking_finding(line=1):
    return fence({"summary": "bad", "findings": [{
        "severity": "high", "file": "greet.py", "line": line, "category": "bug",
        "evidence": "def greeting", "problem": "returns the wrong greeting", "suggestion": "return 'hello'"}]})


class FakeCaller:
    """Replies per role, in order; the last one repeats once exhausted."""

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


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], check=True)
    (tmp_path / "README.md").write_text("# Project\n")
    (tmp_path / ".gitignore").write_text(".ai/\n.shared/\n")
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-qm", "init"], check=True)
    return tmp_path


def run_graph(caller, verify="test -f greet.py", max_iterations=3, max_verify_retries=2):
    base = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    rt = ap.Runtime(caller, base, verify, 30, max_iterations, max_verify_retries, 4, "", "CHANGELOG.md", 7)
    final = ap.build_graph(rt).invoke({"plan": PLAN, "iteration": 1, "verify_attempts": 0, "notes": [], "rounds": []},
                                      config={"recursion_limit": 200})
    return rt, final


def commits(rt):
    return subprocess.run(["git", "log", "--format=%s", f"{rt.base_sha}..HEAD"], capture_output=True, text=True
                          ).stdout.splitlines()


class TestLoop:
    def test_happy_path_converges_and_records_docs_and_changelog(self, repo):
        caller = FakeCaller({
            "writer": [write_change()], "reviewer-correctness": [no_findings()], "reviewer-security": [no_findings()],
            "final-reviewer": [no_findings()],
            "doc-reviewer": [fence({"explanation": "greeting is documented", "changes": [
                {"file": "README.md", "find": "# Project", "replace": "# Project\n\nUse `greeting()`."}]})],
        })
        rt, final = run_graph(caller)
        assert final["outcome"] == "converged"
        subjects = commits(rt)
        assert any(s.startswith("feat(agent)") for s in subjects)
        assert any(s.startswith("docs(agent)") for s in subjects)
        assert any(s.startswith("docs(changelog)") for s in subjects)
        assert "greeting()" in pathlib.Path("README.md").read_text()
        text = pathlib.Path("CHANGELOG.md").read_text()
        assert "Add the greeting module (#7)" in text and "### Added" in text

    def test_blocking_finding_goes_back_to_the_writer_then_converges(self, repo):
        fix = fence({"explanation": "fix greeting", "changes": [
            {"file": "greet.py", "find": "'hello'", "replace": "'hello, world'"}]})
        caller = FakeCaller({
            "writer": [write_change(), fix],
            "reviewer-correctness": [blocking_finding(), no_findings()],
            "reviewer-security": [no_findings()], "final-reviewer": [no_findings()],
            "doc-reviewer": [fence({"explanation": "nothing needed", "changes": []})],
        })
        rt, final = run_graph(caller)
        assert final["outcome"] == "converged"
        writer_prompts = [u for r, u in caller.log if r == "writer"]
        assert "REVIEW FINDINGS" in writer_prompts[1] and "returns the wrong greeting" in writer_prompts[1]
        assert "hello, world" in pathlib.Path("greet.py").read_text()
        assert any(s.startswith("fix(agent)") for s in commits(rt))

    def test_does_not_converge_stops_at_the_limit_and_escalates(self, repo):
        caller = FakeCaller({
            "writer": [write_change(), fence({"explanation": "tweak", "changes": [
                {"file": "greet.py", "find": "'hello'", "replace": "'hi'"}]}),
                fence({"explanation": "tweak", "changes": [{"file": "greet.py", "find": "'hi'", "replace": "'yo'"}]})],
            "reviewer-correctness": [blocking_finding()], "reviewer-security": [no_findings()],
        })
        rt, final = run_graph(caller, max_iterations=3)
        assert final["outcome"] == "escalated"
        assert any("blocking finding" in n for n in final["notes"])
        assert len([1 for r, _ in caller.log if r == "reviewer-correctness"]) == 3
        assert len(commits(rt)) == 3  # the work is kept for a person to look at

    def test_failing_checks_are_reverted_and_never_committed(self, repo):
        caller = FakeCaller({"writer": [write_change()]})
        rt, final = run_graph(caller, verify="false", max_verify_retries=1)
        assert final["outcome"] == "failed"
        assert commits(rt) == [] and not pathlib.Path("greet.py").exists()
        writer_prompts = [u for r, u in caller.log if r == "writer"]
        assert len(writer_prompts) == 2 and "FAILED VERIFICATION" in writer_prompts[1]

    def test_unusable_reviewer_fails_closed(self, repo):
        caller = FakeCaller({"writer": [write_change()], "reviewer-correctness": ["garbage"]})
        _, final = run_graph(caller)
        assert final["outcome"] == "escalated" and "could not be certified" in final["notes"][-1]

    def test_finding_on_a_line_outside_the_diff_is_dropped_not_acted_on(self, repo):
        caller = FakeCaller({
            "writer": [write_change()], "reviewer-correctness": [blocking_finding(line=500)],
            "reviewer-security": [no_findings()], "final-reviewer": [no_findings()],
            "doc-reviewer": [fence({"explanation": "none", "changes": []})],
        })
        _, final = run_graph(caller)
        assert final["outcome"] == "converged" and final["rounds"][0]["dropped"] == 1

    def test_writer_cannot_touch_workflows_or_the_changelog_is_not_duplicated(self, repo):
        bad = fence({"explanation": "x", "changes": [{"file": ".github/workflows/ci.yml", "content": "x"}]})
        caller = FakeCaller({"writer": [bad]})
        _, final = run_graph(caller)
        assert final["outcome"] == "failed"
        assert not pathlib.Path(".github").exists()

    def test_doc_edits_outside_documentation_are_refused(self, repo):
        caller = FakeCaller({
            "writer": [write_change()], "reviewer-correctness": [no_findings()], "reviewer-security": [no_findings()],
            "final-reviewer": [no_findings()],
            "doc-reviewer": [fence({"explanation": "x", "changes": [{"file": "greet.py", "find": "hello", "replace": "bye"}]})],
        })
        _, final = run_graph(caller)
        assert final["outcome"] == "converged"
        assert "hello" in pathlib.Path("greet.py").read_text()
        assert any("documentation reviewer returned no usable reply" in n for n in final["notes"])


class TestPlanner:
    def test_valid_and_infeasible_plans(self):
        assert ap.validate_plan(PLAN)["summary"] == "Add the greeting module"
        assert ap.validate_plan({**PLAN, "feasible": False, "scope": [], "acceptance_criteria": [], "reason": ""}) is None
        assert ap.validate_plan({**PLAN, "feasible": False, "scope": [], "acceptance_criteria": [], "reason": "vague"})
        assert ap.validate_plan({**PLAN, "acceptance_criteria": []}) is None

    def test_workflow_files_are_never_in_scope(self):
        plan = ap.validate_plan({**PLAN, "files_hint": [".github/workflows/x.yml", "a.py"]})
        assert plan["files_hint"] == ["a.py"]

    def test_explores_read_only_then_plans(self, repo):
        caller = FakeCaller({"planner": [fence({"read": "README.md"}), fence(PLAN)]})
        plan = ap.run_planner(caller, "title", "body", "ctx")
        assert plan["summary"] == PLAN["summary"] and "# Project" in caller.log[1][1]


class TestStandaloneReview:
    def test_review_diff_validates_and_dedups_across_reviewers(self):
        diff = "diff --git a/g.py b/g.py\n--- a/g.py\n+++ b/g.py\n@@ -0,0 +1,2 @@\n+a = 1\n+b = 2\n"
        f = lambda sev: fence({"findings": [{"severity": sev, "file": "g.py", "line": 1, "category": "bug",  # noqa: E731
                                              "evidence": "a = 1", "problem": "bad", "suggestion": "fix"}]})
        caller = FakeCaller({"reviewer-correctness": [f("medium")], "reviewer-security": [f("critical")]})
        findings, dropped = ap.review_diff(caller, diff, None)
        assert len(findings) == 1 and findings[0].severity == "critical" and not dropped
        assert sorted(findings[0].reviewers) == ["reviewer-correctness", "reviewer-security"]
        assert "No plan was provided" in caller.log[0][1] and "L1|+a = 1" in caller.log[0][1]


class TestDocsArchitect:
    FILES = {"README.md", "docs/a.md", "docs/b.md", "app/main.py", "app/settings.py"}
    DOCS = {"README.md", "docs/a.md", "docs/b.md"}

    def proposal(self, **over):
        base = {
            "summary": "s",
            "inventory": [{"path": "README.md", "quadrant": "explanation", "ambiguous": False, "reason": "r"},
                          {"path": "docs/a.md", "quadrant": "unclear", "ambiguous": False, "reason": "mixed"},
                          {"path": "docs/ghost.md", "quadrant": "reference", "reason": "x"},
                          {"path": "docs/b.md", "quadrant": "wiki", "reason": "x"}],
            "gaps": [{"topic": "settings", "quadrant": "reference", "evidence": ["app/settings.py"], "verification": "code", "why": "w"},
                     {"topic": "invented", "quadrant": "reference", "evidence": ["app/nope.py"], "verification": "code", "why": "w"}],
            "structure": {"directories": [{"path": "docs/reference", "quadrant": "reference", "purpose": "p"}],
                          "navigation": [{"section": "Reference", "entries": [{"title": "t", "path": "docs/reference/s.md"}]}]},
            "mapping": [{"from": "docs/a.md", "to": "docs/reference/a.md", "action": "move", "notes": ""},
                        {"from": "docs/ghost.md", "to": "x", "action": "move", "notes": ""}],
            "duplicates": [{"paths": ["docs/a.md", "docs/b.md"], "note": "same"}],
            "obsolete": [{"path": "docs/b.md", "evidence": [], "note": "old"}],
            "new_docs": [{"path": "docs/reference/settings.md", "quadrant": "reference", "title": "Settings",
                          "evidence": ["app/settings.py"], "verification": "code"},
                         {"path": "docs/howto/x.md", "quadrant": "how-to", "title": "X", "evidence": [], "verification": "human"},
                         {"path": "CHANGELOG.md", "quadrant": "reference", "title": "c", "evidence": ["app/main.py"], "verification": "code"}],
            "open_questions": ["who deploys?"],
        }
        base.update(over)
        return base

    def test_only_what_the_repository_backs_up_survives(self):
        out, warnings = ap.validate_docs_proposal(self.proposal(), self.DOCS, self.FILES)
        assert [i["path"] for i in out["inventory"]] == ["README.md", "docs/a.md"]
        assert out["inventory"][1]["ambiguous"] is True  # `unclear` is always flagged ambiguous
        assert [g["topic"] for g in out["gaps"]] == ["settings"]
        assert [m["from"] for m in out["mapping"]] == ["docs/a.md"]
        assert out["obsolete"] == []  # claimed obsolete without evidence
        assert [n["path"] for n in out["new_docs"]] == ["docs/reference/settings.md"]  # no evidence / changelog dropped
        assert out["duplicates"][0]["paths"] == ["docs/a.md", "docs/b.md"]
        assert len(warnings) >= 5

    def test_report_is_plan_only_and_names_unclassified_docs(self):
        out, warnings = ap.validate_docs_proposal(self.proposal(), self.DOCS, self.FILES)
        report = ap.render_docs_report(out, sorted(self.DOCS), warnings)
        assert "No document was created, moved or rewritten" in report and "nothing is merged automatically" in report
        assert "needs human confirmation" not in report.split("## Gaps")[1].split("##")[0]  # the gap is code-verifiable
        assert "`docs/b.md`" in report.split("Not classified")[1]

    def test_changelog_is_not_a_documentation_input(self):
        assert not ap.is_architect_doc("CHANGELOG.md") and not ap.is_architect_doc("docs/CHANGELOG.md")
        assert ap.is_architect_doc("docs/x.md") and ap.is_architect_doc("README.md") and not ap.is_architect_doc("requirements.txt")
        # app templates are HTML but not documentation; HTML under docs/ is
        assert not ap.is_architect_doc("app/templates/index.html") and ap.is_architect_doc("docs/case-study.html")

    def test_command_writes_only_under_ai_and_refuses_if_the_tree_changes(self, repo, monkeypatch):
        (repo / "docs").mkdir()
        (repo / "docs" / "a.md").write_text("# A\n")
        (repo / "app").mkdir()
        (repo / "app" / "s.py").write_text("import os\nX = os.getenv('X')\n")
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-qm", "docs"], check=True)
        good = fence({"summary": "s", "inventory": [{"path": "docs/a.md", "quadrant": "reference", "ambiguous": False, "reason": "r"}],
                      "gaps": [], "structure": {"directories": [], "navigation": []}, "mapping": [], "duplicates": [],
                      "obsolete": [], "new_docs": [], "open_questions": []})

        class Meddling(FakeCaller):
            def call(self, role, system, user):
                (repo / "docs" / "a.md").write_text("rewritten by the model")
                return super().call(role, system, user)

        args = type("A", (), {"ai_script": "x", "max_chars": 1, "timeout": 1, "post_issue": False, "repo": ""})()
        monkeypatch.setattr(lib, "ModelCaller", lambda *a, **k: FakeCaller({"docs-architect": [good]}))
        assert ap.cmd_docs_plan(args) == 0
        assert (repo / ".ai/docs-architect/proposal.md").is_file()
        assert (repo / "docs" / "a.md").read_text() == "# A\n"
        monkeypatch.setattr(lib, "ModelCaller", lambda *a, **k: Meddling({"docs-architect": [good]}))
        assert ap.cmd_docs_plan(args) == 1


class TestPublish:
    def args(self):
        return type("A", (), {"repo": "o/r", "issue": 7, "base_branch": "main"})()

    def test_not_feasible_comments_and_succeeds(self, repo, monkeypatch):
        (repo / ".ai/agent-run").mkdir(parents=True)
        (repo / ".ai/agent-run/result.json").write_text(json.dumps({"outcome": "not_feasible", "notes": ["too vague"]}))
        calls = []
        monkeypatch.setattr(ap, "_gh", lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
        assert ap.cmd_publish(self.args()) == 0
        assert calls and "too vague" in calls[0][-1]

    def test_failed_run_is_a_red_job(self, repo, monkeypatch):
        (repo / ".ai/agent-run").mkdir(parents=True)
        (repo / ".ai/agent-run/result.json").write_text(json.dumps({"outcome": "failed", "notes": ["x"]}))
        monkeypatch.setattr(ap, "_gh", lambda *a: subprocess.CompletedProcess(a, 0, "", ""))
        assert ap.cmd_publish(self.args()) == 1

    def test_refuses_to_publish_without_the_push_token(self, repo, monkeypatch):
        (repo / ".ai/agent-run").mkdir(parents=True)
        (repo / ".ai/agent-run/result.json").write_text(json.dumps(
            {"outcome": "converged", "branch": "agent/issue-7-1", "title": "t"}))
        monkeypatch.delenv("AGENT_PUSH_TOKEN", raising=False)
        assert ap.cmd_publish(self.args()) == 1
