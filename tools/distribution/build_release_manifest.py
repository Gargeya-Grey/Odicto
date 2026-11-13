"""Build odicto-release.json, the file the installer and the updater read before the GitHub API.

It holds the fields of the API's latest-release answer that the code uses: the tag, the draft and
pre-release flags, and the two assets. The Release workflow publishes it for a stable release only.
Usage:
    python tools/distribution/build_release_manifest.py v1.5.1 out/odicto-release.json
"""
import importlib.util
import json
from pathlib import Path
import sys


def _manager():
    """manager.py by path: the tag rule and the repository stay in one place."""
    spec = importlib.util.spec_from_file_location("odicto_release_manager", Path(__file__).with_name("manager.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build(tag: str) -> dict:
    """The manifest for tag. Raises DistributionError for a tag that is not vMAJOR.MINOR.PATCH."""
    manager = _manager()
    manager.version_tuple(tag)
    name = f"Odicto-{tag}.zip"
    prefix = f"https://github.com/{manager.REPOSITORY}/releases/download/{tag}/"
    manifest = {"tag_name": tag, "draft": False, "prerelease": False,
                "assets": [{"name": asset, "browser_download_url": prefix + asset}
                           for asset in (name, name + ".sha256")]}
    manager.Release.from_metadata(manifest)  # the reader must accept what this writes
    return manifest


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        manifest = build(argv[0])
    except RuntimeError as error:  # DistributionError
        print(f"No manifest was written: {error}", file=sys.stderr)
        return 1
    Path(argv[1]).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
