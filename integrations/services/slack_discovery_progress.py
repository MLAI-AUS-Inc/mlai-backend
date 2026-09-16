"""Resume directory metadata reads without persisting partial chat authority.

Only the directory worker opens this context. Successful Slack pages survive
provider deferral, while every read/write still uses its current discovery lease
and OAuth/consent checks. No message bodies or credentials enter this cursor.
"""

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from django.db import transaction
from django.utils import timezone

KEY = "slack_directory_progress_v1"
MAX_MEMBERS = 100_000
MAX_PAGES = 1_000
MEMBER_SCAN_MAX_AGE = 3_600
MEMBER_RESULT_MAX_AGE = 300
_current = ContextVar("slack_directory_progress", default=None)


def _membership_fence(conversation):
    if conversation is None:
        return "unseen"
    values = [
        sorted(conversation.participant_slack_ids or []),
        conversation.participant_hash, str(conversation.mlai_channel_id or ""),
        conversation.status,
    ]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


class DirectoryProgress:
    """One channel's metadata checkpoint, fenced by the current source grant."""

    def __init__(self, authority, channel_id, kind, cycle_started_at):
        from . import slack_dm_mirror as dm

        self.authority = authority
        self.scope = {
            **dm._discovery_checkpoint_identity(authority),
            "connection_id": authority.connection_id,
            "channel_id": channel_id,
            "kind": kind,
            "cycle_started_at": cycle_started_at.isoformat(),
        }
        with transaction.atomic():
            grant, connection = dm._lock_slack_grant_api_authority(
                authority, required_scopes=dm.DIRECT_DM_SCOPES,
            )
            self.scope["history_days"] = dm._grant_history_days(grant)
            value = (connection.sync_cursor or {}).get(KEY)
            self.value = deepcopy(value) if (
                isinstance(value, dict)
                and all(value.get(key) == expected for key, expected in self.scope.items())
            ) else dict(self.scope)
            conversation = grant.conversations.filter(slack_conversation_id=channel_id).first()
            self.membership_fence = _membership_fence(conversation)
            if self.value.get("membership_fence") != self.membership_fence:
                # An inbox membership update/retirement wins over a previously
                # completed directory snapshot. Names are not authority.
                self.value.pop("members", None)
            self.value["membership_fence"] = self.membership_fence

    def assert_membership_locked(self, conversation):
        """Reject an inbox/device boundary change before saving source intent."""
        from . import slack_dm_mirror as dm

        if _membership_fence(conversation) != self.membership_fence:
            raise dm.SlackDmMirrorAuthorizationError("Slack conversation membership changed.")

    def adopt_membership_locked(self, connection, conversation):
        """Advance only this context's own validated membership transaction."""
        self.membership_fence = _membership_fence(conversation)
        self.value["membership_fence"] = self.membership_fence
        connection.sync_cursor = {**(connection.sync_cursor or {}), KEY: self.value}
        connection.save(update_fields=("sync_cursor", "updated_at"))

    def save(self):
        """Commit successful page metadata only under current worker authority."""
        from . import slack_dm_mirror as dm

        with transaction.atomic():
            grant, connection = dm._lock_slack_grant_api_authority(
                self.authority, required_scopes=dm.DIRECT_DM_SCOPES,
            )
            if dm._grant_history_days(grant) != self.scope["history_days"]:
                raise dm.SlackDmMirrorAuthorizationError("Slack import window changed.")
            self.assert_membership_locked(grant.conversations.filter(
                slack_conversation_id=self.scope["channel_id"],
            ).first())
            cursor = dict(connection.sync_cursor or {})
            cursor[KEY] = self.value
            connection.sync_cursor = cursor
            connection.save(update_fields=("sync_cursor", "updated_at"))

    def clear(self):
        """Remove a completed conversation checkpoint under the same authority."""
        from . import slack_dm_mirror as dm

        with transaction.atomic():
            _, connection = dm._lock_slack_grant_api_authority(
                self.authority, required_scopes=dm.DIRECT_DM_SCOPES,
            )
            cursor = dict(connection.sync_cursor or {})
            value = cursor.get(KEY)
            if isinstance(value, dict) and all(
                value.get(key) == expected for key, expected in self.scope.items()
            ):
                cursor.pop(KEY)
                connection.sync_cursor = cursor
                connection.save(update_fields=("sync_cursor", "updated_at"))

    def members(self):
        """Expire membership independently from already collected profiles."""
        value = self.value.get("members")
        now = timezone.now().timestamp()
        if isinstance(value, dict):
            age = now - float(value.get("started_at") or 0)
            complete_age = now - float(value.get("completed_at") or 0)
            if 0 <= age < MEMBER_SCAN_MAX_AGE and (
                not value.get("complete") or 0 <= complete_age < MEMBER_RESULT_MAX_AGE
            ):
                return deepcopy(value)
        return {"ids": [], "cursor": "", "seen_cursors": [], "started_at": now}

    def save_members(self, ids, cursor, seen_cursors, started_at):
        from . import slack_dm_mirror as dm

        if len(ids) > MAX_MEMBERS or len(seen_cursors) > MAX_PAGES:
            raise dm.SlackDmMirrorError("Slack membership pagination exceeds supported limits.")
        self.value["members"] = {
            "ids": sorted(ids), "cursor": cursor,
            "seen_cursors": sorted(seen_cursors), "started_at": started_at,
            "complete": not cursor,
            "completed_at": timezone.now().timestamp() if not cursor else None,
        }
        self.save()

    def profiles(self):
        """Hydrate a fresh worker's display cache once per conversation."""
        return deepcopy(self.value.get("profiles") or {})

    def profile(self, user_id):
        """Read one small profile without copying an entire large channel."""
        value = (self.value.get("profiles") or {}).get(user_id)
        return dict(value) if value is not None else None

    def save_profiles(self, profiles, *, bulk=None):
        """Persist only sanitized display metadata, never raw Slack users."""
        from . import slack_dm_mirror as dm

        result = dict(self.value.get("profiles") or {})
        result.update({
            str(user_id)[:100]: {
                "display_name": str(profile.get("display_name") or "")[:255],
                "avatar_url": str(profile.get("avatar_url") or "")[:2000],
            }
            for user_id, profile in profiles.items()
        })
        if len(result) > MAX_MEMBERS:
            raise dm.SlackDmMirrorError("Slack profile pagination exceeds supported limits.")
        self.value["profiles"] = result
        if bulk is not None:
            if len(bulk.get("seen_cursors") or []) > MAX_PAGES:
                raise dm.SlackDmMirrorError("Slack profile pagination exceeds supported limits.")
            self.value["bulk"] = bulk
        self.save()


@contextmanager
def conversation_progress(authority, channel_id, kind, cycle_started_at):
    """Retain completed metadata stages across deferrals within one scan cycle."""
    progress = DirectoryProgress(authority, channel_id, kind, cycle_started_at)
    token = _current.set(progress)
    try:
        yield progress
    except Exception:
        # The next turn revalidates authority and resumes successful pages.
        # A new cycle/channel/consent replaces the bounded checkpoint entirely.
        raise
    else:
        progress.clear()
    finally:
        _current.reset(token)


def current_progress(authority):
    """Return a checkpoint only inside its explicitly scoped directory call."""
    progress = _current.get()
    return progress if progress is not None and progress.authority == authority else None
