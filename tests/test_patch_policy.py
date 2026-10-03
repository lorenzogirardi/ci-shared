"""The policy that does not depend on a model: a patch is checked BEFORE it is applied, and everything the
agents added is checked again BEFORE it is published."""

import json
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import agent_lib as lib  # noqa: E402
import agent_pipeline as ap  # noqa: E402

TOKEN = "ghp_" + "a" * 36


def fence(obj) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


class TestPrimitives:
    @pytest.mark.parametrize("path", [".github/workflows/ci.yml", ".git/config", ".env", "app/.env.production", "deploy/id_rsa",
                                      "certs/server.pem", "k/tls.key"])
    def test_protected_paths(self, path):
        assert lib.is_protected_path(path)

    @pytest.mark.parametrize("path", ["app/main.py", ".github/ai-review-rules.md", "docs/env.md", "tests/test_key.py", "app/keyboard.py"])
    def test_ordinary_paths_are_not_protected(self, path):
        assert not lib.is_protected_path(path)

    def test_credential_shapes_are_found_but_ordinary_assignments_are_not(self):
        assert lib.contains_secret(f"token = '{TOKEN}'")
        assert lib.contains_secret("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----")
        assert not lib.contains_secret('password = "test-password-only"')           # normal in tests and docs
        assert not lib.contains_secret("def keyboard(): return 'ghp'")

    def test_the_parser_refuses_protected_paths_outright(self):
        for path in (".env", "keys/server.pem"):
            assert lib.parse_changes({"explanation": "e", "changes": [{"file": path, "content": "x"}]}) is None


class TestValidatePatch:
    PLAN = {"out_of_scope": ["The api router (app/routers/api.py) and anything in docs/", "config.yaml"]}

    def test_a_secret_in_new_text_is_refused_for_edits_and_new_files(self):
        edit = [{"file": "app/x.py", "find": "a", "replace": f"KEY = '{TOKEN}'"}]
        new = [{"file": "app/y.py", "content": f"KEY = '{TOKEN}'"}]
        assert "credential" in lib.validate_patch(edit, self.PLAN)[0]
        assert "credential" in lib.validate_patch(new, self.PLAN)[0]

    def test_files_the_plan_declared_out_of_scope_are_refused(self):
        problems = lib.validate_patch([{"file": "app/routers/api.py", "find": "a", "replace": "b"},
                                       {"file": "docs/x.md", "content": "y"}, {"file": "deploy/config.yaml", "content": "z"}], self.PLAN)
        assert len(problems) == 3 and all("out of scope" in p for p in problems)

    def test_a_patch_inside_the_scope_passes(self):
        assert lib.validate_patch([{"file": "app/services/new.py", "content": "x = 1"}], self.PLAN) == []

    def test_a_plan_with_no_exclusions_restricts_nothing_but_secrets(self):
        assert lib.validate_patch([{"file": "docs/x.md", "content": "x"}], {"out_of_scope": []}) == []


def git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        git(*cmd)
    (tmp_path / ".gitignore").write_text(".ai/\n.shared/\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    return git("rev-parse", "HEAD")


def commit(files: dict[str, str | bytes]):
    for name, content in files.items():
        path = pathlib.Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
    git("add", "-A")
    git("commit", "-qm", "agent change")


class TestFinalGate:
    def test_a_clean_patch_has_no_violations(self, repo):
        commit({"a.py": "x = 2\n", "tests/test_a.py": "def test_a(): pass\n"})
        assert lib.policy_violations(repo) == []

    def test_a_credential_in_an_added_line_is_caught(self, repo):
        commit({"a.py": f"x = '{TOKEN}'\n"})
        assert any("credential" in v for v in lib.policy_violations(repo))

    def test_binary_files_protected_paths_and_oversized_patches_are_caught(self, repo):
        commit({"logo.png": b"\x89PNG\x00\x01\x02", "certs/x.pem": "k\n", "big.py": "y = 1\n" * 50})
        found = " | ".join(lib.policy_violations(repo, max_files=2, max_lines=40))
        assert "binary" in found and "protected path" in found and "files changed" in found and "lines changed" in found

    def test_removed_secrets_are_not_blamed_on_the_patch(self, repo):
        (pathlib.Path("a.py")).write_text(f"x = '{TOKEN}'\n")
        git("add", "-A")
        git("commit", "-qm", "someone's old mistake")
        base = git("rev-parse", "HEAD")
        commit({"a.py": "x = 1\n"})                                     # the agent removes it
        assert lib.policy_violations(base) == []


class FakeCaller:
    def __init__(self, replies):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.log = []
        self.max_chars = 1000
        self.usage = []

    def call(self, role, system, user):
        self.log.append((role, user))
        queue = self.replies.get(role)
        if not queue:
            return None
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def total_cost_usd(self):
        return 0.0

    def cost_by_role(self):
        return {}


class TestWriterIsRefusedBeforeApplying:
    def runtime(self, caller, base):
        return ap.Runtime(caller, base, "true", 30, 3, 2, 4, "", "", 0)

    def test_a_patch_with_a_secret_is_refused_and_the_writer_proposes_again(self, repo):
        bad = fence({"explanation": "x", "changes": [{"file": "b.py", "content": f"KEY = '{TOKEN}'\n"}]})
        good = fence({"explanation": "x", "changes": [{"file": "b.py", "content": "KEY = None\n"}]})
        caller = FakeCaller({"writer": [bad, good]})
        applied, _ = ap.run_writer(self.runtime(caller, repo), {"scope": ["s"], "out_of_scope": []}, "")
        assert applied is not None and pathlib.Path("b.py").read_text() == "KEY = None\n"
        assert "refused before it touched anything" in caller.log[1][1] and "credential" in caller.log[1][1]

    def test_a_writer_that_keeps_proposing_a_forbidden_patch_never_touches_the_tree(self, repo):
        bad = fence({"explanation": "x", "changes": [{"file": "app/api.py", "content": "x = 1\n"}]})
        caller = FakeCaller({"writer": [bad]})
        applied, why = ap.run_writer(self.runtime(caller, repo), {"scope": ["s"], "out_of_scope": ["app/api.py"]}, "")
        assert applied is None and not pathlib.Path("app/api.py").exists()


class TestFinalGateStopsPublishing:
    """Defence in depth: even if a patch got past the writer's check (a bug, a different code path), the
    last deterministic gate runs on the commits themselves, before the result can be published."""

    def args(self, base, mode="pr"):
        import argparse
        pathlib.Path(".ai").mkdir(exist_ok=True)
        pathlib.Path(".ai/t.txt").write_text("feat: x")
        pathlib.Path(".ai/b.txt").write_text("")
        return argparse.Namespace(ai_script="x", max_chars=10, timeout=1, title_file=".ai/t.txt", body_file=".ai/b.txt",
                                  context_files="", base_sha=base, mode=mode, verify_command_file="", verify_timeout=1,
                                  max_iterations=1, max_verify_retries=1, writer_rounds=1, ci_sha="", ci_repo="")

    def test_a_commit_with_a_credential_makes_the_result_abandoned_and_unpublishable(self, repo, monkeypatch):
        def engine(make_runtime, initial):
            commit({"leak.py": f"KEY = '{TOKEN}'\n"})
            return {"outcome": "converged", "notes": []}
        monkeypatch.setattr(ap.lib, "ModelCaller", lambda *a, **k: FakeCaller({}))
        monkeypatch.setattr(ap, "invoke_with_retry", engine)
        assert ap.cmd_change(self.args(repo)) == 0
        result = json.loads(pathlib.Path(".ai/agent-run/result.json").read_text())
        assert result["outcome"] == "abandoned" and result["commits"] == 0
        assert any("refusing to publish, policy" in n and "credential" in n for n in result["notes"])

    def test_a_clean_commit_is_published_as_before(self, repo, monkeypatch):
        def engine(make_runtime, initial):
            commit({"fine.py": "x = 1\n"})
            return {"outcome": "converged", "notes": []}
        monkeypatch.setattr(ap.lib, "ModelCaller", lambda *a, **k: FakeCaller({}))
        monkeypatch.setattr(ap, "invoke_with_retry", engine)
        ap.cmd_change(self.args(repo))
        assert json.loads(pathlib.Path(".ai/agent-run/result.json").read_text())["outcome"] == "converged"
