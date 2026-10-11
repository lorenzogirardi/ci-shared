"""The reviewers of a change: which roles run, which of them are shown the
pipeline's tool evidence, and which is shown the project's own description.
A test that says nothing about the SRE or the architect is not about them."""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import agent_pipeline as ap  # noqa: E402


def fence(obj) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


DIFF = """\
diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 line one
+added line
 line two
 line three
"""


class Caller:
    """Records the user prompt each role was given and finds nothing."""

    def __init__(self):
        self.prompts = {}

    def _respond(self, *args, **kwargs):
        role = args[0] if args else (kwargs.get("role") or kwargs.get("name") or kwargs.get("model") or "")
        if len(args) > 1:
            user = args[-1]
        else:
            user = next((kwargs[k] for k in ("user", "prompt", "user_prompt", "content") if k in kwargs), "")
        self.prompts[role] = user
        return fence({"findings": []})

    def __call__(self, *args, **kwargs):
        return self._respond(*args, **kwargs)

    def call(self, *args, **kwargs):
        return self._respond(*args, **kwargs)

    def complete(self, *args, **kwargs):
        return self._respond(*args, **kwargs)

    def total_cost_usd(self):
        return 0.0


def test_the_four_reviewers_run_in_declared_order():
    assert ap.REVIEWERS == (
        "reviewer-correctness",
        "reviewer-security",
        "reviewer-sre",
        "reviewer-architect",
    )


def test_only_security_and_sre_are_shown_the_evidence():
    assert ap.EVIDENCE_ROLES == frozenset({"reviewer-security", "reviewer-sre"})


def test_the_architect_sees_the_project_and_the_sre_sees_the_evidence():
    caller = Caller()
    ap.review_diff(caller, DIFF, None, context="CONTEXT-MARKER", evidence="EVIDENCE-MARKER")

    # The architect, and only it, is handed the project's own description.
    assert "CONTEXT-MARKER" in caller.prompts["reviewer-architect"]
    assert "CONTEXT-MARKER" not in caller.prompts["reviewer-correctness"]
    assert "CONTEXT-MARKER" not in caller.prompts["reviewer-sre"]

    # The evidence goes to security and the SRE, and to nobody else.
    assert "EVIDENCE-MARKER" in caller.prompts["reviewer-sre"]
    assert "EVIDENCE-MARKER" in caller.prompts["reviewer-security"]
    assert "EVIDENCE-MARKER" not in caller.prompts["reviewer-architect"]
    assert "EVIDENCE-MARKER" not in caller.prompts["reviewer-correctness"]
