"""Goal invariant: no way a change can end waits for a person.

If someone adds an outcome that hands work to a human, or writes "needs human"
back into a label, a title or a comment, these tests fail."""

import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_pipeline as ap  # noqa: E402

FORBIDDEN = re.compile(r"needs-human|\[needs human\]|This needs a person|A person needs to look|needs a person: the loop|serve una persona",
                       re.IGNORECASE)


class TestTerminalStates:
    def test_every_ending_is_an_automatic_action(self):
        assert set(ap.TERMINAL_ACTIONS.values()) == {"certify", "abandon"}
        assert not any("human" in v or "person" in v for v in ap.TERMINAL_ACTIONS.values())

    @pytest.mark.parametrize("graph_outcome", ["converged", "escalated", "failed", "clean"])
    def test_every_outcome_the_engine_can_produce_ends_in_a_known_terminal_state(self, graph_outcome):
        assert ap.settle(graph_outcome) in ap.TERMINAL_ACTIONS

    def test_a_loop_that_does_not_converge_is_abandoned_not_handed_off(self):
        assert ap.settle("escalated") == ap.settle("failed") == "abandoned"

    def test_the_repository_never_says_a_person_must_step_in(self):
        offenders = []
        files = [*ROOT.glob("scripts/*.py"), *ROOT.glob("prompts/**/*.md"), *ROOT.glob(".github/workflows/*.yml"), ROOT / "README.md"]
        for path in files:
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if FORBIDDEN.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:90]}")
        assert not offenders, "a terminal state hands work to a person:\n" + "\n".join(offenders)


class Final(dict):
    pass


class StubGraph:
    def __init__(self, finals):
        self.finals = list(finals)
        self.states = []

    def invoke(self, state, config=None):
        self.states.append(state)
        return self.finals.pop(0)


@pytest.fixture
def stub(monkeypatch):
    holder = {}

    def install(finals):
        holder["g"] = StubGraph(finals)
        monkeypatch.setattr(ap, "build_graph", lambda rt: holder["g"])
        return holder["g"]
    return install


def runtime(**over):
    base = dict(caller=None, base_sha="b", verify_command="", verify_timeout=1, max_iterations=3, max_verify_retries=2,
                writer_rounds=4, context="", changelog_path="", issue_number=0)
    base.update(over)
    return ap.Runtime(**base)


class TestRetryWithMoreBudget:
    def test_a_converged_first_attempt_is_not_repeated(self, stub):
        graph = stub([{"outcome": "converged"}])
        rt = runtime()
        final = ap.invoke_with_retry(lambda a, p: ap.boosted(rt, a, p), {"notes": [], "rounds": []})
        assert final["outcome"] == "converged" and len(graph.states) == 1

    def test_a_failed_attempt_is_retried_once_with_twice_the_budget(self, stub):
        graph = stub([{"outcome": "escalated", "notes": ["blocking findings remain"], "committed": True},
                      {"outcome": "converged"}])
        seen = []
        rt = runtime()

        def make(attempt, previous):
            r = ap.boosted(rt, attempt, previous)
            seen.append((attempt, r.max_iterations, r.max_verify_retries, r.writer_rounds))
            return r
        final = ap.invoke_with_retry(make, {"notes": [], "rounds": []})
        assert final["outcome"] == "converged"
        assert seen == [(1, 3, 2, 4), (2, 6, 4, 8)]
        second = graph.states[1]
        assert "THE PREVIOUS ATTEMPT DID NOT CONVERGE" in second["feedback"] and second["committed"] is True
        assert any("retrying once" in n for n in second["notes"])

    def test_two_failures_end_abandoned_and_there_is_no_third_attempt(self, stub):
        graph = stub([{"outcome": "escalated", "notes": ["a"]}, {"outcome": "failed", "notes": ["b"]}, {"outcome": "converged"}])
        rt = runtime()
        final = ap.invoke_with_retry(lambda a, p: ap.boosted(rt, a, p), {"notes": [], "rounds": []})
        assert len(graph.states) == 2 and ap.settle(final["outcome"]) == "abandoned"

    def test_an_issue_with_committed_work_resumes_from_checking_it(self):
        rt = runtime(start="write")
        assert ap.boosted(rt, 2, {"committed": True}).start == "verify"
        assert ap.boosted(rt, 2, {"committed": False}).start == "write"      # nothing to check yet
        assert ap.boosted(runtime(start="review"), 2, {"committed": True}).start == "review"
