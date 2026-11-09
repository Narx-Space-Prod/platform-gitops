#!/usr/bin/env python3
"""Read-only CI evidence bridge for the central release graph; never runs source code."""

import argparse
import io
import zipfile
import json
import os
from pathlib import Path
import re
import time
from delivery import run

REPOS = {
    "Narx-Space-Prod/commerce-" + name for name in ("frontend", "catalogue", "orders")
}


def api(path):
    return json.loads(run("gh", "api", path))


def identity(event):
    source = event.get("client_payload", {})
    repo, commit, run_id = (source.get(k) for k in ("repository", "commit", "runId"))
    if (
        repo not in REPOS
        or not isinstance(commit, str)
        or not re.fullmatch("[a-f0-9]{40}", commit)
        or type(run_id) is not int
        or run_id <= 0
    ):
        raise ValueError("Invalid source CI identity")
    return repo, commit, run_id


def validate(record, repo, commit):
    if not (
        record.get("head_sha") == commit
        and record.get("event") == "push"
        and record.get("head_branch") == "main"
        and record.get("path") == ".github/workflows/ci.yml"
        and record.get("head_repository", {}).get("full_name") == repo
    ):
        raise ValueError("Evidence is not protected-main application CI")


def evidence(gate, record, repo, commit, run_id):
    attempt = record.get("run_attempt")
    if type(attempt) is not int or attempt < 1:
        raise ValueError("Invalid source run attempt")
    pages = json.loads(
        run(
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100",
        )
    )
    name = f"narx-ci-v1-{gate}-{attempt}"
    matches = [
        a
        for page in pages
        for a in page["artifacts"]
        if a["name"] == name and not a["expired"]
    ]
    if not matches:
        if record.get("status") == "completed":
            raise ValueError("Completed CI lacks versioned gate evidence")
        return None
    if len(matches) != 1 or matches[0]["size_in_bytes"] > 65536:
        raise ValueError("Invalid evidence artifact size or uniqueness")
    artifact = matches[0]
    if artifact.get("workflow_run", {}).get("id") != run_id:
        raise ValueError("Evidence artifact belongs to another run")
    raw = run(
        "gh", "api", f"repos/{repo}/actions/artifacts/{artifact['id']}/zip", binary=True
    )
    if len(raw) > 65536:
        raise ValueError("Evidence archive exceeds size limit")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if (
            len(entries) != 1
            or entries[0].filename != "gate.json"
            or entries[0].file_size > 16384
        ):
            raise ValueError("Unexpected evidence archive contents")
        value = json.loads(archive.read(entries[0]))
    expected = {
        "schemaVersion": 1,
        "gate": gate,
        "repository": repo,
        "commit": commit,
        "runId": run_id,
        "runAttempt": attempt,
        "result": "success",
    }
    if set(value) != set(expected) | {"releases"} or any(
        value.get(k) != v or type(value[k]) is not type(v) for k, v in expected.items()
    ):
        raise ValueError("Gate evidence identity or schema mismatch")
    releases = value["releases"]
    if not isinstance(releases, list) or (gate != "publication" and releases):
        raise ValueError("Invalid release evidence")
    allowed = (
        {"orders", "cart"}
        if repo.endswith("/commerce-orders")
        else {repo.rsplit("-", 1)[-1]}
    )
    seen = set()
    for release in releases:
        if not isinstance(release, dict) or set(release) != {
            "component",
            "image",
            "digest",
        }:
            raise ValueError("Invalid release fields")
        component = release["component"]
        if (
            component not in allowed
            or component in seen
            or release["image"] != "ghcr.io/narx-space-prod/commerce-" + component
            or not isinstance(release["digest"], str)
            or not re.fullmatch("sha256:[a-f0-9]{64}", release["digest"])
        ):
            raise ValueError("Invalid published image identity")
        seen.add(component)
    return value


def observe_versioned(gate, event, timeout):
    repo, commit, run_id = identity(event)
    deadline = time.monotonic() + timeout
    missing_completed_since = None
    while time.monotonic() < deadline:
        record = api(f"repos/{repo}/actions/runs/{run_id}")
        validate(record, repo, commit)
        if (
            record.get("status") == "completed"
            and record.get("conclusion") != "success"
        ):
            raise ValueError("Application CI failed or was cancelled")
        try:
            value = evidence(gate, record, repo, commit, run_id)
        except ValueError as error:
            # GitHub may complete the run just before artifact indexing catches up.
            # Only missing evidence gets a bounded grace period, never invalid evidence.
            if str(error) != "Completed CI lacks versioned gate evidence":
                raise
            if missing_completed_since is None:
                missing_completed_since = time.monotonic()
            if time.monotonic() - missing_completed_since >= 60:
                raise
            time.sleep(15)
            continue
        if value is not None and (
            gate != "publication" or record.get("status") == "completed"
        ):
            if gate == "publication":
                with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                    output.write(
                        "deploy=" + str(bool(value["releases"])).lower() + "\n"
                    )
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
                output.write(
                    f"### Verified {gate} (evidence v1)\n\n[Source run {run_id}, attempt {record['run_attempt']}](https://github.com/{repo}/actions/runs/{run_id}) · `{commit}`\n"
                )
            return value
        time.sleep(15)
    raise ValueError("Versioned source gate timed out: " + gate)


def observe(gate, event, timeout=1800):
    version = event.get("client_payload", {}).get("evidenceVersion")
    if type(version) is not int or version != 1:
        raise ValueError("CI evidenceVersion 1 is required")
    return observe_versioned(gate, event, timeout)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("gate", choices=["tests", "quality", "publication"])
    args = parser.parse_args()
    observe(args.gate, json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()))
