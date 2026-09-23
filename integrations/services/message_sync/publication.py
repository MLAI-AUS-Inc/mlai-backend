"""Durable availability of an imported private history, separate from freshness.

The caller of each write helper holds the grant and conversation authority locks.
No message bodies or credentials are retained here. Catalog readers only inspect
the exact scoped proof; fresh scans cannot grant publication before delivery.
"""

from django.utils import timezone

from integrations.models import BridgeSyncState, SlackDmMirrorConversation, SlackDmMirrorDelivery

PUBLICATION_KEY = "publication"


def _boundary(conversation):
    from integrations.services.slack_dm_mirror import _grant_history_days
    from integrations.services.slack_dm_registration_ledger import grant_consent_generation

    grant = conversation.grant
    return {
        "user_id": grant.user_id,
        "grant_id": grant.pk,
        "connection_id": grant.connection_id,
        "grant_slack_workspace_id": grant.slack_workspace_id,
        "grant_slack_user_id": grant.slack_user_id,
        "consent_generation": grant_consent_generation(grant),
        "consent_version": grant.consent_version,
        "channel_id": str(conversation.mlai_channel_id or ""),
        "slack_workspace_id": conversation.slack_workspace_id,
        "slack_conversation_id": conversation.slack_conversation_id,
        "participant_slack_ids": sorted(conversation.participant_slack_ids or []),
        "history_days": _grant_history_days(grant),
    }


def _audience(conversation):
    from integrations.services.slack_chat_catalog import _publication_key

    return {
        "scope": _publication_key(conversation),
        "participant_hash": conversation.participant_hash,
        "participant_pubkeys": sorted(conversation.participant_buzz_pubkeys or []),
    }


def record_publication_locked(conversation):
    """Qualify once after a complete current-room scan and all deliveries drain.

    Writers already serialize with consent/device changes on grant→conversation.
    Re-read the complete source/outbox qualification under that boundary instead
    of trusting an earlier callback or a cached display decision.
    """
    from integrations.services.slack_chat_catalog import catalog_conversations

    state = BridgeSyncState.objects.select_for_update().filter(private_conversation=conversation).first()
    if state is None:
        return False
    existing = (state.verified_ranges or {}).get(PUBLICATION_KEY) or {}
    if (conversation.grant.status == "active" and conversation.grant.revoked_at is None
            and conversation.status == "live" and conversation.mlai_channel_id
            and existing.get("scope") == _audience(conversation)["scope"]):
        return True
    current = catalog_conversations(
        SlackDmMirrorConversation.objects.filter(pk=conversation.pk),
    ).prefetch_related(None).get()
    # The caller already holds this grant's authority lock. Publication needs
    # its consent fields, not another copy of the full workspace catalogue.
    current.grant = conversation.grant
    grant = current.grant
    if grant.status != "active" or grant.revoked_at is not None or current.status != "live" or not current.mlai_channel_id:
        return False
    audience = _audience(current)
    if (current.history_backfilled_at is None or current.history_backfilled_at < grant.consented_at
            or not current._import_verified or current._import_pending or current._import_limited):
        return False
    ranges = dict(state.verified_ranges or {})
    ranges[PUBLICATION_KEY] = {**_boundary(current), **audience, "published_at": timezone.now().isoformat()}
    state.verified_ranges = ranges
    state.save(update_fields=["verified_ranges"])
    return True


def invalidate_publication_locked(conversation):
    """Invalidate an explicit reset or withdrawn source/consent under its locks."""
    state = BridgeSyncState.objects.select_for_update().filter(private_conversation=conversation).first()
    if state is None or PUBLICATION_KEY not in (state.verified_ranges or {}):
        return
    ranges = dict(state.verified_ranges)
    ranges.pop(PUBLICATION_KEY, None)
    state.verified_ranges = ranges
    state.save(update_fields=["verified_ranges"])


def publication_for_transition(conversation):
    """Freeze only an exact publication proof for a device-only room transition."""
    from integrations.services.slack_dm_registration_ledger import (
        REGISTRATION_STATE_PREFIX, grant_consent_generation,
        registration_generation, registration_slack_participant_ids,
    )

    state = BridgeSyncState.objects.select_for_update().filter(private_conversation=conversation).first()
    proof = dict((state.verified_ranges or {}).get(PUBLICATION_KEY) or {}) if state else {}
    if not proof or any(proof.get(key) != value for key, value in _boundary(conversation).items()):
        return None
    if proof.get("scope") == _audience(conversation)["scope"]:
        return proof
    # Preparation updates the audience before I/O. A retry may reuse its frozen
    # proof only while the same consent, source membership and room still apply.
    for attempt in SlackDmMirrorDelivery.objects.filter(
        conversation=conversation, source_message_id__startswith=REGISTRATION_STATE_PREFIX,
        metadata__private_audience__publication_proof=proof,
    ):
        if (registration_generation(attempt) == grant_consent_generation(conversation.grant)
                and registration_slack_participant_ids(attempt) == sorted(conversation.participant_slack_ids or [])
                and (attempt.metadata or {}).get("private_audience", {}).get("channel_id") == str(conversation.mlai_channel_id)):
            return proof
    return None


def rebind_publication_locked(conversation, registration):
    """Carry an unchanged frozen proof only after the relay audience CAS succeeds."""
    proof = (registration.metadata or {}).get("private_audience", {}).get("publication_proof")
    if not proof or any(proof.get(key) != value for key, value in _boundary(conversation).items()):
        return
    state = BridgeSyncState.objects.select_for_update().filter(private_conversation=conversation).first()
    if state is None or (state.verified_ranges or {}).get(PUBLICATION_KEY) != proof:
        return  # Reset, retirement or a newer proof won while relay I/O was pending.
    state.verified_ranges = {**state.verified_ranges, PUBLICATION_KEY: {**proof, **_audience(conversation)}}
    state.save(update_fields=["verified_ranges"])
