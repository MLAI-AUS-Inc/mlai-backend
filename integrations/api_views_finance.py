from __future__ import annotations

import hashlib
import hmac
import json
import time

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import permissions, serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import ContentFactoryRun, ContentFactoryRunStatus, Organization
from core.permissions import HasRooApiKey
from integrations.models import ExternalServiceConnection, ExternalServiceConnectionStatus, ExternalServiceProvider
from integrations.services.finance import (
    FINANCIAL_METRIC_SOURCE,
    FINANCIAL_MONTHLY_METRICS_WORKFLOW,
    calculate_and_publish_monthly_revenue,
    create_financial_sync_run,
    enqueue_financial_sync_run,
    get_financial_status,
    serialize_revenue_snapshot,
    sync_next_financial_page,
)
from integrations.services.valley_harness import notify_valley_run_created
from integrations.utils import normalize_domain
from startup_updates.models import StartupMetricObservation, UserStartupBinding

User = get_user_model()


class FinancialSyncCreateSerializer(serializers.Serializer):
    domain = serializers.CharField(required=False, allow_blank=True)
    user_id = serializers.IntegerField(required=False, allow_null=True)
    connection_ids = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    trigger = serializers.CharField(required=False, allow_blank=True, default="manual")


class FinancialStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        organization = _optional_authorized_organization(request, request.query_params.get("domain"))
        return Response(get_financial_status(organization=organization, user=request.user), status=status.HTTP_200_OK)


class FinancialSyncView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        organization = _resolve_financial_sync_organization(request)
        connection_ids = list(request.data.get("connection_ids") or request.data.get("connectionIds") or [])
        try:
            run, created = create_financial_sync_run(
                organization=organization,
                user=request.user,
                connection_ids=connection_ids,
                trigger="manual",
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        if created:
            transaction.on_commit(lambda: notify_valley_run_created(run.run_id))

        return Response(
            {
                "run_id": run.run_id,
                "runId": run.run_id,
                "status": run.status,
                "current_step": run.current_step,
                "currentStep": run.current_step,
                "reused_existing_run": not created,
                "reusedExistingRun": not created,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class FinancialConnectionDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, connection_id: int):
        connection = get_object_or_404(
            ExternalServiceConnection.objects.select_related("organization", "user"),
            id=connection_id,
        )
        if not _user_can_access_organization(request.user, connection.organization):
            return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        connection.status = ExternalServiceConnectionStatus.DISCONNECTED
        connection.access_token = ""
        connection.refresh_token = ""
        connection.last_error = ""
        connection.provider_metadata = {}
        connection.sync_cursor = {}
        connection.save(
            update_fields=[
                "status",
                "access_token",
                "refresh_token",
                "last_error",
                "provider_metadata",
                "sync_cursor",
                "updated_at",
            ]
        )
        return Response({"status": "disconnected", "connection_id": connection.id, "connectionId": connection.id}, status=status.HTTP_200_OK)


class FinancialRunCreateView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        serializer = FinancialSyncCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        organization = get_object_or_404(Organization, domain=normalize_domain(data.get("domain") or request.data.get("domain") or ""))
        user_id = data.get("user_id") or request.data.get("user_id")
        user = get_object_or_404(User, pk=user_id) if user_id else None
        if user is None:
            connection = organization.external_service_connections.order_by("-updated_at").first()
            user = connection.user if connection else None
        if user is None:
            return Response({"error": "user_id is required when no financial connection exists."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            run, created = create_financial_sync_run(
                organization=organization,
                user=user,
                connection_ids=data.get("connection_ids") or None,
                trigger=data.get("trigger") or "internal",
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        if created:
            transaction.on_commit(lambda: notify_valley_run_created(run.run_id))

        return Response(
            {
                "run_id": run.run_id,
                "runId": run.run_id,
                "run": _serialize_financial_run(run),
                "reused_existing_run": not created,
                "reusedExistingRun": not created,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class FinancialRunSyncNextPageView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        run = _get_financial_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response

        _update_run_step(run, step_key="financial_backfill")
        result = sync_next_financial_page(run=run)
        run.refresh_from_db()
        return Response({"run": _serialize_financial_run(run), **result}, status=status.HTTP_200_OK)


class FinancialRunCalculateView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        run = _get_financial_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response

        _update_run_step(run, step_key="revenue_calculation")
        result = calculate_and_publish_monthly_revenue(run=run)
        run.refresh_from_db()
        return Response({"run": _serialize_financial_run(run), **result}, status=status.HTTP_200_OK)


class FinancialRunSnapshotsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        run = _get_financial_run_or_404(run_id)
        organization = get_object_or_404(Organization, id=(run.run_request or {}).get("organization_id"))
        metrics = (
            StartupMetricObservation.objects.filter(
                organization=organization,
                source_provider=FINANCIAL_METRIC_SOURCE,
            )
            .order_by("-period_month", "metric_key")[:48]
        )
        return Response(
            {
                "run": _serialize_financial_run(run),
                "snapshots": [serialize_revenue_snapshot(metric) for metric in metrics],
            },
            status=status.HTTP_200_OK,
        )


class StripeFinancialWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    RELEVANT_EVENTS = {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.paid",
        "invoice.payment_failed",
        "checkout.session.completed",
    }

    def post(self, request):
        payload = request.body or b""
        signature_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")
        webhook_secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", "")
        if not webhook_secret:
            return Response({"error": "Stripe webhook secret is not configured."}, status=status.HTTP_400_BAD_REQUEST)
        if not _valid_stripe_signature(payload, signature_header, webhook_secret):
            return Response({"error": "Invalid Stripe signature."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            event = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return Response({"error": "Invalid JSON payload."}, status=status.HTTP_400_BAD_REQUEST)

        event_type = str(event.get("type") or "")
        if event_type not in self.RELEVANT_EVENTS:
            return Response({"status": "ignored", "event_type": event_type}, status=status.HTTP_200_OK)

        stripe_account_id = str(event.get("account") or "").strip()
        if not stripe_account_id:
            return Response({"status": "ignored", "reason": "missing_connected_account"}, status=status.HTTP_200_OK)

        connections = list(
            ExternalServiceConnection.objects.select_related("organization", "user").filter(
                provider=ExternalServiceProvider.STRIPE,
                external_account_id=stripe_account_id,
                status=ExternalServiceConnectionStatus.CONNECTED,
            )
        )
        run_ids = []
        for connection in connections:
            if connection.organization_id is None:
                continue
            run = enqueue_financial_sync_run(
                organization=connection.organization,
                user=connection.user,
                trigger=f"stripe_webhook:{event_type}",
            )
            if run is not None:
                run_ids.append(run.run_id)

        return Response(
            {
                "status": "accepted",
                "event_type": event_type,
                "connection_count": len(connections),
                "connectionCount": len(connections),
                "run_ids": run_ids,
                "runIds": run_ids,
            },
            status=status.HTTP_202_ACCEPTED,
        )


def _optional_authorized_organization(request, raw_domain) -> Organization | None:
    if not str(raw_domain or "").strip():
        return None
    return _get_authorized_organization(request, raw_domain)


def _resolve_financial_sync_organization(request) -> Organization:
    # The web client posts {providers} with no domain, so resolve the
    # organization from the user like the other connector endpoints do.
    raw_domain = request.data.get("domain")
    if str(raw_domain or "").strip():
        return _get_authorized_organization(request, raw_domain)

    connection = (
        ExternalServiceConnection.objects.select_related("organization")
        .filter(
            user=request.user,
            organization__isnull=False,
            provider__in=[
                ExternalServiceProvider.XERO,
                ExternalServiceProvider.STRIPE,
                ExternalServiceProvider.BANK_FEED,
            ],
        )
        .exclude(status=ExternalServiceConnectionStatus.DISCONNECTED)
        .order_by("-updated_at", "-id")
        .first()
    )
    if connection is not None and connection.organization is not None:
        return connection.organization

    binding = (
        UserStartupBinding.objects.select_related("organization")
        .filter(user=request.user)
        .order_by("-updated_at", "-id")
        .first()
    )
    if binding is not None and binding.organization is not None:
        return binding.organization

    raise serializers.ValidationError({"domain": "domain is required."})


def _get_authorized_organization(request, raw_domain) -> Organization:
    domain = normalize_domain(raw_domain or "")
    if not domain:
        raise serializers.ValidationError({"domain": "domain is required."})
    organization = get_object_or_404(Organization, domain=domain)
    if not _user_can_access_organization(request.user, organization):
        raise serializers.ValidationError({"domain": "No startup binding or financial connection found for this user and domain."})
    return organization


def _user_can_access_organization(user, organization: Organization | None) -> bool:
    if organization is None:
        return False
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if organization.user_startup_bindings.filter(user=user).exists():
        return True
    return organization.external_service_connections.filter(user=user).exists()


def _get_financial_run_or_404(run_id: str) -> ContentFactoryRun:
    return get_object_or_404(
        ContentFactoryRun,
        run_id=run_id,
        workflow=FINANCIAL_MONTHLY_METRICS_WORKFLOW,
    )


def _reject_if_run_cancelled(run: ContentFactoryRun):
    if run.status != ContentFactoryRunStatus.CANCELLED:
        return None
    return Response(
        {
            "error": "run_cancelled",
            "detail": "This financial sync run was cancelled and cannot accept more workflow writes.",
            "run_id": run.run_id,
            "status": run.status,
        },
        status=status.HTTP_409_CONFLICT,
    )


def _update_run_step(run: ContentFactoryRun, *, step_key: str) -> None:
    if run.status == ContentFactoryRunStatus.CANCELLED:
        return
    if run.status == ContentFactoryRunStatus.QUEUED:
        run.status = ContentFactoryRunStatus.RUNNING
    run.current_step = step_key
    run.save(update_fields=["status", "current_step", "updated_at"])


def _serialize_financial_run(run: ContentFactoryRun) -> dict:
    return {
        "run_id": run.run_id,
        "runId": run.run_id,
        "workflow": run.workflow,
        "domain": run.domain,
        "status": run.status,
        "current_step": run.current_step,
        "currentStep": run.current_step,
        "run_request": run.run_request or {},
        "runRequest": run.run_request or {},
        "result": run.result or {},
        "step_order": run.step_order or [],
        "stepOrder": run.step_order or [],
        "created_at": run.created_at.isoformat(),
        "createdAt": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "updatedAt": run.updated_at.isoformat(),
    }


def _valid_stripe_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    values = {}
    for part in str(signature_header or "").split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        values.setdefault(key, []).append(value)
    timestamps = values.get("t") or []
    signatures = values.get("v1") or []
    if not timestamps or not signatures:
        return False
    try:
        timestamp = int(timestamps[0])
    except (TypeError, ValueError):
        return False
    if abs(time.time() - timestamp) > 300:
        return False
    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, signature) for signature in signatures)
