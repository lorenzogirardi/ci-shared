"""The test steward lost a correct test to a formatting slip and then regenerated something else.

Real incident (flask-test-api PR #190): the reply wrote a whole test file into a JSON string with raw
line breaks; it was refused as invalid, and the retry started from scratch and invented an anchor.
"""

import subprocess

import agent_lib as lib
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
