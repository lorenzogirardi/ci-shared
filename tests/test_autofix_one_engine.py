"""The sweep's repair of a red Renovate PR runs on the same engine as every other change, keeps what the old
loop knew (install the new dependency first, read library sources, fix every call site), and does not leave a
push token where the PR's code runs."""

import argparse
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import agent_lib  # noqa: E402
import agent_pipeline  # noqa: E402
import pr_review_sweep as sweep  # noqa: E402

KEY = "http.https://github.com/.extraheader"


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
    (work / "requirements.txt").write_text("mcp==1.0\n")
    git("add", "-A", cwd=work)
    git("commit", "-qm", "init", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    base = git("rev-parse", "HEAD", cwd=work)
    git("checkout", "-qb", "renovate/mcp", cwd=work)
    (work / "requirements.txt").write_text("mcp==2.0\n")
    git("add", "-A", cwd=work)
    git("commit", "-qm", "bump", cwd=work)
    git("push", "-q", "origin", "renovate/mcp", cwd=work)
    head = git("rev-parse", "HEAD", cwd=work)
    git("config", "--local", KEY, "AUTHORIZATION: basic SECRET", cwd=work)
    monkeypatch.chdir(work)
    monkeypatch.setattr(sweep, "collect_failure_logs", lambda *a, **k: "ImportError: cannot import name 'FastMCP'")
    monkeypatch.setattr(sweep, "_prepare_diff", lambda pr, args: "diff")
    pr = {"number": 7, "title": "Update dependency mcp to v2", "body": "notes", "base": {"sha": base},
          "head": {"sha": head, "ref": "renovate/mcp", "repo": {"full_name": "o/r"}}}
    return origin, pr, head


def args():
    return argparse.Namespace(ai_script="x", max_chars=1000, timeout=5, verify_command_file="", verify_timeout=5,
                              max_autofix_attempts=20, review_guidance_file="")


class TestAutofixOne:
    def test_a_converged_repair_is_pushed_and_the_new_dependency_is_installed_first(self, pr_repo, monkeypatch):
        origin, pr, head = pr_repo
        order, seen = [], {}
        monkeypatch.setattr(agent_lib, "run_verify_isolated", lambda cmd, t: order.append("prime") or (False, ""))

        def engine(make_runtime, initial):
            order.append("engine")
            rt = make_runtime(1, {})
            seen.update(start=rt.start, context=rt.context, failure=initial["failure_output"],
                        secret=git("config", "--local", "--get-all", KEY) if subprocess.run(
                            ["git", "config", "--local", "--get-all", KEY], capture_output=True).returncode == 0 else "")
            pathlib.Path("migrated.py").write_text("from mcp.server.mcpserver import MCPServer\n")
            git("add", "-A")
            git("-c", "user.name=ci-shared agents", "commit", "-qm", "fix(agent): migrate to mcp 2")
            return {"outcome": "converged", "notes": ["migrated the call sites"]}
        monkeypatch.setattr(agent_pipeline, "invoke_with_retry", engine)
        outcome, detail = sweep.autofix_one(pr, args(), "o/r", head)
        assert outcome == "pushed" and "migrated" in detail
        assert order == ["prime", "engine"]                                   # dependencies installed before the model reads anything
        assert seen["start"] == "ci" and "ImportError" in seen["failure"]
        assert "grep the repo once for the OLD name" in seen["context"]       # the migration know-how survived
        assert git("--git-dir", str(origin), "log", "-1", "--format=%s", "renovate/mcp") == "fix(agent): migrate to mcp 2"

    def test_no_push_token_is_on_disk_while_the_prs_code_runs_and_it_is_back_for_the_push(self, pr_repo, monkeypatch):
        _, pr, head = pr_repo
        during = {}
        monkeypatch.setattr(agent_lib, "run_verify_isolated", lambda cmd, t: during.update(prime=_has_token()) or (False, ""))
        monkeypatch.setattr(agent_pipeline, "invoke_with_retry",
                            lambda *a: during.update(engine=_has_token()) or {"outcome": "escalated", "notes": ["x"]})
        sweep.autofix_one(pr, args(), "o/r", head)
        assert during == {"prime": False, "engine": False}
        assert _has_token()                                                    # restored afterwards

    def test_a_repair_that_does_not_converge_pushes_nothing(self, pr_repo, monkeypatch):
        origin, pr, head = pr_repo
        monkeypatch.setattr(agent_lib, "run_verify_isolated", lambda cmd, t: (False, ""))
        monkeypatch.setattr(agent_pipeline, "invoke_with_retry", lambda *a: {"outcome": "failed", "notes": ["no usable change"]})
        outcome, detail = sweep.autofix_one(pr, args(), "o/r", head)
        assert outcome == "exhausted" and "no usable change" in detail
        assert git("--git-dir", str(origin), "rev-parse", "renovate/mcp") == head

    def test_converging_without_a_change_is_declined(self, pr_repo, monkeypatch):
        _, pr, head = pr_repo
        monkeypatch.setattr(agent_lib, "run_verify_isolated", lambda cmd, t: (False, ""))
        monkeypatch.setattr(agent_pipeline, "invoke_with_retry", lambda *a: {"outcome": "converged", "notes": []})
        assert sweep.autofix_one(pr, args(), "o/r", head)[0] == "declined"

    def test_forks_and_missing_logs_are_skipped_before_anything_runs(self, pr_repo, monkeypatch):
        _, pr, head = pr_repo
        monkeypatch.setattr(agent_pipeline, "invoke_with_retry", lambda *a: pytest.fail("must not run"))
        fork = dict(pr, head=dict(pr["head"], repo={"full_name": "someone/else"}))
        assert sweep.autofix_one(fork, args(), "o/r", head)[0] == "skipped"
        monkeypatch.setattr(sweep, "collect_failure_logs", lambda *a, **k: "  ")
        assert sweep.autofix_one(pr, args(), "o/r", head)[0] == "skipped"


def _has_token() -> bool:
    return subprocess.run(["git", "config", "--local", "--get-all", KEY], capture_output=True).returncode == 0


def test_hidden_credentials_come_back_even_if_the_code_inside_raises(pr_repo):
    with pytest.raises(RuntimeError):
        with sweep.hidden_git_credentials():
            assert not _has_token()
            raise RuntimeError("boom")
    assert _has_token()
