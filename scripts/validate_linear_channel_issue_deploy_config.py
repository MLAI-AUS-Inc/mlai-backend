#!/usr/bin/env python3
"""Validate deployment-managed configuration for Roo's Linear issue reader."""

from __future__ import annotations

import json
import os
import re
import uuid


SLACK_BINDING_RE = re.compile(r"^T[A-Z0-9]+:C[A-Z0-9]+$")
REQUIRED_BINDING_FIELDS = (
    "display_name",
    "team_name",
    "state_name",
    "linear_team_id",
    "linear_state_id",
)


def fail(message: str) -> None:
    raise SystemExit(message)


api_key = os.environ.get("LINEAR_API_KEY", "").strip()
if len(api_key) < 32:
    fail("LINEAR_API_KEY must be configured as a repository secret with at least 32 characters")

raw_bindings = os.environ.get("LINEAR_CHANNEL_ISSUE_BINDINGS_JSON", "")
try:
    bindings = json.loads(raw_bindings)
except json.JSONDecodeError as exc:
    fail(f"LINEAR_CHANNEL_ISSUE_BINDINGS_JSON must be valid JSON: {exc.msg}")

if not isinstance(bindings, dict) or not bindings:
    fail("LINEAR_CHANNEL_ISSUE_BINDINGS_JSON must be a non-empty JSON object")

for slack_binding, binding in bindings.items():
    if not isinstance(slack_binding, str) or not SLACK_BINDING_RE.fullmatch(slack_binding):
        fail("Linear channel binding keys must have the form T<workspace>:C<channel>")
    if not isinstance(binding, dict):
        fail(f"Linear channel binding {slack_binding} must be a JSON object")
    for field in REQUIRED_BINDING_FIELDS:
        if not isinstance(binding.get(field), str) or not binding[field].strip():
            fail(f"Linear channel binding {slack_binding} requires non-empty {field}")
    for field in ("linear_team_id", "linear_state_id"):
        try:
            uuid.UUID(binding[field])
        except ValueError:
            fail(f"Linear channel binding {slack_binding} has invalid {field}")

max_comments = os.environ.get("LINEAR_CHANNEL_ISSUE_MAX_COMMENTS", "")
if not re.fullmatch(r"[1-9][0-9]*", max_comments):
    fail("LINEAR_CHANNEL_ISSUE_MAX_COMMENTS must be a positive integer")
