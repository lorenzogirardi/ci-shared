"""The engine's interpreter asks the application's interpreter where its libraries live, once,
and copes with one that does not answer. Before the separation there is no `app_paths` at all."""

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import pr_review_sweep as sweep  # noqa: E402


def _interpreter(tmp_path, name):
    exe = tmp_path / name
    exe.write_text("#!/bin/sh\n")
    return str(exe)


def test_the_engines_own_interpreter_is_never_asked(monkeypatch):
    monkeypatch.delenv(sweep.APP_PYTHON_ENV, raising=False)

    def run(*a, **kw):
        raise AssertionError("the engine's interpreter must not be asked")

    monkeypatch.setattr(sweep.subprocess, "run", run)
    assert sweep.app_python() == sys.executable
    assert sweep.app_paths() == sweep.sysconfig.get_paths()


def test_the_other_interpreters_paths_are_asked_once_and_then_cached(tmp_path, monkeypatch):
    exe = _interpreter(tmp_path, "python")
    monkeypatch.setenv(sweep.APP_PYTHON_ENV, exe)
    monkeypatch.delitem(sweep._app_paths, exe, raising=False)
    asked = []

    def run(cmd, **kw):
        asked.append(cmd)
        assert cmd[0] == exe and cmd[1] == "-c"
        return subprocess.CompletedProcess(cmd, 0, '{"purelib": "/app/lib", "syspath": ["/app/lib"]}', "")

    monkeypatch.setattr(sweep.subprocess, "run", run)
    assert sweep.app_python() == exe
    assert sweep.app_paths() == {"purelib": "/app/lib", "syspath": ["/app/lib"]}
    assert sweep.app_paths() == {"purelib": "/app/lib", "syspath": ["/app/lib"]}
    assert len(asked) == 1


def test_an_interpreter_that_fails_or_prints_nonsense_leaves_nothing(tmp_path, monkeypatch):
    broken = _interpreter(tmp_path, "broken")
    garbled = _interpreter(tmp_path, "garbled")
    monkeypatch.delitem(sweep._app_paths, broken, raising=False)
    monkeypatch.delitem(sweep._app_paths, garbled, raising=False)

    def failed(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    monkeypatch.setenv(sweep.APP_PYTHON_ENV, broken)
    monkeypatch.setattr(sweep.subprocess, "run", failed)
    assert sweep.app_paths() == {}

    def unparsable(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, "not json at all", "")

    monkeypatch.setenv(sweep.APP_PYTHON_ENV, garbled)
    monkeypatch.setattr(sweep.subprocess, "run", unparsable)
    assert sweep.app_paths() == {}
