#!/usr/bin/env python3
"""Validate the founder-journey Articles and My startup switches before production mutation.

Every setting is optional: an empty value keeps whatever the host's ``.env``
already holds, so a release never switches a founder path off by omission.
mlai/settings.py turns most of these into ints at import, so a malformed value
would stop the web and worker containers from starting.
"""

from __future__ import annotations

from datetime import date, datetime
import json
import os
import re
import sys


FLAG_KEYS = (
    "ARTICLES_FREE_SETUP_RESEARCH_ENABLED",
    "ARTICLES_FREE_FIRST_ARTICLE_ENABLED",
    "ARTICLES_FIRST_ARTICLE_GOAL_ENABLED",
    "ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED",
    # Article preview switches: restart portable previews, and publish only a rendered one.
    "CONTENT_FACTORY_PORTABLE_PREVIEW_REARM",
    # Run snapshots merge by meaning, and Content Factory run events are kept.
    "ARTICLES_SNAPSHOT_MERGE_BY_MEANING_ENABLED",
    "ARTICLES_RUN_EVENTS_ENABLED",
    # Phase 3 job/run merge: dual write, shadow read, then read from the run.
    "ARTICLES_RUN_JOB_DUAL_WRITE_ENABLED",
    "ARTICLES_RUN_JOB_SHADOW_READ_ENABLED",
    "ARTICLES_RUN_JOB_READ_ENABLED",
    # P1 founder journey ("one simple path"): the master switch, then the join,
    # sign-in and cohort-gated feature switches, in the P1 turn-on order.
    "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED",
    "MY_STARTUP_FOUNDER_JOURNEY_API_ENABLED",
    "MY_STARTUP_FOUNDER_JOURNEY_OPT_IN_ENABLED",
    "COMMUNITY_CHAT_ONE_STEP_JOIN_ENABLED",
    "COMMUNITY_CHAT_INVITE_TOKENS_ENABLED",
    "MY_STARTUP_PENDING_MEMBER_ACCESS_ENABLED",
    "COMMUNITY_CHAT_REVIEW_CLEARED_EMAIL_ENABLED",
    "COMMUNITY_CHAT_DEVICE_AUTH_V2_ENABLED",
    "MY_STARTUP_ONE_FIELD_START_ENABLED",
    "MY_STARTUP_NO_WEBSITE_ARTICLES_ENABLED",
    "ARTICLES_FIRST_ARTICLE_CARD_ENABLED",
    "ARTICLES_COPY_READY_BEFORE_VERIFICATION_ENABLED",
    "ARTICLES_DRAFT_READY_NOTIFICATIONS_ENABLED",
    "ARTICLES_COPY_WITHOUT_PREVIEW_ENABLED",
    "ARTICLES_DRAFT_TOOLS_ENABLED",
    "ARTICLES_LIVE_URL_VERIFICATION_ENABLED",
    "MY_STARTUP_GITHUB_CALLBACK_REDIRECTS_ENABLED",
    # The WHOLE existing staff console (every staff_console action, including
    # retry_now, release_or_refund, resolve_and_notify and verify_safety_claims,
    # and the staff console routes), plus the acknowledgement due times of
    # existing founder journey alerts (founder_journey_alerts._ack_due_applies).
    # Empty keeps the host value; only an explicit operator true/false installs.
    "MY_STARTUP_STAFF_CONSOLE_ENABLED",
    # Every other founder Articles, My startup and Content Factory switch, so a
    # release can set any of them and none can only be changed by hand on the
    # host. Empty keeps the host value; no default changes. The stuck-founder
    # alert, the three GitHub linking kill switches below it, the stand-in
    # preview approval switch, the ready-on-merge and no-workflow kill
    # switches, ARTICLES_SOURCE_FOLLOW_ENABLED,
    # ARTICLES_PROOF_CARRY_FORWARD_ENABLED,
    # ARTICLES_CALLBACK_CONTRACT_FROM_RUN_ENABLED,
    # ARTICLES_SOURCE_CHECK_OWNS_PUSH_ENABLED,
    # ARTICLES_CLOSE_SUPERSEDED_SETUP_PRS_ENABLED,
    # CONTENT_FACTORY_EDITORIAL_SUPERSEDED_OK,
    # ARTICLES_SELF_HEAL_CLASS_BEFORE_WALL_ENABLED and
    # ARTICLES_SELF_HEAL_PRESERVE_CODE_ENABLED are on by default.
    "ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED",
    "ARTICLES_REPOSITORY_IDENTITY_REFRESH_ENABLED",
    "MY_STARTUP_GITHUB_CALLBACK_RETURN_FOR_ALL_ENABLED",
    "ARTICLES_GITHUB_OWNER_APPROVAL_PICKUP_ENABLED",
    "ARTICLES_STAND_IN_PREVIEW_APPROVAL_ENABLED",
    "ARTICLES_READY_ON_MERGE_ENABLED",
    # Cross-repo dependency: with ARTICLES_LIVE_URL_VERIFICATION_ENABLED on,
    # this switch sends verification_mode=hosted for every cohort, which needs
    # Content Factory's CONTENT_FACTORY_ARTICLES_HOSTED_VERIFICATION_ENABLED=true
    # (check with CF's articles_rollout_inspection before a release). This repo
    # cannot read CF's value, so turn this off if CF's is false.
    "ARTICLES_NO_WORKFLOW_ALL_COHORTS_ENABLED",
    "ARTICLES_STAFF_ALERT_RULES_ENABLED",
    "ARTICLES_FIRST_ARTICLE_REQUIRES_CONNECTED_WEBSITE",
    "ARTICLES_FAILURE_POLICY_V2_ENABLED",
    "ARTICLES_SELF_HEAL_POLICY_ENABLED",
    "ARTICLES_SELF_HEAL_REPORT_ENABLED",
    "ARTICLES_SELF_HEAL_CLASS_BEFORE_WALL_ENABLED",
    "ARTICLES_SELF_HEAL_PRESERVE_CODE_ENABLED",
    "ARTICLES_DISPATCH_RELIABILITY_ENABLED",
    "ARTICLES_NO_CODELESS_TERMINAL_ENABLED",
    "ARTICLES_REGISTRY_REFUNDS_ENABLED",
    "ARTICLES_CHANNEL_SWITCH_ENABLED",
    "ARTICLES_SOURCE_REPIN_ENABLED",
    "ARTICLES_SOURCE_FOLLOW_ENABLED",
    "ARTICLES_PROOF_CARRY_FORWARD_ENABLED",
    "ARTICLES_CALLBACK_CONTRACT_FROM_RUN_ENABLED",
    "ARTICLES_SOURCE_CHECK_OWNS_PUSH_ENABLED",
    "ARTICLES_CLOSE_SUPERSEDED_SETUP_PRS_ENABLED",
    "CONTENT_FACTORY_EDITORIAL_SUPERSEDED_OK",
    "ARTICLES_CONNECTION_REFUSAL_CODES_ENABLED",
    "ARTICLES_MERGE_WORKER_ENABLED",
    "ARTICLES_DEPLOYMENT_WATCH_SWEEP_ENABLED",
    "ARTICLES_GITHUB_INSTALLATION_WAKES_ENABLED",
    "ARTICLES_SETUP_EXAMPLE_AUTO_REMOVE_ENABLED",
    "ARTICLES_PUBLISH_REVIEWED_DRAFT_ENABLED",
    "MY_STARTUP_CANONICAL_ACTION_IDS_ENABLED",
    "MY_STARTUP_IDEMPOTENT_PROFILE_SAVE_ENABLED",
    "MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED",
    "MY_STARTUP_TURN_REMINDERS_ENABLED",
    "MY_STARTUP_TEAM_ENABLED",
    "MY_STARTUP_SHARED_ACCESS_ENABLED",
    "MY_STARTUP_EMAIL_INVITES_ENABLED",
    "MY_STARTUP_TEAM_DOMAIN_AUTO_JOIN_ENABLED",
    "MY_STARTUP_ARTICLE_IMPROVEMENTS_ENABLED",
    "MY_STARTUP_WEBSITE_CHECKS_BACKGROUND_ENABLED",
    "MY_STARTUP_DELIVERY_LINKS_ENABLED",
    "ARTICLES_RUN_OWNERSHIP_ENABLED",
    "ARTICLES_RUN_FACT_AUTHORITY_ENABLED",
    "ARTICLES_RUN_EVENT_AUTHORITY_ENABLED",
    "ARTICLES_RUN_SDK_CONTROL_ENABLED",
    "ARTICLES_ARTICLE_RECOVERY_ENABLED",
    "ARTICLES_HOSTED_VERIFICATION_ENABLED",
    "ARTICLES_HOSTED_ENVIRONMENT_RECOVERY_ENABLED",
    "ARTICLES_HOSTED_ATTESTATION_ENABLED",
    "ARTICLES_PUBLISHING_INPUTS_V3_ENABLED",
    "ARTICLES_DEPLOYMENT_DETECTION_ENABLED",
    "ARTICLES_SITE_FINGERPRINT_ENABLED",
    "ARTICLES_EXISTING_INVENTORY_DISPLAY_ENABLED",
    "ARTICLES_GOAL_NOTIFICATIONS_ENABLED",
    "ARTICLES_SETUP_CONSENT_ENABLED",
    "ARTICLES_AUTO_PAGE_ENABLED",
    "ARTICLES_WEBSITE_STRATEGY_ENABLED",
    "ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED",
    "ARTICLES_COST_ACCOUNTING_ENABLED",
    "ARTICLES_OPERATIONAL_ALERTS_ENABLED",
    "ARTICLES_WEBSITE_EVENT_WAKES_ENABLED",
    "ARTICLES_WEBSITE_DOCTOR_ENABLED",
    "ARTICLES_WEBSITE_STATUS_V1_ENABLED",
    "ARTICLES_WEBSITE_CONSISTENCY_ENABLED",
    "ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED",
    "ARTICLES_FAILURE_TAXONOMY_OBSERVE_ENABLED",
    "ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED",
    "CONTENT_FACTORY_WORKFLOW_ORDER_FORMATTING_ADMISSION_ENABLED",
    "CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED",
    "CONTENT_FACTORY_SETUP_DELIVERY_RECONCILIATION_ENABLED",
    "CONTENT_FACTORY_ARTICLE_DELIVERY_V1_APPLY_ENABLED",
    "WEBSITE_VERIFICATION_PAGE_REFRESH_ENABLED",
)
# 0 means "no cap", "off" or, for the queue slots, "no ETA".
COUNT_KEYS = (
    "ARTICLES_FREE_SETUP_AUTOFILL_DAILY_CAP",
    "ARTICLES_FREE_SETUP_BASELINE_DAILY_CAP",
    "ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP",
    "ARTICLES_FREE_FIRST_ARTICLE_SPIKE_THRESHOLD",
    "CONTENT_FACTORY_INTERACTIVE_QUEUE_SLOTS",
    # Setup runs per website preparation (default 3, 0 = no cap).
    "ARTICLES_PREPARE_SETUP_DISPATCH_LIMIT",
)
# 0 is refused: the sweep reads it as 1, and a per-IP sign-in limit of 0 removes
# that ceiling altogether. Raise a limit for a large event instead.
POSITIVE_KEYS = (
    "ARTICLES_FOUNDER_JOURNEY_ALERTS_MAX_PER_TICK",
    "AUTH_THROTTLE_IP_CODE_REQUESTS_PER_HOUR",
    "AUTH_THROTTLE_IP_VERIFY_PER_10M",
    "AUTH_THROTTLE_IP_PASSWORD_PER_10M",
    "AUTH_THROTTLE_IP_BOOTSTRAP_AUTHENTICATED_PER_10M",
    "AUTH_THROTTLE_IP_DEVICE_AUTH_PER_10M",
    # Invite links and desktop sign-in requests must live at least an hour or a second.
    "COMMUNITY_CHAT_INVITE_TOKEN_DEFAULT_TTL_HOURS",
    "COMMUNITY_CHAT_INVITE_TOKEN_MAX_TTL_HOURS",
    "COMMUNITY_CHAT_DEVICE_AUTH_MAX_LIFETIME_SECONDS",
)
ALERTS_SINCE = "ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE"
WORKSHOP_WINDOWS = "ARTICLES_WORKSHOP_WINDOWS"
# Startups created at or after this time join the new journey. The backend
# ignores a naive time, which would leave the launch silently switched off.
JOURNEY_LAUNCHED_AT = "MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT"
# Per-organisation kill switch: organisation ids or domains, comma separated.
# An empty release value keeps the host's list, so clear it with "none".
JOURNEY_DISABLED_ORGANIZATIONS = "MY_STARTUP_FOUNDER_JOURNEY_V2_DISABLED_ORGANIZATIONS"
# A Django REST framework rate such as 30/minute. A malformed one breaks every
# invite resolve at request time.
INVITE_RESOLVE_RATE = "COMMUNITY_CHAT_INVITE_RESOLVE_RATE"
DISCOVERY_RECONCILIATION_SINCE = "CONTENT_FACTORY_DISCOVERY_RECONCILIATION_SINCE"
NIGHTLY_RECONCILIATION_SINCE = "CONTENT_FACTORY_NIGHTLY_REFRESH_RECONCILIATION_SINCE"
KEYS = (*FLAG_KEYS, *COUNT_KEYS, *POSITIVE_KEYS, ALERTS_SINCE, WORKSHOP_WINDOWS,
        JOURNEY_LAUNCHED_AT, JOURNEY_DISABLED_ORGANIZATIONS, INVITE_RESOLVE_RATE, DISCOVERY_RECONCILIATION_SINCE, NIGHTLY_RECONCILIATION_SINCE)
MAX_DISABLED_ORGANIZATIONS_LENGTH = 2000

MAX_WORKSHOP_WINDOWS = 50
# An explicit offset is required: the scheduler reads a naive time as UTC,
# which would put a Melbourne workshop eleven hours out.
WINDOW_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})")
# Only what a list of {"start", "end"} timestamps needs, so the line stays
# literal in the host's .env (no '#', '$', quotes or backslashes).
WINDOWS_CHARACTERS = re.compile(r'[\[\]{}",: 0-9TZ+\-adenrst]*')


def _window_time(value) -> datetime | None:
    if not isinstance(value, str) or not WINDOW_TIME.fullmatch(value):
        return None
    try:
        # datetime.fromisoformat accepts "Z" only from Python 3.11.
        return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None


def _validate_workshop_windows(key: str, value: str) -> None:
    if not WINDOWS_CHARACTERS.fullmatch(value):
        raise ValueError(f'{key} must be a JSON list of {{"start", "end"}} timestamps')
    try:
        windows = json.loads(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be valid JSON") from exc
    if not isinstance(windows, list) or len(windows) > MAX_WORKSHOP_WINDOWS:
        raise ValueError(f"{key} must be a JSON list of at most {MAX_WORKSHOP_WINDOWS} windows")
    for index, window in enumerate(windows, start=1):
        if not isinstance(window, dict) or set(window) != {"start", "end"}:
            raise ValueError(f'{key} window {index} must have exactly "start" and "end"')
        start, end = _window_time(window["start"]), _window_time(window["end"])
        if start is None or end is None:
            raise ValueError(f"{key} window {index} needs ISO 8601 times with an explicit offset")
        if end <= start:
            raise ValueError(f"{key} window {index} must end after it starts")


def validate_single(key: str, value: str, *, optional: bool = False) -> None:
    """Reject malformed single-line configuration without echoing its value."""
    if optional and not value:
        return
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ValueError(f"{key} must be a non-empty single-line value")
    if key in FLAG_KEYS:
        if value not in {"true", "false"}:
            raise ValueError(f"{key} must be true or false")
    elif key in COUNT_KEYS:
        if not re.fullmatch(r"0|[1-9][0-9]{0,8}", value):
            raise ValueError(f"{key} must be a whole number of 0 or more")
    elif key in POSITIVE_KEYS:
        if not re.fullmatch(r"[1-9][0-9]{0,8}", value):
            raise ValueError(f"{key} must be a whole number of 1 or more")
    elif key == ALERTS_SINCE:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be an ISO date (YYYY-MM-DD)") from exc
    elif key == WORKSHOP_WINDOWS:
        _validate_workshop_windows(key, value)
    elif key == JOURNEY_LAUNCHED_AT:
        if _window_time(value) is None:
            raise ValueError(f"{key} must be an ISO 8601 time with an explicit offset")
    elif key in {DISCOVERY_RECONCILIATION_SINCE, NIGHTLY_RECONCILIATION_SINCE}:
        if value != "disabled" and _window_time(value) is None:
            raise ValueError(f"{key} must be an ISO 8601 time with an explicit offset or disabled")
    elif key == JOURNEY_DISABLED_ORGANIZATIONS:
        if (len(value) > MAX_DISABLED_ORGANIZATIONS_LENGTH
                or not re.fullmatch(r"[A-Za-z0-9.-]+(?: *, *[A-Za-z0-9.-]+)*", value)):
            raise ValueError(f"{key} must be comma-separated organisation ids or domains")
    elif key == INVITE_RESOLVE_RATE:
        if not re.fullmatch(r"[1-9][0-9]{0,6}/(?:s|sec|second|m|min|minute|h|hour|d|day)", value):
            raise ValueError(f"{key} must be a rate such as 30/minute")
    else:
        raise ValueError("Unsupported Articles configuration key")


def validate(environment: dict[str, str]) -> None:
    """Validate every Articles setting a release may carry; each may be left empty."""
    for key in KEYS:
        validate_single(key, environment.get(key, ""), optional=True)


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--stdin":
            validate_single(sys.argv[2], sys.stdin.read())
        elif len(sys.argv) == 1:
            validate(dict(os.environ))
        else:
            raise ValueError("Use no arguments, or --stdin with a configuration key")
    except ValueError as error:
        raise SystemExit(str(error)) from error
