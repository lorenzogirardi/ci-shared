"""Tests for the no-PR autofix path: a broken push straight to a protected
branch (no PR to push a fix to) gets a NEW branch + a NEW PR instead, so the
fix goes through the normal per-PR gate rather than landing on the protected
branch directly. `run_autofix_graph` itself is exercised by
test_autofix_core.py -- these tests only check main_autofix's own plumbing:
when it opens a PR, and what it does when it can't.
"""

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import main_autofix  # noqa: E402


def _common_args(tmp_path, **overrides):
    args = argparse_ns(
        repo="o/r",
        head_sha="deadbeef",
        base_sha="c0ffee",
        base_branch="main",
        run_number=42,
        ai_script="openrouter_ai.py",
        max_chars=140_000,
        timeout=30,
        verify_command_file="",
        verify_timeout=5,
        max_autofix_attempts=5,
    )
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def argparse_ns(**kwargs):
    import argparse
    return argparse.Namespace(**kwargs)


@pytest.fixture(autouse=True)
def _chdir_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield tmp_path


class TestNoLogsYet:
    def test_returns_zero_and_does_nothing_when_no_failure_logs_exist(self, monkeypatch):
        monkeypatch.setattr(main_autofix, "collect_failure_logs", lambda *a, **k: "")
        called = {"run": False}
        monkeypatch.setattr(main_autofix, "run", lambda *a, **k: called.__setitem__("run", True))
        rc = _run_main(monkeypatch, _common_args(None))
        assert rc == 0
        assert called["run"] is False


def _run_main(monkeypatch, args, existing_pr=""):
    # The dedup lookup shells out to `gh` through the same `run` these tests
    # replace wholesale; stub it so they keep exercising only their own path.
    monkeypatch.setattr(main_autofix, "open_autofix_pr", lambda *a, **k: existing_pr)
    monkeypatch.setattr(main_autofix.argparse.ArgumentParser, "parse_args", lambda self: args)
    return main_autofix.main()


class TestOpensAPROnSuccess:
    def test_ready_outcome_commits_pushes_and_opens_a_pr(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main_autofix, "collect_failure_logs", lambda *a, **k: "ERROR: boom")
        monkeypatch.setattr(main_autofix, "build_diff", lambda base, head, out: out.write_text("diff"))
        monkeypatch.setattr(main_autofix, "run_verify", lambda *a, **k: (True, ""))
        monkeypatch.setattr(
            main_autofix, "run_autofix_graph",
            lambda **kw: {"outcome": "ready", "detail": "fixed it (edited requirements.txt, verified locally in 1 attempt(s))",
                          "changed": ["requirements.txt"], "explanation": "fixed it"},
        )

        calls = []

        def fake_run(cmd, check=True, capture=True):
            calls.append(cmd)
            if cmd[:2] == ["git", "checkout"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[:2] == ["git", "config"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[:2] == ["git", "add"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[:2] == ["git", "commit"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[:2] == ["git", "push"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[:2] == ["gh", "pr"]:
                return subprocess.CompletedProcess(cmd, 0, "https://github.com/o/r/pull/999\n", "")
            raise AssertionError(f"unexpected command: {cmd}")

        monkeypatch.setattr(main_autofix, "run", fake_run)
        (tmp_path / "requirements.txt").write_text("x==1\n")

        rc = _run_main(monkeypatch, _common_args(tmp_path))
        assert rc == 0

        pr_create = next(c for c in calls if c[:2] == ["gh", "pr"])
        assert "--base" in pr_create and "main" in pr_create
        assert "--head" in pr_create and "autofix/main-42" in pr_create
        assert "--repo" in pr_create and "o/r" in pr_create

        push = next(c for c in calls if c[:2] == ["git", "push"])
        assert "HEAD:refs/heads/autofix/main-42" in push


class TestDoesNotOpenAPROnFailure:
    @pytest.mark.parametrize("outcome", ["declined", "rejected", "exhausted", "skipped"])
    def test_non_ready_outcome_pushes_nothing(self, monkeypatch, tmp_path, outcome):
        monkeypatch.setattr(main_autofix, "collect_failure_logs", lambda *a, **k: "ERROR: boom")
        monkeypatch.setattr(main_autofix, "build_diff", lambda base, head, out: out.write_text("diff"))
        monkeypatch.setattr(main_autofix, "run_verify", lambda *a, **k: (True, ""))
        monkeypatch.setattr(
            main_autofix, "run_autofix_graph",
            lambda **kw: {"outcome": outcome, "detail": "no fix", "changed": [], "explanation": ""},
        )

        def fail_if_pushed(cmd, check=True, capture=True):
            if cmd[:2] in (["git", "push"], ["gh", "pr"]):
                raise AssertionError(f"must not push or open a PR on outcome={outcome}")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(main_autofix, "run", fail_if_pushed)

        rc = _run_main(monkeypatch, _common_args(tmp_path))
        assert rc == 1


class TestOpenAutofixPr:
    @staticmethod
    def _fake_run(stdout, returncode=0):
        return lambda *a, **k: subprocess.CompletedProcess(a, returncode, stdout=stdout, stderr="")

    def test_finds_open_autofix_branch(self, monkeypatch):
        out = '[{"headRefName": "renovate/x", "url": "u1"}, {"headRefName": "autofix/main-7", "url": "u2"}]'
        monkeypatch.setattr(main_autofix, "run", self._fake_run(out))
        assert main_autofix.open_autofix_pr("o/r", "main") == "u2"

    def test_ignores_unrelated_prs(self, monkeypatch):
        monkeypatch.setattr(main_autofix, "run", self._fake_run('[{"headRefName": "renovate/x", "url": "u1"}]'))
        assert main_autofix.open_autofix_pr("o/r", "main") == ""

    def test_gh_failure_or_bad_json_does_not_block_autofix(self, monkeypatch):
        monkeypatch.setattr(main_autofix, "run", self._fake_run("", returncode=1))
        assert main_autofix.open_autofix_pr("o/r", "main") == ""
        monkeypatch.setattr(main_autofix, "run", self._fake_run("not json"))
        assert main_autofix.open_autofix_pr("o/r", "main") == ""

    def test_main_skips_when_autofix_pr_already_open(self, monkeypatch):
        monkeypatch.setattr(main_autofix, "collect_failure_logs", lambda *a, **k: "boom")
        monkeypatch.setattr(main_autofix, "build_diff", lambda *a, **k: pytest.fail("must not reach the model"))
        assert _run_main(monkeypatch, _common_args(None), existing_pr="https://x/pr/1") == 0
