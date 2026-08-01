#!/usr/bin/env python3
"""Decide whether a blocked Admin Brain staging attempt may be skipped.

The production deploy stages and activates the Admin Brain pilot binding
after migrations, while web traffic is paused. Evidence ingestion, however,
runs in the memory worker, which only receives new code once a deploy
completes. Hard-failing the deploy while the pilot simply has no retrievable
evidence yet therefore deadlocks the rollout: the deploy waits for evidence
that only a deployed worker can produce.

This helper reads the captured stdout of ``stage_org_memory_pilot --apply``
and reports (via exit code 0) when the attempt was blocked exclusively by
missing retrievable evidence, so deploy.sh can skip the binding for this
release and leave retrieval fail-closed. Every other failure mode — any
other readiness blocker, apply errors after a passing readiness report, or
output without a readiness report at all — keeps the deploy hard-failing
(exit code 1).
"""

import json
import sys

# Blockers that describe the pilot's pre-ingestion bootstrap state rather
# than a governance regression. Anything outside this set must fail the
# deploy loudly.
TOLERATED_BLOCKERS = frozenset({"retrievable_evidence_missing"})


def extract_readiness_blockers(stdout_text):
    """Return the blocker list from the last readiness JSON line, else None."""

    blockers = None
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        readiness = payload.get("readiness")
        if not isinstance(readiness, dict):
            continue
        candidate = readiness.get("blockers")
        if isinstance(candidate, list):
            blockers = candidate
    return blockers


def staging_skip_allowed(stdout_text):
    """True when staging was blocked only by tolerated bootstrap blockers."""

    blockers = extract_readiness_blockers(stdout_text)
    if not blockers:
        # No readiness report (or an empty blocker list on a failed apply)
        # means the failure is not the awaited-evidence state. Fail closed.
        return False
    return all(
        isinstance(blocker, str) and blocker in TOLERATED_BLOCKERS
        for blocker in blockers
    )


def main(argv):
    if len(argv) != 2:
        print(
            "usage: org_memory_staging_skip.py <captured-stage-stdout-file>",
            file=sys.stderr,
        )
        return 2
    try:
        with open(argv[1], "r", encoding="utf-8", errors="replace") as handle:
            stdout_text = handle.read()
    except OSError as exc:
        print(f"could not read staging output: {exc}", file=sys.stderr)
        return 2
    if staging_skip_allowed(stdout_text):
        print(
            "Admin Brain staging is blocked only by missing retrievable "
            "evidence; the deploy may skip the binding and stay fail-closed."
        )
        return 0
    print(
        "Admin Brain staging failed for reasons beyond missing retrievable "
        "evidence; the deploy must fail.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
