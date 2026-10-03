"""Merge authority and the safety net after the merge: a PR merges only when its
head commit is certified by a trusted author and CI is green; if the base branch
turns red, the culprit is reverted by itself and the work is queued again."""

import argparse
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import agent_pipeline as ap  # noqa: E402

HEAD = "a" * 40
OLD = "b" * 40


def cert(sha=HEAD, login="owner", extra=""):
    return {"user": {"login": login}, "body": f"<!-- agent-pr -->\n<!-- agent-certified: {sha} -->\n{extra}"}


class TestCertification:
    def test_trusted_author_and_exact_sha(self):
        assert ap.is_certified([cert()], HEAD, {"owner"})

    def test_a_forged_marker_from_someone_else_does_not_count(self):
        assert not ap.is_certified([cert(login="stranger")], HEAD, {"owner"})

    def test_a_certification_for_an_older_commit_does_not_apply_after_a_new_push(self):
        assert not ap.is_certified([cert(sha=OLD)], HEAD, {"owner"})

    def test_no_comments_means_not_certified(self):
        assert not ap.is_certified([], HEAD, {"owner"})


def pr_data(**over):
    base = {"state": "open", "draft": False, "head": {"sha": HEAD, "repo": {"full_name": "o/r"}}, "labels": []}
    base.update(over)
    return base


def gate_args(**over):
    base = dict(repo="o/r", pr=5, base_branch="main", required_checks="checks,integration,workflows",
                merge_method="squash", poll_seconds=0, trusted="owner", max_reverts=3)
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture
def gate(monkeypatch):
    merges = []

    def setup(pr=None, comments=None, commits=None):
        def fake_gh_json(args):
            url = args[0]
            if url.endswith("/pulls/5"):
                return pr or pr_data()
            if "/issues/5/comments" in url:
                return [cert()] if comments is None else comments
            if "/commits?sha=" in url:
                return commits or []
            raise AssertionError(url)
        monkeypatch.setattr(ap, "gh_json", fake_gh_json)
        monkeypatch.setattr(ap, "try_merge", lambda *a, **k: merges.append((a, k)) or "merged")
        return merges
    return setup


class TestMergeGate:
    def test_certified_head_with_green_ci_is_merged_on_exactly_that_commit(self, gate):
        merges = gate()
        assert ap.cmd_merge_gate(gate_args()) == 0
        (call, _), = merges
        assert call[:4] == ("o/r", 5, HEAD, "squash") and call[4] == ("checks", "integration", "workflows")

    @pytest.mark.parametrize("pr", [pr_data(draft=True), pr_data(state="closed"),
                                    pr_data(head={"sha": HEAD, "repo": {"full_name": "fork/r"}}),
                                    pr_data(labels=[{"name": ap.ABANDONED_LABEL}])])
    def test_draft_closed_fork_or_abandoned_is_never_merged(self, gate, pr):
        merges = gate(pr=pr)
        ap.cmd_merge_gate(gate_args())
        assert merges == []

    def test_not_merged_without_a_trusted_certification_of_this_head(self, gate):
        merges = gate(comments=[cert(sha=OLD), cert(login="stranger")])
        ap.cmd_merge_gate(gate_args())
        assert merges == []

    def test_circuit_breaker_stops_merging_after_repeated_reverts(self, gate):
        reverted = [{"commit": {"message": f"{ap.REVERT_PREFIX}: x{i} (pipeline red)"}} for i in range(3)]
        merges = gate(commits=reverted + [{"commit": {"message": "feat: fine"}}])
        ap.cmd_merge_gate(gate_args())
        assert merges == []
        merges_ok = gate(commits=reverted[:2])
        ap.cmd_merge_gate(gate_args())
        assert len(merges_ok) == 1                      # below the limit: merging continues


class TestRecentReverts:
    def test_counts_only_automatic_reverts(self, monkeypatch):
        commits = [{"commit": {"message": f"{ap.REVERT_PREFIX}: a"}}, {"commit": {"message": "Revert \"b\""}},
                   {"commit": {"message": "feat: c"}}]
        monkeypatch.setattr(ap, "gh_json", lambda a: commits)
        assert ap.recent_reverts("o/r", "main") == 1


def git(*a, cwd=None):
    return subprocess.run(["git", *a], capture_output=True, text=True, check=True, cwd=cwd).stdout.strip()


@pytest.fixture
def remote(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    git("clone", "-q", str(origin), str(work))
    git("config", "user.email", "t@t", cwd=work)
    git("config", "user.name", "t", cwd=work)
    (work / "x").write_text("1\n")
    git("add", "-A", cwd=work)
    git("commit", "-qm", "feat: good", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    (work / "x").write_text("2\n")
    git("add", "-A", cwd=work)
    git("commit", "-qm", "feat: bad change (#4)\n\nbody", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    monkeypatch.chdir(work)
    return origin, git("rev-parse", "HEAD", cwd=work)


def guard_env(monkeypatch, sha, *, subject="feat: bad change (#4)", jobs=("build", "k8s-check"), previous="success",
              reverts=0, conclusion="failure"):
    calls = []

    def fake_gh_json(args):
        url = args[0]
        if url.endswith("/actions/runs/9"):
            return {"head_sha": sha, "head_branch": "main", "conclusion": conclusion, "workflow_id": 7, "run_number": 12}
        if f"/commits/{sha}" in url and "?" not in url:
            return {"commit": {"message": subject + "\n\nbody"}}
        if url.endswith("/runs/9/jobs?per_page=100"):
            return {"jobs": [{"name": j, "conclusion": "failure"} for j in jobs] + [{"name": "docker-sbom", "conclusion": "success"}]}
        if "/workflows/7/runs" in url:
            return {"workflow_runs": [{"run_number": 12, "conclusion": "failure"}, {"run_number": 11, "conclusion": previous}]}
        if "/commits?sha=" in url:
            return [{"commit": {"message": f"{ap.REVERT_PREFIX}: r"}}] * reverts
        raise AssertionError(url)

    def fake_gh(*a):
        calls.append(a)
        return subprocess.CompletedProcess(a, 0, "https://github.com/o/r/issues/1", "")
    monkeypatch.setattr(ap, "gh_json", fake_gh_json)
    monkeypatch.setattr(ap, "_gh", fake_gh)
    monkeypatch.setattr(ap, "collect_failure_logs", lambda *a, **k: "FAILED tests/integration/test_mgmt.py::test_x")
    monkeypatch.setenv("AGENT_PUSH_TOKEN", "tok")
    return calls


def guard_args():
    return argparse.Namespace(repo="o/r", run_id=9, base_branch="main", max_reverts=3)


class TestMainGuard:
    def test_a_code_change_that_turned_main_red_is_reverted_and_queued_to_be_redone(self, remote, monkeypatch):
        origin, sha = remote
        calls = guard_env(monkeypatch, sha)
        assert ap.cmd_main_guard(guard_args()) == 0
        subjects = git("--git-dir", str(origin), "log", "--format=%s", "main").splitlines()
        assert subjects[0].startswith(f"{ap.REVERT_PREFIX}: feat: bad change (#4)") and "pipeline red" in subjects[0]
        assert git("--git-dir", str(origin), "show", "main:x") == "1"                    # the bad change is out
        assert git("--git-dir", str(origin), "log", "-1", "--format=%an", "main") == ap.AGENT_AUTHOR
        issue = next(c for c in calls if c[:2] == ("issue", "create"))
        assert "agent" in issue and "Redo: feat: bad change (#4)" in issue

    @pytest.mark.parametrize("kwargs,why", [
        ({"jobs": ("security-gate-trivy",)}, "not ones a code change causes"),
        ({"previous": "failure"}, "already red before"),
        ({"subject": f"{ap.REVERT_PREFIX}: feat: bad (pipeline red)"}, "not reverting a revert"),
        ({"reverts": 3}, "circuit breaker"),
    ])
    def test_it_does_not_revert_when_the_change_is_not_the_likely_cause(self, remote, monkeypatch, kwargs, why):
        origin, sha = remote
        calls = guard_env(monkeypatch, sha, **kwargs)
        assert ap.cmd_main_guard(guard_args()) == 0
        assert git("--git-dir", str(origin), "rev-parse", "main") == sha                  # main untouched
        assert not any(c[:2] == ("issue", "create") for c in calls)
        assert any(why in " ".join(c) for c in calls)                                     # and it says why, on the commit

    def test_a_green_or_foreign_run_is_ignored(self, remote, monkeypatch):
        origin, sha = remote
        calls = guard_env(monkeypatch, sha, conclusion="success")
        assert ap.cmd_main_guard(guard_args()) == 0 and calls == []

    def test_without_the_push_token_it_fails_loudly_instead_of_pretending(self, remote, monkeypatch):
        _, sha = remote
        guard_env(monkeypatch, sha)
        monkeypatch.delenv("AGENT_PUSH_TOKEN")
        assert ap.cmd_main_guard(guard_args()) == 1
