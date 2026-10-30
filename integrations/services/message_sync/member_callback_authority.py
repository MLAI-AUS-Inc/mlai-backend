"""Server-owned account lock fence for one durable Slack callback transaction.

Payload fields identify signed Slack subjects, never lock/consent proof. A
ContextVar records only User rows actually locked by this processing attempt.
Downstream authority helpers refuse a newly discovered owner before taking a
lower-order lock or refreshing a token; the inbox then retries atomically.
"""
from contextlib import contextmanager
from contextvars import ContextVar

from django.contrib.auth import get_user_model
from django.db import connection

from integrations.models import SlackDmMirrorConversation, SlackDmMirrorGrant

MAX_CALLBACK_USERS = 2000
MAX_CALLBACK_SUBJECTS = 5000
_locked_users = ContextVar('slack_callback_locked_users', default=None)


class CallbackAuthorityChanged(RuntimeError):
    """A bounded callback must retry before acquiring an unplanned owner."""


def assert_callback_user_locked(user_id):
    """No-op for ordinary callers; never accept a proof from callback JSON."""
    allowed = _locked_users.get()
    if allowed is not None and user_id not in allowed:
        raise CallbackAuthorityChanged('callback_owner_cohort_changed')


def assert_callback_grant_locked(grant_id):
    """Guard legacy private paths that begin with only a grant primary key."""
    if _locked_users.get() is None:
        return
    owner = SlackDmMirrorGrant.objects.filter(pk=grant_id).values_list('user_id', flat=True).first()
    if owner is not None:
        assert_callback_user_locked(owner)


def _bounded_ids(query):
    ids = set(query[:MAX_CALLBACK_USERS + 1])
    if len(ids) > MAX_CALLBACK_USERS:
        raise CallbackAuthorityChanged('callback_owner_cohort_too_large')
    return ids


def relevant_user_ids(payload):
    """Include every owner that existing private intake may select.

    Retired/private owners remain relevant for Connect cleanup and legacy DM
    recipient fallback. Expanded authorizations select staging recipients;
    the event actor is included separately for public membership observations.
    """
    from integrations.services.slack_dm_mirror import _slack_event_authorized_user_ids

    workspace = payload.get('team_id')
    if not isinstance(workspace, str) or not workspace or len(workspace) > 100:
        return set()
    event = payload.get('event') if isinstance(payload.get('event'), dict) else {}
    subjects = _slack_event_authorized_user_ids(payload)
    actor = event.get('user')
    if isinstance(actor, str) and actor and len(actor) <= 100:
        subjects.add(actor)
    if len(subjects) > MAX_CALLBACK_SUBJECTS:
        raise CallbackAuthorityChanged('callback_subject_cohort_too_large')
    users = _bounded_ids(SlackDmMirrorGrant.objects.filter(
        slack_workspace_id=workspace, slack_user_id__in=subjects,
    ).order_by('user_id').values_list('user_id', flat=True).distinct()) if subjects else set()
    item = event.get('item') if isinstance(event.get('item'), dict) else {}
    raw_channel = item.get('channel') if str(event.get('type') or '').startswith('reaction_') else event.get('channel')
    channel = raw_channel.get('id') if isinstance(raw_channel, dict) else raw_channel
    if isinstance(channel, str) and channel and len(channel) <= 100:
        users.update(_bounded_ids(SlackDmMirrorConversation.objects.filter(
            slack_workspace_id=workspace, slack_conversation_id=channel,
        ).order_by('grant__user_id').values_list('grant__user_id', flat=True).distinct()))
    if payload.get('type') == 'app_rate_limited':
        users.update(_bounded_ids(SlackDmMirrorGrant.objects.filter(
            slack_workspace_id=workspace, status='active', revoked_at__isnull=True,
        ).order_by('user_id').values_list('user_id', flat=True).distinct()))
    from integrations.services.slack_member_access import community_reader_user_ids
    users.update(community_reader_user_ids(payload))
    if len(users) > MAX_CALLBACK_USERS:
        raise CallbackAuthorityChanged('callback_owner_cohort_too_large')
    return users


def _recheck(payload, allowed):
    if not relevant_user_ids(payload).issubset(allowed):
        raise CallbackAuthorityChanged('callback_owner_cohort_changed')


@contextmanager
def callback_user_fence(payload):
    """Lock the complete bounded cohort before hints, state or channel rows."""
    if not connection.in_atomic_block:
        raise RuntimeError('callback_owner_fence_requires_transaction')
    if _locked_users.get() is not None:
        raise RuntimeError('callback_owner_fence_already_active')
    candidates = relevant_user_ids(payload)
    allowed = frozenset(get_user_model().objects.select_for_update().filter(
        pk__in=candidates,
    ).order_by('pk').values_list('pk', flat=True))
    _recheck(payload, allowed)
    token = _locked_users.set(allowed)
    try:
        yield
        # A new recipient/grant must not silently disappear merely because it
        # arrived after the pre-lock query. Re-queue without taking its User.
        _recheck(payload, allowed)
    finally:
        _locked_users.reset(token)
