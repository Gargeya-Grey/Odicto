"""CI re-runs only jobs that GitHub never gave a runner, and never hides a real failure."""
import importlib.util
from pathlib import Path
import unittest


path = Path(__file__).resolve().parents[1] / "tools" / "ci_rerun_unacquired.py"
spec = importlib.util.spec_from_file_location("ci_rerun_unacquired", path)
retry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retry)

NOT_ACQUIRED = [{"message": "The job was not acquired by Runner of type hosted even after multiple attempts"},
                {"message": "Due to capacity constraints, jobs targeting macOS arm64 runners may "
                            "experience longer queue times."}]


def passed(job_id, name):
    return dict(id=job_id, name=name, status="completed", conclusion="success",
                runner_name="GitHub Actions 1", steps=[{"name": "x"}])


def unacquired(job_id, name="platform-macos-26"):
    return dict(id=job_id, name=name, status="completed", conclusion="cancelled",
                runner_name="", steps=[])


def run(**changes):
    base = dict(id=7, path=retry.CI_WORKFLOW_PATH, status="completed", conclusion="failure",
                run_attempt=1)
    base.update(changes)
    return base


class TestDecide(unittest.TestCase):
    def test_unacquired_job_is_rerun(self):
        jobs = [passed(1, "platform-windows-2025"), unacquired(2)]
        plan = retry.decide(run(), jobs, {2}, 5)
        self.assertEqual(("rerun", ("platform-macos-26",)), (plan.action, plan.jobs))

    def test_two_unacquired_jobs_rerun_together(self):
        jobs = [unacquired(2), unacquired(3, "platform-xcode-27")]
        plan = retry.decide(run(), jobs, {2, 3}, 5)
        self.assertEqual(("platform-macos-26", "platform-xcode-27"), plan.jobs)

    def test_real_failure_is_never_rerun(self):
        failed = dict(passed(1, "platform-windows-2025"), conclusion="failure")
        for jobs in ([failed, unacquired(2)], [failed]):
            with self.subTest(jobs=len(jobs)):
                self.assertEqual("none", retry.decide(run(), jobs, {2}, 5).action)

    def test_timed_out_job_with_steps_is_not_unacquired(self):
        timed = dict(passed(1, "platform-macos-26"), conclusion="timed_out")
        self.assertEqual("none", retry.decide(run(), [timed], {1}, 5).action)

    def test_cancel_without_github_annotation_is_a_person_and_not_rerun(self):
        plan = retry.decide(run(conclusion="cancelled"), [unacquired(2)], set(), 5)
        self.assertEqual("none", plan.action)

    def test_cancelled_job_that_started_is_not_unacquired(self):
        started = dict(unacquired(2), runner_name="GitHub Actions 9", steps=[{"name": "x"}])
        self.assertEqual("none", retry.decide(run(), [started], {2}, 5).action)

    def test_attempt_limit_gives_up(self):
        plan = retry.decide(run(run_attempt=5), [unacquired(2)], {2}, 5)
        self.assertEqual("give_up", plan.action)
        self.assertEqual("rerun", retry.decide(run(run_attempt=4), [unacquired(2)], {2}, 5).action)

    def test_other_workflows_are_never_touched(self):
        for workflow in (".github/workflows/release.yml", None):
            with self.subTest(workflow=workflow):
                plan = retry.decide(run(path=workflow), [unacquired(2)], {2}, 5)
                self.assertEqual("none", plan.action)

    def test_passed_or_running_run_needs_nothing(self):
        self.assertEqual("none", retry.decide(run(conclusion="success"), [], set(), 5).action)
        self.assertEqual("wait", retry.decide(run(status="in_progress"), [], set(), 5).action)

    def test_annotation_match(self):
        self.assertTrue(retry.is_unacquired_annotation(NOT_ACQUIRED))
        self.assertFalse(retry.is_unacquired_annotation(NOT_ACQUIRED[1:]))
        self.assertFalse(retry.is_unacquired_annotation([{"message": None}]))


class FakeGitHub:
    """Plays a run whose macOS job is unacquired for `misses` attempts, then passes."""

    def __init__(self, misses, path=retry.CI_WORKFLOW_PATH):
        self.attempt, self.misses, self.path = 1, misses, path
        self.reruns, self.polls_while_running, self.running = 0, 0, False

    def run(self, run_id):
        if self.running:
            self.polls_while_running += 1
            if self.polls_while_running >= 2:
                self.running = False
        status = "in_progress" if self.running else "completed"
        ok = self.attempt > self.misses
        return run(path=self.path, status=status, run_attempt=self.attempt,
                   conclusion=None if self.running else ("success" if ok else "failure"))

    def jobs(self, run_id):
        ok = self.attempt > self.misses
        return [passed(1, "platform-windows-2025"), passed(2, "platform-macos-26") if ok else unacquired(2)]

    def annotations(self, job_id):
        return NOT_ACQUIRED if job_id == 2 else []

    def rerun_failed_jobs(self, run_id):
        self.reruns += 1
        self.attempt += 1
        self.running, self.polls_while_running = True, 0


class TestDrive(unittest.TestCase):
    def drive(self, api, max_attempts=5, dry_run=False, deadline=10_000):
        now = [0.0]
        def sleep(seconds):
            now[0] += seconds
        return retry.drive(api, 7, max_attempts, dry_run, 30, deadline, log=lambda *_: None,
                           clock=lambda: now[0], sleep=sleep)

    def test_reruns_until_a_runner_takes_the_job(self):
        api = FakeGitHub(misses=2)
        plan = self.drive(api)
        self.assertEqual(("none", 2), (plan.action, api.reruns))
        self.assertIn("passed", plan.reason)

    def test_gives_up_at_the_attempt_limit(self):
        api = FakeGitHub(misses=99)
        plan = self.drive(api, max_attempts=3)
        self.assertEqual(("give_up", 2), (plan.action, api.reruns))

    def test_dry_run_changes_nothing(self):
        api = FakeGitHub(misses=1)
        self.assertEqual(("rerun", 0), (self.drive(api, dry_run=True).action, api.reruns))

    def test_deadline_stops_the_wait(self):
        api = FakeGitHub(misses=99)
        api.rerun_failed_jobs = lambda run_id: setattr(api, "running", True) or setattr(
            api, "attempt", api.attempt + 1)
        api.polls_while_running = -10**9  # never finishes
        plan = self.drive(api, deadline=120)
        self.assertEqual("give_up", plan.action)

    def test_release_run_is_never_rerun(self):
        api = FakeGitHub(misses=1, path=".github/workflows/release.yml")
        self.assertEqual(("none", 0), (self.drive(api).action, api.reruns))


if __name__ == "__main__":
    unittest.main()
