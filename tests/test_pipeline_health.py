"""The pipeline checks itself: each case here is a silence that really happened."""

import datetime as dt
import pathlib

import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "scripts"))

import pipeline_health as h  # noqa: E402

NOW = dt.datetime(2026, 10, 8, 18, 0, tzinfo=dt.timezone.utc)


def commit(sha, subject, minutes_ago):
    when = (NOW - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"sha": sha, "commit": {"message": subject + "\n\nbody", "committer": {"date": when}}}


def build(sha, conclusion="success", status="completed"):
    return {"head_sha": sha, "status": status, "conclusion": conclusion}


class TestUnbuiltCommits:
    def test_merges_made_with_a_token_that_starts_no_workflow_are_found(self):
        """flask-test-api #183, #185, #186: merged by the job token, no build ever started."""
        commits = [commit("c3", "chore(deps): fastapi (#186)", 60), commit("c2", "chore(deps): ddtrace (#185)", 180),
                   commit("c1", "chore(deps): harden-runner (#184)", 600)]
        missing = h.unbuilt_commits(commits, [build("c1")], NOW, 20)
        assert [m["sha"] for m in missing] == ["c3", "c2"]

    def test_a_later_successful_build_covers_the_commits_before_it(self):
        commits = [commit("c3", "ci: x (#187)", 60), commit("c2", "chore(deps): a", 180), commit("c1", "chore(deps): b", 300)]
        assert h.unbuilt_commits(commits, [build("c3")], NOW, 20) == []

    def test_a_later_build_still_running_covers_them_too(self):
        commits = [commit("c2", "x", 60), commit("c1", "y", 180)]
        assert h.unbuilt_commits(commits, [build("c2", None, "in_progress")], NOW, 20) == []

    def test_a_later_build_that_failed_covers_nothing(self):
        commits = [commit("c2", "x", 60), commit("c1", "y", 180)]
        assert [m["sha"] for m in h.unbuilt_commits(commits, [build("c2", "failure")], NOW, 20)] == ["c1"]

    def test_bookkeeping_commits_never_need_a_build(self):
        commits = [commit("c2", "docs(changelog): update for abc", 60), commit("c1", "Done  by Github Actions   Job changemanifest: 290", 90)]
        assert h.unbuilt_commits(commits, [], NOW, 20) == []

    def test_a_commit_younger_than_the_grace_period_is_left_alone(self):
        assert h.unbuilt_commits([commit("c1", "feat: x", 5)], [], NOW, 20) == []


class TestDegradedCertifications:
    def _comment(self, body, number=190):
        return {"body": body, "issue_url": f"https://api.github.com/repos/o/r/issues/{number}", "html_url": "u"}

    def test_a_certification_next_to_a_failed_steward_is_found(self):
        """flask-test-api #190."""
        body = ("<!-- agent-pr -->\n<!-- agent-certified: 59710d08f864219b2d2df70fa110e1cc3fce254e -->\n### Notes\n"
                "- test steward changes were not applicable: tests/test_api.py: anchor appears 0 times, expected exactly 1")
        found = h.degraded_certifications([self._comment(body)])
        assert found[0]["pr"] == "190" and found[0]["notes"] == ["were not applicable"]

    def test_a_clean_certification_is_not_reported(self):
        body = "<!-- agent-pr -->\n<!-- agent-certified: 59710d08f864219b2d2df70fa110e1cc3fce254e -->\nNo blocking findings."
        assert h.degraded_certifications([self._comment(body)]) == []

    def test_an_uncertified_report_is_not_reported(self):
        body = "<!-- agent-pr -->\n### Notes\n- test steward returned no usable reply"
        assert h.degraded_certifications([self._comment(body)]) == []

    def test_comments_of_other_people_are_ignored(self):
        body = "agent-certified: deadbeef and it returned no usable reply"
        assert h.degraded_certifications([self._comment(body)]) == []

    def test_the_engine_still_uses_these_sentences(self):
        """If the engine rewords a note, this check would go blind. Keep the two in step."""
        engine = pathlib.Path(h.__file__).with_name("agent_pipeline.py").read_text()
        for note in h.DEGRADED_NOTES:
            assert note in engine, note


def test_failed_agent_workflows_are_reported_and_others_are_not():
    runs = [{"name": "Agent Merge", "conclusion": "failure", "html_url": "u", "created_at": "t"},
            {"name": "PR Checks", "conclusion": "failure"}, {"name": "Agent Change", "conclusion": "success"}]
    assert [f["name"] for f in h.failing_agent_workflows(runs, ("Agent Merge", "Agent Change"))] == ["Agent Merge"]


def test_the_report_says_when_nothing_is_wrong():
    assert "Nothing found" in h.render([], [], [], False)
