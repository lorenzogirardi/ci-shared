"""A blocking finding is never a stop sign. The reviewers are given the CI results as evidence,
the sweep waits for CI instead of judging early, and a real finding goes to the writer loop;
what it cannot fix is abandoned. Nothing is left waiting."""

import argparse
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import agent_pipeline  # noqa: E402
import pr_review_sweep as sweep  # noqa: E402


class TestCiEvidence:
    def test_lists_each_check_with_its_conclusion(self, monkeypatch):
        runs = {"check_runs": [{"name": "image", "conclusion": "success"}, {"name": "checks", "conclusion": "success"},
                               {"name": "integration", "conclusion": None, "status": "in_progress"}]}
        monkeypatch.setattr(sweep, "gh_json", lambda a: runs)
        text = sweep.ci_evidence("o/r", "a" * 40)
        assert "- image: success" in text and "- integration: in_progress" in text
        assert text.index("- checks") < text.index("- image")                      # stable order

    def test_no_results_is_said_plainly(self, monkeypatch):
        monkeypatch.setattr(sweep, "gh_json", lambda a: {"check_runs": []})
        assert "No check results" in sweep.ci_evidence("o/r", "a" * 40)

    def test_the_reviewers_receive_it_as_part_of_their_guidance(self, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sweep, "ci_evidence", lambda repo, sha: "- image: success")
        monkeypatch.setattr(agent_pipeline, "structured_review",
                            lambda caller, diff, title, body, guidance="": seen.update(g=guidance) or (True, "ok"))
        args = argparse.Namespace(ai_script="x", max_chars=10, timeout=1, review_guidance_file="", repo="o/r")
        pr = {"number": 1, "title": "t", "body": "", "head": {"sha": "a" * 40}}
        sweep.structured_verdict(args, pr, "diff")
        assert "Deterministic check results" in seen["g"] and "- image: success" in seen["g"]


def commit(name):
    return {"commit": {"author": {"name": name}}}


def test_the_agent_engine_counts_towards_the_repair_cap_like_the_autofix():
    assert sweep.autofix_streak([commit("ci-shared agents"), commit(sweep.AUTOFIX_AUTHOR)]) == 2


PR = {"number": 5, "title": "Update python docker tag", "body": "", "user": {"login": "renovate[bot]"},
      "base": {"ref": "main", "sha": "b" * 40},
      "head": {"sha": "a" * 40, "ref": "renovate/python", "repo": {"full_name": "o/r"}}}


@pytest.fixture
def sweep_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "system.txt").write_text("s")
    monkeypatch.setattr(sweep, "list_open_prs", lambda repo: [PR])
    monkeypatch.setattr(sweep, "existing_sweep_comment", lambda *a: None)
    monkeypatch.setattr(sweep, "gh_json", lambda a: [])

    def go(*extra):
        monkeypatch.setattr(sys, "argv", ["sweep", "--repo", "o/r", "--system-file", "system.txt", "--ai-script", "x",
                                          "--authors", "renovate[bot]", "--structured-review", "--required-checks", "checks,image",
                                          "--auto-merge", *extra])
        return sweep.main()
    return go


class TestSweepLoop:
    def test_it_does_not_judge_while_required_checks_are_still_running(self, sweep_run, monkeypatch):
        monkeypatch.setattr(sweep, "checks_state", lambda *a, **k: ("pending", "image not finished"))
        monkeypatch.setattr(sweep, "review_one", lambda *a, **k: pytest.fail("reviewers must wait for the evidence"))
        assert sweep_run() == 0

    def dirty(self, monkeypatch, fix):
        monkeypatch.setattr(sweep, "checks_state", lambda *a, **k: ("green", "ok"))
        monkeypatch.setattr(sweep, "review_one", lambda *a, **k: "- **[high]** `Dockerfile:1` bug\nVERDICT: NEEDS_REVIEW")
        monkeypatch.setattr(sweep, "try_merge", lambda *a, **k: pytest.fail("a blocked PR is never merged"))
        monkeypatch.setattr(sweep, "fix_findings_one", fix)
        posted, abandoned = [], []
        monkeypatch.setattr(sweep, "post_comment", lambda repo, n, body, existing: posted.append(body))
        monkeypatch.setattr(sweep, "abandon_pr", lambda repo, n, why: abandoned.append(why))
        return posted, abandoned

    def test_a_blocking_finding_goes_to_the_writer_loop_and_a_fix_is_not_abandoned(self, sweep_run, monkeypatch):
        calls = []
        posted, abandoned = self.dirty(monkeypatch, lambda *a: calls.append(a[4]) or ("pushed", "fixed by the writer loop"))
        assert sweep_run("--fix-findings") == 0
        assert calls and "Dockerfile:1" in calls[0]                           # the finding text is what the writer gets
        assert abandoned == [] and "fixed by the writer loop" in posted[0]

    def test_what_the_writer_cannot_fix_ends_abandoned_not_waiting(self, sweep_run, monkeypatch):
        posted, abandoned = self.dirty(monkeypatch, lambda *a: ("abandoned", "the writer could not fix it"))
        assert sweep_run("--fix-findings") == 0
        assert abandoned == ["the writer could not fix it"]

    def test_after_the_cap_it_abandons_without_another_attempt(self, sweep_run, monkeypatch):
        posted, abandoned = self.dirty(monkeypatch, lambda *a: pytest.fail("cap reached"))
        monkeypatch.setattr(sweep, "gh_json", lambda a: [commit("ci-shared agents")] * 3)
        assert sweep_run("--fix-findings", "--max-autofix-commits", "3") == 0
        assert abandoned and "3 automatic fixes" in abandoned[0]


def git(*a, cwd=None):
    return subprocess.run(["git", *a], capture_output=True, text=True, check=True, cwd=cwd).stdout.strip()


@pytest.fixture
def pr_repo(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    git("clone", "-q", str(origin), str(work))
    git("config", "user.email", "t@t", cwd=work)
    git("config", "user.name", "t", cwd=work)
    (work / "Dockerfile").write_text("FROM python:3.14.7-slim\n")
    git("add", "-A", cwd=work)
    git("commit", "-qm", "init", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    git("checkout", "-qb", "renovate/python", cwd=work)
    (work / "Dockerfile").write_text("FROM python:3.14.8-slim\n")
    git("add", "-A", cwd=work)
    git("commit", "-qm", "bump", cwd=work)
    git("push", "-q", "origin", "renovate/python", cwd=work)
    monkeypatch.chdir(work)
    pr = dict(PR, base={"ref": "main", "sha": git("rev-parse", "main", cwd=work)},
              head={"sha": git("rev-parse", "HEAD", cwd=work), "ref": "renovate/python", "repo": {"full_name": "o/r"}})
    git("checkout", "-q", "main", cwd=work)
    return origin, pr


def fix_args():
    return argparse.Namespace(ai_script="x", max_chars=10, timeout=1, verify_command_file="", verify_timeout=1)


class TestFixFindingsOne:
    def test_a_converged_fix_is_pushed_to_the_pr_branch(self, pr_repo, monkeypatch):
        origin, pr = pr_repo

        def fake_engine(make_runtime, initial):
            assert "REVIEW FINDINGS" in initial["feedback"] and "the finding" in initial["feedback"]
            pathlib.Path("fix.txt").write_text("fixed\n")
            git("add", "-A")
            git("-c", "user.name=ci-shared agents", "commit", "-qm", "fix(agent): resolve the finding")
            return {"outcome": "converged", "notes": ["resolved"]}
        monkeypatch.setattr(agent_pipeline, "invoke_with_retry", fake_engine)
        outcome, _ = sweep.fix_findings_one(pr, fix_args(), "o/r", pr["head"]["sha"], "the finding")
        assert outcome == "pushed"
        assert git("--git-dir", str(origin), "log", "-1", "--format=%s", "renovate/python") == "fix(agent): resolve the finding"

    def test_a_loop_that_does_not_converge_pushes_nothing_and_abandons(self, pr_repo, monkeypatch):
        origin, pr = pr_repo
        before = git("--git-dir", str(origin), "rev-parse", "renovate/python")
        monkeypatch.setattr(agent_pipeline, "invoke_with_retry", lambda *a: {"outcome": "escalated", "notes": ["no usable change"]})
        outcome, why = sweep.fix_findings_one(pr, fix_args(), "o/r", pr["head"]["sha"], "the finding")
        assert outcome == "abandoned" and "no usable change" in why
        assert git("--git-dir", str(origin), "rev-parse", "renovate/python") == before

    def test_converging_without_changing_anything_does_not_count_as_a_fix(self, pr_repo, monkeypatch):
        _, pr = pr_repo
        monkeypatch.setattr(agent_pipeline, "invoke_with_retry", lambda *a: {"outcome": "converged", "notes": []})
        outcome, why = sweep.fix_findings_one(pr, fix_args(), "o/r", pr["head"]["sha"], "the finding")
        assert outcome == "abandoned" and "finding stands" in why

    def test_a_fork_is_never_pushed_to(self, pr_repo):
        _, pr = pr_repo
        fork = dict(pr, head=dict(pr["head"], repo={"full_name": "someone/else"}))
        assert sweep.fix_findings_one(fork, fix_args(), "o/r", pr["head"]["sha"], "x")[0] == "abandoned"
