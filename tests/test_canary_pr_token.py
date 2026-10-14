"""The canary opens its scenario pull requests with the push token, so their
workflows start without an approval, and no longer pushes an empty commit to
start them instead."""

import inspect
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import pipeline_canary as pc  # noqa: E402


class _Done:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _open_scenario(monkeypatch, tmp_path, token, stdout="https://github.com/o/r/pull/7\n"):
    monkeypatch.chdir(tmp_path)
    run_calls = []
    git_calls = []
    push_calls = []

    def fake_run(cmd, **kwargs):
        run_calls.append((list(cmd), kwargs))
        return _Done(stdout)

    def fake_git(*args, **kwargs):
        git_calls.append(list(args))
        return ""

    def fake_push(*args, **kwargs):
        push_calls.append(list(args))
        return True

    variant = {"name": "s1", "title": "canary: s1", "body": "body", "expect": {},
               "edits": [], "files": {}}
    scenario = {"name": "s1", "title": "canary: s1", "body": "body", "expect": {},
                "edits": [], "files": {}, "variants": [variant]}

    monkeypatch.setattr(pc, "run", fake_run)
    monkeypatch.setattr(pc, "git", fake_git)
    monkeypatch.setattr(pc, "push", fake_push)
    monkeypatch.setattr(pc, "pick_variant", lambda *a, **k: variant, raising=False)

    sig = inspect.signature(pc.open_scenario)
    known = {"repo": "owner/repo", "scenario": scenario, "base_branch": "main",
             "base_sha": "deadbeef", "token": token}
    kwargs = {}
    for name, param in sig.parameters.items():
        if param.kind not in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
            continue
        if name in known:
            kwargs[name] = known[name]
        elif param.default is inspect.Parameter.empty:
            kwargs[name] = str(tmp_path)
    result = pc.open_scenario(**kwargs)
    return result, run_calls, git_calls, push_calls


def test_the_canary_opens_its_pull_request_with_the_push_token(monkeypatch, tmp_path):
    result, run_calls, git_calls, push_calls = _open_scenario(monkeypatch, tmp_path, "PUSH-TOKEN")

    creates = [kw for cmd, kw in run_calls if cmd[:3] == ["gh", "pr", "create"]]
    assert len(creates) == 1
    assert creates[0].get("env", {}).get("GH_TOKEN") == "PUSH-TOKEN"
    assert result["pr"] == 7
    assert result["problems"] == []


def test_the_canary_no_longer_pushes_an_empty_commit_to_start_the_workflows(monkeypatch, tmp_path):
    _, run_calls, git_calls, push_calls = _open_scenario(monkeypatch, tmp_path, "PUSH-TOKEN")

    assert not any("--allow-empty" in " ".join(str(a) for a in call) for call in git_calls)
    branch_pushes = [call for call in push_calls if any("refs/heads/" in str(a) for a in call)]
    assert len(branch_pushes) == 1
    assert "PUSH-TOKEN" in branch_pushes[0]
