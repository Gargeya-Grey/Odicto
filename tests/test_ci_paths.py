"""The CI pull-request path filter skips Markdown-only changes and nothing else.

GitHub decides from `on.pull_request.paths`: the last pattern that matches a changed file
wins, and a `!` pattern excludes. The matcher here follows that rule, with `**` for any
characters and `*` for any characters except `/`.
"""
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SKIPPED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "work", "output", "outputs"}
CODE_DIRS = ("app", "tests", "tools", "scripts")
CODE_SUFFIXES = {".py", ".ps1", ".sh", ".bat", ".cmd"}


def triggers() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return data.get("on", data.get(True))


def pattern_regex(pattern: str) -> re.Pattern:
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def runs_ci(path: str, patterns: list) -> bool:
    result = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        if pattern_regex(pattern.lstrip("!")).match(path):
            result = not negated
    return result


def repo_files(suffixes) -> list:
    found = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in SKIPPED_DIRS for part in relative.parts[:-1]):
            continue
        if path.is_file() and path.suffix in suffixes:
            found.append(relative.as_posix())
    return found


class TestCiPathFilter(unittest.TestCase):
    def setUp(self):
        self.patterns = triggers()["pull_request"]["paths"]

    def test_push_runs_every_job_on_every_change(self):
        push = triggers()["push"]
        self.assertNotIn("paths", push, "the release gate needs a full push run on main")
        self.assertNotIn("paths-ignore", push, "the release gate needs a full push run on main")

    def test_markdown_that_code_names_still_runs_ci(self):
        markdown = repo_files({".md"})
        this_file = Path(__file__).resolve().relative_to(ROOT).as_posix()
        named = set()
        for code in repo_files(CODE_SUFFIXES):
            if code == this_file or not code.startswith(CODE_DIRS):
                continue
            text = (ROOT / code).read_text(encoding="utf-8", errors="replace")
            for name in set(re.findall(r"[\w.-]+\.md\b", text)):
                named.update(m for m in markdown if m.rsplit("/", 1)[-1] == name)
        self.assertTrue(named, "expected at least the changelog to be named by a test")
        missing = sorted(m for m in named if not runs_ci(m, self.patterns))
        self.assertEqual(missing, [], "add these to on.pull_request.paths in ci.yml")

    def test_code_workflow_and_dot_files_run_ci(self):
        for path in ("app/main.py", "tests/test_units.py", "requirements.txt", "VERSION",
                     ".github/workflows/ci.yml", ".github/workflows/release.yml",
                     ".env.example", ".gitignore", "assets/setup.html", "prompt.txt.example"):
            self.assertTrue(runs_ci(path, self.patterns), path)

    def test_other_markdown_skips_ci(self):
        for path in ("README" + ".md", "docs/install" + ".md", "docs/plans/x" + ".md",
                     ".agents/skills/verify-odicto/SKILL" + ".md"):
            self.assertFalse(runs_ci(path, self.patterns), path)


if __name__ == "__main__":
    unittest.main()
