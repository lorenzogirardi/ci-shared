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
            if note not in h.LEGACY_NOTES:
                assert note in engine, note


def test_failed_agent_workflows_are_reported_and_others_are_not():
    runs = [{"name": "Agent Merge", "conclusion": "failure", "html_url": "u", "created_at": "t"},
            {"name": "PR Checks", "conclusion": "failure"}, {"name": "Agent Change", "conclusion": "success"}]
    assert [f["name"] for f in h.failing_agent_workflows(runs, ("Agent Merge", "Agent Change"))] == ["Agent Merge"]


def test_the_report_says_when_nothing_is_wrong():
    assert "Nothing found" in h.render([], [], [], False)


class TestStuckPullRequests:
    SHA = "59183e7a681f5d029e27ba2d0e7b281042e490a5"

    def pr(self, **over):
        base = {"number": 197, "title": "feat: lower the sleep limit", "draft": False, "user": {"login": "lorenzogirardi"},
                "head": {"sha": self.SHA, "repo": {"full_name": "o/r"}}, "base": {"repo": {"full_name": "o/r"}},
                "updated_at": "2026-10-08T17:19:00Z"}
        base.update(over)
        return base

    def checks(self, minutes_ago=200, status="completed"):
        done = (NOW - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return [{"name": "checks", "status": status, "conclusion": "success", "completed_at": done if status == "completed" else None}]

    def find(self, pulls, comments=(), checks=None, minutes=90):
        return h.stuck_pull_requests(pulls, lambda n: [{"body": b} for b in comments], lambda sha: checks if checks is not None else self.checks(),
                                     NOW, minutes, ("renovate[bot]",))

    def test_a_green_pull_request_with_no_verdict_for_hours_is_stuck(self):
        """flask-test-api PR #197: green, uncertified, nothing left to run, open for 3.5 hours."""
        found = self.find([self.pr()], comments=["<!-- agent-pr -->\nThe agent fixed this and could not push"])
        assert [f["pr"] for f in found] == [197] and found[0]["idle_minutes"] == 200

    def test_a_verdict_on_the_head_commit_means_it_is_not_stuck(self):
        assert self.find([self.pr()], comments=[f"<!-- agent-certified: {self.SHA} -->"]) == []
        assert self.find([self.pr()], comments=[f"<!-- agent-abandoned: {self.SHA} -->"]) == []

    def test_a_verdict_on_an_older_commit_does_not_count(self):
        assert len(self.find([self.pr()], comments=["<!-- agent-certified: " + "a" * 40 + " -->"])) == 1

    def test_recent_or_running_work_is_left_alone(self):
        assert self.find([self.pr()], checks=self.checks(minutes_ago=10)) == []
        assert self.find([self.pr()], checks=self.checks(status="in_progress")) == []

    def test_drafts_forks_and_the_dependency_bot_are_not_ours(self):
        assert self.find([self.pr(draft=True)]) == []
        assert self.find([self.pr(user={"login": "renovate[bot]"})]) == []
        assert self.find([self.pr(head={"sha": self.SHA, "repo": {"full_name": "someone/fork"}})]) == []


def test_a_run_that_never_started_because_the_job_token_opened_the_pull_request_is_not_a_failure():
    """Every canary pull request left one of these, and the health check counted five in one evening."""
    phantom = {"name": "Agent Change", "conclusion": "failure", "event": "pull_request",
               "triggering_actor": {"login": "github-actions[bot]"}}
    real = {"name": "Agent Change", "conclusion": "failure", "event": "pull_request", "triggering_actor": {"login": "someone"},
            "html_url": "u", "created_at": "t"}
    pushed = {"name": "Agent Change", "conclusion": "failure", "event": "push", "triggering_actor": {"login": "github-actions[bot]"}}
    assert [f["url"] for f in h.failing_agent_workflows([phantom, real], ("Agent Change",))] == ["u"]
    assert len(h.failing_agent_workflows([pushed], ("Agent Change",))) == 1
