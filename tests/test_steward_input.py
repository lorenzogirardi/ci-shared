"""The test steward lost a correct test to a formatting slip and then regenerated something else.

Real incident (flask-test-api PR #190): the reply wrote a whole test file into a JSON string with raw
line breaks; it was refused as invalid, and the retry started from scratch and invented an anchor.
"""

import subprocess

import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "scripts"))

import agent_lib as lib  # noqa: E402
from pr_review_sweep import _parse_json_reply


def test_a_json_string_with_raw_line_breaks_is_accepted():
    reply = '```json\n{"explanation": "x", "changes": [{"file": "tests/t.py", "content": "line1\nline2\n"}]}\n```'
    data = _parse_json_reply(reply)
    assert data is not None
    assert data["changes"][0]["content"] == "line1\nline2\n"


def test_malformed_json_is_still_refused():
    assert _parse_json_reply('```json\n{"explanation": }\n```') is None


class FakeCaller:
    def __init__(self, replies):
        self.replies, self.users = list(replies), []

    def call(self, role, system, user):
        self.users.append(user)
        return self.replies.pop(0)


def test_the_retry_shows_the_model_its_own_reply():
    bad = '```json\n{"explanation": "x", "changes": [\n```'
    good = '```json\n{"ok": true}\n```'
    caller = FakeCaller([bad, good])
    out = lib.ask_json(caller, "test-steward", "sys", "user", lambda d: d if d.get("ok") else None)
    assert out == {"ok": True}
    assert bad in caller.users[1]
    assert "fixing only the format" in caller.users[1]


def test_conventions_show_the_shared_fixtures_and_one_test_module(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text("import pytest\n\n@pytest.fixture\nasync def client():\n    ...\n")
    (tmp_path / "tests" / "test_api.py").write_text("import pytest\n\n@pytest.mark.anyio\nasync def test_x(client):\n    ...\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    text = lib.test_conventions()
    assert "async def client" in text
    assert "@pytest.mark.anyio" in text


def test_failed_tests_are_found_in_a_ci_log_with_timestamps():
    """Real incident (flask-test-api PR #191): the CI log has a timestamp before every line, the pattern wanted
    FAILED at the start of the line, so no failing test was found and the adjudicator never ran."""
    log = (
        "2026-10-08T16:13:10.3410767Z tests/test_api.py::test_sleep_up_to_30_seconds_allowed[asyncio] FAILED   [ 39%]\n"
        "2026-10-08T16:13:12.1041702Z FAILED tests/test_api.py::test_sleep_up_to_30_seconds_allowed[asyncio] - assert 400 == 200\n"
        "2026-10-08T16:13:12.1043014Z =================== 1 failed, 71 passed, 25 skipped in 3.10s ===================\n"
    )
    assert lib.parse_failed_tests(log) == ["tests/test_api.py::test_sleep_up_to_30_seconds_allowed[asyncio]"]


def test_failed_tests_are_found_with_job_and_step_prefixes():
    log = "checks\tTests\t2026-10-08T16:13:12.1Z FAILED tests/test_a.py::test_x - boom\n"
    assert lib.parse_failed_tests(log) == ["tests/test_a.py::test_x"]


def test_plain_output_still_parses():
    assert lib.parse_failed_tests("FAILED tests/test_a.py::test_x - boom\nERROR tests/test_b.py::test_y\n") == [
        "tests/test_a.py::test_x", "tests/test_b.py::test_y"]


DIFF = '''diff --git a/app/routers/api.py b/app/routers/api.py
--- a/app/routers/api.py
+++ b/app/routers/api.py
@@ -90,4 +90,4 @@ async def sleep_endpoint(seconds: int):
-    if seconds > 30:
+    if seconds > 25:
+        limit = os.environ.get("SLEEP_MAX_SECONDS")
+@router.get("/api/sleep/{seconds}")
'''
DOCS = {"README.md": "# App\nGET /api/sleep/{seconds} delays the response.\n", "docs/01-intro.md": "History of the project.\n",
        "docs/02-config.md": "Set SLEEP_MAX_SECONDS to change the limit.\n", "docs/03-other.md": "Unrelated.\n"}


def test_the_terms_of_a_diff_are_what_a_document_would_have_to_mention():
    terms = lib.diff_terms(DIFF)
    assert {"/api/sleep/{seconds}", "/api/sleep", "SLEEP_MAX_SECONDS"} <= terms
    assert "sleep_endpoint" not in terms      # the hunk header names a nearby function, often the wrong one
    # the path of a file that is only edited is in every architecture overview: it is not a term
    assert "app/routers/api.py" not in terms


def test_a_test_file_gives_urls_but_not_its_test_names_and_a_new_file_gives_its_path():
    diff = ("diff --git a/tests/test_api.py b/tests/test_api.py\n--- a/tests/test_api.py\n+++ b/tests/test_api.py\n"
            "@@ -1 +1 @@ async def test_sleep_too_long(client):\n-    resp = await client.get(\"/api/sleep/31\")\n"
            "+async def test_sleep_up_to_25_seconds_allowed(client):\n"
            "diff --git a/app/canary.py b/app/canary.py\nnew file mode 100644\n--- /dev/null\n+++ b/app/canary.py\n"
            "@@ -0,0 +1 @@\n+def within_limit(value):\n")
    terms = lib.diff_terms(diff)
    assert {"/api/sleep/31", "/api/sleep", "app/canary.py", "within_limit"} <= terms
    assert not any(t.startswith("test_") for t in terms) and "tests/test_api.py" not in terms


def test_only_the_documents_that_mention_the_change_are_handed_over():
    text = lib.relevant_docs(DIFF, list(DOCS), read=DOCS.get)
    assert "### README.md" in text and "### docs/02-config.md" in text
    assert "docs/01-intro.md" not in text and "docs/03-other.md" not in text


def test_when_nothing_mentions_the_change_the_first_document_and_the_list_of_the_others_are_given():
    diff = "+++ b/app/new_thing.py\n+def brand_new_feature():\n"
    text = lib.relevant_docs(diff, list(DOCS), read=DOCS.get)
    assert text.startswith("No document mentions") and "### README.md" in text and "docs/02-config.md" in text
    assert "History of the project" not in text


def test_a_passage_deep_in_a_long_document_is_found_and_the_rest_is_left_out():
    filler = "\n".join(f"line {i} about something else" for i in range(400))
    long = {"README.md": filler + "\nGET /api/sleep/{seconds} delays the response.\n" + filler}
    text = lib.relevant_docs(DIFF, ["README.md"], read=long.get)
    assert "GET /api/sleep/{seconds} delays the response." in text and "[...]" in text
    assert "line 200 about" not in text and len(text) < 1500


def test_excerpts_keep_the_lines_verbatim_so_an_edit_can_be_anchored_on_them():
    doc = "a\nb\n| `/api/sleep/{seconds}` | max 30 seconds |\nc\nd"
    assert "| `/api/sleep/{seconds}` | max 30 seconds |" in lib.excerpts(doc, {"/api/sleep"}, around=1)


class TestEvidenceNeedsAPytestThatRan:
    """flask-test-api PR #197: entered from a failed CI run, the job had no pytest yet. `python -m pytest` then
    exits 1, which was read as 'the test fails', on the head and on the base commit alike, so a test that passes
    on the base was declared 'already failing before this change' and that evidence overruled a correct verdict."""

    def fake(self, monkeypatch, returncode, text):
        monkeypatch.setattr(lib.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, returncode, "", text))

    def test_an_interpreter_without_pytest_is_an_error_not_a_failure(self, monkeypatch):
        self.fake(monkeypatch, 1, "/usr/bin/python3: No module named pytest")
        assert lib.run_pytest(["tests/test_x.py::test_y"], ".")[0] == "error"

    def test_a_real_failure_is_still_a_failure(self, monkeypatch):
        self.fake(monkeypatch, 1, "FAILED tests/test_x.py::test_y - assert 1 == 2\n1 failed in 0.02s")
        assert lib.run_pytest(["tests/test_x.py::test_y"], ".")[0] == "fail"

    def test_so_the_hint_is_unreproducible_and_never_preexisting(self):
        assert lib.evidence_hint({"head": ["error", "error"], "base": "error"}) == "unreproducible"
