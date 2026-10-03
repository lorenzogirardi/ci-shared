"""The sweep keeps itself current without anyone running anything: a PR that fell
behind the base branch is brought up to date (so its CI describes current code),
and repair attempts are capped so being triggered often cannot loop."""

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import pr_review_sweep as sweep  # noqa: E402


def commit(name):
    return {"commit": {"author": {"name": name}}}


class TestAutofixStreak:
    def test_counts_only_the_trailing_autofix_commits(self):
        commits = [commit("lorenzo"), commit(sweep.AUTOFIX_AUTHOR), commit("renovate"), commit(sweep.AUTOFIX_AUTHOR),
                   commit(sweep.AUTOFIX_AUTHOR)]
        assert sweep.autofix_streak(commits) == 2

    def test_a_person_or_bot_commit_on_top_resets_it(self):
        assert sweep.autofix_streak([commit(sweep.AUTOFIX_AUTHOR), commit("renovate")]) == 0
        assert sweep.autofix_streak([]) == 0


class TestHelpers:
    def test_behind_is_read_from_the_mergeable_state(self, monkeypatch):
        monkeypatch.setattr(sweep, "gh_json", lambda a: {"mergeable_state": "behind"})
        assert sweep.pr_is_behind("o/r", 5)
        monkeypatch.setattr(sweep, "gh_json", lambda a: {"mergeable_state": "unknown"})
        assert not sweep.pr_is_behind("o/r", 5)

    def test_the_update_uses_the_push_token_not_the_default_one(self, monkeypatch):
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(cmd=cmd, token=kw["env"]["GH_TOKEN"])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setenv("GH_TOKEN", "default-github-token")
        monkeypatch.setenv("UPDATE_TOKEN", "push-pat")
        assert sweep.update_branch("o/r", 5)
        assert seen["token"] == "push-pat" and seen["cmd"][-1] == "repos/o/r/pulls/5/update-branch"

    def test_abandoning_labels_comments_and_closes_without_touching_the_base(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sweep, "run", lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
        sweep.abandon_pr("o/r", 5, "3 automatic fixes in a row did not make CI pass")
        assert any(sweep.ABANDONED_LABEL in " ".join(c) for c in calls)
        close = next(c for c in calls if c[:3] == ["gh", "pr", "close"])
        assert "Nothing was merged" in " ".join(close)


PR = {"number": 5, "title": "Update x", "user": {"login": "renovate[bot]"}, "base": {"ref": "main", "sha": "b" * 40},
      "head": {"sha": "a" * 40, "ref": "renovate/x", "repo": {"full_name": "o/r"}}}


@pytest.fixture
def sweep_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "system.txt").write_text("s")
    monkeypatch.setattr(sweep, "list_open_prs", lambda repo: [PR])

    def argv(*extra):
        monkeypatch.setattr(sys, "argv", ["sweep", "--repo", "o/r", "--system-file", "system.txt",
                                          "--ai-script", "x", "--authors", "renovate[bot]", *extra])
    return argv


class TestMainLoop:
    def test_a_pr_behind_main_is_updated_not_judged_on_its_stale_checks(self, sweep_env, monkeypatch):
        updated = []
        monkeypatch.setattr(sweep, "pr_is_behind", lambda repo, n: True)
        monkeypatch.setattr(sweep, "update_branch", lambda repo, n: updated.append(n) or True)
        monkeypatch.setattr(sweep, "existing_sweep_comment", lambda *a: pytest.fail("a stale PR must not be judged"))
        sweep_env("--update-behind")
        assert sweep.main() == 0 and updated == [5]

    def test_without_the_flag_behind_is_ignored(self, sweep_env, monkeypatch):
        monkeypatch.setattr(sweep, "pr_is_behind", lambda *a: pytest.fail("not asked to"))
        monkeypatch.setattr(sweep, "existing_sweep_comment", lambda *a: {"body": f"reviewed-sha: {'a' * 40}"})
        sweep_env()
        assert sweep.main() == 0

    def test_too_many_automatic_fixes_in_a_row_end_in_abandonment_not_another_attempt(self, sweep_env, monkeypatch):
        abandoned = []
        monkeypatch.setattr(sweep, "existing_sweep_comment", lambda *a: None)
        monkeypatch.setattr(sweep, "checks_state", lambda *a, **k: ("failing", "checks failing"))
        monkeypatch.setattr(sweep, "gh_json", lambda a: [commit(sweep.AUTOFIX_AUTHOR), commit(sweep.AUTOFIX_AUTHOR)])
        monkeypatch.setattr(sweep, "abandon_pr", lambda repo, n, why: abandoned.append((n, why)))
        monkeypatch.setattr(sweep, "autofix_one", lambda *a, **k: pytest.fail("the cap was reached: no new attempt"))
        sweep_env("--autofix", "--max-autofix-commits", "2")
        assert sweep.main() == 0
        assert abandoned and abandoned[0][0] == 5 and "2 automatic fixes" in abandoned[0][1]

    def test_below_the_cap_the_autofix_still_runs(self, sweep_env, monkeypatch):
        attempted = []
        monkeypatch.setattr(sweep, "existing_sweep_comment", lambda *a: None)
        monkeypatch.setattr(sweep, "checks_state", lambda *a, **k: ("failing", "checks failing"))
        monkeypatch.setattr(sweep, "gh_json", lambda a: [commit(sweep.AUTOFIX_AUTHOR)])
        monkeypatch.setattr(sweep, "abandon_pr", lambda *a: pytest.fail("below the cap"))
        monkeypatch.setattr(sweep, "autofix_one", lambda *a, **k: attempted.append(1) or ("exhausted", "no fix"))
        monkeypatch.setattr(sweep, "post_comment", lambda *a, **k: None)
        sweep_env("--autofix", "--max-autofix-commits", "2")
        assert sweep.main() == 0 and attempted == [1]
