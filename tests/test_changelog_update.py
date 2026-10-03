"""Changelog updates are deterministic and idempotent: one entry per merge,
none for the pipeline's own bookkeeping commits, none for changes that already
updated the changelog themselves."""

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import changelog_update as cu  # noqa: E402


class TestAddEntry:
    def test_creates_file_structure_and_canonical_category_order(self):
        text = ""
        for cat, entry in (("Fixed", "Fix a"), ("Added", "Add b"), ("Fixed", "Fix c"), ("Dependencies", "Bump d")):
            text = cu.add_entry(text, cat, entry)
        assert text.startswith("# Changelog")
        assert text.index("### Added") < text.index("### Fixed") < text.index("### Dependencies")
        assert "- Fix a\n- Fix c" in text

    def test_entries_go_into_unreleased_not_older_releases(self):
        text = "# Changelog\n\n## [Unreleased]\n\n### Added\n- New\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- Old\n"
        out = cu.add_entry(text, "Added", "Newer")
        assert out.index("- Newer") < out.index("## [1.0.0]")
        assert out.count("- Old") == 1

    def test_unknown_category_rejected(self):
        with pytest.raises(ValueError):
            cu.add_entry("", "Misc", "x")


class TestSubjects:
    @pytest.mark.parametrize("subject,category", [
        ("feat(api): add search (#3)", "Added"),
        ("fix: handle empty body", "Fixed"),
        ("Update dependency pydantic to v2.13.5 (#119)", "Dependencies"),
        ("Update bridgecrewio/checkov-action action to v12 (#157)", "Dependencies"),
        ("refactor: split storage", "Changed"),
        ("plain subject", "Changed"),
    ])
    def test_category(self, subject, category):
        assert cu.category_for(subject) == category

    def test_clean_subject_drops_prefix_and_pr_ref(self):
        assert cu.clean_subject("feat(api): add search (#3)") == "Add search"

    def test_entry_links_the_pull_request(self):
        assert cu.format_entry("fix: a (#7)", "7", "abc1234", "https://github.com/o/r") == \
            "A ([#7](https://github.com/o/r/pull/7))"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def git(*a):
        return subprocess.run(["git", *a], capture_output=True, text=True, check=True).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "a.txt").write_text("0")
    git("add", "-A")
    git("commit", "-qm", "init")
    return git


def _commit(git, subject, files):
    for name, content in files.items():
        pathlib.Path(name).write_text(content)
    git("add", "-A")
    git("commit", "-qm", subject)
    return git("rev-parse", "HEAD")


class TestFromPush:
    def test_one_entry_per_merge_and_bookkeeping_skipped(self, repo):
        before = repo("rev-parse", "HEAD")
        _commit(repo, "feat: add search (#12)", {"a.txt": "1"})
        _commit(repo, "Done  by Github Actions   Job changemanifest: 9", {"a.txt": "2"})
        after = _commit(repo, "fix: crash on empty input (#13)", {"a.txt": "3"})
        added = cu.entries_for_push(pathlib.Path("CHANGELOG.md"), before, after, "https://github.com/o/r")
        text = pathlib.Path("CHANGELOG.md").read_text()
        assert added == 2
        assert "Add search" in text and "Crash on empty input" in text
        assert "changemanifest" not in text

    def test_rerun_is_idempotent(self, repo):
        before = repo("rev-parse", "HEAD")
        after = _commit(repo, "feat: add search (#12)", {"a.txt": "1"})
        path = pathlib.Path("CHANGELOG.md")
        assert cu.entries_for_push(path, before, after, "") == 1
        assert cu.entries_for_push(path, before, after, "") == 0
        assert path.read_text().count("Add search") == 1

    def test_change_that_updated_the_changelog_itself_is_not_logged_twice(self, repo):
        before = repo("rev-parse", "HEAD")
        after = _commit(repo, "feat: agent change (#20)", {
            "a.txt": "1", "CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n\n### Added\n- Agent change (#7)\n"})
        assert cu.entries_for_push(pathlib.Path("CHANGELOG.md"), before, after, "") == 0

    def test_new_branch_zero_before_uses_only_the_tip(self, repo):
        after = _commit(repo, "fix: only me (#1)", {"a.txt": "1"})
        assert cu.entries_for_push(pathlib.Path("CHANGELOG.md"), "0" * 40, after, "") == 1
