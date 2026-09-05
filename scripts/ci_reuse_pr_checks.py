#!/usr/bin/env python3
"""Fail-closed routing for reusing an exact PR validation on a main push.

A successful pull-request workflow records the Git tree it tested as an
artifact name.  The subsequent main-branch workflow may skip the expensive
test matrix only when GitHub associates the pushed commit with that PR and a
successful run of this same workflow contains an unexpired attestation for the
exact tree now being deployed.  Direct pushes, stale-base merges, missing
artifacts, and API failures all fall back to running the full test matrix.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ATTESTATION_PREFIX = "ci-validated-tree"
GITHUB_API_ROOT = "https://api.github.com"
ApiGet = Callable[[str, Optional[Mapping[str, str]]], Any]


def attestation_name(tree_sha: str, head_sha: str, run_attempt: int) -> str:
    """Return the artifact name that binds a tested tree to a PR head."""

    return (
        f"{ATTESTATION_PREFIX}-{tree_sha.lower()}-{head_sha.lower()}-{run_attempt}"
    )


class GitHubApi:
    """Small read-only GitHub API client used on the routing runner."""

    def __init__(self, token: str):
        self._token = token

    def get(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        request = Request(
            f"{GITHUB_API_ROOT}{path}{query}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "mlai-backend-ci-validation-router",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed API root
            return json.load(response)


def _matching_pull_requests(
    pulls: Any,
    *,
    commit_sha: str,
    branch: str,
) -> list[dict[str, Any]]:
    if not isinstance(pulls, list):
        return []
    return [
        pull
        for pull in pulls
        if isinstance(pull, dict)
        and pull.get("merged_at")
        and str(pull.get("merge_commit_sha", "")).lower() == commit_sha.lower()
        and isinstance(pull.get("base"), dict)
        and pull["base"].get("ref") == branch
        and isinstance(pull.get("head"), dict)
        and pull["head"].get("sha")
        and isinstance(pull.get("number"), int)
    ]


def find_reusable_validation(
    *,
    api_get: ApiGet,
    repository: str,
    commit_sha: str,
    branch: str,
    workflow_id: int,
    current_tree: str,
) -> tuple[bool, str]:
    """Find an exact successful PR-tree attestation for ``commit_sha``."""

    pulls = api_get(
        f"/repos/{repository}/commits/{commit_sha}/pulls",
        {"per_page": "100"},
    )
    candidates = _matching_pull_requests(
        pulls,
        commit_sha=commit_sha,
        branch=branch,
    )
    if not candidates:
        return False, "no exact merged pull request is associated with this commit"

    for pull in candidates:
        head_sha = str(pull["head"]["sha"]).lower()
        pull_number = int(pull["number"])
        runs_response = api_get(
            f"/repos/{repository}/actions/workflows/{workflow_id}/runs",
            {
                "event": "pull_request",
                "head_sha": head_sha,
                "per_page": "100",
                "status": "success",
            },
        )
        runs = (
            runs_response.get("workflow_runs", [])
            if isinstance(runs_response, dict)
            else []
        )
        for run in runs:
            if not isinstance(run, dict) or not isinstance(run.get("id"), int):
                continue
            if run.get("event") != "pull_request" or run.get("conclusion") != "success":
                continue
            run_attempt = run.get("run_attempt")
            if not isinstance(run_attempt, int) or run_attempt < 1:
                continue
            run_pulls = run.get("pull_requests")
            if not isinstance(run_pulls, list):
                continue
            # GitHub currently returns an empty pull_requests array for some
            # same-repository pull_request workflow runs. The merged-commit
            # lookup, exact head SHA, successful workflow run, and tree-bound
            # artifact already provide the association in that case. When
            # GitHub does provide PR references, still require the candidate
            # PR number so an inconsistent response fails closed.
            if run_pulls and not any(
                isinstance(run_pull, dict) and run_pull.get("number") == pull_number
                for run_pull in run_pulls
            ):
                continue
            run_id = int(run["id"])
            artifacts_response = api_get(
                f"/repos/{repository}/actions/runs/{run_id}/artifacts",
                {"per_page": "100"},
            )
            artifacts = (
                artifacts_response.get("artifacts", [])
                if isinstance(artifacts_response, dict)
                else []
            )
            expected_artifact = attestation_name(
                current_tree,
                head_sha,
                run_attempt,
            )
            if any(
                isinstance(artifact, dict)
                and artifact.get("name") == expected_artifact
                and artifact.get("expired") is False
                for artifact in artifacts
            ):
                return (
                    True,
                    f"PR #{pull_number} workflow run {run_id} validated tree {current_tree}",
                )

    return False, "no successful workflow attested the exact deployed tree"


def _write_outputs(reuse: bool, reason: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise RuntimeError("GITHUB_OUTPUT is not set")
    clean_reason = " ".join(reason.splitlines()).strip()
    with open(output_path, "a", encoding="utf-8") as output:
        output.write(f"reuse_pr_checks={'true' if reuse else 'false'}\n")
        output.write(f"reason={clean_reason}\n")


def main() -> int:
    if os.environ.get("GITHUB_EVENT_NAME") != "push":
        _write_outputs(False, "pull requests always run the full validation suite")
        return 0

    try:
        repository = os.environ["GITHUB_REPOSITORY"]
        commit_sha = os.environ["GITHUB_SHA"]
        branch = os.environ.get("GITHUB_REF_NAME", "main")
        run_id = int(os.environ["GITHUB_RUN_ID"])
        token = os.environ["GITHUB_TOKEN"]
        current_tree = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"],
            text=True,
        ).strip()
        api = GitHubApi(token)
        current_run = api.get(f"/repos/{repository}/actions/runs/{run_id}")
        workflow_id = int(current_run["workflow_id"])
        reuse, reason = find_reusable_validation(
            api_get=api.get,
            repository=repository,
            commit_sha=commit_sha,
            branch=branch,
            workflow_id=workflow_id,
            current_tree=current_tree,
        )
    except Exception as exc:  # Fail closed: full checks run on any uncertainty.
        reuse = False
        reason = f"validation lookup failed; running full checks ({type(exc).__name__})"
        print(f"::warning::{reason}", file=sys.stderr)

    _write_outputs(reuse, reason)
    print(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
