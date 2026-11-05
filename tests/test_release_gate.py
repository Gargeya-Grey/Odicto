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


class TestSpeechFixtureFetch(unittest.TestCase):
    """A network blip must not fail CI; a changed fixture must."""

    def opener(self, outcomes):
        import contextlib, io
        calls = []
        def open_(url, timeout):
            calls.append(url)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return contextlib.closing(io.BytesIO(outcome))
        return open_, calls

    def test_timeout_is_retried(self):
        import hashlib
        data = b"fixture"
        open_, calls = self.opener([TimeoutError("read timed out"), OSError("reset"), data])
        got = smoke.fetch_fixture("u", hashlib.sha256(data).hexdigest(), sleep=lambda s: None, opener=open_)
        self.assertEqual((data, 3), (got, len(calls)))

    def test_retries_are_bounded(self):
        open_, calls = self.opener([TimeoutError("t")] * 4)
        with self.assertRaises(TimeoutError):
            smoke.fetch_fixture("u", "0" * 64, sleep=lambda s: None, opener=open_)
        self.assertEqual(4, len(calls))

    def test_wrong_checksum_is_not_retried(self):
        open_, calls = self.opener([b"changed", b"changed"])
        with self.assertRaises(RuntimeError):
            smoke.fetch_fixture("u", "0" * 64, sleep=lambda s: None, opener=open_)
        self.assertEqual(1, len(calls))


class TestReleaseGateWaitsForRunnerRetry(unittest.TestCase):
    """Release waits while CI runs or retries a job GitHub gave no runner; never on a real failure."""
    NOT_ACQUIRED = [{"message": "The job was not acquired by Runner of type hosted even after multiple attempts"}]

    def states(self, *attempts):
        sha = "a" * 40
        def job(name, conclusion, ran=True):
            return dict(id=hash((name, conclusion, ran)) & 0xFFFF, name=name, status="completed",
                        conclusion=conclusion, runner_name="GitHub Actions 1" if ran else "",
                        steps=[{"name": "x"}] if ran else [])
        out = []
        for index, kind in enumerate(attempts, start=1):
            run = dict(id=10, head_sha=sha, head_branch="main", event="push", workflow_id=4,
                       path=".github/workflows/ci.yml", status="completed", conclusion="success",
                       run_attempt=index, html_url="https://example.invalid/run/10")
            jobs = [job(name, "success") for name in sorted(gate.REQUIRED_JOBS)]
            if kind == "running":
                run.update(status="in_progress", conclusion=None)
            elif kind == "no-runner":
                run["conclusion"] = "failure"
                jobs[0] = job(jobs[0]["name"], "cancelled", ran=False)
            elif kind == "failed":
                run["conclusion"] = "failure"
                jobs[0] = job(jobs[0]["name"], "failure")
            out.append((run, jobs))
        return sha, out

    def fake_api(self, states):
        position = [0]
        def api(path, token):
            run, jobs = states[min(position[0], len(states) - 1)]
            if path.endswith("workflows/ci.yml"):
                return {"id": 4}
            if "workflows/ci.yml/runs?" in path:
                return {"workflow_runs": [run]}
            if "/annotations" in path:
                return self.NOT_ACQUIRED
            position[0] += 1  # each jobs read moves the story on by one state
            return {"jobs": jobs, "total_count": len(jobs)}
        return api

    def check(self, sha, states, wait_seconds):
        now = [0.0]
        def sleep(seconds):
            now[0] += seconds
        with patch.object(gate, "api", side_effect=self.fake_api(states)):
            return gate.check("owner/repo", sha, "synthetic-token", wait_seconds=wait_seconds,
                              poll_seconds=60, sleep=sleep, clock=lambda: now[0], log=lambda *_: None)

    def test_waits_through_running_and_no_runner_then_passes(self):
        sha, states = self.states("running", "no-runner", "running", "ok")
        self.assertEqual("pass", self.check(sha, states, wait_seconds=6000)["result"])

    def test_real_failure_blocks_without_waiting(self):
        sha, states = self.states("failed", "ok")
        with self.assertRaises(ValueError):
            self.check(sha, states, wait_seconds=6000)

    def test_no_wait_by_default_keeps_the_old_rule(self):
        sha, states = self.states("no-runner", "ok")
        with self.assertRaises(ValueError):
            self.check(sha, states, wait_seconds=0)

    def test_real_failure_while_other_jobs_run_blocks_at_once(self):
        sha, states = self.states("running")
        run, jobs = states[0]
        jobs[1] = dict(jobs[1], conclusion="failure")
        jobs[2] = dict(jobs[2], status="in_progress", conclusion=None)
        slept = []
        with patch.object(gate, "api", side_effect=self.fake_api(states)), self.assertRaises(ValueError):
            gate.check("owner/repo", sha, "synthetic-token", wait_seconds=6000, poll_seconds=60,
                       sleep=slept.append, clock=lambda: 0.0, log=lambda *_: None)
        self.assertEqual([], slept)

    def test_read_error_is_retried_then_passes(self):
        sha, states = self.states("no-runner", "ok")
        api = self.fake_api(states)
        failures = [OSError("502 from GitHub")]
        def flaky(path, token):
            if "/annotations" in path and failures:
                raise failures.pop()
            return api(path, token)
        now = [0.0]
        with patch.object(gate, "api", side_effect=flaky):
            result = gate.check("owner/repo", sha, "synthetic-token", wait_seconds=6000, poll_seconds=60,
                                sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0],
                                log=lambda *_: None)
        self.assertEqual("pass", result["result"])

    def test_read_error_past_the_deadline_fails_closed(self):
        sha, states = self.states("ok")
        def broken(path, token):
            if path.endswith("workflows/ci.yml"):
                return {"id": 4}
            raise OSError("GitHub down")
        with patch.object(gate, "api", side_effect=broken), self.assertRaises(OSError):
            gate.check("owner/repo", sha, "synthetic-token", wait_seconds=0, sleep=lambda s: None,
                       clock=lambda: 0.0, log=lambda *_: None)

    def test_newer_failed_run_after_selection_is_judged_instead(self):
        sha, states = self.states("ok")
        good_run, jobs = states[0]
        newer = dict(good_run, id=11, conclusion="failure")
        bad_jobs = [dict(job, conclusion="failure") for job in jobs]
        runs_reads = [0]
        def api(path, token):
            if path.endswith("workflows/ci.yml"):
                return {"id": 4}
            if "workflows/ci.yml/runs?" in path:
                runs_reads[0] += 1
                return {"workflow_runs": [good_run] if runs_reads[0] == 1 else [good_run, newer]}
            if "/annotations" in path:
                return []
            run_id = int(path.split("/runs/")[1].split("/")[0])
            chosen = jobs if run_id == 10 else bad_jobs
            return {"jobs": chosen, "total_count": len(chosen)}
        now = [0.0]
        with patch.object(gate, "api", side_effect=api), self.assertRaises(ValueError):
            gate.check("owner/repo", sha, "synthetic-token", wait_seconds=6000, poll_seconds=60,
                       sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0],
                       log=lambda *_: None)

    def test_no_run_yet_waits_for_it(self):
        sha, states = self.states("ok")
        api = self.fake_api(states)
        empty = [True]
        def late(path, token):
            if "workflows/ci.yml/runs?" in path and empty:
                empty.pop()
                return {"workflow_runs": []}
            return api(path, token)
        now = [0.0]
        with patch.object(gate, "api", side_effect=late):
            result = gate.check("owner/repo", sha, "synthetic-token", wait_seconds=6000, poll_seconds=60,
                                sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0],
                                log=lambda *_: None)
        self.assertEqual("pass", result["result"])

    def test_wait_is_bounded(self):
        sha, states = self.states(*(["running"] * 500))
        with self.assertRaises(ValueError):
            self.check(sha, states, wait_seconds=600)


class TestSmokeFailureIsVisibleInTheLog(unittest.TestCase):
    def test_failed_command_prints_its_last_lines(self):
        import contextlib, io, sys
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "install-first.txt"
            command = [sys.executable, "-c", "print(chr(10).join(f'line {i}' for i in range(100))); raise SystemExit(2)"]
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(RuntimeError):
                smoke.run(command, cwd=Path(temp), output=output)
        shown = stderr.getvalue()
        self.assertIn("line 99", shown)
        self.assertIn("line 40", shown)
        self.assertNotIn("line 39\n", shown)


if __name__ == "__main__":
    unittest.main()
