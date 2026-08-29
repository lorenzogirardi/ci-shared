"""Tests for the langgraph propose/explore/verify graph.

Only `_call_model` and `run` (git plumbing) are faked -- `apply_fix`,
`run_verify`, and the read/find/grep/list dispatch functions are the real,
already-tested pure functions from pr_review_sweep.py, exercised against real
files in tmp_path. This checks the graph's control flow (propose -> explore
-> propose, propose -> apply -> retry -> propose, budget exhaustion, decline,
malformed reply) reproduces autofix_one's pre-refactor behavior exactly.
"""

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import autofix_core  # noqa: E402
from autofix_core import _budget_line, run_autofix_graph  # noqa: E402


class TestBudgetLine:
    """Real incident this addresses: a migration correctly renamed a class in
    one file, then kept exploring instead of wrapping up, and ran out of
    rounds before a second call site of the same rename got an edit."""

    def test_states_the_round_and_total(self):
        line = _budget_line(1, 20)
        assert "round 1 of 20" in line

    def test_no_urgency_nudge_with_plenty_of_budget_left(self):
        assert "left after this one" not in _budget_line(1, 20)

    def test_urgency_nudge_appears_when_budget_is_nearly_spent(self):
        line = _budget_line(19, 20)
        assert "1 round(s) left after this one" in line
        assert "propose a concrete edit, do that now" in line

    def test_urgency_nudge_on_the_last_round(self):
        line = _budget_line(20, 20)
        assert "0 round(s) left after this one" in line


def _fix_reply(edits, explanation="fix it"):
    return "```json\n" + json.dumps({"explanation": explanation, "edits": edits}) + "\n```"


def _scripted_model(monkeypatch, replies):
    """Each call to _call_model returns the next scripted reply."""
    calls = iter(replies)

    def fake(args, system_file, user_path, number):
        return next(calls, None)

    monkeypatch.setattr(autofix_core, "_call_model", fake)


def _common_kwargs(**overrides):
    kwargs = dict(
        header="PR #1 title: bump pydantic",
        logs="ERROR: something failed",
        diff="--- a/requirements.txt\n+++ b/requirements.txt\n",
        verify_command="",
        verify_timeout=5,
        max_attempts=5,
        ai_script="openrouter_ai.py",
        max_chars=140_000,
        timeout=30,
        identifier=1,
    )
    kwargs.update(overrides)
    return kwargs


class TestReadyPath:
    def test_immediate_fix_that_verifies_is_ready_in_one_attempt(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "requirements.txt").write_text("pydantic==2.11.7\n")
        _scripted_model(monkeypatch, [
            _fix_reply([{"file": "requirements.txt", "find": "2.11.7", "replace": "2.13.4"}]),
        ])
        result = run_autofix_graph(**_common_kwargs(verify_command="exit 0"))
        assert result["outcome"] == "ready"
        assert result["changed"] == ["requirements.txt"]
        assert result["explanation"] == "fix it"
        assert "verified locally in 1 attempt(s)" in result["detail"]
        assert (tmp_path / "requirements.txt").read_text() == "pydantic==2.13.4\n"

    def test_explore_then_fix_still_reaches_ready(self, tmp_path, monkeypatch):
        """Real incident this graph replaces: reading a file used to cost the
        model its whole context on the next round via a flattened string --
        history must carry the read result into round 2's prompt."""
        monkeypatch.chdir(tmp_path)
        app = tmp_path / "app"
        app.mkdir()
        (app / "mcpserver.py").write_text("class MCPServer:\n    pass\n")
        (tmp_path / "requirements.txt").write_text("mcp==1.0\n")

        seen_prompts = []

        def fake(args, system_file, user_path, number):
            seen_prompts.append(pathlib.Path(user_path).read_text())
            if len(seen_prompts) == 1:
                return '```json\n{"read": "app/mcpserver.py"}\n```'
            return _fix_reply([{"file": "requirements.txt", "find": "1.0", "replace": "2.0"}])

        monkeypatch.setattr(autofix_core, "_call_model", fake)
        result = run_autofix_graph(**_common_kwargs(verify_command="exit 0"))

        assert result["outcome"] == "ready"
        # Round 2's prompt must contain what round 1 learned.
        assert "class MCPServer" in seen_prompts[1]
        assert (tmp_path / "requirements.txt").read_text() == "mcp==2.0\n"


class TestRetryOnFailedVerify:
    def test_failed_verify_reverts_and_retries_with_the_real_output_fed_back(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "requirements.txt").write_text("pydantic==2.11.7\n")
        monkeypatch.setattr(autofix_core, "run", lambda *a, **k: None)  # skip real git checkout

        seen_prompts = []

        def fake(args, system_file, user_path, number):
            seen_prompts.append(pathlib.Path(user_path).read_text())
            replace = "2.13.4" if len(seen_prompts) == 1 else "2.13.5"
            return _fix_reply([{"file": "requirements.txt", "find": "2.11.7" if len(seen_prompts) == 1 else "2.13.4",
                                 "replace": replace}])

        monkeypatch.setattr(autofix_core, "_call_model", fake)
        # First verify fails, second passes.
        verify_calls = {"n": 0}

        def fake_verify(command, timeout):
            verify_calls["n"] += 1
            if verify_calls["n"] == 1:
                return False, "ResolutionImpossible: pydantic conflict"
            return True, ""

        monkeypatch.setattr(autofix_core, "run_verify", fake_verify)
        result = run_autofix_graph(**_common_kwargs(verify_command="whatever"))

        assert result["outcome"] == "ready"
        assert "verified locally in 2 attempt(s)" in result["detail"]
        # The second prompt must carry the first attempt's real failure output.
        assert "ResolutionImpossible: pydantic conflict" in seen_prompts[1]

    def test_exhausts_after_max_attempts_and_reports_the_last_failure(self, tmp_path, monkeypatch):
        # A real git repo, not a mocked `run`: the graph's own revert-on-failed-verify
        # step (`git checkout -- <changed>`) must actually restore the anchor text
        # between rounds, or round 2's `find` would no longer match round 1's edit.
        monkeypatch.chdir(tmp_path)
        import subprocess
        (tmp_path / "requirements.txt").write_text("pydantic==2.11.7\n")
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
        subprocess.run(["git", "add", "requirements.txt"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
        monkeypatch.setattr(
            autofix_core, "_call_model",
            lambda *a, **k: _fix_reply([{"file": "requirements.txt", "find": "2.11.7", "replace": "2.13.4"}]),
        )
        monkeypatch.setattr(autofix_core, "run_verify", lambda c, t: (False, "still broken"))

        result = run_autofix_graph(**_common_kwargs(max_attempts=3, verify_command="whatever"))
        assert result["outcome"] == "exhausted"
        assert "used all 3 attempt(s)" in result["detail"]
        assert "still broken" in result["detail"]


class TestTerminalNonFixOutcomes:
    def test_decline_is_not_a_failure(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _scripted_model(monkeypatch, [_fix_reply([], explanation="no concrete fix found")])
        result = run_autofix_graph(**_common_kwargs())
        assert result["outcome"] == "declined"
        assert result["detail"] == "no concrete fix found"

    def test_malformed_reply_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _scripted_model(monkeypatch, ["not json at all, and no fenced block either"])
        result = run_autofix_graph(**_common_kwargs())
        assert result["outcome"] == "rejected"

    def test_model_call_failure_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _scripted_model(monkeypatch, [])  # exhausted iterator -> None on first call
        result = run_autofix_graph(**_common_kwargs())
        assert result["outcome"] == "skipped"
        assert "model call failed" in result["detail"]

    def test_invalid_edit_target_does_not_burn_a_verify_but_does_retry(self, tmp_path, monkeypatch):
        """apply_fix errors are recoverable, same as a failed verify -- the
        model can see and correct a bad file path on the next round, so this
        must not be treated as terminal after a single bad edit."""
        monkeypatch.chdir(tmp_path)
        calls = {"verify": 0}
        monkeypatch.setattr(autofix_core, "run_verify", lambda c, t: calls.__setitem__("verify", calls["verify"] + 1) or (True, ""))
        seen_prompts = []

        def fake(args, system_file, user_path, number):
            seen_prompts.append(pathlib.Path(user_path).read_text())
            return None if len(seen_prompts) > 1 else _fix_reply(
                [{"file": "does-not-exist.txt", "find": "a", "replace": "b"}]
            )

        monkeypatch.setattr(autofix_core, "_call_model", fake)
        result = run_autofix_graph(**_common_kwargs())

        assert calls["verify"] == 0  # the bad edit never reached run_verify
        assert result["outcome"] == "skipped"  # round 2's model call returned None
        assert "does-not-exist.txt" in seen_prompts[1]  # round 2 saw round 1's real error
        assert "does not exist" in seen_prompts[1]

    def test_recovers_from_a_bad_edit_and_still_reaches_ready(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "requirements.txt").write_text("pydantic==2.11.7\n")
        replies = [
            _fix_reply([{"file": "does-not-exist.txt", "find": "a", "replace": "b"}]),
            _fix_reply([{"file": "requirements.txt", "find": "2.11.7", "replace": "2.13.4"}]),
        ]
        _scripted_model(monkeypatch, replies)
        result = run_autofix_graph(**_common_kwargs(verify_command="exit 0"))
        assert result["outcome"] == "ready"
        assert "verified locally in 2 attempt(s)" in result["detail"]


class TestSelfCorrectingReply:
    """Real incident, verified end-to-end against flask-test-api PR #118: the
    model wrote a malformed first JSON block, caught its own mistake mid-reply
    ("Wait, that's wrong -- let me issue it correctly"), and wrote a second,
    correct block. The old first-match parser grabbed the abandoned attempt
    (an edit to a file literally named "repo") and burned the whole budget on
    it in round one -- the model's own correction never got a chance."""

    def test_grep_request_after_a_self_correction_is_recognized(self, monkeypatch):
        reply = (
            '```json\n{"explanation": "wrong tool", "edits": '
            '[{"file": "repo", "find": "FastMCP", "replace": "__GREP_ONLY__"}]}\n```\n\n'
            "Wait, that's wrong -- grep is a separate tool, not an edit. "
            "Let me issue it correctly:\n\n"
            '```json\n{"grep": "FastMCP"}\n```'
        )
        assert autofix_core.parse_grep_request(reply) == "FastMCP"
        assert autofix_core.parse_fix(reply) is None  # the edits shape is not what wins

    def test_full_graph_follows_the_corrected_request_not_the_abandoned_one(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "app.py").write_text("class FastMCP:\n    pass\n")
        reply = (
            '```json\n{"explanation": "wrong tool", "edits": '
            '[{"file": "repo", "find": "FastMCP", "replace": "__GREP_ONLY__"}]}\n```\n\n'
            "Wait, that's wrong. Let me issue it correctly:\n\n"
            '```json\n{"grep": "FastMCP"}\n```'
        )
        _scripted_model(monkeypatch, [reply])
        result = run_autofix_graph(**_common_kwargs(max_attempts=1))
        # Must explore (the corrected request), never try to apply an edit to "repo".
        assert result["outcome"] == "exhausted"
        assert "repo" not in (result.get("changed") or [])
