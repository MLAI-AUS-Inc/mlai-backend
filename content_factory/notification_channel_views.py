"""User-facing notification channel management for the daily research reminder.

Mounted under /api/v1/vibe-marketing/notifications/ (session user auth).
Channel verification rules live in integrations.services.notification_channels;
these views only resolve the founder context and translate errors to HTTP.
"""

import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from content_factory.models import (
    AutomationRun,
    AutomationRunStatus,
    NotificationChannel,
    NotificationChannelType,
    NotificationConsentState,
    NotificationDelivery,
    NotificationDeliveryStatus,
    ResearchAutomation,
    ResearchAutomationStatus,
)
from integrations.services.notification_adapters import pause_automations_if_no_active_channels
from integrations.services.research_automations import start_manual_automation_run
from integrations.services.notification_channels import (
    ChannelActionError,
    deactivate_channel,
    ensure_research_automation_for_org,
    initiate_email_channel,
    initiate_whatsapp_channel,
    link_slack_channel,
    list_org_channels,
    send_email_verification,
    send_whatsapp_otp,
    serialize_automation,
    serialize_channel,
    verify_whatsapp_otp,
)

from .vibe_marketing_views import _get_config, _resolve_context_or_response


logger = logging.getLogger(__name__)


def _channel_error_response(exc: ChannelActionError) -> Response:
    payload = {"detail": exc.detail, "code": exc.code}
    if "retry_after_seconds" in exc.extra:
        payload["retryAfterSeconds"] = exc.extra["retry_after_seconds"]
    return Response(payload, status=exc.http_status)


def _org_automation(organization):
    return ResearchAutomation.objects.filter(organization=organization).order_by("created_at").first()


def _channels_payload(organization) -> dict:
    automation = _org_automation(organization)
    primary_id = automation.notification_channel_id if automation else None
    config = _get_config(organization)
    return {
        "channels": [
            serialize_channel(channel, primary_channel_id=primary_id)
            for channel in list_org_channels(organization)
        ],
        "automation": serialize_automation(automation),
        "dailyDiscoveryEnabled": bool(config.daily_discovery_enabled),
    }


def _channel_or_none(organization, channel_id):
    from content_factory.models import NotificationChannel

    return NotificationChannel.objects.filter(id=channel_id, organization=organization).first()


class VibeMarketingNotificationChannelsView(APIView):
    def get(self, request):
        context, error = _resolve_context_or_response(request)
        if error:
            return error
        return Response(_channels_payload(context.organization), status=status.HTTP_200_OK)

    def post(self, request):
        context, error = _resolve_context_or_response(request)
        if error:
            return error
        channel_type = str(
            request.data.get("channelType") or request.data.get("channel_type") or ""
        ).strip().lower()
        route_id = str(request.data.get("routeId") or request.data.get("route_id") or "").strip()
        organization = context.organization

        try:
            if channel_type == NotificationChannelType.EMAIL:
                channel = initiate_email_channel(
                    organization=organization, user=request.user, route_id=route_id
                )
                if channel.consent_state == NotificationConsentState.ACTIVE:
                    result_status = "active"
                else:
                    send_email_verification(channel)
                    channel.refresh_from_db()
                    result_status = "verification_sent"
            elif channel_type == NotificationChannelType.WHATSAPP:
                channel = initiate_whatsapp_channel(
                    organization=organization, user=request.user, phone=route_id
                )
                if channel.consent_state == NotificationConsentState.ACTIVE:
                    result_status = "active"
                else:
                    send_whatsapp_otp(channel)
                    channel.refresh_from_db()
                    result_status = "otp_sent"
            elif channel_type == NotificationChannelType.SLACK:
                channel = link_slack_channel(
                    organization=organization,
                    user=request.user,
                    config=_get_config(organization),
                )
                result_status = "active"
            else:
                return Response(
                    {"detail": "channelType must be slack, whatsapp, or email.", "code": "invalid_channel_type"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except ChannelActionError as exc:
            return _channel_error_response(exc)

        automation = _org_automation(organization)
        return Response(
            {
                "channel": serialize_channel(
                    channel,
                    primary_channel_id=automation.notification_channel_id if automation else None,
                ),
                "status": result_status,
            },
            status=status.HTTP_200_OK,
        )


class VibeMarketingNotificationChannelVerifyView(APIView):
    def post(self, request, channel_id):
        context, error = _resolve_context_or_response(request)
        if error:
            return error
        channel = _channel_or_none(context.organization, channel_id)
        if channel is None:
            return Response({"detail": "Channel not found."}, status=status.HTTP_404_NOT_FOUND)
        if channel.consent_state == NotificationConsentState.ACTIVE:
            return Response(
                {"channel": serialize_channel(channel), "status": "active"},
                status=status.HTTP_200_OK,
            )
        if channel.channel_type != NotificationChannelType.WHATSAPP:
            return Response(
                {"detail": "Only WhatsApp channels verify with a code.", "code": "invalid_channel_type"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            verify_whatsapp_otp(channel, str(request.data.get("code") or ""))
        except ChannelActionError as exc:
            return _channel_error_response(exc)
        return Response(
            {"channel": serialize_channel(channel), "status": "active"},
            status=status.HTTP_200_OK,
        )


class VibeMarketingNotificationChannelResendView(APIView):
    def post(self, request, channel_id):
        context, error = _resolve_context_or_response(request)
        if error:
            return error
        channel = _channel_or_none(context.organization, channel_id)
        if channel is None:
            return Response({"detail": "Channel not found."}, status=status.HTTP_404_NOT_FOUND)
        if channel.consent_state == NotificationConsentState.ACTIVE:
            return Response(
                {"channel": serialize_channel(channel), "status": "active"},
                status=status.HTTP_200_OK,
            )
        try:
            if channel.channel_type == NotificationChannelType.WHATSAPP:
                send_whatsapp_otp(channel)
                result_status = "otp_sent"
            elif channel.channel_type == NotificationChannelType.EMAIL:
                send_email_verification(channel)
                result_status = "verification_sent"
            else:
                return Response(
                    {"detail": "This channel type has nothing to resend.", "code": "invalid_channel_type"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except ChannelActionError as exc:
            return _channel_error_response(exc)
        channel.refresh_from_db()
        return Response(
            {"channel": serialize_channel(channel), "status": result_status},
            status=status.HTTP_200_OK,
        )


class VibeMarketingNotificationChannelDetailView(APIView):
    def patch(self, request, channel_id):
        """Toggle whether this channel receives the daily research reminder.

        Flips delivery_enabled without touching consent_state, so unchecking a
        channel excludes it from delivery while keeping its verification intact.
        """
        context, error = _resolve_context_or_response(request)
        if error:
            return error
        organization = context.organization
        channel = _channel_or_none(organization, channel_id)
        if channel is None:
            return Response({"detail": "Channel not found."}, status=status.HTTP_404_NOT_FOUND)

        raw = request.data.get("deliveryEnabled")
        if raw is None:
            raw = request.data.get("delivery_enabled")
        if raw is None:
            return Response(
                {"detail": "deliveryEnabled is required.", "code": "missing_delivery_enabled"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        enabled = str(raw).strip().lower() not in {"false", "0", "no", "off", ""}

        # Only a consented, verified channel can be a delivery target. Pending or
        # revoked channels use the connect/verify flow instead of this toggle.
        if channel.consent_state != NotificationConsentState.ACTIVE:
            return Response(
                {
                    "detail": "Connect and verify this channel before choosing it for delivery.",
                    "code": "channel_not_active",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Last-channel guard: a checkbox must never silently drop the org to zero
        # delivery targets — the daily run would fan out to nobody yet still spend a
        # discovery. Turning the reminder off entirely is the separate Enabled toggle.
        if not enabled and channel.delivery_enabled:
            others_enabled = (
                NotificationChannel.objects.filter(
                    organization=organization,
                    consent_state=NotificationConsentState.ACTIVE,
                    delivery_enabled=True,
                )
                .exclude(pk=channel.pk)
                .exists()
            )
            if not others_enabled:
                return Response(
                    {
                        "detail": (
                            "Keep at least one channel on, or turn the daily reminder "
                            "off with the Enabled toggle."
                        ),
                        "code": "last_delivery_channel",
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        if channel.delivery_enabled != enabled:
            channel.delivery_enabled = enabled
            channel.save(update_fields=["delivery_enabled", "updated_at"])

        return Response(_channels_payload(organization), status=status.HTTP_200_OK)

    def delete(self, request, channel_id):
        context, error = _resolve_context_or_response(request)
        if error:
            return error
        channel = _channel_or_none(context.organization, channel_id)
        if channel is None:
            return Response({"detail": "Channel not found."}, status=status.HTTP_404_NOT_FOUND)
        deactivate_channel(channel)
        automation_paused = pause_automations_if_no_active_channels(context.organization)
        return Response(
            {"channel": serialize_channel(channel), "automationPaused": automation_paused},
            status=status.HTTP_200_OK,
        )


class VibeMarketingNotificationChannelDeliveryView(APIView):
    """Turn daily-reminder delivery on/off for a whole channel TYPE.

    Unlike the id-based toggle (which needs an already-connected channel), this
    connects on first enable where the method allows it: Slack links instantly,
    Email sends a verification link (delivery starts once confirmed), and WhatsApp
    still needs its number + OTP setup first. Disable flips delivery_enabled off
    for the type's active channels, keeping the connection intact.
    """

    _TYPES = frozenset(
        {
            NotificationChannelType.SLACK,
            NotificationChannelType.EMAIL,
            NotificationChannelType.WHATSAPP,
        }
    )

    def post(self, request):
        context, error = _resolve_context_or_response(request)
        if error:
            return error
        organization = context.organization
        channel_type = str(
            request.data.get("channelType") or request.data.get("channel_type") or ""
        ).strip().lower()
        if channel_type not in self._TYPES:
            return Response(
                {"detail": "channelType must be slack, whatsapp, or email.", "code": "invalid_channel_type"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        raw = request.data.get("enabled")
        if raw is None:
            return Response(
                {"detail": "enabled is required.", "code": "missing_enabled"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        enabled = str(raw).strip().lower() not in {"false", "0", "no", "off", ""}
        if enabled:
            return self._enable(request, organization, channel_type)
        return self._disable(organization, channel_type)

    def _enable(self, request, organization, channel_type):
        active = list(
            NotificationChannel.objects.filter(
                organization=organization,
                channel_type=channel_type,
                consent_state=NotificationConsentState.ACTIVE,
            )
        )
        if active:
            for channel in active:
                if not channel.delivery_enabled:
                    channel.delivery_enabled = True
                    channel.save(update_fields=["delivery_enabled", "updated_at"])
            return self._payload(organization, "active")

        # No active channel of this type yet — connect where the method allows it.
        try:
            if channel_type == NotificationChannelType.SLACK:
                link_slack_channel(
                    organization=organization,
                    user=request.user,
                    config=_get_config(organization),
                )
                result_status = "active"
            elif channel_type == NotificationChannelType.EMAIL:
                channel = initiate_email_channel(
                    organization=organization,
                    user=request.user,
                    route_id=str(request.data.get("routeId") or request.data.get("route_id") or ""),
                )
                if channel.consent_state == NotificationConsentState.ACTIVE:
                    result_status = "active"
                else:
                    send_email_verification(channel)
                    result_status = "verification_sent"
            else:  # whatsapp needs the number + OTP setup flow first
                return Response(
                    {
                        "detail": "Add a WhatsApp number first to receive daily reminders there.",
                        "code": "whatsapp_setup_required",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except ChannelActionError as exc:
            return _channel_error_response(exc)
        return self._payload(organization, result_status)

    def _disable(self, organization, channel_type):
        active_enabled = list(
            NotificationChannel.objects.filter(
                organization=organization,
                channel_type=channel_type,
                consent_state=NotificationConsentState.ACTIVE,
                delivery_enabled=True,
            )
        )
        if not active_enabled:
            return self._payload(organization, "disabled")  # already off / not connected

        # Last-channel guard: never drop the org to zero delivery targets via a
        # checkbox. At least one active+enabled channel of another type must remain.
        others_enabled = (
            NotificationChannel.objects.filter(
                organization=organization,
                consent_state=NotificationConsentState.ACTIVE,
                delivery_enabled=True,
            )
            .exclude(channel_type=channel_type)
            .exists()
        )
        if not others_enabled:
            return Response(
                {
                    "detail": (
                        "Keep at least one channel on, or turn the daily reminder "
                        "off with the Enabled toggle."
                    ),
                    "code": "last_delivery_channel",
                },
                status=status.HTTP_409_CONFLICT,
            )
        for channel in active_enabled:
            channel.delivery_enabled = False
            channel.save(update_fields=["delivery_enabled", "updated_at"])
        return self._payload(organization, "disabled")

    @staticmethod
    def _payload(organization, result_status):
        return Response(
            {**_channels_payload(organization), "status": result_status},
            status=status.HTTP_200_OK,
        )


class VibeMarketingResearchAutomationView(APIView):
    def get(self, request):
        context, error = _resolve_context_or_response(request)
        if error:
            return error
        return Response(_channels_payload(context.organization), status=status.HTTP_200_OK)

    def post(self, request):
        context, error = _resolve_context_or_response(request)
        if error:
            return error
        organization = context.organization
        config = _get_config(organization)
        timezone_name = str(
            request.data.get("timezone") or request.data.get("defaultTimezone") or ""
        ).strip()
        enabled_raw = request.data.get("enabled")
        if enabled_raw is None:
            automation = _org_automation(organization)
            enabled = (
                automation.status == ResearchAutomationStatus.ACTIVE
                if automation
                else bool(config.daily_discovery_enabled)
            )
        else:
            enabled = str(enabled_raw).strip().lower() not in {"false", "0", "no", "off", ""}

        if not enabled:
            ResearchAutomation.objects.filter(
                organization=organization, status=ResearchAutomationStatus.ACTIVE
            ).update(status=ResearchAutomationStatus.PAUSED)
            update_fields = []
            if config.daily_discovery_enabled:
                config.daily_discovery_enabled = False
                update_fields.append("daily_discovery_enabled")
            if timezone_name:
                config.default_timezone = timezone_name
                update_fields.append("default_timezone")
            if update_fields:
                config.save(update_fields=update_fields + ["updated_at"])
            return Response(_channels_payload(organization), status=status.HTTP_200_OK)

        automation, channels = ensure_research_automation_for_org(
            organization=organization,
            user=request.user,
            timezone_name=timezone_name,
            enabled=True,
            config=config,
        )
        if automation is None:
            return Response(
                {
                    "detail": "Connect and verify a notification channel first.",
                    "code": "no_verified_channels",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        update_fields = []
        if not config.daily_discovery_enabled:
            config.daily_discovery_enabled = True
            update_fields.append("daily_discovery_enabled")
        if timezone_name and config.default_timezone != automation.timezone:
            config.default_timezone = automation.timezone
            update_fields.append("default_timezone")
        if update_fields:
            config.save(update_fields=update_fields + ["updated_at"])
        return Response(_channels_payload(organization), status=status.HTTP_200_OK)


_RUN_NOW_ERROR_DETAIL = {
    "insufficient_roo_points": "You don't have enough Roo points to research topics right now.",
    "billing_identity_missing": "Link a Slack account to bill Roo points for this domain.",
}


def _run_now_error_detail(code: str) -> str:
    return _RUN_NOW_ERROR_DETAIL.get(
        code, "We couldn't start your research run. Please try again in a moment."
    )


def serialize_manual_run_status(run: AutomationRun) -> dict:
    """Status of a 'Run today now' AutomationRun for the inline poller.

    phase collapses the run/delivery machine into three UI states:
    researching (discovery in flight) -> sent (topics delivered) -> failed.
    """
    delivery_objs = list(
        NotificationDelivery.objects.filter(automation_run=run, event_type="topic_selection")
        .select_related("channel")
        .order_by("created_at")
    )
    deliveries = [
        {
            "channelType": delivery.channel.channel_type,
            "routeId": delivery.channel.route_id,
            "status": delivery.status,
            "deliveredAt": delivery.delivered_at.isoformat() if delivery.delivered_at else None,
        }
        for delivery in delivery_objs
    ]
    any_sent = any(d.status == NotificationDeliveryStatus.SENT for d in delivery_objs)
    topics_dispatched = run.status in {
        AutomationRunStatus.TOPIC_SELECTION_SENT,
        AutomationRunStatus.DELIVERY_MODE_REQUIRED,
        AutomationRunStatus.GENERATING,
        AutomationRunStatus.COMPLETED,
    }
    all_failed = bool(delivery_objs) and all(
        d.status
        in {
            NotificationDeliveryStatus.FAILED,
            NotificationDeliveryStatus.BOUNCED,
            NotificationDeliveryStatus.OPTED_OUT,
        }
        for d in delivery_objs
    )
    if any_sent:
        phase = "sent"
    elif run.status in {AutomationRunStatus.FAILED, AutomationRunStatus.CANCELLED}:
        phase = "failed"
    elif topics_dispatched and all_failed:
        # Topics fanned out but every channel send failed.
        phase = "failed"
    else:
        phase = "researching"
    return {
        "id": str(run.id),
        "status": run.status,
        "phase": phase,
        "lastError": run.last_error or "",
        "deliveries": deliveries,
    }


class VibeMarketingResearchAutomationRunNowView(APIView):
    """POST: start an on-demand daily-research run and dispatch it immediately.

    Same pipeline as the 8am send (top-3 topics; WhatsApp/Slack/email fan-out to the
    enabled channels; tappable buttons). Returns a run id to poll — delivery is
    asynchronous because the discovery research takes minutes.
    """

    def post(self, request):
        context, error = _resolve_context_or_response(request)
        if error:
            return error
        result = start_manual_automation_run(
            context.organization, requested_by_user_id=getattr(request.user, "id", None)
        )
        result_status = result.get("status")
        if result_status == "no_automation":
            return Response(
                {"detail": "Turn on the daily reminder first.", "code": "automation_not_enabled"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if result_status == "no_delivery_channels":
            return Response(
                {
                    "detail": "Turn on at least one channel to receive your topics.",
                    "code": "no_delivery_channels",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if result_status == "failed":
            code = result.get("error") or "dispatch_failed"
            http_status = (
                status.HTTP_402_PAYMENT_REQUIRED
                if code == "insufficient_roo_points"
                else status.HTTP_502_BAD_GATEWAY
            )
            return Response(
                {
                    "detail": _run_now_error_detail(code),
                    "code": code,
                    "automationRunId": result.get("automation_run_id"),
                },
                status=http_status,
            )
        # queued / reused / skipped -> accepted; the client polls the status endpoint.
        return Response(
            {
                "automationRunId": result.get("automation_run_id"),
                "status": result_status,
                "reused": result_status == "reused",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class VibeMarketingResearchAutomationRunStatusView(APIView):
    """GET: poll a manual run's status + per-channel delivery for the inline UI."""

    def get(self, request, automation_run_id):
        context, error = _resolve_context_or_response(request)
        if error:
            return error
        run = (
            AutomationRun.objects.filter(
                id=automation_run_id,
                automation__organization=context.organization,
            )
            .select_related("automation")
            .first()
        )
        if run is None:
            return Response({"detail": "Run not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(serialize_manual_run_status(run), status=status.HTTP_200_OK)
