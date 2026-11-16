"""A fresh-shell bootstrap makes its newly installed private uv visible to the manager."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


if os.name != "nt":
    class TestFreshBootstrap(unittest.TestCase):
        def test_fresh_uv_is_in_the_manager_environment(self):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                stubs = root / "stubs"
                stubs.mkdir()
                # The bootstrap installs uv in its own cache folder, whatever UV_INSTALL_DIR says outside,
                # and never in the managed home.
                installed = root / "home" / "Library" / "Caches" / "Odicto-bootstrap" / "uv"
                programs = {
                    "uname": "#!/bin/sh\nprintf 'Darwin\\n'\n",
                    "curl": "#!/bin/sh\nfor argument do destination=$argument; done\n"
                            "printf '%s\\n' '#!/bin/sh' 'mkdir -p \"$UV_INSTALL_DIR\"' "
                            "'cp \"$ODICTO_TEST_UV\" \"$UV_INSTALL_DIR/uv\"' "
                            "'chmod +x \"$UV_INSTALL_DIR/uv\"' > \"$destination\"\n",
                }
                for name, source in programs.items():
                    path = stubs / name
                    path.write_text(source)
                    path.chmod(0o700)
                fake_uv = root / "fake-uv"
                fake_uv.write_text('#!/bin/sh\n[ "$(command -v uv)" = "$ODICTO_TEST_EXPECTED_UV" ] || exit 42\n')
                fake_uv.chmod(0o700)
                environment = dict(os.environ, PATH=str(stubs) + ":/usr/bin:/bin", HOME=str(root / "home"),
                                   UV_INSTALL_DIR=str(root / "outside"), ODICTO_TEST_UV=str(fake_uv),
                                   ODICTO_TEST_EXPECTED_UV=str(installed / "uv"))
                bootstrap = Path(__file__).resolve().parents[1] / "tools/distribution/bootstrap.sh"
                result = subprocess.run(["/bin/bash", str(bootstrap)], env=environment,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue((installed / "uv").is_file())
                self.assertFalse((root / "home" / "Library" / "Application Support").exists())
