"""Generate the module-dependency diagram in docs/architecture.md.

The one diagram in the architecture doc that would otherwise rot is the import graph, so it
is generated from the source and checked for staleness rather than hand-drawn.

  python tools/module_graph.py --write     # refresh the block in docs/architecture.md
  python tools/module_graph.py --check     # exit 1 if the committed block is stale
  python tools/module_graph.py             # print the block

stdlib only; never imports the application, so it cannot have side effects.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(REPO, "docs", "architecture.md")
BEGIN = "<!-- BEGIN GENERATED: module-graph (tools/module_graph.py) -->"
END = "<!-- END GENERATED: module-graph -->"

# Modules we draw. Tests and tooling are deliberately excluded: the diagram is about the
# shipped application, and test_units.py is not an import edge in the running app.
APP_MODULES = [
    "main",
    "odicto",
    "config",
    "app_state",
    "recorder",
    "transcriber",
    "refiner",
    "typer",
    "indicator",
    "openrouter_catalog",
    "setup_web",
    "platforms",
]


def _module_path(name: str) -> str:
    if name == "platforms":
        return os.path.join(REPO, "platforms", "__init__.py")
    return os.path.join(REPO, name + ".py")


def _local_imports(name: str) -> set[str]:
    """Local modules that `name` imports, including inside functions (lazy imports count)."""
    with open(_module_path(name), encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), _module_path(name))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in APP_MODULES:
                    found.add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import
                continue
            root = (node.module or "").split(".")[0]
            if root in APP_MODULES:
                found.add(root)
    found.discard(name)
    return found


def edges() -> list[tuple[str, str]]:
    return [(src, dst) for src in APP_MODULES for dst in sorted(_local_imports(src))]


def render() -> str:
    # Order the nodes by how central they are, so the layout is stable across runs.
    lines = [BEGIN, "```mermaid", "graph LR"]
    for src, dst in edges():
        lines.append("    %s --> %s" % (src, dst))
    lines += ["```", END]
    return "\n".join(lines) + "\n"


def _read_doc() -> str:
    with open(DOC, encoding="utf-8") as handle:
        return handle.read()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="update docs/architecture.md")
    parser.add_argument("--check", action="store_true", help="fail if the block is stale")
    args = parser.parse_args(argv)

    block = render()
    if not args.check and not args.write:
        sys.stdout.write(block)
        return 0

    text = _read_doc()
    if BEGIN not in text or END not in text:
        print("markers not found in %s" % DOC, file=sys.stderr)
        return 1

    start = text.index(BEGIN)
    end = text.index(END) + len(END) + 1
    current = text[start:end]

    if args.check:
        if current.strip() != block.strip():
            print(
                "module graph is stale - run: python tools/module_graph.py --write",
                file=sys.stderr,
            )
            return 1
        print("module graph is up to date")
        return 0

    if args.write:
        with open(DOC, "w", encoding="utf-8", newline="") as handle:
            handle.write(text[:start] + block + text[end:])
        print("updated %s (%d edges)" % (DOC, len(edges())))
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
