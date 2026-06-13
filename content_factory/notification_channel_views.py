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
    NotificationChannelType,
    NotificationConsentState,
    ResearchAutomation,
    ResearchAutomationStatus,
)
from integrations.services.notification_adapters import pause_automations_if_no_active_channels
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
