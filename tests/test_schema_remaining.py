"""Reviewers run at the same time, findings outside the plan's scope are not acted on, and a CI job that
failed in the runner (not in the code) is re-run once instead of being sent to the repair loop."""

import json
import pathlib
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import agent_lib as lib  # noqa: E402
import agent_pipeline as ap  # noqa: E402


def fence(obj) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


def finding(file="a.py", line=1, severity="high", problem="bad"):
    return {"severity": severity, "file": file, "line": line, "category": "bug", "evidence": "e", "problem": problem, "suggestion": "s"}


DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n"


class TestParallelReviewers:
    def test_the_reviewers_really_run_at_the_same_time(self):
        barrier = threading.Barrier(2, timeout=5)           # sequential calls would never meet and would raise

        class Caller:
            max_chars = 1000

            def call(self, role, system, user):
                barrier.wait()
                return fence({"findings": [finding(problem=f"found by {role}")]})
        found, dropped = ap.review_diff(Caller(), DIFF, None)
        assert found and not dropped

    def test_the_order_of_the_results_does_not_depend_on_who_finishes_first(self):
        class Caller:
            max_chars = 1000

            def call(self, role, system, user):
                if role == "reviewer-correctness":
                    threading.Event().wait(0.2)             # A is the slow one
                return fence({"findings": [finding(line=1 if role == "reviewer-correctness" else 2, problem=f"by {role}", severity="medium")]})
        found, _ = ap.review_diff(Caller(), DIFF, None)
        assert len(found) == 1                                                   # same place, same topic: merged
        assert found[0].reviewers == ["reviewer-correctness", "reviewer-security"]   # declared order, not finishing order

    def test_one_unusable_reviewer_still_fails_closed(self):
        class Caller:
            max_chars = 1000

            def call(self, role, system, user):
                return "garbage" if role == "reviewer-security" else fence({"findings": []})
        assert ap.review_diff(Caller(), DIFF, None) is None

    def test_the_caller_counts_calls_and_costs_correctly_under_concurrency(self, tmp_path):
        script = tmp_path / "ai.py"
        script.write_text("import sys, json\na = sys.argv\nopen(a[a.index('--usage-file')+1], 'w').write(json.dumps({'cost_usd': 0.01}))\nprint('ok')\n")
        caller = lib.ModelCaller(str(script), work_dir=str(tmp_path / "w"))
        threads = [threading.Thread(target=caller.call, args=(f"role{i}", "s", "u")) for i in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert caller.calls == 12 and len(caller.usage) == 12 and caller.total_cost_usd() == 0.12
        assert len(list((tmp_path / "w").glob("*.reply.txt"))) == 12             # no two calls overwrote each other's files


class TestFindingsOutsideTheScope:
    PLAN = {"out_of_scope": ["The legacy docs in docs/legacy/ and config.yaml"]}

    def make(self, file):
        return lib.Finding("high", file, 1, "bug", "e", "p", "s", ["reviewer-correctness"])

    def test_findings_about_excluded_files_are_not_in_scope(self):
        assert not lib.finding_in_scope(self.make("docs/legacy/old.md"), self.PLAN)
        assert not lib.finding_in_scope(self.make("deploy/config.yaml"), self.PLAN)
        assert lib.finding_in_scope(self.make("app/main.py"), self.PLAN)

    def test_a_plan_with_no_exclusions_keeps_everything(self):
        assert lib.finding_in_scope(self.make("docs/legacy/old.md"), {"out_of_scope": []})


def git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True, check=True).stdout.strip()


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


class TestGraphIgnoresOutOfScopeFindings:
    def test_a_blocking_finding_on_an_excluded_file_does_not_send_the_writer_round_again(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            git(*cmd)
        pathlib.Path(".gitignore").write_text(".ai/\n.shared/\n")
        pathlib.Path("README.md").write_text("x\n")
        git("add", "-A")
        git("commit", "-qm", "init")
        base = git("rev-parse", "HEAD")
        git("checkout", "-qb", "feature")
        pathlib.Path("docs/legacy").mkdir(parents=True)
        pathlib.Path("docs/legacy/old.md").write_text("old text\n")
        git("add", "-A")
        git("commit", "-qm", "docs: touch legacy")
        plan = dict(ap.derived_plan("docs: touch legacy", ""), out_of_scope=["docs/legacy/"])
        caller = FakeCaller({
            "reviewer-correctness": [fence({"findings": [finding("docs/legacy/old.md", 1)]})],
            "reviewer-security": [fence({"findings": []})],
            "doc-reviewer": [fence({"explanation": "none", "changes": []})]})
        rt = ap.Runtime(caller, base, "true", 30, 3, 2, 4, "", "", 0, start="verify", fix_kind="fix", write_tests=False)
        final = ap.build_graph(rt).invoke({"plan": plan, "iteration": 1, "verify_attempts": 0, "notes": [], "rounds": []},
                                          config={"recursion_limit": 100})
        assert final["outcome"] == "converged"
        assert "writer" not in [r for r, _ in caller.log]                        # nothing was sent back to be "fixed"
        assert any("out of scope and were not acted on" in n for n in final["notes"])


def jobs(*specs):
    """specs: (job name, [(step name, conclusion), ...])"""
    return {"jobs": [{"name": n, "conclusion": "failure" if any(c == "failure" for _, c in steps) or not steps else "success",
                      "steps": [{"name": s, "conclusion": c} for s, c in steps]} for n, steps in specs]}


class TestRunnerFailureVersusCodeFailure:
    def run(self, monkeypatch, tmp_path, payload, attempt=1, dry_run=False):
        out = tmp_path / "o.txt"
        out.unlink(missing_ok=True)
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        reruns = []
        monkeypatch.setattr(ap, "rerun_failed_jobs", lambda repo, rid: reruns.append(rid) or True)
        monkeypatch.setattr(ap, "gh_json", lambda a: {"run_attempt": attempt} if a[0].endswith("/runs/9") else payload)
        args = type("A", (), {"repo": "o/r", "run_id": 9, "dry_run": dry_run})()
        assert ap.cmd_ci_infra(args) == 0
        return out.read_text().strip(), reruns

    def test_a_cluster_that_would_not_start_is_re_run_once_and_not_repaired(self, monkeypatch, tmp_path):
        payload = jobs(("image", [("Set up job", "success"), ("Create a kind cluster", "failure")]))
        assert self.run(monkeypatch, tmp_path, payload) == ("infra_retry=true", [9])

    def test_a_checkout_that_failed_is_infrastructure_too(self, monkeypatch, tmp_path):
        payload = jobs(("checks", [("Run actions/checkout@v7", "failure")]))
        assert self.run(monkeypatch, tmp_path, payload)[0] == "infra_retry=true"

    def test_a_failing_test_is_the_codes_problem_so_the_repair_loop_judges_it(self, monkeypatch, tmp_path):
        payload = jobs(("integration", [("Set up job", "success"), ("Integration tests", "failure")]))
        assert self.run(monkeypatch, tmp_path, payload) == ("infra_retry=false", [])

    def test_a_mixed_failure_is_not_treated_as_infrastructure(self, monkeypatch, tmp_path):
        payload = jobs(("image", [("Create a kind cluster", "failure")]), ("checks", [("Tests", "failure")]))
        assert self.run(monkeypatch, tmp_path, payload) == ("infra_retry=false", [])

    def test_the_second_attempt_is_judged_for_real_there_is_no_endless_retry(self, monkeypatch, tmp_path):
        payload = jobs(("image", [("Create a kind cluster", "failure")]))
        assert self.run(monkeypatch, tmp_path, payload, attempt=2) == ("infra_retry=false", [])

    def test_dry_run_reports_without_re_running(self, monkeypatch, tmp_path, capsys):
        payload = jobs(("image", [("Create a kind cluster", "failure")]))
        assert self.run(monkeypatch, tmp_path, payload, dry_run=True) == ("infra_retry=true", [])
        assert "would re-run it once" in capsys.readouterr().out
