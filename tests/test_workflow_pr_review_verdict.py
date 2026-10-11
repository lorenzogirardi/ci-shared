"""The verdict on a pull request that touches `.github/workflows/`: such a pull
request is now reviewed like any other and the verdict is left as the commit
status `agent-review`, which the dependency bot waits for before it merges the
pull request itself."""

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import pr_review_sweep  # noqa: E402


def record_run(monkeypatch, returncode=0, stderr=""):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    monkeypatch.setattr(pr_review_sweep, "run", fake_run)
    return calls


def test_the_context_the_bot_waits_for_is_agent_review():
    assert pr_review_sweep.REVIEW_STATUS == "agent-review"


@pytest.mark.parametrize("state", ["pending", "success", "failure"])
def test_the_verdict_is_a_commit_status_on_the_head_commit(monkeypatch, state):
    calls = record_run(monkeypatch)
    pr_review_sweep.set_review_status("o/r", "abc1234", state, "verdict")

    cmd, kwargs = calls[0]
    assert "repos/o/r/statuses/abc1234" in cmd
    assert f"state={state}" in cmd
    assert "context=agent-review" in cmd
    assert "description=verdict" in cmd
    assert kwargs.get("check") is False


def test_the_description_is_capped_to_what_the_status_api_accepts(monkeypatch):
    calls = record_run(monkeypatch)
    pr_review_sweep.set_review_status("o/r", "abc1234", "failure", "z" * 300)

    cmd, _ = calls[0]
    assert [arg for arg in cmd if arg.startswith("description=")] == ["description=" + "z" * 140]


def test_a_status_that_cannot_be_written_is_a_warning_and_not_a_crash(monkeypatch, capsys):
    record_run(monkeypatch, returncode=1, stderr="Resource not accessible")
    pr_review_sweep.set_review_status("o/r", "abcdef1234", "pending", "waiting")

    assert "could not set the agent-review status on abcdef1" in capsys.readouterr().out
