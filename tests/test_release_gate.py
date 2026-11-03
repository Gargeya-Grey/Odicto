"""A release cannot borrow platform evidence from another commit or skipped job."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


path = Path(__file__).resolve().parents[1] / "tools" / "release" / "check_platform_gate.py"
spec = importlib.util.spec_from_file_location("release_platform_gate", path)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
smoke_path = path.parents[1] / "ci_platform_smoke.py"
smoke_spec = importlib.util.spec_from_file_location("ci_platform_smoke", smoke_path)
smoke = importlib.util.module_from_spec(smoke_spec)
smoke_spec.loader.exec_module(smoke)


class TestReleasePlatformGate(unittest.TestCase):
    def fixture(self):
        sha = "a" * 40
        run = dict(id=10, head_sha=sha, head_branch="main", event="push", workflow_id=4,
                   status="completed", conclusion="success", html_url="https://example.invalid/run/10")
        jobs = [dict(name=name, status="completed", conclusion="success") for name in gate.REQUIRED_JOBS]
        return sha, run, jobs

    def test_exact_commit_and_all_platforms_pass(self):
        sha, run, jobs = self.fixture()
        gate.validate_run(run, jobs, sha, 4)

    def test_wrong_commit_branch_event_workflow_or_state_reject(self):
        sha, run, jobs = self.fixture()
        for key, value in [("head_sha", "b" * 40), ("head_branch", "feature/test"),
                           ("event", "pull_request"), ("workflow_id", 5),
                           ("status", "in_progress"), ("conclusion", "failure")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                gate.validate_run(dict(run, **{key: value}), jobs, sha, 4)

    def test_missing_failed_skipped_or_pending_platform_reject(self):
        sha, run, jobs = self.fixture()
        with self.assertRaises(ValueError):
            gate.validate_run(run, jobs[1:], sha, 4)
        for conclusion in ("failure", "skipped", "cancelled", None):
            with self.subTest(conclusion=conclusion), self.assertRaises(ValueError):
                gate.validate_run(run, [dict(jobs[0], conclusion=conclusion)] + jobs[1:], sha, 4)

    def test_newer_failed_run_cannot_borrow_old_success(self):
        sha, run, jobs = self.fixture()
        failed = dict(run, id=11, conclusion="failure")
        def api(path, token):
            if path.endswith("workflows/ci.yml"):
                return {"id": 4}
            if "workflows/ci.yml/runs?" in path:
                return {"workflow_runs": [run, failed]}
            return {"jobs": jobs, "total_count": len(jobs)}
        with patch.object(gate, "api", side_effect=api), self.assertRaises(ValueError):
            gate.check("owner/repo", sha, "synthetic-token")

    def test_missing_ci_run_reject(self):
        sha, _, _ = self.fixture()
        with patch.object(gate, "api", side_effect=[{"id": 4}, {"workflow_runs": []}]), self.assertRaises(ValueError):
            gate.check("owner/repo", sha, "synthetic-token")

    def test_platform_matrix_matches_gate(self):
        source = (path.parents[2] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for name in gate.REQUIRED_JOBS:
            self.assertIn(name.removeprefix("platform-"), source)

    def test_fresh_installer_never_runs_on_an_ordinary_machine(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}), patch.object(smoke, "run") as run:
            with self.assertRaises(RuntimeError):
                smoke.fresh_install(Path("source"), Path("target"), Path("evidence"), "a" * 40)
        run.assert_not_called()

    def test_existing_install_directory_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "existing"
            root.mkdir()
            sentinel = root / "owner.txt"
            sentinel.write_text("retain", encoding="utf-8")
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_TEMP": temp}), patch.object(smoke, "run") as run:
                with self.assertRaises(RuntimeError):
                    smoke.fresh_install(Path("source"), root, Path(temp), "a" * 40)
            run.assert_not_called()
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "retain")

    def test_smoke_requires_its_disposable_fixture_marker(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with self.assertRaises(RuntimeError):
                smoke.installed(Path(temp), Path(temp), "a" * 40)

    def test_requested_mac_os_and_architecture_are_enforced(self):
        report = dict(system="Darwin", macos_version="27.0.1", machine="arm64")
        smoke.validate_platform("xcode-27", report)
        with self.assertRaises(RuntimeError):
            smoke.validate_platform("macos-26", report)
        with self.assertRaises(RuntimeError):
            smoke.validate_platform("macos-26-intel", dict(report, macos_version="26.6.2"))

    def test_latest_ubuntu_vm_cannot_be_substituted_by_host_distro(self):
        report = dict(system="Linux", os_release={"ID": "ubuntu", "VERSION_ID": "26.10"})
        smoke.validate_platform("ubuntu-26.10-vm", report)
        with self.assertRaises(RuntimeError):
            smoke.validate_platform("ubuntu-26.10-vm", dict(report, os_release={"ID": "ubuntu", "VERSION_ID": "26.04"}))


if __name__ == "__main__":
    unittest.main()
