"""Require successful fresh-install platform jobs for the exact release commit."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
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


def api(path: str, token: str):
    request = urllib.request.Request("https://api.github.com/" + path, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Odicto-release-gate",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def check(repository: str, sha: str, token: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Release SHA must be a full commit SHA")
    prefix = f"repos/{repository}/actions"
    workflow = api(prefix + "/workflows/ci.yml", token)
    query = urllib.parse.urlencode({"head_sha": sha, "branch": "main", "event": "push", "per_page": 100})
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
    validate_run(run, jobs, sha, workflow["id"])
    return {"head_sha": sha, "run_id": run["id"], "run_url": run["html_url"],
            "required_jobs": sorted(REQUIRED_JOBS), "result": "pass"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        parser.error("GH_TOKEN or GITHUB_TOKEN is required")
    try:
        result = check(args.repository, args.sha, token)
    except Exception as error:
        print(f"Release blocked: {type(error).__name__}: {error}")
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Release platform gate passed for " + args.sha)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
