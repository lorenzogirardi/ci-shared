"""The roles read what the pipeline's tools reported, and what they say about it is a note.

Evidence informs a review and can never be required: every way of not having it gives an empty text.
What a role writes under `awareness` is reported and never becomes a finding, so a scanner cannot
send a change back."""

import io
import json
import pathlib
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import agent_lib as lib  # noqa: E402
import agent_pipeline as ap  # noqa: E402


def zipped(**files: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


SARIF = json.dumps({"runs": [{"results": [
    {"ruleId": "CKV_K8S_38", "message": {"text": "Service account token mounted"},
     "locations": [{"physicalLocation": {"artifactLocation": {"uri": "helm/deployment.yaml"}}}]},
    {"ruleId": "CKV_K8S_38", "message": {"text": "Service account token mounted"},
     "locations": [{"physicalLocation": {"artifactLocation": {"uri": "helm/deployment.yaml"}}}]},
]}]})


class FakeGitHub:
    """Runs and artifacts of one commit. `pending` polls answer 'in progress' before the runs finish."""

    def __init__(self, artifacts: dict[str, bytes], pending: int = 0, runs=(1, 2)):
        self.artifacts, self.pending, self.runs, self.slept = artifacts, pending, runs, 0

    def api(self, path: str):
        if "/artifacts" in path:
            if not path.split("/runs/")[1].startswith("2/"):
                return {"artifacts": []}
            return {"artifacts": [{"id": i, "name": name, "size_in_bytes": len(blob)}
                                  for i, (name, blob) in enumerate(self.artifacts.items())]}
        status = "in_progress" if self.pending > 0 else "completed"
        self.pending -= 1
        return {"workflow_runs": [{"id": r, "status": "in_progress" if r == 1 else status} for r in self.runs]}

    def download(self, path: str) -> bytes:
        return list(self.artifacts.values())[int(path.split("/artifacts/")[1].split("/")[0])]

    def sleep(self, seconds: int) -> None:
        self.slept += seconds


def fetch(hub: FakeGitHub, **kw) -> str:
    return lib.fetch_evidence("o/r", "abc", own_run="1", api=hub.api, download=hub.download, sleep=hub.sleep, **kw)


class TestFetchEvidence:
    def test_artifacts_with_the_prefix_become_sections(self):
        hub = FakeGitHub({"ai-context-trivy": zipped(**{"trivy.txt": "Total: 1 (CRITICAL: 1)"}),
                          "coverage-report": zipped(**{"c.txt": "not evidence"})})
        text = fetch(hub)
        assert "### trivy" in text and "CRITICAL: 1" in text
        assert "not evidence" not in text

    def test_a_sarif_report_is_one_line_per_distinct_result(self):
        text = fetch(FakeGitHub({"ai-context-checkov": zipped(**{"results.sarif": SARIF})}))
        assert text.count("CKV_K8S_38") == 1 and "helm/deployment.yaml" in text

    def test_waits_for_the_other_runs_and_ignores_its_own(self):
        hub = FakeGitHub({"ai-context-trivy": zipped(**{"t.txt": "ok"})}, pending=2)
        assert "### trivy" in fetch(hub, poll=10)
        assert hub.slept == 20          # run 1 is this run and never finishes: it is not waited for

    def test_the_wait_is_bounded_and_takes_what_is_there(self):
        hub = FakeGitHub({"ai-context-trivy": zipped(**{"t.txt": "partial"})}, pending=10_000)
        assert "partial" in fetch(hub, wait_seconds=30, poll=15)
        assert hub.slept == 30

    def test_no_evidence_is_not_an_error(self):
        assert fetch(FakeGitHub({})) == ""
        assert lib.fetch_evidence("", "abc") == ""
        assert lib.fetch_evidence("o/r", "abc", api=lambda path: None, sleep=lambda s: 1 / 0) == ""
        assert fetch(FakeGitHub({"ai-context-x": b"not a zip"})) == ""

    def test_secret_shaped_text_in_a_tool_output_is_redacted(self):
        secret = "ghp_" + "a" * 36
        text = fetch(FakeGitHub({"ai-context-log": zipped(**{"log.txt": f"token {secret} leaked"})}))
        assert secret not in text


class TestAwarenessIsANoteNeverAFinding:
    def test_parsed_leniently_and_capped(self):
        assert lib.parse_awareness({"awareness": ["  Trivy:  one\n critical ", 3, ""]}) == ["Trivy: one critical"]
        assert lib.parse_awareness({"awareness": "nope"}) == [] and lib.parse_awareness(None) == []
        assert len(lib.parse_awareness({"awareness": ["x"] * 50})) == lib.MAX_AWARENESS

    def test_only_evidence_roles_see_the_evidence_and_notes_do_not_block(self):
        seen: dict[str, str] = {}

        class Caller:
            def call(self, role, system, user):
                seen[role] = user
                notes = ["Checkov: the chart mounts the service account token"] if role == "reviewer-security" else []
                return "```json\n" + json.dumps({"findings": [], "awareness": notes}) + "\n```"

        notes: list[str] = []
        result = ap.review_diff(Caller(), "", None, evidence="### checkov\n- CKV_K8S_38", awareness=notes)
        assert result == ([], [])                                   # nothing to send back to the developer
        assert notes == ["Checkov: the chart mounts the service account token"]
        assert "CKV_K8S_38" in seen["reviewer-security"]
        assert "CKV_K8S_38" not in seen["reviewer-correctness"]

    def test_the_pull_request_comment_reports_them_as_not_blocking(self):
        comment = ap.build_change_comment(
            {"outcome": "clean", "commits": 0, "final_sha": "a" * 40, "notes": [], "rounds": [],
             "awareness": ["Trivy: one critical vulnerability in the base image"]}, None)
        assert "### What the tools reported" in comment and "did not hold the change" in comment and "Trivy: one critical" in comment
        assert "Certified at" in comment


class TestTheEvidenceIsCapped:
    def test_a_single_file_is_capped(self):
        line = "log line about nothing in particular. "
        text = fetch(FakeGitHub({"ai-context-log": zipped(**{"log.txt": line * 200})}))
        assert len(text.split("### log\n", 1)[1]) == lib.EVIDENCE_FILE_CHARS

    def test_the_whole_evidence_is_capped(self):
        line = "log line about nothing in particular. "
        big = zipped(**{f"f{i}.txt": line * 200 for i in range(5)})
        text = fetch(FakeGitHub({"ai-context-big": big}))
        assert len(text) == lib.EVIDENCE_TOTAL_CHARS
