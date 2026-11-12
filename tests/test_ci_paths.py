"""The CI pull-request path filter skips Markdown-only changes and nothing else.

GitHub decides from `on.pull_request.paths`: the last pattern that matches a changed file
wins, and a `!` pattern excludes. The matcher here follows that rule, with `**` for any
characters and `*` for any characters except `/`.

The Markdown files that the test gate reads must still start CI. A read is found in Python
code under app/, tests/, tools/ and scripts/: a string value (not a comment or a docstring)
that names a Markdown file, in a statement that builds a path from a name that holds ROOT or
REPO (for example `ROOT / "CHANGELOG.md"` or `os.path.join(REPO, "docs", "x.md")`). Each
folder of the file must be a string value of the same statement; when several files fit, the
deepest one counts. A fixture under a temporary folder is not a read.
"""
import ast
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SKIPPED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "work", "output", "outputs"}
CODE_DIRS = ("app", "tests", "tools", "scripts")


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


def repo_files(suffix: str) -> list:
    found = []
    for path in ROOT.rglob("*" + suffix):
        relative = path.relative_to(ROOT)
        if path.is_file() and not any(part in SKIPPED_DIRS for part in relative.parts[:-1]):
            found.append(relative.as_posix())
    return found


def docstrings(tree: ast.AST) -> set:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def markdown_reads(source: str, markdown: list) -> set:
    tree = ast.parse(source)
    skip = docstrings(tree)
    reads = set()
    for statement in ast.walk(tree):
        if not isinstance(statement, ast.stmt) or isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        nodes = [n for n in ast.walk(statement) if not isinstance(n, ast.stmt) or n is statement]
        if any(isinstance(n, (ast.FunctionDef, ast.ClassDef)) for n in nodes if n is not statement):
            continue
        if not any(isinstance(n, ast.Name) and re.search("ROOT|REPO", n.id) for n in nodes):
            continue
        values = {n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)
                  and id(n) not in skip}
        for name in values:
            if name.endswith(".md"):
                fits = [path for path in markdown if path.split("/")[-1] == name
                        and all(part in values for part in path.split("/")[:-1])]
                if fits:
                    reads.add(max(fits, key=lambda path: path.count("/")))
    return reads


class TestCiPathFilter(unittest.TestCase):
    def setUp(self):
        self.patterns = triggers()["pull_request"]["paths"]

    def test_push_runs_every_job_on_every_change(self):
        push = triggers()["push"]
        self.assertNotIn("paths", push, "the release gate needs a full push run on main")
        self.assertNotIn("paths-ignore", push, "the release gate needs a full push run on main")

    def test_markdown_that_the_test_gate_reads_still_runs_ci(self):
        markdown = repo_files(".md")
        reads = set()
        for code in repo_files(".py"):
            if code.startswith(CODE_DIRS):
                reads |= markdown_reads((ROOT / code).read_text(encoding="utf-8"), markdown)
        self.assertIn("CHANGELOG" + ".md", reads, "the reader scan found no known read")
        missing = sorted(m for m in reads if not runs_ci(m, self.patterns))
        self.assertEqual(missing, [], "add these to on.pull_request.paths in ci.yml")

    def test_only_real_reads_count(self):
        md = ["docs/summary" + ".md", "docs/notes" + ".md", "summary" + ".md"]
        source = "\n".join(['"""See docs/notes' + '.md."""',
                            "# docs/notes" + ".md",
                            "path = Path(folder) / 'summary" + ".md'",
                            "DOC = os.path.join(REPO, 'docs', 'summary" + ".md')", ""])
        self.assertEqual(markdown_reads(source, md), {"docs/summary" + ".md"})

    def test_code_workflow_and_dot_files_run_ci(self):
        for path in ("app/main.py", "tests/test_units.py", "requirements.txt", "VERSION",
                     ".github/workflows/ci.yml", ".github/workflows/release.yml",
                     ".env.example", ".gitignore", "assets/setup.html", "prompt.txt.example"):
            self.assertTrue(runs_ci(path, self.patterns), path)

    def test_other_markdown_skips_ci(self):
        for path in ("README" + ".md", "docs/install" + ".md", "docs/plans/x" + ".md",
                     "docs/engineering-notes" + ".md", "docs/summary" + ".md",
                     ".agents/skills/verify-odicto/SKILL" + ".md"):
            self.assertFalse(runs_ci(path, self.patterns), path)


if __name__ == "__main__":
    unittest.main()
