"""Versioned proof that a complete private window reached the current room."""
from decimal import Decimal

from django.db.models import Case, CharField, DecimalField, F, IntegerField, OuterRef, Q, Value, When
from django.db import connections
from django.db.models.functions import Cast, Concat, Greatest, Least, Replace
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.db.models import Func
from django.utils import timezone

from integrations.models import BridgeSyncState

IMPORT_CONTRACT_VERSION = 2


def coverage_attempt_rows():
    """Correlate a completed current-contract scan, including limited coverage."""
    def text_at(key):
        return KeyTextTransform(key, KeyTransform('archive', 'verified_ranges'))
    # UUID columns differ between SQLite fixtures and PostgreSQL; compare the
    # same canonical UUID without hyphens while retaining normal IDs in proof.
    return BridgeSyncState.objects.filter(
        private_conversation_id=OuterRef('pk'),
        verified_ranges__archive__import_contract_version=IMPORT_CONTRACT_VERSION,
    ).annotate(
        coverage_boundary=text_at('participant_hash'),
        coverage_channel=Replace(text_at('channel_id'), Value('-'), Value(''), output_field=CharField()),
    ).filter(
        coverage_boundary=OuterRef('participant_hash'),
        coverage_channel=Replace(Cast(OuterRef('mlai_channel_id'), CharField()), Value('-'), Value(''), output_field=CharField()),
    )


def current_coverage_rows():
    """Require complete unrestricted source coverage for first publication."""
    return coverage_attempt_rows().filter(
        verified_ranges__archive__classification__in=['accessible_range', 'empty_accessible_range'],
    )


def recent_conversations(query, *, now=None):
    """Restrict repair candidates to known activity inside their selected window."""
    from integrations.services.slack_dm_mirror import _history_days
    now = now or timezone.now()
    configured_days = _history_days()
    decimal = DecimalField(max_digits=20, decimal_places=6)
    source = Func(F('grant__connection__provider_metadata'), Value('mlai_chat_conversations_v1'),
                  F('slack_conversation_id'), Value('latest_message_ts'),
                  function='jsonb_extract_path_text', output_field=CharField())
    if connections[query.db].vendor == 'sqlite':
        source = Func(F('grant__connection__provider_metadata'),
                      Concat(Value('$."mlai_chat_conversations_v1"."'), F('slack_conversation_id'), Value('"."latest_message_ts"')),
                      function='json_extract', output_field=CharField())
    query = query.annotate(coverage_source_ts=source).annotate(
        coverage_latest=Case(When(latest_synced_ts__regex=r'^\d{10}(\.\d{1,6})?$',
                                 then=Cast('latest_synced_ts', decimal)),
                             default=Value(Decimal(0)), output_field=decimal),
        coverage_source=Case(When(coverage_source_ts__regex=r'^\d{10}(\.\d{1,6})?$',
                                 then=Cast('coverage_source_ts', decimal)),
                             default=Value(Decimal(0)), output_field=decimal),
        coverage_days=Case(When(grant__history_days=0, grant__consent_version='slack-chat-v5-all-available-history', then=Value(0)),
                           When(grant__history_days__gt=0, then=Least(F('grant__history_days'), Value(configured_days))),
                           default=Value(configured_days), output_field=IntegerField()),
    ).annotate(coverage_activity=Greatest(
        Case(When(coverage_latest__lte=int(now.timestamp())+300, then=F('coverage_latest')), default=Value(Decimal(0)), output_field=decimal),
        Case(When(coverage_source__lte=int(now.timestamp())+300, then=F('coverage_source')), default=Value(Decimal(0)), output_field=decimal),
    ))
    return query.filter(coverage_activity__gt=0).filter(
        Q(coverage_days=0) | Q(coverage_activity__gte=Value(int(now.timestamp())) - F('coverage_days') * Value(86400)),
    )


def request_current_coverage(conversation, authority, required_scopes):
    """Honor explicit compose intent without restarting an active private scan."""
    from django.db import transaction
    from django.db.models import Exists
    from integrations.models import SlackDmMirrorConversation
    from integrations.services import slack_dm_mirror as dm
    from .history import ensure_state

    if conversation.history_backfilled_at is None or conversation.status != 'live':
        return False
    with transaction.atomic():
        conversation, grant = dm._locked_history_write_context(
            conversation.pk, conversation.grant_id, authority, required_scopes,
        )
        if (conversation.history_backfilled_at is None
                or SlackDmMirrorConversation.objects.filter(pk=conversation.pk).filter(Exists(current_coverage_rows())).exists()):
            return False
        state = ensure_state(conversation)
        state = BridgeSyncState.objects.select_for_update().get(pk=state.pk)
        now = timezone.now()
        if (state.jobs.filter(lease_expires_at__gt=now).exists()
                or state.jobs.exclude(checkpoint={}).exists()
                or conversation.deliveries.filter(source_platform='slack', metadata__history_scan_state__in=['main', 'thread']).exists()):
            return False
        dm._mark_conversation_history_due(conversation, reason='Explicit compose requires current-room source coverage', reset_deliveries=False)
        state.jobs.filter(kind='archive', due_at__gt=now).update(due_at=now)
        return True
