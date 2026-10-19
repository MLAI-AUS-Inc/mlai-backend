"""Scope expensive bootstrap memory acceptance to the implementation it covers."""

import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
EXACT_PATHS = {
    "content_factory/vibe_marketing_views.py",
    "content_factory/bootstrap_run_lifetime.py",
    "content_factory/models.py",
    "workflow_runs/models.py",
    "scripts/gunicorn.conf.py",
    "scripts/worker_memory_profile.py",
    "scripts/bootstrap_worker_profile.py",
    "scripts/check_bootstrap_profile_scope.py",
    "scripts/check_worker_memory_smoke.py",
    "mlai/settings.py",
    "requirements.txt",
    "requirements-engine.txt",
    "requirements.lock",
}


def covers(paths):
    """Recognize the measured endpoint, dependency graph and fixture paths."""
    return any(path in EXACT_PATHS or path.startswith((
        "tests/test_articles_bootstrap", "tests/test_bootstrap", "tests/test_vibe_marketing_bootstrap",
        "tests/test_worker_memory_profile", "tests/fixtures/gunicorn_",
    )) for path in paths)


def base_revision(event, kind):
    """Use immutable event revisions; manual branch validation compares main."""
    if kind == "pull_request":
        base = event.get("pull_request", {}).get("base", {}).get("sha", "")
    elif kind == "push":
        base = event.get("before", "")
    elif kind == "workflow_dispatch":
        return "origin/main"
    else:
        raise ValueError("Unsupported bootstrap memory acceptance event")
    if not isinstance(base, str) or re.fullmatch(r"[0-9a-f]{40}", base) is None or set(base) == {"0"}:
        raise ValueError("Bootstrap memory acceptance needs its immutable event base")
    return base


def main():
    """Emit a boolean scope, with errors failing the existing validation job."""
    if os.environ.get("GITHUB_REPOSITORY") != "MLAI-AUS-Inc/mlai-backend":
        raise ValueError("Bootstrap memory acceptance requires its owned repository")
    head = os.environ.get("GITHUB_SHA", "")
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise ValueError("Bootstrap memory acceptance needs the exact tested revision")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    base = base_revision(event, os.environ.get("GITHUB_EVENT_NAME"))
    paths = subprocess.check_output(["git", "diff", "--name-only", "--no-renames", base, head, "--"],
        cwd=ROOT, text=True).splitlines()
    required = covers(paths)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as handle:
        handle.write("required=" + str(required).lower() + "\n")
    print("Bootstrap memory acceptance " + ("required for changed paths" if required else "outside changed paths"))


if __name__ == "__main__":
    main()
