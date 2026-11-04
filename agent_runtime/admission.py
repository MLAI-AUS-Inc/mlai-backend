"""Resolve signed Slack command identities to existing explicit member grants."""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from .models import AgentGrant, AgentTask
from .runtime import enqueue_task, _grant
from .sources import parse_resource, slack_authority


def unique_identity(values, *, empty_code, ambiguous_code):
    values = list(dict.fromkeys(values))
    if not values:
        raise PermissionDenied(empty_code)
    if len(values) != 1:
        raise PermissionDenied(ambiguous_code)
    return values[0]


def admit_slack_command(*, team_id, user_id, channel_id, source_event_id, instruction):
    from integrations.models import ExternalServiceConnection, ExternalServiceProvider, ExternalServiceConnectionStatus
    resource = f"slack:{team_id}:{channel_id}"
    parse_resource(resource)
    if not source_event_id.startswith(f"slack:{team_id}:"):
        raise ValidationError("Source event identity does not match the workspace.")
    candidates = ExternalServiceConnection.objects.filter(
        provider=ExternalServiceProvider.SLACK, organization__isnull=True,
        external_account_id=team_id, provider_metadata__authed_user__id=user_id,
        status__in=(ExternalServiceConnectionStatus.CONNECTED, ExternalServiceConnectionStatus.SYNCING),
        user__is_active=True,
    ).values_list("user_id", flat=True).distinct()[:2]
    owner_id = unique_identity(candidates, empty_code="no_agent_connection", ambiguous_code="ambiguous_slack_account")
    with transaction.atomic():
        owner = get_user_model().objects.select_for_update().get(pk=owner_id)
        if not owner.is_active:
            raise PermissionDenied("account_inactive")
        # Resolve again inside the identity fence. Multiple accounts are never
        # guessed from a display name or caller-supplied user/grant identifier.
        current_ids = list(ExternalServiceConnection.objects.filter(
            provider=ExternalServiceProvider.SLACK, organization__isnull=True,
            external_account_id=team_id, provider_metadata__authed_user__id=user_id,
            status__in=(ExternalServiceConnectionStatus.CONNECTED, ExternalServiceConnectionStatus.SYNCING),
            user__is_active=True,
        ).values_list("user_id", flat=True).distinct()[:2])
        if unique_identity(current_ids, empty_code="no_agent_connection", ambiguous_code="ambiguous_slack_account") != owner_id:
            raise PermissionDenied("slack_account_changed")
        command_key = f"slack-command:{source_event_id}"
        prior = list(AgentTask.objects.filter(idempotency_key=command_key).select_related(
            "grant__installation").order_by("created_at")[:2])
        if prior:
            if (len(prior) != 1 or prior[0].grant.installation.owner_id != owner_id
                    or prior[0].resource != resource or prior[0].instruction != instruction.strip()):
                raise PermissionDenied("command_idempotency_conflict")
            with slack_authority(owner, resource, expected_slack_user=user_id):
                return prior[0], False
        eligible = []
        grants = list(AgentGrant.objects.filter(installation__owner=owner,
                installation__is_active=True, installation__runtime="hermes",
                revoked_at__isnull=True, expires_at__gt=timezone.now()).order_by("pk")[:101])
        if len(grants) > 100:
            raise PermissionDenied("too_many_agent_grants")
        for grant in grants:
            if resource in grant.resources and {"tasks:write", "messages:read"}.issubset(set(grant.scopes)):
                eligible.append(grant.pk)
                if len(eligible) > 1:
                    break
        grant_id = unique_identity(eligible, empty_code="no_agent_connection", ambiguous_code="ambiguous_agent")
        grant = _grant(grant_id)
        def actor_access(current_owner, scope, selected_resource):
            if current_owner.pk != owner_id or selected_resource != resource or scope != "messages:read":
                return False
            try:
                with slack_authority(current_owner, selected_resource, expected_slack_user=user_id):
                    return True
            except PermissionDenied:
                return False
        task = enqueue_task(grant, instruction=instruction,
            idempotency_key=command_key, resource=resource,
            access_checker=actor_access)
        return task, True
