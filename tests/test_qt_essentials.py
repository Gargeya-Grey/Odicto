"""The app ships PySide6-Essentials only: QtCore, QtGui and QtWidgets.

A release installs from tools/distribution/requirements.lock. It leaves out the big Qt packages
(addons, webengine, pdf), so an import of any other Qt module would fail on a user's machine while it
works on a developer's full PySide6. Imports and package metadata are read with `ast`, so a comment
or a string value never counts.
"""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE_DIRS = ("app", "tests", "tools")
SKIPPED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}
ALLOWED_MODULES = {"QtCore", "QtGui", "QtWidgets"}
METADATA_CALLS = {"version", "distribution", "metadata", "requires", "files"}
BANNED_LOCK_PINS = ("pyside6==", "pyside6-addons==", "pyside6-webengine==", "pyside6-pdf==")


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def qt_violations(source: str, filename: str = "<source>") -> list:
    """Return one line per import outside Essentials and per metadata read of the PySide6 package."""
    found = []
    tree = ast.parse(source, filename)
    # `from importlib.metadata import version as v` binds another name to a metadata call.
    metadata_calls = set(METADATA_CALLS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("importlib.metadata", "importlib_metadata"):
            metadata_calls.update(alias.asname for alias in node.names
                                  if alias.asname and alias.name in METADATA_CALLS)
    for node in ast.walk(tree):
        where = "%s:%d" % (filename, getattr(node, "lineno", 0))
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            parts = node.module.split(".")
            if parts[0] != "PySide6":
                continue
            if len(parts) > 1:
                if parts[1] not in ALLOWED_MODULES:
                    found.append("%s imports %s" % (where, node.module))
                continue
            for alias in node.names:
                if alias.name not in ALLOWED_MODULES:
                    found.append("%s imports PySide6.%s" % (where, alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "PySide6" and len(parts) > 1 and parts[1] not in ALLOWED_MODULES:
                    found.append("%s imports %s" % (where, alias.name))
        elif isinstance(node, ast.Call):
            first = node.args[0] if node.args else next(
                (keyword.value for keyword in node.keywords if keyword.arg == "distribution_name"), None)
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            name = _call_name(node)
            if name in metadata_calls and _normalized(first.value) == "pyside6":
                found.append("%s reads the metadata of the PySide6 package" % where)
            elif name == "import_module" and first.value.split(".")[0] == "PySide6":
                parts = first.value.split(".")
                if len(parts) > 1 and parts[1] not in ALLOWED_MODULES:
                    found.append("%s imports %s" % (where, first.value))
    return found


def python_files():
    for folder in CODE_DIRS:
        for path in sorted((ROOT / folder).rglob("*.py")):
            if not SKIPPED_DIRS.intersection(path.relative_to(ROOT).parts):
                yield path


def requirement_names(text: str) -> list:
    names = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
        if match:
            names.append(_normalized(match.group(0)))
    return names


class TestQtEssentialsOnly(unittest.TestCase):
    def test_no_file_uses_a_qt_module_outside_essentials(self):
        problems = []
        files = 0
        for path in python_files():
            files += 1
            problems += qt_violations(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))
        self.assertGreater(files, 50, "the scan found too few Python files")
        self.assertEqual([], problems)

    def test_requirements_ask_for_essentials_not_full_pyside6(self):
        names = requirement_names((ROOT / "requirements.txt").read_text(encoding="utf-8"))
        self.assertIn("pyside6-essentials", names)
        self.assertNotIn("pyside6", names)

    def test_lock_has_no_qt_extras(self):
        pins = [line.strip().lower() for line in
                (ROOT / "tools" / "distribution" / "requirements.lock").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(line.startswith("pyside6-essentials==") for line in pins))
        for banned in BANNED_LOCK_PINS:
            self.assertFalse([line for line in pins if line.startswith(banned)], banned)


class TestScannerCatchesWhatItShould(unittest.TestCase):
    def test_flags_every_spelling_of_a_forbidden_import(self):
        for source in (
            "from PySide6.QtWebEngineWidgets import QWebEngineView",
            "from PySide6.QtMultimedia import QAudioSource",
            "import PySide6.QtSvg",
            "import PySide6.QtSvg as svg",
            "from PySide6 import QtNetwork",
            "from PySide6 import QtCore, QtPdf",
            "from PySide6 import *",
            "import importlib\nimportlib.import_module('PySide6.QtQml')",
            "def f():\n    from PySide6.QtOpenGL import QOpenGLWindow",
        ):
            with self.subTest(source=source):
                self.assertTrue(qt_violations(source), source)

    def test_flags_metadata_of_the_full_distribution(self):
        for source in (
            "import importlib.metadata\nimportlib.metadata.version('PySide6')",
            "from importlib.metadata import version\nversion('PySide6')",
            "from importlib.metadata import distribution\ndistribution('pyside6')",
            "import importlib.metadata as md\nmd.version('PySide6')",
            "from importlib.metadata import version as v\nv('PySide6')",
            "from importlib.metadata import version\nversion(distribution_name='PySide6')",
        ):
            with self.subTest(source=source):
                self.assertTrue(qt_violations(source), source)

    def test_accepts_essentials_and_ignores_strings_and_comments(self):
        for source in (
            "from PySide6.QtCore import Qt, QTimer",
            "from PySide6.QtGui import QColor\nfrom PySide6.QtWidgets import QApplication",
            "import PySide6\nimport PySide6.QtCore",
            "from PySide6 import QtCore, QtGui, QtWidgets",
            "import importlib.metadata\nimportlib.metadata.version('PySide6-Essentials')",
            "# from PySide6.QtWebEngineWidgets import QWebEngineView\nx = 'import PySide6.QtSvg'",
            "'''from PySide6 import QtPdf'''",
            "from other import PySide6\nPySide6.QtSvg",
            "version('sounddevice')",
            "from importlib.metadata import version as v\nv('PySide6-Essentials')\nw('PySide6')",
        ):
            with self.subTest(source=source):
                self.assertEqual([], qt_violations(source), source)

    def test_requirement_parser_separates_the_two_packages(self):
        names = requirement_names("PySide6>=6.6\n# PySide6-Essentials\nPySide6-Essentials>=6.6; sys_platform != 'x'")
        self.assertEqual(["pyside6", "pyside6-essentials"], names)
        self.assertEqual(["pyside6-essentials"], requirement_names("PySide6_Essentials>=6"))


if __name__ == "__main__":
    unittest.main()
