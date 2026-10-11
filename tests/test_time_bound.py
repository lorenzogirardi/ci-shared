"""A run is bounded in time and says what it is doing.

One model call once held a review for more than twenty minutes, twice in the same run, with nothing in
the log: Actions minutes are limited, so that must not be possible. A role's answer has a hard limit, the
whole run has a budget after which no model is called, and every call writes one line."""

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import agent_lib as lib  # noqa: E402

FAST = ("import sys, json\na = sys.argv\n"
        "open(a[a.index('--usage-file')+1], 'w').write(json.dumps({'cost_usd': 0.01, 'completion_tokens': 7}))\nprint('ok')\n")
SLOW = "import time\ntime.sleep(30)\nprint('too late')\n"


def caller(tmp_path, script: str) -> lib.ModelCaller:
    path = tmp_path / "model.py"
    path.write_text(script)
    return lib.ModelCaller(str(path), work_dir=str(tmp_path / "calls"))


def test_every_call_writes_one_line_with_its_time_and_cost(tmp_path, capsys):
    assert caller(tmp_path, FAST).call("reviewer-sre", "s", "u").strip() == "ok"
    out = capsys.readouterr().out
    assert "model call 001 reviewer-sre:" in out and "7 output tokens" in out and "$0.0100" in out


def test_a_call_that_does_not_answer_is_stopped(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(lib.ModelCaller, "CALL_SECONDS", 1)
    c = caller(tmp_path, SLOW)
    started = time.time()
    assert c.call("reviewer-correctness", "s", "u") is None
    assert time.time() - started < 10
    assert "stopped after 1s without an answer" in capsys.readouterr().out
    assert c.out_of_time is False            # one slow call is not the run's budget


def test_past_the_run_budget_no_model_is_called(tmp_path, capsys):
    c = caller(tmp_path, SLOW)
    c.deadline = time.time() - 1
    started = time.time()
    assert c.call("writer", "s", "u") is None
    assert time.time() - started < 2 and c.out_of_time is True
    assert "not made, the run is out of time" in capsys.readouterr().out


def test_a_call_is_cut_at_the_run_budget_even_if_its_own_limit_is_longer(tmp_path):
    c = caller(tmp_path, SLOW)
    c.deadline = time.time() + 1
    started = time.time()
    assert c.call("writer", "s", "u") is None
    assert time.time() - started < 10 and c.out_of_time is True
