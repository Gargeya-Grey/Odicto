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
# Select menus render as a fixed layer on <body> so cards and the save bar cannot clip or cover them.
# Personal vocabulary moved outside Gemini; Polish now has its own page.
# Selection-only polish controls and separate model suggestions.
# Styled searchable Polish picker with provider and custom model state.
# Polish label, model summary and help text now have explicit vertical spacing.
# Polish shortcut now uses the shared key recorder and keycaps.
# Selection polish explicitly reviews/copies instead of injecting into unverified fields.
# 2026-11-04: the setup page gained an Appearance view (nav item, stylesheet, view, preview script)
# for the HUD_WAVEFORM, HUD_TEXTURE, HUD_EDGE and HUD_SHAPE keys. Before rebasing, the previous
# template reproduced every old golden, and each new page differed from its old page by
# inserted lines only (no line removed or changed), the same 465 lines in every state.
# Review fixes, same day: Plain keeps the grain, a still preview follows a new pixel ratio,
# and two comments. Every state changed by the same 4 lines and nothing else.
# 2026-11-04, HUD polish (Plain texture): the Plain subtitle reads "no veins, faint grain" and the preview draws
# the Plain grain at 0.4 (LEAF_GRAIN_PLAIN). Before rebasing, the previous template reproduced every old golden,
# and every state changed by the same lines and nothing else (compare output kept in the hand-back).
# 2026-11-04, HUD polish (HUD_SIZE): the Appearance view gained a three-way Size control and the preview
# follows it (a larger preview canvas holds the 1.5x leaf and its shadow). Before rebasing, the previous template
# reproduced every old golden, and every state changed by the same lines and nothing else (compare output kept in
# the hand-back).
# 2026-11-04, setup page in the leaf language: the whole page was restyled (tokens, ground, rail, panels, controls, status marks)
# and the Home hero draws the leaf with the preview's painter. Before rebasing, the previous template reproduced every old
# golden. Every state changed by the same lines and nothing else (compare output kept in the hand-back). A behaviour proof
# (IDs, form fields and values, data and aria attributes, button labels, visible text) found no difference except the
# hero canvas and the nav item titles; the script text changed only by the hero painter.
# 2026-11-04, THEME_COLOR: the page follows the theme colour. The server seeds --h and --k, the Appearance view gained a
# Colour panel (swatches and a custom colour), and the preview paints with the theme's palette (the THEME maths block).
# Before rebasing, the previous template reproduced every old golden. Every state changed by the same lines and nothing
# else (compare output kept in the hand-back).
# Review fix, same day: a grey custom colour (chroma under 0.02) takes Graphite's hue, so it gets no random tint.
# Every state changed by the same 2 lines of the THEME maths block and nothing else.
# 2026-11-04, 1.4.1: the rail shows the original Odicto mark again, in its own green (#8edcbb), not the leaf.
# Every state changed by the same logo lines (brand SVG, its size, the logo tokens) and nothing else.
# 2026-11-04, 1.4.3: the Polish row says the polished text replaces the selection (Ctrl+Z undoes it), not
# that a review window opens. With only that sentence reverted, every state reproduced its old golden.
_GOLDEN_PAGE_HASHES = {'fresh_empty_env': '5c478e756460aca1b9246ba929758751b31e69efdc078d3adbb1bf21d5bc959f',
 'provider_meta': 'feed29495d2a48b95b7cafbcf51b42c8b868145e2fee2069f6e80669dcc604f3',
 'provider_openrouter': 'f1d583a4087eb640a4a9567aad12d6e6d4aac2d6bbe5265a6821675f25818aa8',
 'provider_gemini': 'c549cde218f6c631c03bcec5109e7b9c46610579badf27a8390b1f5735a283f0',
 'provider_ollama': '0f043ed55f715826283da0dbf915bbbfaa54c03c8d435a7aab9e1d04250aea3d',
 'provider_none': '5c478e756460aca1b9246ba929758751b31e69efdc078d3adbb1bf21d5bc959f',
 'provider_groq': '7a615b25cb23dba8b0a399a2b614d0f87631c4ecaa80af2bd878f74bb000a8ae',
 'polish_enabled': 'fbe0cb5d57c29b09e6379571ecf72cc9a210961d968406d7f032b59aecbbd852',
 'history_and_custom': '94476e681e2d966979929e7f9e64cd6407b8c7dab8ac9ad83808d7de11f5569e',
 'hotkey_short': '39194e5d87b80271a0b138c312ca690c1415000bf328dfe9692bf5c86d50284b',
 'stt_gemini': 'bafe6d68a2736b251594d76024783d7c55141cec761393a10903a244a3fb9f51',
 'prompt_from_file': '6d16db28ebdcc85fde6aee008af675ae646a721a1253dd8ae058d6e12abd2285',
 'prompt_from_inline_env': 'ab91d71a405b128451f4674ff445aeeabc80f354e515c4205c8d87d905ea2fa2',
 'msg_ok': '45e7184de8461183bee9627bad93ecc9e3e8fdbefc9f0f1c3d899fbdbb930402',
 'msg_err': '36003b1c842adafd3de41b234d9b939c1fdb0ff47ed51773dfdf410977600d5d',
 'msg_neutral': '3d3c3a75d268d4a2fdc67ac8447fee287922f834812f8d7076d175aedb06842b',
 'admin_on': 'd1af8eda2f89dd855ed049d391cbb9e13419426f7c5685c9940e10e95239b25f',
 'admin_unavailable': '1aa7bcdf2622225763a7ca381b776cf5b67ba824dbe6d3d497c0b025b9364108'}

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
    with tempfile.TemporaryDirectory(prefix="odicto-equiv-") as tmp:
        return _render_page_in(state, tmp)


def _render_page_in(state: dict, tmp: str) -> str:
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
