"""Re-run CI jobs that GitHub never gave a runner, until they run or the attempt limit is reached.

GitHub sometimes cancels a hosted job after about 15 minutes because no runner took it
(seen on the macOS arm64 pool, 4 Nov 2026). Such a job has no runner, no steps, and the
annotation "The job was not acquired by Runner of type hosted even after multiple attempts".
It never ran, so it says nothing about the code.

Rules:
- Only runs of `.github/workflows/ci.yml` are touched (never Release or any other workflow).
- A job counts as unacquired only when all three hold: conclusion `cancelled`, no runner and
  no steps, and GitHub's "not acquired by Runner" annotation.
- Only `success` counts as passed. If any other job did not pass (a real failure, a timeout,
  a cancel by a person, a skipped or neutral job), nothing is re-run.
- At most `--max-attempts` attempts in total (the first run counts as attempt 1).
- The job list must be complete; a partial list refuses the re-run.

The script waits inside one job for each re-run to finish, so it does not depend on a
re-run's completion starting this workflow again. Every request stays inside the deadline.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import NamedTuple
import urllib.error
import urllib.request


CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
UNACQUIRED_MESSAGE = re.compile(r"not acquired by runner", re.IGNORECASE)
RUN_ID = re.compile(r"[1-9][0-9]{0,19}")
MAX_PAGES = 10
MAX_ATTEMPTS = 5  # the first run counts as attempt 1
START_GRACE_SECONDS = 300  # time GitHub gets to start the attempt a POST asked for


class Plan(NamedTuple):
    action: str  # "wait" | "none" | "rerun" | "give_up"
    reason: str
    jobs: tuple = ()


class DeadlineReached(Exception):
    pass


def has_unacquired_shape(job: dict) -> bool:
    """A cancelled job that no runner ever took: no runner name and no steps."""
    return (job.get("conclusion") == "cancelled" and not job.get("runner_name")
            and not job.get("steps"))


def is_unacquired_annotation(annotations: list[dict]) -> bool:
    return any(UNACQUIRED_MESSAGE.search(item.get("message") or "") for item in annotations)


def decide(run: dict, jobs: list[dict], unacquired_ids: set, max_attempts: int) -> Plan:
    """Pure decision for one CI run. `unacquired_ids` holds the job ids whose annotations
    carry GitHub's "not acquired by Runner" message."""
    if run.get("path") != CI_WORKFLOW_PATH:
        return Plan("none", f"run belongs to {run.get('path')!r}, not {CI_WORKFLOW_PATH}")
    if run.get("status") != "completed":
        return Plan("wait", "run is still in progress")
    if run.get("conclusion") == "success":
        return Plan("none", "run passed")
    not_passed = [job for job in jobs if job.get("conclusion") != "success"]
    if not not_passed:
        return Plan("none", f"run concluded {run.get('conclusion')!r} but every job passed")
    unacquired = [job for job in not_passed
                  if has_unacquired_shape(job) and job.get("id") in unacquired_ids]
    real = [job for job in not_passed if job not in unacquired]
    if real:
        names = ", ".join(sorted(f"{job.get('name')} ({job.get('conclusion')})" for job in real))
        return Plan("none", "a job did not pass for a reason other than a missing runner, "
                    "so nothing is re-run: " + names)
    names = tuple(sorted(job.get("name") for job in unacquired))
    attempt = int(run.get("run_attempt") or 1)
    if attempt >= max_attempts:
        return Plan("give_up", f"attempt {attempt} of {max_attempts} used; GitHub gave no runner "
                    "to " + ", ".join(names), names)
    return Plan("rerun", f"GitHub gave no runner on attempt {attempt}; re-running "
                + ", ".join(names), names)


class GitHub:
    def __init__(self, repository: str, token: str, deadline: float, clock=time.monotonic):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid repository")
        self.repository, self.token, self.deadline, self.clock = repository, token, deadline, clock

    def _remaining(self) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 1:
            raise DeadlineReached("deadline reached during a GitHub request")
        return remaining

    def request(self, method: str, path: str):
        """GET retries transient errors. POST is sent once: its caller reconciles a lost reply."""
        url = "https://api.github.com/" + path
        tries = 3 if method == "GET" else 1
        for attempt in range(tries):
            request = urllib.request.Request(url, method=method, headers={
                "Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Odicto-ci-runner-retry",
            })
            try:
                with urllib.request.urlopen(request, timeout=min(30, self._remaining())) as response:
                    body = response.read()
                    return json.loads(body) if body else None
            except urllib.error.HTTPError as error:
                if error.code < 500 or attempt == tries - 1:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == tries - 1:
                    raise
            time.sleep(min(5 * (attempt + 1), self._remaining()))
        raise AssertionError("unreachable")

    def run(self, run_id: int) -> dict:
        return self.request("GET", f"repos/{self.repository}/actions/runs/{run_id}")

    def jobs(self, run_id: int) -> list[dict]:
        jobs = []
        for page in range(1, MAX_PAGES + 1):
            payload = self.request("GET", f"repos/{self.repository}/actions/runs/{run_id}/jobs"
                                   f"?filter=latest&per_page=100&page={page}")
            jobs.extend(payload["jobs"])
            if len(jobs) >= payload["total_count"] or not payload["jobs"]:
                break
        if len(jobs) != payload["total_count"]:
            raise ValueError(f"job list incomplete: {len(jobs)} of {payload['total_count']}")
        return jobs

    def annotations(self, job_id: int) -> list[dict]:
        items = []
        for page in range(1, MAX_PAGES + 1):
            batch = self.request("GET", f"repos/{self.repository}/check-runs/{job_id}/annotations"
                                 f"?per_page=100&page={page}")
            items.extend(batch)
            if len(batch) < 100:
                break
        return items

    def rerun_failed_jobs(self, run_id: int) -> None:
        self.request("POST", f"repos/{self.repository}/actions/runs/{run_id}/rerun-failed-jobs")


def plan_for(api, run_id: int, max_attempts: int) -> tuple[dict, list[dict], Plan]:
    run = api.run(run_id)
    if run.get("path") != CI_WORKFLOW_PATH or run.get("status") != "completed":
        return run, [], decide(run, [], set(), max_attempts)
    jobs = api.jobs(run_id)
    unacquired_ids = {job["id"] for job in jobs if has_unacquired_shape(job)
                      and is_unacquired_annotation(api.annotations(job["id"]))}
    return run, jobs, decide(run, jobs, unacquired_ids, max_attempts)


def rerun_accepted(api, run_id: int, attempt: int) -> bool:
    """After a POST without a clear reply: did GitHub start a new attempt anyway?"""
    run = api.run(run_id)
    return int(run.get("run_attempt") or 1) > attempt or run.get("status") != "completed"


def drive(api, run_id: int, max_attempts: int, dry_run: bool, poll_seconds: float,
          deadline: float, log=print, clock=time.monotonic, sleep=time.sleep) -> Plan:
    """Re-run, wait for the new attempt, and decide again, until a final plan."""
    waited_for = None    # the attempt number this loop waits to see completed
    posted_at = 0.0
    rerun_ids = set()    # job ids that the last POST should have replaced
    while True:
        if clock() >= deadline:
            return Plan("give_up", "deadline reached while waiting for a CI attempt")
        run, jobs, plan = plan_for(api, run_id, max_attempts)
        attempt = int(run.get("run_attempt") or 1)
        if waited_for is not None and attempt < waited_for:
            if run.get("status") == "completed" and clock() >= posted_at + START_GRACE_SECONDS:
                return Plan("give_up", "GitHub accepted the re-run but started no new attempt "
                            f"within {START_GRACE_SECONDS // 60} minutes", plan.jobs)
            sleep(min(poll_seconds, max(1.0, deadline - clock())))
            continue
        if plan.action == "wait":
            sleep(min(poll_seconds, max(1.0, deadline - clock())))
            continue
        log(f"run {run_id} attempt {attempt}: {plan.action}: {plan.reason}")
        if plan.action != "rerun" or dry_run:
            return plan
        same = sorted(job["name"] for job in jobs if job["id"] in rerun_ids)
        if same:
            return Plan("give_up", "GitHub did not re-run " + ", ".join(same), plan.jobs)
        # Read again just before the POST: a person may have re-run the run meanwhile.
        fresh_run, fresh_jobs, fresh = plan_for(api, run_id, max_attempts)
        if (fresh.action != "rerun" or int(fresh_run.get("run_attempt") or 1) != attempt
                or sorted(job["id"] for job in fresh_jobs) != sorted(job["id"] for job in jobs)):
            log(f"run {run_id} changed before the re-run; deciding again")
            continue
        try:
            api.rerun_failed_jobs(run_id)
        except Exception:
            if not rerun_accepted(api, run_id, attempt):
                raise
            log("the re-run request had no clear reply, but GitHub started a new attempt")
        rerun_ids = {job["id"] for job in jobs if job["name"] in plan.jobs}
        waited_for, posted_at = attempt + 1, clock()
        sleep(min(poll_seconds, max(1.0, deadline - clock())))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    parser.add_argument("--poll-seconds", type=float, default=30)
    parser.add_argument("--deadline-minutes", type=float, default=90)
    parser.add_argument("--dry-run", action="store_true", help="decide and print; re-run nothing")
    args = parser.parse_args(argv)
    if not RUN_ID.fullmatch(args.run_id):
        parser.error("--run-id must be a plain decimal run id with no leading zero")
    if args.max_attempts < 1:
        parser.error("--max-attempts must be 1 or more")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        parser.error("GH_TOKEN or GITHUB_TOKEN is required")
    run_id = int(args.run_id)
    deadline = time.monotonic() + args.deadline_minutes * 60
    try:
        plan = drive(GitHub(args.repository, token, deadline), run_id, args.max_attempts,
                     args.dry_run, args.poll_seconds, deadline)
    except Exception as error:  # report every failure in the summary, then fail the job
        plan = Plan("give_up", f"{type(error).__name__}: {error}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### CI runner retry, run {run_id}\n\n"
                         f"Result: **{plan.action}**. {plan.reason}\n")
    if plan.action == "give_up":
        print("CI runner retry gave up: " + plan.reason, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
