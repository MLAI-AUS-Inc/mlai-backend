"""Fast owner-authorized DM directory with live, verified Chat identity links."""

import re

from django.core.cache import cache
from django.db import transaction

from integrations.models import CommunityBridgeIdentityLink
from integrations.services import slack_dm_mirror as mirror
from integrations.services.community_bridge.identity import verified_identity_for_slack
from integrations.services.slack_mentions import (
    SNAPSHOT_CURSOR_PREFIX,
    _key,
    _snapshot_page,
    sanitized_directory_page,
)
from integrations.services.slack_workspace_users import cached_workspace_snapshot


def _validate(authority):
    with transaction.atomic():
        mirror._lock_slack_grant_api_authority(
            authority, required_scopes=mirror.DIRECT_DM_SCOPES
        )


def _people(users, authority):
    return [
        user
        for user in users
        if not user.get("is_bot") and user["slack_user_id"] != authority.slack_user_id
    ]


def _linked_people(users, workspace):
    # A whole page of Slack-only people needs one lookup. Re-resolve linked
    # devices live so a saved name can never resurrect a revoked identity.
    linked_ids = (
        set(
            CommunityBridgeIdentityLink.objects.filter(
                slack_workspace_id=workspace,
                slack_user_id__in=[user["slack_user_id"] for user in users],
                revoked_at__isnull=True,
            ).values_list("slack_user_id", flat=True)
        )
        if users
        else set()
    )
    result = []
    for user in users:
        identity = (
            verified_identity_for_slack(
                slack_workspace_id=workspace,
                slack_user_id=user["slack_user_id"],
            )
            if user["slack_user_id"] in linked_ids
            else None
        ) or {}
        result.append(
            {key: value for key, value in user.items() if key != "search"}
            | {
                "pubkey": identity.get("buzz_pubkey") or None,
                "profile_id": identity.get("user_profile_id") or None,
            }
        )
    return result


def search_dm_directory(authority, *, query="", limit=20, cursor=""):
    """Page cached names under current owner consent; resolve links on each read."""
    query = str(query or "").strip().casefold()
    if len(query) > 100:
        raise mirror.SlackDmMirrorError(
            "Slack user search is limited to 100 characters."
        )
    limit = max(1, min(int(limit), 50))
    slack_cursor, offset = mirror._decode_directory_cursor(cursor)
    _validate(authority)

    version = ""
    start = offset
    if slack_cursor.startswith(SNAPSHOT_CURSOR_PREFIX):
        suffix = slack_cursor.removeprefix(SNAPSHOT_CURSOR_PREFIX)
        version, separator, position = suffix.partition(":")
        if (
            not separator
            or not re.fullmatch(r"[0-9a-f]{32}", version)
            or not position.isdecimal()
            or len(position) > 10
        ):
            raise mirror.SlackDmMirrorError("Slack directory cursor is invalid.")
        start += int(position)
    snapshot = (
        cached_workspace_snapshot(authority.workspace_id, version=version)
        if not slack_cursor or version
        else None
    )
    if snapshot is not None:
        # Filter before paging, keeping the worker snapshot immutable and its
        # stable cursor version across a background directory replacement.
        users, next_cursor = _snapshot_page(
            {**snapshot, "users": _people(snapshot["users"], authority)},
            query=query,
            start=start,
            limit=limit,
        )
    else:
        if version:
            slack_cursor, offset = "", 0
        users, next_cursor = _owner_pages(
            authority, query=query, limit=limit, cursor=slack_cursor, offset=offset
        )
    result = _linked_people(users, authority.workspace_id)
    _validate(authority)
    return {"users": result, "next_cursor": next_cursor}


def _owner_pages(authority, *, query, limit, cursor, offset):
    """Retain the bounded owner-token fallback, sharing pages between queries."""
    users = []
    next_cursor = ""
    seen = set()
    for _ in range(20):
        if cursor in seen:
            raise mirror.SlackDmMirrorError("Slack user pagination made no progress.")
        seen.add(cursor)
        key = _key(authority, "dm-users", cursor)
        page = cache.get(key)
        if page is None:
            response = mirror._call_slack_with_grant_authority(
                authority,
                "users_list",
                required_scopes=mirror.DIRECT_DM_SCOPES,
                limit=200,
                cursor=cursor,
            )
            page = sanitized_directory_page(response, authority.workspace_id)
            cache.set(key, page, timeout=600)
        matches = [
            user
            for user in _people(page["users"], authority)
            if query in user["search"]
        ][offset:]
        remaining = limit - len(users)
        users.extend(matches[:remaining])
        consumed = min(len(matches), remaining)
        if consumed < len(matches):
            next_cursor = mirror._encode_directory_cursor(cursor, offset + consumed)
            break
        cursor, offset = page["next"], 0
        if not cursor:
            next_cursor = ""
            break
        next_cursor = mirror._encode_directory_cursor(cursor, 0)
        if len(users) >= limit:
            break
    return users, next_cursor
