"""Schedule fresh source reads for erased private backfill tombstones."""
from django.db import transaction
from django.db.models import CharField, Exists, F, Func, Min, OuterRef, Q, Subquery, Value
from django.utils import timezone

from integrations.models import BridgeSyncJob, BridgeSyncState, SlackDmMirrorConversation, SlackDmMirrorDelivery


def recoverable_rows():
    """Exclude irreversible rejection and previously handled source tombstones."""
    rows = SlackDmMirrorDelivery.objects.filter(
        source_platform="slack",
        status__in=["failed", "dead"], available_at__lte=timezone.now(),
    ).exclude(operation="delete")
    from .reaction_recovery import CONTRACT_KEY, legacy_failure_query

    from .parent_recovery import CONTRACT_KEY as PARENT_KEY, stale_parent_failure_query
    parent_failure = stale_parent_failure_query() & Q(**{f"metadata__{PARENT_KEY}__isnull": True})
    rows = rows.filter(Q(metadata__backfill=True) | parent_failure)

    # Original permanent-failure handling already set recovery_scheduled. Those
    # known legacy reactions may acquire the new audited exception exactly once.
    legacy = legacy_failure_query() & Q(**{f"metadata__{CONTRACT_KEY}__isnull": True})
    rows = rows.filter(
        Q(metadata__permanent_failure__isnull=True) | Q(metadata__permanent_failure=False)
        | legacy | parent_failure,
    ).filter(
        Q(metadata__history_recovery_scheduled__isnull=True)
        | Q(metadata__history_recovery_scheduled=False) | legacy | parent_failure,
    )
    for key in ("history_recovery_superseded", "history_outside_window"):
        rows = rows.filter(Q(**{f"metadata__{key}__isnull": True}) | Q(**{f"metadata__{key}": False}))
    return rows


def _record_attempt(conversation_id):
    # Only an operational timestamp, never delivery or consent state. Recording
    # a turn before authority validation also rotates malformed/stale owners
    # instead of letting them occupy every bounded scheduling round.
    with transaction.atomic():
        state = BridgeSyncState.objects.select_for_update(skip_locked=True).filter(
            private_conversation_id=conversation_id,
        ).exclude(status__in=["paused", "revoked"]).first()
        if state is None:
            return False
        recovery = {**(state.verified_ranges or {}).get("recovery", {}), "considered_at": timezone.now().isoformat()}
        state.verified_ranges = {**(state.verified_ranges or {}), "recovery": recovery}
        state.save(update_fields=["verified_ranges"])
    return True


def schedule_private_recoveries(limit=5, *, row_limit=200):
    """Schedule at most one conversation per owner, rotating durable owner turns.

    Never replay erased bodies or interrupt a partially imported window. A
    qualified new source scan either rebuilds a row under current authority or
    records an absent row as a superseded tombstone. Permanent failures remain
    blocked until their cause is resolved explicitly.
    """
    from integrations.services import slack_dm_mirror as dm

    from .private_coverage import coverage_attempt_rows, recent_conversations
    recent_ids = recent_conversations(SlackDmMirrorConversation.objects.all()).values('pk')
    candidates = SlackDmMirrorConversation.objects.annotate(
        coverage_current=Exists(coverage_attempt_rows()),
    ).filter(
        status="live", grant__status="active", grant__revoked_at__isnull=True,
        grant__connection__status__in=["connected", "syncing"],
        sync_state__isnull=False,
        history_backfilled_at__gte=F("grant__consented_at"),
    ).exclude(sync_state__status__in=["paused", "revoked"]).exclude(grant__connection__access_token="").filter(
        Q(Exists(recoverable_rows().filter(conversation_id=OuterRef("pk"))))
        | Q(coverage_current=False, pk__in=Subquery(recent_ids)),
        ~Exists(BridgeSyncJob.objects.filter(state__private_conversation_id=OuterRef("pk"), lease_expires_at__gt=timezone.now())),
    )
    # Read turns from ALL owner states, including conversations already removed
    # from the candidate set. Otherwise a busy owner's next old chat jumps ahead.
    turns = BridgeSyncState.objects.filter(
        private_conversation__grant__user_id=OuterRef("grant__user_id"),
    ).annotate(recovery_turn=Func(
        F("verified_ranges"), Value("recovery"), Value("considered_at"),
        function="jsonb_extract_path_text", output_field=CharField(),
    )).order_by(F("recovery_turn").desc(nulls_last=True)).values("recovery_turn")[:1]
    owners = candidates.annotate(recovery_turn=Subquery(turns)).values(
        "grant__user_id", "recovery_turn",
    ).annotate(oldest=Min("history_backfilled_at")).order_by(
        F("recovery_turn").asc(nulls_first=True), "oldest", "grant__user_id",
    )[:max(1, min(int(limit), 20))]
    scheduled = 0
    for owner in owners:
        candidate = candidates.filter(grant__user_id=owner["grant__user_id"]).select_related(
            "grant__connection",
        ).annotate(conversation_turn=Func(
            F("sync_state__verified_ranges"), Value("recovery"), Value("considered_at"),
            function="jsonb_extract_path_text", output_field=CharField(),
        )).order_by(F("conversation_turn").asc(nulls_first=True), "history_backfilled_at", "id").first()
        if candidate is None:
            continue
        if not _record_attempt(candidate.pk):
            continue
        try:
            authority = dm._capture_slack_grant_api_authority(candidate.grant, refresh_token=False)
            scopes = dm._history_required_scopes(candidate.slack_conversation_id, kind=dm.conversation_kind(candidate))
            with transaction.atomic():
                conversation, grant = dm._locked_history_write_context(candidate.pk, candidate.grant_id, authority, scopes)
                if conversation.history_backfilled_at is None or conversation.history_backfilled_at < grant.consented_at:
                    continue
                state = BridgeSyncState.objects.select_for_update().filter(private_conversation=conversation).first()
                if state is None or state.status in {"paused", "revoked"}:
                    continue
                now = timezone.now()
                recovery = (state.verified_ranges or {}).get("recovery", {})
                if (state.jobs.filter(lease_expires_at__gt=now).exists()
                        or state.jobs.exclude(checkpoint={}).exists()
                        or conversation.deliveries.filter(source_platform="slack", metadata__history_scan_state__in=["main", "thread"]).exists()):
                    continue
                registration = dm._ensure_current_registration_row_locked(conversation, grant)
                if registration is None or dm._registration_state(registration) != dm.REGISTRATION_STATE_ACTIVE:
                    continue
                rows = list(recoverable_rows().filter(conversation=conversation).select_for_update().defer(
                    "encrypted_text",
                ).order_by("id")[:max(1, min(int(row_limit), 1000))])
                needs_coverage = (
                    not SlackDmMirrorConversation.objects.filter(pk=conversation.pk).filter(Exists(coverage_attempt_rows())).exists()
                    and recent_conversations(SlackDmMirrorConversation.objects.filter(pk=conversation.pk)).exists()
                )
                if not rows and not needs_coverage:
                    continue
                current_rows = []
                excluded_rows = 0
                for row in rows:
                    row.conversation = conversation
                    if (row.metadata or {}).get("permanent_failure"):
                        from .reaction_recovery import CONTRACT_KEY, legacy_failure_eligible
                        if legacy_failure_eligible(row, conversation):
                            row.metadata = {**row.metadata, CONTRACT_KEY: {
                                "failed_at": row.updated_at.isoformat(), "error_code": "adapter_http_400",
                                "contract": "pre_145_unicode_reaction",
                            }}
                        else:
                            from .parent_recovery import CONTRACT_KEY as PARENT_KEY, stale_parent_failure_audit
                            audit = stale_parent_failure_audit(row, conversation)
                            if audit is None:
                                continue
                            row.metadata = {**row.metadata, "backfill": True, PARENT_KEY: audit}
                    if dm._backfill_delivery_is_outside_history_window(row, now=now, history_days=dm._grant_history_days(grant)):
                        # Old terminal rows cannot block a currently active chat.
                        # This erases data and records exclusion; it sends nothing.
                        dm._complete_outside_history_window_delivery_locked(row, now=now)
                        excluded_rows += 1
                        continue
                    row.metadata = {**(row.metadata or {}), "history_recovery_scheduled": True}
                    row.encrypted_text = ""
                    row.updated_at = now
                    current_rows.append(row)
                if not current_rows and not excluded_rows and not needs_coverage:
                    continue
                if current_rows:
                    SlackDmMirrorDelivery.objects.bulk_update(current_rows, ["metadata", "updated_at", "encrypted_text"])
                if current_rows or needs_coverage:
                    dm._mark_conversation_history_due(
                        conversation, reason="Source recovery for erased backfill rows",
                        reset_deliveries=False,
                    )
                    # The state lock excludes concurrent claims; preserve all
                    # cursors and provider budgets, only make archive work due.
                    state.jobs.filter(kind="archive", due_at__gt=now).update(due_at=now)
                # History scheduling can qualify publication through another
                # locked state instance; never replace its newer evidence.
                state.refresh_from_db(fields=["verified_ranges"])
                state.verified_ranges = {**state.verified_ranges, "recovery": {**recovery, "scheduled_at": now.isoformat()}}
                state.save(update_fields=["verified_ranges"])
                scheduled += 1
        except dm.SlackDmMirrorAuthorizationError:
            # Reauthorization/disconnect won. Do not alter its outbox or scan.
            continue
    return scheduled
