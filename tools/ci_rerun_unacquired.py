"""Re-run CI jobs that GitHub never gave a runner, until they run or the attempt limit is reached.

GitHub sometimes cancels a hosted job after about 15 minutes because no runner took it
(seen on the macOS arm64 pool, 4 Nov 2026). Such a job has no runner, no steps, and the
annotation "The job was not acquired by Runner of type hosted even after multiple attempts".
It never ran, so it says nothing about the code.

Rules:
- Only runs of `.github/workflows/ci.yml` are touched (never Release or any other workflow).
- A job counts as unacquired only when all three hold: conclusion `cancelled`, no runner and
  no steps, and GitHub's "not acquired by Runner" annotation.
- If any job that did not pass is not unacquired (a real failure, a timeout, a cancel by a
  person), nothing is re-run: a real failure must stay visible.
- At most `--max-attempts` attempts in total (the first run counts as attempt 1).

The script waits inside one job for each re-run to finish, so it does not depend on a
re-run's completion starting this workflow again.
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
PASSED = frozenset({"success", "skipped", "neutral"})


class Plan(NamedTuple):
    action: str  # "wait" | "none" | "rerun" | "give_up"
    reason: str
    jobs: tuple = ()


def has_unacquired_shape(job: dict) -> bool:
    """A cancelled job that no runner ever took: no runner name and no steps."""
    return (job.get("conclusion") == "cancelled" and not job.get("runner_name")
            and not job.get("steps"))


def is_unacquired_annotation(annotations: list[dict]) -> bool:
    return any(UNACQUIRED_MESSAGE.search(item.get("message") or "") for item in annotations)


def decide(run: dict, jobs: list[dict], unacquired_ids: set, max_attempts: int) -> Plan:
    """Pure decision for one completed CI run. `unacquired_ids` holds the job ids whose
    annotations carry GitHub's "not acquired by Runner" message."""
    if run.get("path") != CI_WORKFLOW_PATH:
        return Plan("none", f"run belongs to {run.get('path')!r}, not {CI_WORKFLOW_PATH}")
    if run.get("status") != "completed":
        return Plan("wait", "run is still in progress")
    if run.get("conclusion") == "success":
        return Plan("none", "run passed")
    not_passed = [job for job in jobs if job.get("conclusion") not in PASSED]
    if not not_passed:
        return Plan("none", f"run concluded {run.get('conclusion')!r} but no job failed")
    unacquired = [job for job in not_passed
                  if has_unacquired_shape(job) and job.get("id") in unacquired_ids]
    real = [job for job in not_passed if job not in unacquired]
    if real:
        names = ", ".join(sorted(f"{job.get('name')} ({job.get('conclusion')})" for job in real))
        return Plan("none", "a job really failed or was stopped, so nothing is re-run: " + names)
    names = tuple(sorted(job.get("name") for job in unacquired))
    attempt = int(run.get("run_attempt") or 1)
    if attempt >= max_attempts:
        return Plan("give_up", f"attempt {attempt} of {max_attempts} used; GitHub gave no runner "
                    "to " + ", ".join(names), names)
    return Plan("rerun", f"GitHub gave no runner on attempt {attempt}; re-running "
                + ", ".join(names), names)


class GitHub:
    def __init__(self, repository: str, token: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid repository")
        self.repository = repository
        self.token = token

    def request(self, method: str, path: str):
        url = "https://api.github.com/" + path
        for attempt in range(4):
            request = urllib.request.Request(url, method=method, headers={
                "Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Odicto-ci-runner-retry",
            })
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = response.read()
                    return json.loads(body) if body else None
            except urllib.error.HTTPError as error:
                if error.code < 500 or attempt == 3:
                    raise
            except urllib.error.URLError:
                if attempt == 3:
                    raise
            time.sleep(5 * (attempt + 1))
        raise AssertionError("unreachable")

    def run(self, run_id: int) -> dict:
        return self.request("GET", f"repos/{self.repository}/actions/runs/{run_id}")

    def jobs(self, run_id: int) -> list[dict]:
        payload = self.request(
            "GET", f"repos/{self.repository}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
        return payload["jobs"]

    def annotations(self, job_id: int) -> list[dict]:
        return self.request(
            "GET", f"repos/{self.repository}/check-runs/{job_id}/annotations?per_page=100")

    def rerun_failed_jobs(self, run_id: int) -> None:
        self.request("POST", f"repos/{self.repository}/actions/runs/{run_id}/rerun-failed-jobs")


def plan_for(api, run_id: int, max_attempts: int) -> tuple[dict, Plan]:
    run = api.run(run_id)
    if run.get("path") != CI_WORKFLOW_PATH or run.get("status") != "completed":
        return run, decide(run, [], set(), max_attempts)
    jobs = api.jobs(run_id)
    unacquired_ids = {job["id"] for job in jobs if has_unacquired_shape(job)
                      and is_unacquired_annotation(api.annotations(job["id"]))}
    return run, decide(run, jobs, unacquired_ids, max_attempts)


def drive(api, run_id: int, max_attempts: int, dry_run: bool, poll_seconds: float,
          deadline_seconds: float, log=print, clock=time.monotonic, sleep=time.sleep) -> Plan:
    """Re-run, wait for the new attempt, and decide again, until a final plan."""
    deadline = clock() + deadline_seconds
    waited_for = None  # the attempt number this loop is waiting to see completed
    while True:
        run, plan = plan_for(api, run_id, max_attempts)
        attempt = int(run.get("run_attempt") or 1)
        if plan.action == "wait" or (waited_for is not None and attempt < waited_for):
            if clock() >= deadline:
                return Plan("give_up", f"deadline reached while attempt {attempt} was running")
            sleep(poll_seconds)
            continue
        log(f"run {run_id} attempt {attempt}: {plan.action}: {plan.reason}")
        if plan.action != "rerun" or dry_run:
            return plan
        if clock() >= deadline:
            return Plan("give_up", "deadline reached before the next re-run", plan.jobs)
        api.rerun_failed_jobs(run_id)
        waited_for = attempt + 1
        sleep(poll_seconds)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--poll-seconds", type=float, default=30)
    parser.add_argument("--deadline-minutes", type=float, default=100)
    parser.add_argument("--dry-run", action="store_true", help="decide and print; re-run nothing")
    args = parser.parse_args(argv)
    if args.max_attempts < 1:
        parser.error("--max-attempts must be 1 or more")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        parser.error("GH_TOKEN or GITHUB_TOKEN is required")
    plan = drive(GitHub(args.repository, token), args.run_id, args.max_attempts, args.dry_run,
                 args.poll_seconds, args.deadline_minutes * 60)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### CI runner retry, run {args.run_id}\n\n"
                         f"Result: **{plan.action}**. {plan.reason}\n")
    if plan.action == "give_up":
        print("CI runner retry gave up: " + plan.reason, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
