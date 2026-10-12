"""Equivalence oracle for the efficient-modular refactor (see docs/architecture.md).

Pins the rendered setup page and a set of pure config functions so that
"behaviour is unchanged" is *proved* rather than asserted while the code is
de-duplicated. Goldens were captured from the pre-refactor tree.
It was deliberately re-baselined once for the Quiet Console setup-page redesign
(September 2026): same 14 states, same render harness, same hash mechanism - only the
recorded page digests moved. Config/platform/token goldens were not touched.

Design notes
------------
* Imports only ``setup_web`` and ``config`` - never ``main``, which calls
  ``attach_pythonw_log()`` at import time and would touch the on-disk log.
  The oracle is deliberately hermetic.
* Page goldens are SHA-256 hashes, not fixtures: committing a ~1,750-line HTML
  fixture per state would dwarf the line savings the refactor is chasing. On a
  mismatch the rendered page is dumped to disk so the diff is still diagnosable.
* Every input that could differ between machines is pinned, including
  ``config._PRESENT_AT_IMPORT`` - it is computed at import from the real ``.env``,
  and leaving it unpinned produces a golden that passes locally and fails on CI.
  (This is not hypothetical: an early capture of ``OPENROUTER_REASONING_EFFORT``
  silently encoded the developer's own ``.env`` value.)
* ``test_units.py`` is not touched by this refactor; this is an independent,
  additional oracle. Run both.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import config
import setup_web

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_ROOT = os.path.join(REPO_ROOT, "app")

_BACKEND_FOR_PLATFORM = {"win32": "windows", "darwin": "macos"}
_SUBMODULE_NAMES = {"base", "_keyboard", "_posix", "windows", "macos", "linux"}
# Set on the ``platforms`` package directly from platforms.base (see
# platforms/__init__.py), so they are legitimately absent from a backend's __all__.
_BASE_REEXPORTS = {"clipboard_read", "clipboard_write"}

# Updated for Groq chat, optional polish and explicit screenshot settings; two new states.
# sha256 of setup_web._page() per state. Captured pre-refactor; re-baselined when
# the speech page gained Groq, Grok, OpenRouter, and the GPU device control
# (same states, same mechanism, new rendering).
# Folder cleanup: recovery now uses the public odicto.py setup entry point.
# Before rebasing, every old golden was verified with only that sentence reverted.
# Re-baselined for the redesigned setup page (desktop layout in the mobile app's design,
# version chip, running-state card, admin-windows switch). The app version and the admin
# switch state are now pinned; admin_on and admin_unavailable are new states. Every state
# was checked for leftover __TOKENS__ and for the test API keys before capture.
# Review fixes: one status poll loop, and the select search box no longer marks the page dirty.
_GOLDEN_PAGE_HASHES = {'fresh_empty_env': 'b48f385c47d336d5c41418ce43f159c49342a62b53346e0c1d293450092962bf',
 'provider_meta': '44e49c9be2e601a878b55f43fb0e99f32487443c64c8f2504c9ff94d07a62c46',
 'provider_openrouter': 'dc63dad2d86b51c39267efa5222f402e889fec4a43cb232a9433b54dc17bbc72',
 'provider_gemini': '119f6afd707b0870aa18825919a39b31a3ba8fabbf23b90e1303c10cc74a793a',
 'provider_ollama': 'a1a85ec93442dc5387b6621fd402a5a8002ae8c6d5fbd493a6d10f27649e75bb',
 'provider_none': 'b48f385c47d336d5c41418ce43f159c49342a62b53346e0c1d293450092962bf',
 'provider_groq': '694ea0ac35cf8dd521879e6eb89bceff5efb81e4409f70d5bb68f168c9ddafa6',
 'polish_enabled': '6d0929a6c135ffe729d78ea1b6520149332008d13448db422dc43babf787157c',
 'history_and_custom': '866a25298f399fda7f8319d61e598bfcedb092d243976351e2f67ec0b3d3edbd',
 'hotkey_short': '021c11de00631dea34e5c4e0f40b8c376815a83fb0ff8c12cad611ee921b32fa',
 'stt_gemini': '3f484da7f20894eb012458bf502b64717d64281c5e368d14a49d36e1952fba52',
 'prompt_from_file': '14b283c658e791703df67b5d167fee12d2c31a0f7001f43ef135ddc47ef9d5a0',
 'prompt_from_inline_env': '903210be1f58189e4b405edb606f6dad1a88fb40611e3c2b6f77d80d700cf93f',
 'msg_ok': '073772e8fec8250d872bbac897e64e204d64d1384bc2b374bd84c77496e9c691',
 'msg_err': 'acd2121f6b8d2ad8db59b8404f19a6d2007e51dc08d33b42ae39244efd616b5a',
 'msg_neutral': '0333fb47479cce35cbf593b7ecdc6d7987e5b78cbaca516ad9b62d6c707ad2d9',
 'admin_on': '3a115cfbadd1b8168f575c76a81e4c40a1429a737b71956d6227fefd67c05e93',
 'admin_unavailable': '3b01237d558ca08ee6d94fa3b749ad1159eae7f26465faf7d0fa23703549f14f'}

# Captured from the pre-refactor tree with every machine-dependent input pinned.
_GOLDEN_PURE_VALUES = {
    "parse_hold_hotkey_default": (("ctrl",), "grave"),
    "parse_hold_hotkey_ai": (("ctrl", "shift"), "grave"),
    "sanitize_hash_tail": "openai/gpt-5.6-luna",
    "sanitize_plain": "plain/model",
    "normalize_key_aliases": ["right ctrl", "left shift", "grave", "ctrl", "f7"],
    "eff_meta": ["m1", "https://api.meta.ai/v1", "k", 1024, "low"],
    "eff_openrouter": ["o1", "https://openrouter.ai/api/v1", "k", 1024, "none"],
    "eff_gemini": ["g1", "https://generativelanguage.googleapis.com", "k", 1024, "medium"],
    "eff_ollama": ["q1", "", "", 1024, ""],
    "eff_none": ["", "", "", 1024, ""],
}

# env -> value written to a temporary .env; optional prompt_txt / system_prompt / present.
_PAGE_STATES = {
    "fresh_empty_env": {"env": {}},
    "provider_meta": {
        "env": {"LLM_PROVIDER": "meta", "META_API_KEY": "sk-meta-secret", "META_MODEL": "muse-test"}
    },
    "provider_openrouter": {
        "env": {
            "LLM_PROVIDER": "openrouter",
            "OPENROUTER_API_KEY": "sk-or-secret",
            "OPENROUTER_MODEL": "openai/test-model",
        }
    },
    "provider_gemini": {
        "env": {"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "sk-gem-secret", "GEMINI_MODEL": "gemini-test"}
    },
    "provider_ollama": {"env": {"LLM_PROVIDER": "ollama", "OLLAMA_MODEL": "qwen-test"}},
    "provider_none": {"env": {"LLM_PROVIDER": "none"}},
    "provider_groq": {"env": {"LLM_PROVIDER": "groq", "GROQ_API_KEY": "fake-test-key", "GROQ_MODEL": "openai/gpt-oss-120b"}},
    "polish_enabled": {"env": {"LLM_PROVIDER": "groq", "POLISH_DICTATION": "true", "POLISH_MODEL": "polish/model", "AI_CLIPBOARD_IMAGE": "true"}},
    "history_and_custom": {
        "env": {
            "LLM_PROVIDER": "openrouter",
            "OPENROUTER_MODEL_HISTORY": "alpha,beta,alpha",
            "META_MODEL_HISTORY": "one,two",
            "GEMINI_MODEL_HISTORY": "g1,g2",
            "OLLAMA_MODEL_HISTORY": "o1",
            "OPENROUTER_REASONING_EFFORT": "law",
            "OPENROUTER_PROVIDER_SORT": "price",
            "LLM_MAX_TOKENS": "2048",
            "LLM_NUM_CTX": "4096",
            "LLM_API_BASE": "http://localhost:11434",
        }
    },
    "hotkey_short": {"env": {"HOTKEY_TOGGLE": "false", "LLM_PROVIDER": "none"}},
    "stt_gemini": {
        "env": {"STT_PROVIDER": "gemini", "GEMINI_TRANSCRIBE_MODE": "verbatim", "LLM_PROVIDER": "none"}
    },
    "prompt_from_file": {"env": {"LLM_PROVIDER": "none"}, "prompt_txt": "PROMPT-FROM-FILE-MARKER"},
    "prompt_from_inline_env": {
        "env": {"LLM_PROVIDER": "none"},
        "system_prompt": "INLINE-PROMPT-MARKER",
        "present": ("SYSTEM_PROMPT",),
    },
    "msg_ok": {"env": {"LLM_PROVIDER": "none"}, "message": "Saved.", "message_kind": "ok"},
    "msg_err": {"env": {"LLM_PROVIDER": "none"}, "message": "Could not write.", "message_kind": "err"},
    "msg_neutral": {"env": {"LLM_PROVIDER": "none"}, "message": "Hello there", "message_kind": "neutral"},
    # (available, enabled) of the Windows admin-windows switch; the default pin is (True, False).
    "admin_on": {"env": {"LLM_PROVIDER": "none"}, "admin": (True, True)},
    "admin_unavailable": {"env": {"LLM_PROVIDER": "none"}, "admin": (False, False)},
}

_PROVIDER_ATTRS = {
    "meta": {
        "LLM_PROVIDER": "meta",
        "META_MODEL": "m1",
        "META_API_KEY": "k",
        "META_REASONING_EFFORT": "low",
    },
    "openrouter": {
        "LLM_PROVIDER": "openrouter",
        "OPENROUTER_MODEL": "o1",
        "OPENROUTER_API_KEY": "k",
        "OPENROUTER_REASONING_EFFORT": "high",
    },
    "gemini": {
        "LLM_PROVIDER": "gemini",
        "GEMINI_MODEL": "g1",
        "GEMINI_API_KEY": "k",
        "GEMINI_THINKING_LEVEL": "medium",
    },
    "ollama": {"LLM_PROVIDER": "ollama", "OLLAMA_MODEL": "q1"},
    "none": {"LLM_PROVIDER": "none"},
}


def _render_page(state: dict) -> str:
    """Render ``setup_web._page()`` with every machine-dependent input pinned."""
    tmp = tempfile.mkdtemp(prefix="odicto-equiv-")
    prompt_dir = os.path.join(tmp, "prompts")
    os.makedirs(prompt_dir)
    if state.get("prompt_txt"):
        with open(os.path.join(prompt_dir, "prompt.txt"), "w", encoding="utf-8") as handle:
            handle.write(state["prompt_txt"])
    env_path = os.path.join(tmp, ".env")
    with open(env_path, "w", encoding="utf-8") as handle:
        for key, value in state.get("env", {}).items():
            handle.write("%s=%s\n" % (key, value))

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(config, "_prompt_dir", lambda: prompt_dir))
        stack.enter_context(mock.patch.object(setup_web, "ENV_PATH", env_path))
        stack.enter_context(
            mock.patch.object(config, "_PRESENT_AT_IMPORT", frozenset(state.get("present", ())))
        )
        stack.enter_context(mock.patch.object(config.Config, "SYSTEM_PROMPT", state.get("system_prompt", "")))
        stack.enter_context(mock.patch.object(config.Config, "SYSTEM_PROMPT_FILE", ""))
        # The VERSION file and the Windows admin marker differ per checkout and machine.
        stack.enter_context(mock.patch.object(setup_web, "app_version", lambda: "9.9.9"))
        admin = state.get("admin", (True, False))
        stack.enter_context(mock.patch.object(setup_web, "_admin_mode_state", lambda: admin))
        sink = io.StringIO()
        stack.enter_context(contextlib.redirect_stdout(sink))
        stack.enter_context(contextlib.redirect_stderr(sink))
        return setup_web._page(state.get("message", ""), state.get("message_kind", "neutral"))


def _pure_values() -> dict:
    """Deterministic config values. See the module docstring on why each pin is required."""
    config_cls = config.Config
    out: dict = {}
    global_pins = (
        (config, "_PRESENT_AT_IMPORT", frozenset()),
        (config_cls, "LLM_MODEL", ""),
        (config_cls, "LLM_API_BASE", ""),
        (config_cls, "LLM_REASONING_EFFORT", ""),
        (config_cls, "LLM_MAX_TOKENS", "1024"),
        (config_cls, "META_API_BASE", ""),
        (config_cls, "OPENROUTER_API_BASE", ""),
    )
    with contextlib.ExitStack() as stack:
        for target, attr, value in global_pins:
            stack.enter_context(mock.patch.object(target, attr, value))
        sink = io.StringIO()
        stack.enter_context(contextlib.redirect_stdout(sink))
        stack.enter_context(contextlib.redirect_stderr(sink))

        out["parse_hold_hotkey_default"] = config.parse_hold_hotkey("ctrl+grave")
        out["parse_hold_hotkey_ai"] = config.parse_hold_hotkey("ctrl+shift+grave")
        out["sanitize_hash_tail"] = config._sanitize_model_id("openai/gpt-5.6-luna#oops")
        out["sanitize_plain"] = config._sanitize_model_id("  plain/model  ")
        out["normalize_key_aliases"] = [
            config.normalize_key_name(key)
            for key in ("right ctrl", "left shift", "grave", "ctrl", "f7")
        ]
        for label, attrs in _PROVIDER_ATTRS.items():
            with contextlib.ExitStack() as inner:
                for key, value in attrs.items():
                    inner.enter_context(mock.patch.object(config_cls, key, value))
                out["eff_%s" % label] = [
                    config_cls.effective_llm_model(),
                    config_cls.effective_llm_api_base(),
                    config_cls.effective_api_key(),
                    config_cls.effective_max_output_tokens(),
                    config_cls.effective_reasoning_effort(),
                ]
    return out


def _normalized(values: dict) -> str:
    """Tuples compare unequal to lists, either of which json round-trips; compare as JSON."""
    return json.dumps(values, sort_keys=True, default=str)


def _top_level_exports(tree: ast.Module):
    """Names a star-import would pick up, plus the modules it would star-import from."""
    names, stars = set(), []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    if node.module:
                        stars.append(node.module)
                else:
                    names.add(alias.asname or alias.name)
    return {name for name in names if not name.startswith("_")}, stars


def _backend_exports(module_name: str, seen=None) -> set:
    """The set of names ``from platforms.<backend> import *`` would bind.

    Uses ``__all__`` when the module declares one, otherwise walks the module's own
    top-level names and recurses through its star-imports (windows.py/linux.py pull
    most of their surface from _keyboard.py).
    """
    seen = seen if seen is not None else set()
    if module_name in seen:
        return set()
    seen.add(module_name)
    relative = module_name.replace("platforms.", "", 1)
    path = os.path.join(APP_ROOT, "platforms", *relative.split(".")) + ".py"
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), path)

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    return {el.value for el in node.value.elts if isinstance(el, ast.Constant)}

    names, stars = _top_level_exports(tree)
    for star in stars:
        if star.startswith("platforms."):
            names |= _backend_exports(star, seen)
    return names


def _referenced_platform_names() -> set:
    """Every ``platforms.<name>`` / ``from platforms import <name>`` in the repo."""
    names = set()
    for dirpath, dirnames, filenames in os.walk(APP_ROOT):
        dirnames[:] = [d for d in dirnames if d not in {".venv", "__pycache__", ".git", "node_modules"}]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), path)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "platforms"
                ):
                    names.add(node.attr)
                elif isinstance(node, ast.ImportFrom) and node.module == "platforms":
                    names.update(alias.name for alias in node.names)
    return names


class TestSetupPageEquivalence(unittest.TestCase):
    """The setup page must render byte-for-byte identically to its recorded golden."""

    def test_page_matches_pre_refactor_golden(self) -> None:
        for name, state in _PAGE_STATES.items():
            with self.subTest(state=name):
                page = _render_page(state)
                digest = hashlib.sha256(page.encode("utf-8")).hexdigest()
                if digest != _GOLDEN_PAGE_HASHES[name]:
                    dump = os.path.join(tempfile.gettempdir(), "odicto-page-%s.html" % name)
                    with open(dump, "w", encoding="utf-8") as handle:
                        handle.write(page)
                    self.fail(
                        "state %r no longer renders identically\n  expected sha256 %s\n"
                        "  actual   sha256 %s\n  rendered page written to %s"
                        % (name, _GOLDEN_PAGE_HASHES[name], digest, dump)
                    )

    def test_goldens_cover_every_state(self) -> None:
        """Guards against a state being added without capturing its golden."""
        self.assertEqual(sorted(_PAGE_STATES), sorted(_GOLDEN_PAGE_HASHES))

    def test_every_provider_is_rendered(self) -> None:
        hashes = [_GOLDEN_PAGE_HASHES["provider_%s" % p] for p in ("meta", "openrouter", "gemini", "ollama", "groq")]
        self.assertEqual(len(set(hashes)), 5, "each provider must render a distinct page")


class TestConfigEquivalence(unittest.TestCase):
    """Cascade resolvers and helpers must return exactly the pre-refactor values."""

    def test_pure_values_match_pre_refactor_golden(self) -> None:
        actual = _pure_values()
        self.assertEqual(_normalized(_GOLDEN_PURE_VALUES), _normalized(actual))

    def test_provider_specific_override_beats_generic(self) -> None:
        with mock.patch.object(config, "_PRESENT_AT_IMPORT", frozenset()), mock.patch.object(
            config.Config, "LLM_PROVIDER", "meta"
        ), mock.patch.object(config.Config, "META_MODEL", "specific"), mock.patch.object(
            config.Config, "LLM_MODEL", "generic"
        ):
            self.assertEqual("specific", config.Config.effective_llm_model())

    def test_generic_tier_applies_when_provider_key_is_blank(self) -> None:
        with mock.patch.object(config, "_PRESENT_AT_IMPORT", frozenset()), mock.patch.object(
            config.Config, "LLM_PROVIDER", "meta"
        ), mock.patch.object(config.Config, "META_MODEL", ""), mock.patch.object(
            config.Config, "LLM_MODEL", "generic"
        ):
            self.assertEqual("generic", config.Config.effective_llm_model())

    def test_builtin_default_is_the_last_tier(self) -> None:
        with mock.patch.object(config, "_PRESENT_AT_IMPORT", frozenset()), mock.patch.object(
            config.Config, "LLM_PROVIDER", "meta"
        ), mock.patch.object(config.Config, "META_MODEL", ""), mock.patch.object(
            config.Config, "LLM_MODEL", ""
        ):
            self.assertEqual(config.ENV_DEFAULTS["META_MODEL"], config.Config.effective_llm_model())


class TestPlatformExportSurface(unittest.TestCase):
    """Guard for the declared platform interface (see docs/architecture.md).

    This is an ``ast`` scan rather than an import on purpose: ``platforms.KEY_DOWN``
    and ``platforms.KEY_UP`` are read *inside* the hotkey handlers in main.py, which
    only run when the real global hook fires. A backend ``__all__`` that drops them
    passes ``import main`` and all of test_units on every OS, and fails only in
    production - so this scan is the only gate that catches it.
    """

    def test_every_referenced_name_is_exported_by_the_active_backend(self) -> None:
        backend = _BACKEND_FOR_PLATFORM.get(sys.platform, "linux")
        required = _referenced_platform_names() - _SUBMODULE_NAMES - _BASE_REEXPORTS
        exported = _backend_exports("platforms.%s" % backend)
        missing = sorted(required - exported)
        self.assertEqual(
            [],
            missing,
            "platforms.%s must export these names (referenced elsewhere in the repo): %s"
            % (backend, missing),
        )

    def test_key_constants_are_exported(self) -> None:
        """The specific production-only failure mode: these are read inside handlers."""
        backend = _BACKEND_FOR_PLATFORM.get(sys.platform, "linux")
        exported = _backend_exports("platforms.%s" % backend)
        for name in ("KEY_DOWN", "KEY_UP"):
            self.assertIn(name, exported, "%s must be exported by platforms.%s" % (name, backend))


class TestTokenSubstitutionIsSinglePass(unittest.TestCase):
    """Substitution is one pass over the template only.

    The pre-refactor code chained ``page.replace(...)`` calls, which re-scanned values they
    had already inserted. A prompt or status message containing a literal token name was
    therefore silently corrupted. These are property assertions rather than golden hashes,
    because the *old* output for these inputs was the corrupt one - there is no worthwhile
    byte-identity to preserve.
    """

    def test_prompt_containing_a_token_name_survives(self) -> None:
        literal = "__MODEL_DEFAULTS_JSON__"
        page = _render_page(
            {
                "env": {"LLM_PROVIDER": "none"},
                "system_prompt": "keep %s literal" % literal,
                "present": ("SYSTEM_PROMPT",),
            }
        )
        self.assertIn("keep %s literal" % literal, page)

    def test_status_message_containing_a_token_name_survives(self) -> None:
        literal = "__SYSTEM_PROMPT__"
        page = _render_page(
            {
                "env": {"LLM_PROVIDER": "none"},
                "message": "failed on %s" % literal,
                "message_kind": "err",
            }
        )
        self.assertIn("failed on %s" % literal, page)

    def test_template_file_is_shipped_in_assets(self) -> None:
        """setup_web loads the asset from the install root, never the CWD."""
        path = os.path.join(REPO_ROOT, "assets", "setup_template.html")
        self.assertTrue(os.path.isfile(path), "setup_template.html must be shipped in assets/")


class TestDocumentationConsistency(unittest.TestCase):
    """The generated parts of docs/architecture.md must match the source."""

    def test_module_graph_is_not_stale(self) -> None:
        import subprocess

        tool = os.path.join(REPO_ROOT, "tools", "module_graph.py")
        result = subprocess.run(
            [sys.executable, tool, "--check"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            0,
            result.returncode,
            "module graph is stale - run: python tools/module_graph.py --write\n%s%s"
            % (result.stdout, result.stderr),
        )


class TestSendTextCap(unittest.TestCase):
    """Regression guard for the 256-character cap that a de-dup could silently drop."""

    def test_over_cap_text_is_rejected_without_typing(self) -> None:
        import platforms

        # Returns False *before* any key injection, so this cannot type into the desktop.
        self.assertFalse(platforms.send_text("x" * 257))

    def test_empty_text_is_a_no_op(self) -> None:
        import platforms

        self.assertTrue(platforms.send_text(""))


if __name__ == "__main__":
    unittest.main()
