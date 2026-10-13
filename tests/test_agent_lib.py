"""Strictness of everything that turns model text into an action."""

import os
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import agent_lib as lib  # noqa: E402

DIFF = """diff --git a/app/x.py b/app/x.py
--- a/app/x.py
+++ b/app/x.py
@@ -1,3 +1,4 @@
 import os
-old = 1
+new = 2
+extra = 3
 tail = 4
diff --git a/app/y.py b/app/y.py
--- a/app/y.py
+++ b/app/y.py
@@ -10,2 +10,2 @@
 keep
-- not a header
+changed = True
"""


def finding(**over):
    base = {"severity": "high", "file": "app/x.py", "line": 2, "category": "bug",
            "evidence": "new = 2", "problem": "wrong value", "suggestion": "use 1"}
    base.update(over)
    return base


class TestParseFindings:
    def test_valid(self):
        out = lib.parse_findings({"findings": [finding()]}, "A")
        assert out and out[0].blocking and out[0].reviewers == ["A"]

    def test_empty_list_is_valid_and_not_none(self):
        assert lib.parse_findings({"findings": []}, "A") == []

    @pytest.mark.parametrize("bad", [
        {"severity": "blocker"}, {"line": 0}, {"line": True}, {"line": "2"}, {"file": "../etc/passwd"},
        {"file": "/abs"}, {"evidence": ""}, {"problem": "  "}, {"suggestion": None},
    ])
    def test_malformed_item_invalidates_the_reply(self, bad):
        assert lib.parse_findings({"findings": [finding(**bad)]}, "A") is None

    def test_not_a_findings_object(self):
        assert lib.parse_findings({"nope": []}, "A") is None
        assert lib.parse_findings(None, "A") is None


class TestDiff:
    def test_ranges_and_header_lookalike_line(self):
        ranges = lib.diff_ranges(DIFF)
        assert ranges["app/x.py"] == [(1, 4)]
        # the removed line "-- not a header" must not be read as a file header
        assert set(ranges) == {"app/x.py", "app/y.py"}
        assert ranges["app/y.py"] == [(10, 11)]

    def test_validate_drops_unknown_files_and_lines_outside_hunks(self):
        ranges = lib.diff_ranges(DIFF)
        fs = lib.parse_findings({"findings": [
            finding(), finding(file="app/z.py"), finding(line=99), finding(file="app/y.py", line=11)]}, "A")
        kept, dropped = lib.validate_findings(fs, ranges)
        assert [(f.file, f.line) for f in kept] == [("app/x.py", 2), ("app/y.py", 11)]
        assert [reason for _, reason in dropped] == ["file is not part of the diff", "line is outside the changed hunks"]

    def test_annotate_numbers_added_and_context_lines_only(self):
        out = lib.annotate_diff(DIFF).splitlines()
        assert "L1| import os" in out
        assert "-old = 1" in out
        assert "L2|+new = 2" in out and "L3|+extra = 3" in out and "L4| tail = 4" in out


class TestDedup:
    def test_same_place_same_topic_merged_keeping_worst_severity_and_both_reviewers(self):
        a = lib.parse_findings({"findings": [finding(severity="medium", line=2)]}, "A")[0]
        b = lib.parse_findings({"findings": [finding(severity="critical", line=3, problem="wrong value assigned")]}, "B")[0]
        merged = lib.dedup_findings([a, b])
        assert len(merged) == 1 and merged[0].severity == "critical"
        assert merged[0].reviewers == ["B", "A"] or sorted(merged[0].reviewers) == ["A", "B"]

    def test_different_files_or_distant_lines_stay_separate(self):
        a = lib.parse_findings({"findings": [finding()]}, "A")[0]
        b = lib.parse_findings({"findings": [finding(line=40, category="design", problem="unrelated")]}, "B")[0]
        c = lib.parse_findings({"findings": [finding(file="app/y.py")]}, "B")[0]
        assert len(lib.dedup_findings([a, b, c])) == 3


class TestParseChanges:
    def test_edit_and_create(self):
        data = {"explanation": "e", "changes": [
            {"file": "a.py", "find": "x", "replace": "y"}, {"file": "tests/t.py", "content": "def test(): pass\n"}]}
        assert lib.parse_changes(data) == (data["changes"], "e")

    @pytest.mark.parametrize("change", [
        {"file": ".github/workflows/ci.yml", "content": "x"}, {"file": ".git/config", "content": "x"},
        {"file": "../x", "content": "x"}, {"file": "a", "find": "x", "replace": "x"},
        {"file": "a", "find": "", "replace": "x"}, {"file": "a", "content": ""},
        {"file": "a", "content": "x" * (lib.MAX_CONTENT_CHARS + 1)},
    ])
    def test_rejected(self, change):
        assert lib.parse_changes({"explanation": "e", "changes": [change]}) is None

    def test_too_many_changes_and_allowed_predicate(self):
        many = {"explanation": "e", "changes": [{"file": f"f{i}", "content": "x"} for i in range(lib.MAX_CHANGES + 1)]}
        assert lib.parse_changes(many) is None
        ok = {"explanation": "e", "changes": [{"file": "app/x.py", "content": "x"}]}
        assert lib.parse_changes(ok, allowed=lambda p: p.endswith(".md")) is None


class TestApplyChanges:
    @pytest.fixture(autouse=True)
    def _repo(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", *cmd], check=True)
        pathlib.Path("a.txt").write_text("one two")
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-qm", "i"], check=True)

    def test_edit_and_create_then_revert(self):
        applied = lib.apply_changes([{"file": "a.txt", "find": "one", "replace": "1"},
                                     {"file": "sub/new.txt", "content": "hi"}])
        assert applied.error is None and applied.files == ["a.txt", "sub/new.txt"]
        assert pathlib.Path("a.txt").read_text() == "1 two"
        lib.revert(applied)
        assert pathlib.Path("a.txt").read_text() == "one two" and not pathlib.Path("sub/new.txt").exists()

    def test_create_over_existing_file_writes_nothing(self):
        applied = lib.apply_changes([{"file": "a.txt", "find": "one", "replace": "1"},
                                     {"file": "a.txt", "content": "clobber"}])
        assert applied.error and pathlib.Path("a.txt").read_text() == "one two"

    def test_ambiguous_anchor_writes_nothing(self):
        pathlib.Path("a.txt").write_text("x x")
        applied = lib.apply_changes([{"file": "a.txt", "find": "x", "replace": "y"},
                                     {"file": "new.txt", "content": "n"}])
        assert applied.error and not pathlib.Path("new.txt").exists()


class TestIsolatedVerify:
    def test_secrets_are_invisible_to_the_checks(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
        monkeypatch.setenv("GH_TOKEN", "ghp_secret")
        monkeypatch.setenv("AGENT_PUSH_TOKEN", "pat_secret")
        ok, out = lib.run_verify_isolated(
            'test -z "${OPENROUTER_API_KEY:-}" && test -z "${GH_TOKEN:-}" && test -z "${AGENT_PUSH_TOKEN:-}" && test "$CI" = true', 30)
        assert ok, out

    def test_failure_returns_the_tail_and_timeout_is_reported(self):
        ok, out = lib.run_verify_isolated("echo boom >&2; exit 3", 30)
        assert not ok and "boom" in out
        ok, out = lib.run_verify_isolated("sleep 5", 1)
        assert not ok and "exceeded" in out

    def test_empty_command_passes(self):
        assert lib.run_verify_isolated("  ", 5) == (True, "")


class TestModelCaller:
    def _script(self, tmp_path, body):
        path = tmp_path / "ai.py"
        path.write_text(body)
        return str(path)

    def test_records_usage_and_cost(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        script = self._script(tmp_path, (
            "import sys, json\n"
            "a = sys.argv\n"
            "open(a[a.index('--usage-file')+1], 'w').write(json.dumps({'cost_usd': 0.25}))\n"
            "print('reply')\n"))
        caller = lib.ModelCaller(script, work_dir=str(tmp_path / "w"))
        assert caller.call("planner", "s", "u") == "reply\n"
        caller.call("writer", "s", "u")
        assert caller.total_cost_usd() == 0.5 and caller.cost_by_role() == {"planner": 0.25, "writer": 0.25}

    def test_failing_call_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        caller = lib.ModelCaller(self._script(tmp_path, "import sys; sys.exit(2)"), work_dir=str(tmp_path / "w"))
        assert caller.call("planner", "s", "u") is None


class _Scripted:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def call(self, role, system, user):
        self.prompts.append(user)
        return self.replies.pop(0) if self.replies else None


class TestAskJson:
    def test_retries_once_on_malformed_then_succeeds(self):
        caller = _Scripted(["not json", '```json\n{"findings": []}\n```'])
        out = lib.ask_json(caller, "A", "s", "u", lambda d: lib.parse_findings(d, "A"))
        assert out == [] and len(caller.prompts) == 2 and "not one valid JSON" in caller.prompts[1]

    def test_gives_up_after_the_retry(self):
        caller = _Scripted(["x", "y"])
        assert lib.ask_json(caller, "A", "s", "u", lambda d: lib.parse_findings(d, "A")) is None

    def test_model_failure_is_none_without_retry(self):
        assert lib.ask_json(_Scripted([]), "A", "s", "u", lambda d: d) is None


def test_untrusted_text_is_redacted_and_capped():
    text = lib.sanitize_untrusted("key sk-or-v1-" + "a" * 40 + " " + "x" * 50, limit=60)
    assert "sk-or-v1-" not in text and len(text) <= 60


class TestArchitectTools:
    """The look-ups an architect may ask for: fixed, read-only, and none of them a way out of the repository."""

    @pytest.fixture(autouse=True)
    def project(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        files = {
            "app/__init__.py": "",
            "app/api.py": "from somelib.server import Old\nfrom app.store import save\n\n\n@router.get('/items')\ndef items():\n    return save(Old())\n",
            "app/store.py": "def save(item, *, retries: int = 2) -> bool:\n    \"\"\"Keep one item.\"\"\"\n    return True\n\n\nclass Store:\n    def __init__(self, url: str):\n        self.url = url\n\n    def get(self, key: str) -> str:\n        return key\n\n    def _hidden(self):\n        pass\n",
            "tests/test_store.py": "from app.store import save\n\n\ndef test_save():\n    assert save(1)\n",
            "tests/test_api.py": "from somelib.client import old_client\n\n\ndef test_items():\n    pass\n",
        }
        for name, text in files.items():
            (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / name).write_text(text)
        run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)  # noqa: E731
        run("init", "-q"), run("config", "user.email", "t@t"), run("config", "user.name", "t")
        run("add", "-A"), run("commit", "-qm", "first: the store keeps items")
        self.base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
        monkeypatch.setattr(lib, "tool_context", {"base_sha": self.base, "evidence": None, "runs_dir": str(tmp_path / "agents-run")})
        monkeypatch.delenv("AGENTS_RUN_DIR", raising=False)
        self.root = tmp_path

    def test_a_request_is_one_key_and_an_empty_value_still_asks(self):
        assert lib.parse_explore('```json\n{"usages": "somelib"}\n```') == ("usages", "somelib")
        assert lib.parse_explore('```json\n{"api": ""}\n```') == ("api", "")
        assert lib.parse_explore('```json\n{"read": "app/api.py"}\n```') == ("read", "app/api.py")
        assert lib.parse_explore('```json\n{"shell": "rm -rf ."}\n```') is None
        assert "needs a value" in lib.explore("usages", "")

    def test_usages_shows_every_place_and_the_imports_first(self):
        out = lib.explore("usages", "somelib")
        assert "2 lines in 2 files" in out
        assert "app/api.py:1: from somelib.server import Old" in out and "tests/test_api.py:1:" in out
        assert "Nothing in this repository" in lib.explore("usages", "nowhere_at_all")

    def test_signature_reads_a_function_and_a_class_without_running_them(self):
        assert "def save(item, *, retries: int=2) -> bool" in lib.explore("signature", "app/store.py::save")
        out = lib.explore("signature", "app/store.py::Store")
        assert "def __init__(self, url: str)" in out and "def get(self, key: str) -> str" in out and "_hidden" not in out
        gone = lib.explore("signature", "app/store.py::Missing")
        assert "defines no function or class named 'Missing'" in gone and "save" in gone

    def test_signature_follows_a_name_a_package_only_passes_on(self):
        assert "def dumps(" in lib.explore("signature", "json.dumps")
        assert "class JSONDecoder" in lib.explore("signature", "json.JSONDecoder")     # defined in json.decoder

    def test_history_says_why_a_file_is_as_it_is(self):
        assert "first: the store keeps items" in lib.explore("history", "app/store.py")
        assert "is not a file or folder of this repository" in lib.explore("history", "/etc/passwd")

    def test_tests_names_the_test_files_that_import_a_source_file(self):
        out = lib.explore("tests", "app/store.py")
        assert "tests/test_store.py (1 tests)" in out and "test_api" not in out
        assert "No test file imports app.api" in lib.explore("tests", "app/api.py")
        assert "not a Python file of this repository" in lib.explore("tests", "../outside.py")

    def test_api_lists_the_routes_and_what_the_change_adds(self):
        assert "no route added or removed" in lib.explore("api", "")
        (self.root / "app/api.py").write_text((self.root / "app/api.py").read_text() + "\n\n@router.post('/items')\ndef add():\n    pass\n")
        out = lib.explore("api", "")
        assert "2 routes" in out and "+ added: POST    /items" in out and "removed" not in out.split("Against")[1]

    def test_imports_runs_one_fixed_command_on_tracked_paths_only(self):
        seen = []

        def run(cmd, **kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 2, "app/api.py:1: No name 'Old' in module 'somelib.server'\n*** noise\n", "")
        out = lib.tool_imports("app ../elsewhere --load-plugins=x", run=run)
        assert "found 1 in app" in out and "No name 'Old'" in out and "noise" not in out
        assert seen[0][-1] == "app" and "--load-plugins=x" not in seen[0] and "../elsewhere" not in seen[0]
        assert "None of" in lib.tool_imports("--help", run=run) and len(seen) == 1

    def test_evidence_is_what_the_run_was_given(self):
        assert "reported nothing" in lib.explore("evidence", "")
        lib.tool_context["evidence"] = lambda: "### trivy\n0 critical"
        assert "0 critical" in lib.explore("evidence", "")

    def test_runs_finds_the_earlier_records_and_not_its_own(self):
        folder = self.root / "agents-run"
        folder.mkdir()
        (folder / "old.md").write_text("# an earlier run\nchanged app/store.py and it failed")
        (folder / "now.md").write_text("this run, on app/store.py")
        lib.tool_context["run_record"] = "now.md"
        out = lib.explore("runs", "app/store.py")
        assert "1 earlier runs" in out and "old.md" in out and "now.md" not in out
        assert "No earlier run on record" in lib.explore("runs", "app/other.py")

    def test_docs_reads_only_the_pages_a_library_names_for_itself(self, monkeypatch):
        import email.message
        meta = email.message.Message()
        for key, value in (("Name", "somelib"), ("Version", "2.0.0"), ("Project-URL", "Documentation, https://docs.somelib.example/"),
                           ("Project-URL", "Repository, https://github.com/someone/somelib"), ("Project-URL", "Old, http://plain.example/")):
            meta[key] = value

        class Dist:
            metadata = meta
        scans = []
        monkeypatch.setattr(lib, "_distributions", lambda: scans.append(1) or [Dist()])
        asked = []

        def fetch(url):
            asked.append(url)
            return None if "CHANGELOG" in url else "intro\n## `old_client` removed\nUse `new_client`.\n## other\n"
        about = lib.tool_docs("somelib", fetch=fetch, release_notes=lambda repo, tag: "renamed Old to New" if tag == "v2.0.0" else "",
                              guides=lambda repo, tag: [f"https://raw.githubusercontent.com/{repo}/{tag}/docs/migration.md"])
        assert "version 2.0.0" in about and "renamed Old to New" in about and "someone/somelib/v2.0.0/docs/migration.md" in about
        page = lib.tool_docs("https://docs.somelib.example/migration::old_client", fetch=fetch)
        assert "## `old_client` removed" in page and "new_client" in page and "## other" not in page
        both = lib.tool_docs("https://docs.somelib.example/migration::old_client::other::nowhere", fetch=fetch)
        assert "new_client" in both and "--- about 'other':\n## other" in both and "'nowhere': the page does not mention it" in both
        long_page = "# Guide\n" + "x" * 13000 + "\n## Clients\ntext\n```\n# a comment in code\n```\n### Auth\n"
        listed = lib.tool_docs("https://docs.somelib.example/migration", fetch=lambda url: long_page)
        assert "Its sections:\n- Guide\n  - Clients\n    - Auth" in listed and "a comment in code" not in listed.split("Its sections:")[1]
        assert "new_client" in lib.tool_docs("https://raw.githubusercontent.com/someone/somelib/v2.0.0/docs/migration.md", fetch=fetch)
        before = len(asked)
        for refused in ("https://evil.example/x", "http://docs.somelib.example/x", "https://github.com/other/repo/blob/main/x.md",
                        "https://raw.githubusercontent.com/someone/other/main/x.md", "https://169.254.169.254/latest/meta-data"):
            assert "cannot be read" in lib.tool_docs(refused, fetch=fetch)
        assert len(asked) == before
        assert "No installed library is named" in lib.tool_docs("not-installed-anywhere", fetch=fetch)
        scans.clear()
        assert lib._page_allowed("https://docs.somelib.example/x") and len(scans) == 1      # the libraries are read once per page
        assert "http://plain.example/" not in lib.tool_docs("somelib", fetch=fetch, release_notes=lambda *a: "", guides=lambda *a: [])

    def test_a_page_on_a_private_address_is_never_fetched(self, monkeypatch):
        monkeypatch.setattr(lib.socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("10.0.0.5", 443))])
        assert lib.fetch_page("https://docs.somelib.example/") is None


class TestTheApplicationHasItsOwnInterpreter:
    """flask-test-api #316: the image moved to a Python a library of the engine has no build for, and with
    one interpreter for both no agent job could start. The engine runs on its own; what runs or reads the
    application uses the application's."""

    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        """A stand-in for another interpreter: a folder with a `python`, and libraries of its own."""
        import pr_review_sweep as sweep
        home = tmp_path / "app-python"
        site = home / "lib" / "site-packages"
        (site / "onlyintheapp" / "sub").mkdir(parents=True)
        (site / "onlyintheapp" / "__init__.py").write_text("from .sub.thing import Thing\n")
        (site / "onlyintheapp" / "sub" / "__init__.py").write_text("")
        (site / "onlyintheapp" / "sub" / "thing.py").write_text("class Thing:\n    def __init__(self, size: int):\n        pass\n")
        meta = site / "onlyintheapp-2.0.dist-info"
        meta.mkdir()
        (meta / "METADATA").write_text("Metadata-Version: 2.1\nName: onlyintheapp\nVersion: 2.0\nProject-URL: Documentation, https://docs.onlyintheapp.example/\n")
        (home / "bin").mkdir()
        exe = home / "bin" / "python"
        exe.write_text("#!/bin/sh\n")
        monkeypatch.setenv(sweep.APP_PYTHON_ENV, str(exe))
        monkeypatch.setitem(sweep._app_paths, str(exe), {"purelib": str(site), "platlib": str(site)})
        return home

    def test_with_no_other_interpreter_named_nothing_changes(self, monkeypatch):
        import pr_review_sweep as sweep
        monkeypatch.delenv(sweep.APP_PYTHON_ENV, raising=False)
        assert lib.app_python() == sys.executable and lib.app_env({"PATH": "/usr/bin"}) == {"PATH": "/usr/bin"}

    def test_the_checks_find_the_applications_python_first(self, app, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("pythonLocation", "/engine")
        env = lib.scrubbed_env()
        assert env["PATH"].split(os.pathsep)[0] == str(app / "bin") and "/usr/bin" in env["PATH"]
        assert env["LD_LIBRARY_PATH"].split(os.pathsep)[0] == str(app / "lib") and "pythonLocation" not in env

    def test_its_libraries_are_read_by_name_and_never_imported(self, app, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert "onlyintheapp" not in sys.modules
        assert "class Thing" in lib.explore("read", "onlyintheapp.sub.thing")
        (tmp_path / "local").mkdir()
        (tmp_path / "local" / "__init__.py").write_text("")
        (tmp_path / "local" / "store.py").write_text("def save(item, *, retries: int = 2):\n    return True\n")
        assert "def save" in lib.explore("read", "local.store")                # the repository's own modules, by name too
        assert "def save(item, *, retries: int=2)" in lib.explore("signature", "local.store.save")
        assert "def __init__(self, size: int)" in lib.explore("signature", "onlyintheapp.Thing")      # followed from the package
        about = lib.tool_docs("onlyintheapp", fetch=lambda url: None, release_notes=lambda *a: "", guides=lambda *a: [])
        assert "version 2.0" in about and "https://docs.onlyintheapp.example/" in about
        assert lib._page_allowed("https://docs.onlyintheapp.example/migration")
        assert "onlyintheapp" not in sys.modules

    def test_with_one_interpreter_every_installed_library_is_listed_as_before(self, monkeypatch):
        import pr_review_sweep as sweep
        monkeypatch.delenv(sweep.APP_PYTHON_ENV, raising=False)
        seen = {}
        monkeypatch.setattr(lib.importlib.metadata, "distributions", lambda **kw: seen.update(kw) or [])
        assert lib._distributions() == [] and seen == {}       # no explicit path: wherever the interpreter looks

    def test_a_virtual_environment_of_the_application_is_named(self, app, monkeypatch):
        (app / "pyvenv.cfg").write_text("home = /usr\n")
        monkeypatch.setenv("VIRTUAL_ENV", "/engine/venv")
        assert lib.scrubbed_env()["VIRTUAL_ENV"] == str(app)

    def test_the_import_check_is_shown_the_applications_libraries_last(self, app):
        hook = lib._lint_sees_the_app()
        assert len(hook) == 1 and hook[0].startswith("--init-hook=import sys; sys.path.extend(") and "site-packages" in hook[0]

    def test_a_test_is_run_by_the_applications_python(self, app, tmp_path, monkeypatch):
        seen = {}

        def run(cmd, **kw):
            seen["cmd"], seen["env"] = cmd, kw["env"]
            return subprocess.CompletedProcess(cmd, 0, "1 passed", "")
        monkeypatch.setattr(lib.subprocess, "run", run)
        assert lib.run_pytest(["tests/test_x.py"], tmp_path)[0] == "pass"
        assert seen["cmd"][0] == str(app / "bin" / "python") and seen["env"]["PATH"].startswith(str(app / "bin"))

    def test_a_library_installed_outside_the_usual_folders_is_still_listed(self, app, tmp_path, monkeypatch):
        """An editable or .pth-added install keeps its metadata outside purelib/platlib; the application
        interpreter's own sys.path is scanned, as the no-argument form scans this one's, so it is found."""
        import pr_review_sweep as sweep
        elsewhere = tmp_path / "elsewhere"
        meta = elsewhere / "editableonly-1.0.dist-info"
        meta.mkdir(parents=True)
        (meta / "METADATA").write_text("Metadata-Version: 2.1\nName: editableonly\nVersion: 1.0\n"
                                       "Project-URL: Documentation, https://docs.editableonly.example/\n")
        exe = str(app / "bin" / "python")
        monkeypatch.setitem(sweep._app_paths, exe, {**sweep._app_paths[exe], "syspath": [str(elsewhere)]})
        assert lib._declared_urls("editableonly") == ("1.0", {"Documentation": "https://docs.editableonly.example/"})


class TestNewFunctionsWithNoTest:
    """Release canary of 2026-10-13: a pull request added `below_limit`, the brief asked for no test and
    the steward answered that the existing tests covered the change."""

    DIFF = ("diff --git a/app/canary.py b/app/canary.py\n--- a/app/canary.py\n+++ b/app/canary.py\n@@ -1,4 +1,12 @@\n"
            " def within_limit(value):\n-    return value <= 3\n+    return value < 3\n+\n+\n+def below_limit(value):\n+    return value < 3\n"
            "+\n+\n+def _helper():\n+    pass\n+\n+\n+class Gauge:\n+    pass\n"
            "diff --git a/tests/test_canary.py b/tests/test_canary.py\n--- a/tests/test_canary.py\n+++ b/tests/test_canary.py\n@@ -1 +1,2 @@\n+def added_in_a_test():\n+    pass\n")

    @pytest.fixture(autouse=True)
    def project(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_canary.py").write_text("from app.canary import within_limit\n\n\ndef test_limit():\n    assert within_limit(3)\n")
        subprocess.run(["git", "init", "-q"], check=True)
        self.tests = tmp_path / "tests"

    def test_names_what_was_added_and_no_test_mentions(self):
        assert lib.untested_new_functions(self.DIFF) == ["app/canary.py: below_limit", "app/canary.py: Gauge"]

    def test_a_test_that_names_it_is_enough_even_before_it_is_committed(self):
        (self.tests / "test_new.py").write_text("from app.canary import below_limit, Gauge\n")
        assert lib.untested_new_functions(self.DIFF) == []

    def test_a_function_only_changed_or_moved_is_not_new(self):
        moved = self.DIFF.replace(" def within_limit(value):", "-def below_limit(value):\n def within_limit(value):")
        assert lib.untested_new_functions(moved) == ["app/canary.py: Gauge"]
        assert lib.untested_new_functions("") == []
