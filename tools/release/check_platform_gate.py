"""Require successful fresh-install platform jobs for the exact release commit."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request


REQUIRED_JOBS = frozenset({
    "platform-windows-2025", "platform-macos-26", "platform-macos-26-intel",
    "platform-xcode-27", "platform-ubuntu-26.04", "platform-ubuntu-26.10-vm",
})


def validate_run(run: dict, jobs: list[dict], sha: str, workflow_id: int) -> None:
    expected = {"head_sha": sha, "head_branch": "main", "event": "push",
                "workflow_id": workflow_id, "status": "completed", "conclusion": "success"}
    for key, value in expected.items():
        if run.get(key) != value:
            raise ValueError(f"CI gate mismatch for {key}: expected {value!r}")
    by_name = {job.get("name"): job for job in jobs}
    for name in sorted(REQUIRED_JOBS):
        job = by_name.get(name)
        if not job or job.get("status") != "completed" or job.get("conclusion") != "success":
            raise ValueError(f"Required platform job has not passed: {name}")


def _load_retry_rules():
    """The CI runner retry rules (tools/ci_rerun_unacquired.py), shared so the gate waits
    exactly when the retry workflow will re-run a job that GitHub gave no runner."""
    source = Path(__file__).resolve().parents[1] / "ci_rerun_unacquired.py"
    spec = importlib.util.spec_from_file_location("ci_rerun_unacquired", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


retry_rules = _load_retry_rules()


def pending_reason(run: dict, jobs: list[dict], annotations) -> str | None:
    """Why the gate should wait instead of judging now, or None. It waits only while CI is
    running, or while the only non-passing jobs are ones GitHub gave no runner and the
    retry workflow still has attempts left. A real failure never waits."""
    if run.get("status") != "completed":
        return f"CI run {run.get('id')} is still {run.get('status')}"
    if run.get("conclusion") == "success":
        return None
    unacquired = {job["id"] for job in jobs if retry_rules.has_unacquired_shape(job)
                  and retry_rules.is_unacquired_annotation(annotations(job["id"]))}
    plan = retry_rules.decide(run, jobs, unacquired, retry_rules.MAX_ATTEMPTS)
    if plan.action == "rerun":
        return f"CI run {run.get('id')}: {plan.reason}; waiting for the CI runner retry"
    return None


def api(path: str, token: str):
    request = urllib.request.Request("https://api.github.com/" + path, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Odicto-release-gate",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def check(repository: str, sha: str, token: str, wait_seconds: float = 0, poll_seconds: float = 60,
          sleep=time.sleep, clock=time.monotonic, log=print) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Release SHA must be a full commit SHA")
    prefix = f"repos/{repository}/actions"
    workflow = api(prefix + "/workflows/ci.yml", token)
    query = urllib.parse.urlencode({"head_sha": sha, "branch": "main", "event": "push", "per_page": 100})
    deadline = clock() + wait_seconds
    while True:
        runs = api(prefix + "/workflows/ci.yml/runs?" + query, token)["workflow_runs"]
        if not runs:
            raise ValueError("No CI run exists for this release SHA")
        # A failed rerun must not fall back to an older green run for the same commit.
        run = max(runs, key=lambda item: item["id"])
        jobs = []
        for page in range(1, 11):
            payload = api(prefix + f"/runs/{run['id']}/jobs?filter=latest&per_page=100&page={page}", token)
            jobs.extend(payload["jobs"])
            if len(jobs) >= payload["total_count"]:
                break
        else:
            raise ValueError("CI job inventory exceeds the release gate bound")
        reason = pending_reason(run, jobs, lambda job_id: api(
            f"repos/{repository}/check-runs/{job_id}/annotations?per_page=100", token))
        if reason is None or clock() >= deadline:
            break
        log("Release gate waits: " + reason)
        sleep(min(poll_seconds, max(1.0, deadline - clock())))
    validate_run(run, jobs, sha, workflow["id"])
    return {"head_sha": sha, "run_id": run["id"], "run_url": run["html_url"],
            "required_jobs": sorted(REQUIRED_JOBS), "result": "pass"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wait-minutes", type=float, default=0,
                        help="wait this long while CI runs or waits for a runner retry")
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        parser.error("GH_TOKEN or GITHUB_TOKEN is required")
    try:
        result = check(args.repository, args.sha, token, wait_seconds=args.wait_minutes * 60)
    except Exception as error:
        print(f"Release blocked: {type(error).__name__}: {error}")
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Release platform gate passed for " + args.sha)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
