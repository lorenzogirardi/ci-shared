"""Merge authority and the safety net after the merge: a PR merges only when its
head commit is certified by a trusted author and CI is green; if the base branch
turns red, the culprit is reverted by itself and the work is queued again."""

import argparse
import json
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
                                    pr_data(head={"sha": HEAD, "repo": {"full_name": "fork/r"}})])
    def test_draft_closed_or_fork_is_never_merged(self, gate, pr):
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
    monkeypatch.setattr(sys.modules[__name__], "GREEN_SHA", git("rev-parse", "HEAD", cwd=work), raising=False)
    (work / "x").write_text("2\n")
    git("add", "-A", cwd=work)
    git("commit", "-qm", "feat: bad change (#4)\n\nbody", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    monkeypatch.chdir(work)
    return origin, git("rev-parse", "HEAD", cwd=work)


def guard_env(monkeypatch, sha, *, subject="feat: bad change (#4)", jobs=("build", "k8s-check"), previous="success",
              reverts=0, conclusion="failure", attempt=2, later=None):
    calls = type("Calls", (list,), {})()
    reruns = []
    monkeypatch.setattr(ap, "rerun_failed_jobs", lambda repo, run_id: reruns.append((repo, run_id)) or True)
    calls.reruns = reruns

    def fake_gh_json(args):
        url = args[0]
        if url.endswith("/actions/runs/9"):
            return {"head_sha": sha, "head_branch": "main", "conclusion": conclusion, "workflow_id": 7, "run_number": 12,
                    "run_attempt": attempt}
        if f"/commits/{sha}" in url and "?" not in url:
            return {"commit": {"message": subject + "\n\nbody"}}
        if url.endswith("/runs/9/jobs?per_page=100"):
            return {"jobs": [{"name": j, "conclusion": "failure"} for j in jobs] + [{"name": "docker-sbom", "conclusion": "success"}]}
        if "/workflows/7/runs" in url:
            return {"workflow_runs": ([{"run_number": 13, "conclusion": later}] if later else [])
                                     + [{"run_number": 12, "conclusion": "failure"},
                                        {"run_number": 11, "conclusion": previous, "head_sha": GREEN_SHA}]}
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


GREEN_SHA = ""


def guard_args(**over):
    base = dict(repo="o/r", run_id=9, base_branch="main", max_reverts=3, dry_run=False)
    base.update(over)
    return argparse.Namespace(**base)


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

    def test_several_commits_between_runs_all_go_back_to_the_last_green_state(self, remote, monkeypatch):
        origin, _ = remote
        work = pathlib.Path.cwd()
        (work / "y").write_text("y\n")
        git("add", "-A")
        git("commit", "-qm", "Done  by Github Actions   Job changemanifest: 9")        # bookkeeping: left alone
        (work / "z").write_text("z\n")
        git("add", "-A")
        git("commit", "-qm", "feat: second change")
        git("push", "-q", "origin", "HEAD:main")
        tip = git("rev-parse", "HEAD")
        calls = guard_env(monkeypatch, tip, subject="feat: second change")
        assert ap.cmd_main_guard(guard_args()) == 0
        assert git("--git-dir", str(origin), "show", "main:x") == "1"                 # the bad change is out too
        assert git("--git-dir", str(origin), "show", "main:y") == "y"                 # bookkeeping stays
        with pytest.raises(subprocess.CalledProcessError):
            git("--git-dir", str(origin), "show", "main:z")                           # second change reverted
        subject = git("--git-dir", str(origin), "log", "-1", "--format=%s", "main")
        assert subject.startswith(f"{ap.REVERT_PREFIX}: 2 commits since the last green run")
        issue = next(c for c in calls if c[:2] == ("issue", "create"))
        assert "Redo: 2 reverted changes" in issue

    def test_only_bookkeeping_since_the_last_green_run_is_not_reverted(self, remote, monkeypatch):
        origin, sha = remote
        work = pathlib.Path.cwd()
        git("reset", "-q", "--hard", GREEN_SHA)
        git("commit", "-q", "--allow-empty", "-m", "docs(changelog): update for abc1234")
        git("push", "-q", "-f", "origin", "HEAD:main")
        tip = git("rev-parse", "HEAD")
        calls = guard_env(monkeypatch, tip, subject="docs(changelog): update for abc1234")
        assert ap.cmd_main_guard(guard_args()) == 0
        assert git("--git-dir", str(origin), "rev-parse", "main") == tip and work.exists()
        assert not any(c[:2] == ("issue", "create") for c in calls)

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

    def test_a_first_failure_is_re_run_once_before_anything_is_reverted(self, remote, monkeypatch):
        origin, sha = remote
        calls = guard_env(monkeypatch, sha, attempt=1)
        assert ap.cmd_main_guard(guard_args()) == 0
        assert calls.reruns == [("o/r", 9)]                                                # only the failed jobs, once
        assert git("--git-dir", str(origin), "rev-parse", "main") == sha                  # nothing reverted yet
        assert not any(c[:2] == ("issue", "create") for c in calls)
        assert any("rule out a flake" in " ".join(c) for c in calls)

    def test_the_second_failure_is_judged_for_real(self, remote, monkeypatch):
        origin, sha = remote
        calls = guard_env(monkeypatch, sha, attempt=2)
        assert ap.cmd_main_guard(guard_args()) == 0
        assert calls.reruns == []                                                          # no endless re-run loop
        assert git("--git-dir", str(origin), "show", "main:x") == "1"                     # reverted

    def test_a_green_or_foreign_run_is_ignored(self, remote, monkeypatch):
        origin, sha = remote
        calls = guard_env(monkeypatch, sha, conclusion="success")
        assert ap.cmd_main_guard(guard_args()) == 0 and calls == []

    def test_without_the_push_token_it_fails_loudly_instead_of_pretending(self, remote, monkeypatch):
        _, sha = remote
        guard_env(monkeypatch, sha)
        monkeypatch.delenv("AGENT_PUSH_TOKEN")
        assert ap.cmd_main_guard(guard_args()) == 1


class TestMergeEverythingOpen:
    """Found live: a certified PR with green CI sat open, because (1) the agent workflows post as
    github-actions[bot], not as the repository owner the gate trusted, and (2) when CI finished before
    the certification no later event picked it up. The gate must work whichever finishes first."""

    def test_the_bot_that_posts_the_certification_is_trusted_by_default(self):
        root = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows"
        for name in ("reusable_agent-merge.yml", "reusable_agent-change.yml"):
            text = (root / name).read_text()
            assert "format('{0},github-actions[bot]', github.repository_owner)" in text, name
        assert ap.is_certified([cert(login="github-actions[bot]")], HEAD, {"owner", "github-actions[bot]"})
        assert not ap.is_certified([cert(login="github-actions[bot]")], HEAD, {"owner"})      # not trusted unless named

    def test_pr_zero_judges_every_open_pr_except_renovates(self, monkeypatch):
        judged = []

        def fake_gh_json(args):
            url = args[0]
            if url.startswith("repos/o/r/pulls?state=open"):
                return [{"number": 5, "user": {"login": "owner"}}, {"number": 6, "user": {"login": "renovate[bot]"}},
                        {"number": 7, "user": {"login": "someone"}}]
            raise AssertionError(url)
        monkeypatch.setattr(ap, "gh_json", fake_gh_json)
        monkeypatch.setattr(ap, "merge_one", lambda args, n: judged.append(n) or "ok")
        assert ap.cmd_merge_gate(gate_args(pr=0)) == 0
        assert judged == [5, 7]                                  # Renovate's PR belongs to the sweep

    def test_with_nothing_open_it_just_says_so(self, monkeypatch):
        monkeypatch.setattr(ap, "gh_json", lambda a: [])
        monkeypatch.setattr(ap, "merge_one", lambda *a: pytest.fail("nothing to judge"))
        assert ap.cmd_merge_gate(gate_args(pr=0)) == 0

    def test_a_pr_certified_after_its_ci_finished_is_merged_by_the_next_pass(self, gate):
        merges = gate(comments=[cert(login="github-actions[bot]")])
        ap.cmd_merge_gate(gate_args(trusted="owner,github-actions[bot]"))
        assert len(merges) == 1


def abandoned(sha=HEAD):
    return {"user": {"login": "github-actions[bot]"}, "body": f"<!-- agent-pr -->\n<!-- agent-abandoned: {sha} -->\nabandoned"}


class TestAbandonmentIsTerminalPerCommit:
    """Found live: a PR the agent had abandoned was handed to the repair loop again by the next failed CI run,
    which paid for the same conclusion twice. Abandonment is bound to the head commit like the certification:
    the same commit is never retried, a new push by the author starts a fresh attempt."""

    def test_an_abandoned_commit_is_never_merged_even_if_it_was_certified_earlier(self, gate):
        merges = gate(comments=[cert(), abandoned()])
        ap.cmd_merge_gate(gate_args())
        assert merges == []

    def test_an_abandonment_of_an_older_commit_does_not_block_a_new_one(self, gate):
        merges = gate(comments=[abandoned(OLD), cert()])
        ap.cmd_merge_gate(gate_args())
        assert len(merges) == 1

    def test_the_label_blocks_until_the_new_commit_is_certified(self, gate):
        labelled = pr_data(labels=[{"name": ap.ABANDONED_LABEL}])
        blocked = gate(pr=labelled, comments=[])
        ap.cmd_merge_gate(gate_args())
        assert blocked == []
        merged = gate(pr=labelled, comments=[cert()])                       # the author pushed a fix and it was certified
        ap.cmd_merge_gate(gate_args())
        assert len(merged) == 1

    def test_the_repair_loop_does_not_run_again_on_an_abandoned_commit(self, remote, monkeypatch, tmp_path):
        out = tmp_path / "o.txt"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        sha = git("rev-parse", "HEAD")
        monkeypatch.setattr(ap, "gh_json", lambda a: [abandoned(sha)])
        for mode in ("ci", "pr"):
            out.unlink(missing_ok=True)
            ap.cmd_guard_change(type("A", (), {"mode": mode, "repo": "o/r", "sha": sha, "pr": 5, "max_streak": 3})())
            assert out.read_text().strip() == "skip=true", mode

    def test_a_new_commit_after_the_abandonment_gets_a_fresh_attempt(self, remote, monkeypatch, tmp_path):
        out = tmp_path / "o.txt"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        monkeypatch.setattr(ap, "gh_json", lambda a: [abandoned(OLD)])
        ap.cmd_guard_change(type("A", (), {"mode": "ci", "repo": "o/r", "sha": git("rev-parse", "HEAD"), "pr": 5, "max_streak": 3})())
        assert out.read_text().strip() == "skip=false"

    def test_the_comment_binds_the_abandonment_to_the_head_the_agent_worked_on(self):
        text = ap.build_change_comment({"outcome": "abandoned", "head_sha": HEAD, "notes": ["why"], "commits": 0}, None)
        assert f"<!-- agent-abandoned: {HEAD} -->" in text and "agent-certified" not in text

    def test_a_certification_takes_the_abandoned_label_off(self, remote, monkeypatch):
        (pathlib.Path(".ai/agent-run")).mkdir(parents=True, exist_ok=True)
        (pathlib.Path(".ai/agent-run/result.json")).write_text(json.dumps(
            {"outcome": "clean", "commits": 0, "rounds": [], "findings": [], "notes": [], "final_sha": HEAD, "cost_usd": 0}))
        calls = []
        monkeypatch.setattr(ap, "_gh", lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
        monkeypatch.setattr(ap, "gh_json", lambda a: [])
        monkeypatch.setattr(ap, "post_comment", lambda *a: None)
        ap.cmd_publish_pr(type("A", (), {"repo": "o/r", "pr": 5, "head_ref": "feature"})())
        assert any("--remove-label" in c and ap.ABANDONED_LABEL in c for c in calls)


class TestGuardDoesNotFightARepair:
    def test_a_later_green_run_means_it_was_already_repaired_so_nothing_is_reverted(self, remote, monkeypatch):
        origin, sha = remote
        calls = guard_env(monkeypatch, sha, later="success")
        assert ap.cmd_main_guard(guard_args()) == 0
        assert git("--git-dir", str(origin), "rev-parse", "main") == sha
        assert not any(c[:2] == ("issue", "create") for c in calls)
        assert any("already repaired" in " ".join(c) for c in calls)

    def test_a_later_RED_run_does_not_stop_the_revert(self, remote, monkeypatch):
        origin, sha = remote
        guard_env(monkeypatch, sha, later="failure")
        assert ap.cmd_main_guard(guard_args()) == 0
        assert git("--git-dir", str(origin), "show", "main:x") == "1"

    def test_dry_run_decides_and_prints_but_changes_nothing(self, remote, monkeypatch, capsys):
        origin, sha = remote
        calls = guard_env(monkeypatch, sha, attempt=2)
        monkeypatch.delenv("AGENT_PUSH_TOKEN", raising=False)                     # not even a token is needed
        assert ap.cmd_main_guard(guard_args(dry_run=True)) == 0
        out = capsys.readouterr().out
        assert "[dry run] would revert 1 change(s)" in out and "feat: bad change (#4)" in out
        assert git("--git-dir", str(origin), "rev-parse", "main") == sha
        assert calls == [] and calls.reruns == []

    def test_dry_run_on_a_first_failure_reports_the_rerun_it_would_do(self, remote, monkeypatch, capsys):
        _, sha = remote
        calls = guard_env(monkeypatch, sha, attempt=1)
        assert ap.cmd_main_guard(guard_args(dry_run=True)) == 0
        assert "would re-run the failed jobs" in capsys.readouterr().out
        assert calls.reruns == []


def test_the_gate_only_merges_into_its_own_base_branch(monkeypatch):
    """A pull request against another branch (the canary's throwaway base) is not this gate's to merge."""
    pr = {"state": "open", "draft": False, "labels": [], "head": {"sha": "a" * 40, "repo": {"full_name": "o/r"}},
          "base": {"ref": "canary/1-base"}}
    monkeypatch.setattr(ap, "gh_json", lambda a: pr)
    monkeypatch.setattr(ap, "try_merge", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not merge")))
    args = type("A", (), {"repo": "o/r", "base_branch": "main", "trusted": "x", "max_reverts": 3, "required_checks": "",
                          "merge_method": "squash", "poll_seconds": 0})()
    assert "its base is canary/1-base" in ap.merge_one(args, 5)
