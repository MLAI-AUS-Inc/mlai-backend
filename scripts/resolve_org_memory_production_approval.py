#!/usr/bin/env python3
"""Build the effective protected pilot approval without exposing its contents."""

import argparse
import json
import sys


PUBLIC_ADMIN_SCOPE = "public_channels:pilot_admins"


def effective_manifest(manifest, *, approve_public_admin_scope: bool):
    if not isinstance(manifest, dict):
        raise ValueError("Admin Brain production approval must be a JSON object")
    resolved = dict(manifest)
    contexts = resolved.get("allowed_slack_contexts")
    if not isinstance(contexts, list):
        raise ValueError("Admin Brain Slack contexts must be a list")
    if approve_public_admin_scope and PUBLIC_ADMIN_SCOPE not in contexts:
        resolved["allowed_slack_contexts"] = [*contexts, PUBLIC_ADMIN_SCOPE]
    return resolved


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--approve-public-admin-scope", action="store_true")
    options = parser.parse_args()
    try:
        manifest = json.load(sys.stdin)
        resolved = effective_manifest(
            manifest,
            approve_public_admin_scope=options.approve_public_admin_scope,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    json.dump(
        resolved,
        sys.stdout,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


if __name__ == "__main__":
    main()
