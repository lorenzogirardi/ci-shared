"""Strictness of everything that turns model text into an action."""

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import agent_lib as lib  # noqa: E402

DIFF = """diff --git a/app/x.py b/app/x.py
--- a/app/x.py
+++ b/app/x.py
@@ -1,3 +1,4 @@
 import os
-old = 1
+new = 2
+extra = 3
 tail = 4
diff --git a/app/y.py b/app/y.py
--- a/app/y.py
+++ b/app/y.py
@@ -10,2 +10,2 @@
 keep
-- not a header
+changed = True
"""


def finding(**over):
    base = {"severity": "high", "file": "app/x.py", "line": 2, "category": "bug",
            "evidence": "new = 2", "problem": "wrong value", "suggestion": "use 1"}
    base.update(over)
    return base


class TestParseFindings:
    def test_valid(self):
        out = lib.parse_findings({"findings": [finding()]}, "A")
        assert out and out[0].blocking and out[0].reviewers == ["A"]

    def test_empty_list_is_valid_and_not_none(self):
        assert lib.parse_findings({"findings": []}, "A") == []

    @pytest.mark.parametrize("bad", [
        {"severity": "blocker"}, {"line": 0}, {"line": True}, {"line": "2"}, {"file": "../etc/passwd"},
        {"file": "/abs"}, {"evidence": ""}, {"problem": "  "}, {"suggestion": None},
    ])
    def test_malformed_item_invalidates_the_reply(self, bad):
        assert lib.parse_findings({"findings": [finding(**bad)]}, "A") is None

    def test_not_a_findings_object(self):
        assert lib.parse_findings({"nope": []}, "A") is None
        assert lib.parse_findings(None, "A") is None


class TestDiff:
    def test_ranges_and_header_lookalike_line(self):
        ranges = lib.diff_ranges(DIFF)
        assert ranges["app/x.py"] == [(1, 4)]
        # the removed line "-- not a header" must not be read as a file header
        assert set(ranges) == {"app/x.py", "app/y.py"}
        assert ranges["app/y.py"] == [(10, 11)]

    def test_validate_drops_unknown_files_and_lines_outside_hunks(self):
        ranges = lib.diff_ranges(DIFF)
        fs = lib.parse_findings({"findings": [
            finding(), finding(file="app/z.py"), finding(line=99), finding(file="app/y.py", line=11)]}, "A")
        kept, dropped = lib.validate_findings(fs, ranges)
        assert [(f.file, f.line) for f in kept] == [("app/x.py", 2), ("app/y.py", 11)]
        assert [reason for _, reason in dropped] == ["file is not part of the diff", "line is outside the changed hunks"]

    def test_annotate_numbers_added_and_context_lines_only(self):
        out = lib.annotate_diff(DIFF).splitlines()
        assert "L1| import os" in out
        assert "-old = 1" in out
        assert "L2|+new = 2" in out and "L3|+extra = 3" in out and "L4| tail = 4" in out


class TestDedup:
    def test_same_place_same_topic_merged_keeping_worst_severity_and_both_reviewers(self):
        a = lib.parse_findings({"findings": [finding(severity="medium", line=2)]}, "A")[0]
        b = lib.parse_findings({"findings": [finding(severity="critical", line=3, problem="wrong value assigned")]}, "B")[0]
        merged = lib.dedup_findings([a, b])
        assert len(merged) == 1 and merged[0].severity == "critical"
        assert merged[0].reviewers == ["B", "A"] or sorted(merged[0].reviewers) == ["A", "B"]

    def test_different_files_or_distant_lines_stay_separate(self):
        a = lib.parse_findings({"findings": [finding()]}, "A")[0]
        b = lib.parse_findings({"findings": [finding(line=40, category="design", problem="unrelated")]}, "B")[0]
        c = lib.parse_findings({"findings": [finding(file="app/y.py")]}, "B")[0]
        assert len(lib.dedup_findings([a, b, c])) == 3


class TestParseChanges:
    def test_edit_and_create(self):
        data = {"explanation": "e", "changes": [
            {"file": "a.py", "find": "x", "replace": "y"}, {"file": "tests/t.py", "content": "def test(): pass\n"}]}
        assert lib.parse_changes(data) == (data["changes"], "e")

    @pytest.mark.parametrize("change", [
        {"file": ".github/workflows/ci.yml", "content": "x"}, {"file": ".git/config", "content": "x"},
        {"file": "../x", "content": "x"}, {"file": "a", "find": "x", "replace": "x"},
        {"file": "a", "find": "", "replace": "x"}, {"file": "a", "content": ""},
        {"file": "a", "content": "x" * (lib.MAX_CONTENT_CHARS + 1)},
    ])
    def test_rejected(self, change):
        assert lib.parse_changes({"explanation": "e", "changes": [change]}) is None

    def test_too_many_changes_and_allowed_predicate(self):
        many = {"explanation": "e", "changes": [{"file": f"f{i}", "content": "x"} for i in range(lib.MAX_CHANGES + 1)]}
        assert lib.parse_changes(many) is None
        ok = {"explanation": "e", "changes": [{"file": "app/x.py", "content": "x"}]}
        assert lib.parse_changes(ok, allowed=lambda p: p.endswith(".md")) is None


class TestApplyChanges:
    @pytest.fixture(autouse=True)
    def _repo(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", *cmd], check=True)
        pathlib.Path("a.txt").write_text("one two")
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-qm", "i"], check=True)

    def test_edit_and_create_then_revert(self):
        applied = lib.apply_changes([{"file": "a.txt", "find": "one", "replace": "1"},
                                     {"file": "sub/new.txt", "content": "hi"}])
        assert applied.error is None and applied.files == ["a.txt", "sub/new.txt"]
        assert pathlib.Path("a.txt").read_text() == "1 two"
        lib.revert(applied)
        assert pathlib.Path("a.txt").read_text() == "one two" and not pathlib.Path("sub/new.txt").exists()

    def test_create_over_existing_file_writes_nothing(self):
        applied = lib.apply_changes([{"file": "a.txt", "find": "one", "replace": "1"},
                                     {"file": "a.txt", "content": "clobber"}])
        assert applied.error and pathlib.Path("a.txt").read_text() == "one two"

    def test_ambiguous_anchor_writes_nothing(self):
        pathlib.Path("a.txt").write_text("x x")
        applied = lib.apply_changes([{"file": "a.txt", "find": "x", "replace": "y"},
                                     {"file": "new.txt", "content": "n"}])
        assert applied.error and not pathlib.Path("new.txt").exists()


class TestIsolatedVerify:
    def test_secrets_are_invisible_to_the_checks(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
        monkeypatch.setenv("GH_TOKEN", "ghp_secret")
        monkeypatch.setenv("AGENT_PUSH_TOKEN", "pat_secret")
        ok, out = lib.run_verify_isolated(
            'test -z "${OPENROUTER_API_KEY:-}" && test -z "${GH_TOKEN:-}" && test -z "${AGENT_PUSH_TOKEN:-}" && test "$CI" = true', 30)
        assert ok, out

    def test_failure_returns_the_tail_and_timeout_is_reported(self):
        ok, out = lib.run_verify_isolated("echo boom >&2; exit 3", 30)
        assert not ok and "boom" in out
        ok, out = lib.run_verify_isolated("sleep 5", 1)
        assert not ok and "exceeded" in out

    def test_empty_command_passes(self):
        assert lib.run_verify_isolated("  ", 5) == (True, "")


class TestModelCaller:
    def _script(self, tmp_path, body):
        path = tmp_path / "ai.py"
        path.write_text(body)
        return str(path)

    def test_records_usage_and_cost(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        script = self._script(tmp_path, (
            "import sys, json\n"
            "a = sys.argv\n"
            "open(a[a.index('--usage-file')+1], 'w').write(json.dumps({'cost_usd': 0.25}))\n"
            "print('reply')\n"))
        caller = lib.ModelCaller(script, work_dir=str(tmp_path / "w"))
        assert caller.call("planner", "s", "u") == "reply\n"
        caller.call("writer", "s", "u")
        assert caller.total_cost_usd() == 0.5 and caller.cost_by_role() == {"planner": 0.25, "writer": 0.25}

    def test_failing_call_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        caller = lib.ModelCaller(self._script(tmp_path, "import sys; sys.exit(2)"), work_dir=str(tmp_path / "w"))
        assert caller.call("planner", "s", "u") is None


class _Scripted:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def call(self, role, system, user):
        self.prompts.append(user)
        return self.replies.pop(0) if self.replies else None


class TestAskJson:
    def test_retries_once_on_malformed_then_succeeds(self):
        caller = _Scripted(["not json", '```json\n{"findings": []}\n```'])
        out = lib.ask_json(caller, "A", "s", "u", lambda d: lib.parse_findings(d, "A"))
        assert out == [] and len(caller.prompts) == 2 and "not one valid JSON" in caller.prompts[1]

    def test_gives_up_after_the_retry(self):
        caller = _Scripted(["x", "y"])
        assert lib.ask_json(caller, "A", "s", "u", lambda d: lib.parse_findings(d, "A")) is None

    def test_model_failure_is_none_without_retry(self):
        assert lib.ask_json(_Scripted([]), "A", "s", "u", lambda d: d) is None


def test_untrusted_text_is_redacted_and_capped():
    text = lib.sanitize_untrusted("key sk-or-v1-" + "a" * 40 + " " + "x" * 50, limit=60)
    assert "sk-or-v1-" not in text and len(text) <= 60
