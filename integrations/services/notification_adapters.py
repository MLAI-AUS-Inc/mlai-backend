from __future__ import annotations

import base64
import html
import hmac
import hashlib
import json
import logging
import re
from datetime import timedelta
from urllib.parse import urlencode
from typing import Any, Optional

from django.conf import settings
from django.core import signing
from django.urls import reverse
from django.utils import timezone

from content_factory.models import (
    AutomationRun,
    AutomationRunStatus,
    ContentFactoryJob,
    NotificationChannel,
    NotificationChannelType,
    NotificationConsentState,
    NotificationDelivery,
    NotificationDeliveryStatus,
    ResearchAutomation,
    ResearchAutomationStatus,
)
from integrations import http_client
from integrations.services.article_generation import (
    CONTENT_FACTORY_REQUEST_SOURCE,
    confirm_topic,
    set_article_delivery_mode,
)
from integrations.services.slack import SlackService


logger = logging.getLogger(__name__)

NOTIFICATION_CONTEXT_KEYS = {
    "automation_id",
    "automation_run_id",
    "channel_type",
    "channel_route_id",
    "recipient_user_id",
}
AUTOMATION_ACTION_SALT = "content-factory-research-automation-action"
DEFAULT_ACTION_MAX_AGE_SECONDS = 14 * 24 * 60 * 60


def normalize_notification_context(value: Optional[dict[str, Any]]) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for key in sorted(NOTIFICATION_CONTEXT_KEYS):
        raw = value.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            normalized[key] = text
    return normalized


def notification_context_for_run(run: AutomationRun) -> dict[str, str]:
    channel = run.automation.notification_channel
    recipient_user_id = str(run.automation.user_id or channel.user_id or "").strip()
    return {
        "automation_id": str(run.automation_id),
        "automation_run_id": str(run.id),
        "channel_type": channel.channel_type,
        "channel_route_id": str(channel.id),
        "recipient_user_id": recipient_user_id,
    }


def resolve_automation_run(context: Optional[dict[str, Any]]) -> Optional[AutomationRun]:
    normalized = normalize_notification_context(context)
    run_id = normalized.get("automation_run_id")
    if not run_id:
        return None
    return (
        AutomationRun.objects.select_related(
            "automation",
            "automation__organization",
            "automation__user",
            "automation__notification_channel",
            "automation__notification_channel__user",
            "automation__notification_channel__provider_connection",
        )
        .filter(id=run_id)
        .first()
    )


def _callback_job_id(data: dict[str, Any]) -> str:
    return str(data.get("job_id") or data.get("run_id") or "").strip()


def _topic_options(data: dict[str, Any]) -> list[dict[str, Any]]:
    selection = data.get("selection") if isinstance(data.get("selection"), dict) else {}
    options = selection.get("options") if isinstance(selection.get("options"), list) else []
    if not options and selection.get("selected_keyword"):
        options = [selection]
    return [option for option in options[:4] if isinstance(option, dict)]


def _option_keyword(option: dict[str, Any]) -> str:
    return str(option.get("keyword") or option.get("selected_keyword") or "").strip()


def _option_title(option: dict[str, Any]) -> str:
    return str(option.get("suggested_title") or _option_keyword(option) or "Untitled topic").strip()


def _action_token(run: AutomationRun, action: str, **kwargs: Any) -> str:
    payload = {
        "automation_run_id": str(run.id),
        "action": action,
        **kwargs,
    }
    return signing.dumps(payload, salt=AUTOMATION_ACTION_SALT)


def build_action_url(run: AutomationRun, action: str, **kwargs: Any) -> str:
    base_url = str(getattr(settings, "DEFAULT_BACKEND_URL", "") or "").rstrip("/")
    path = reverse("content_factory_automation_action")
    token = _action_token(run, action, **kwargs)
    return f"{base_url}{path}?{urlencode({'token': token})}"


def _active_channels_for_run(run: AutomationRun) -> list[NotificationChannel]:
    """All ACTIVE channels for the automation's organization, primary first.

    This is the single seam for delivery targeting: if automations later need
    an explicit channel selection, only this helper has to change.
    """
    primary_id = run.automation.notification_channel_id
    channels = list(
        NotificationChannel.objects.filter(
            organization_id=run.automation.organization_id,
            consent_state=NotificationConsentState.ACTIVE,
        )
    )
    channels.sort(key=lambda channel: (channel.id != primary_id, str(channel.channel_type), str(channel.route_id)))
    return channels


def automation_billing_actor_slack_id(automation) -> str:
    """Slack id of the wallet owner for Roo-points billing on this automation.

    Roo-points wallets are keyed by Slack id. The channel owner (the founder who
    verified the number/email) is the payer; fall back to the automation owner.
    Returns "" when neither has a linked Slack id — harmless for free-listed
    domains, and surfaced as a clear failure for paying ones at dispatch.
    """
    candidates = (
        getattr(automation.notification_channel, "user", None),
        getattr(automation, "user", None),
    )
    for user in candidates:
        slack_id = str(getattr(user, "slack_id", "") or "").strip()
        if slack_id:
            return slack_id
    return ""


def _delivery_for_event(
    *,
    run: AutomationRun,
    channel: NotificationChannel,
    event_type: str,
    request_payload: dict[str, Any],
) -> tuple[NotificationDelivery, bool]:
    delivery, created = NotificationDelivery.objects.get_or_create(
        automation_run=run,
        channel=channel,
        event_type=event_type,
        idempotency_key=f"{run.id}:{channel.id}:{event_type}",
        defaults={
            "status": NotificationDeliveryStatus.PENDING,
            "request_payload": request_payload,
        },
    )
    if not created and delivery.request_payload != request_payload:
        delivery.request_payload = request_payload
        delivery.save(update_fields=["request_payload", "updated_at"])
    return delivery, created


def record_delivery_status(
    delivery: NotificationDelivery,
    *,
    status: str,
    provider_message_id: str = "",
    response_payload: Optional[dict[str, Any]] = None,
    error: str = "",
) -> NotificationDelivery:
    delivery.status = status
    delivery.provider_message_id = str(provider_message_id or "").strip()
    delivery.response_payload = response_payload or {}
    delivery.last_error = str(error or "").strip()
    if status == NotificationDeliveryStatus.SENT:
        delivery.delivered_at = timezone.now()
    delivery.save(
        update_fields=[
            "status",
            "provider_message_id",
            "response_payload",
            "last_error",
            "delivered_at",
            "updated_at",
        ]
    )
    return delivery


def _plain_topic_message(run: AutomationRun, data: dict[str, Any], channel: Optional[NotificationChannel] = None) -> str:
    domain = data.get("domain") or run.automation.organization.domain
    lines = [f"Research topics are ready for {domain}:"]
    for index, option in enumerate(_topic_options(data), start=1):
        keyword = _option_keyword(option)
        title = _option_title(option)
        score = option.get("opportunity_index")
        score_suffix = f" (score {score})" if score not in (None, "") else ""
        lines.append(f"{index}. {title} - {keyword}{score_suffix}")
        lines.append(f"Approve: {build_action_url(run, 'approve_topic', option_index=index - 1)}")
    lines.append(f"Pause these notifications: {_unsubscribe_url(run, channel)}")
    return "\n".join(lines)


def _unsubscribe_url(run: AutomationRun, channel: Optional[NotificationChannel] = None) -> str:
    if channel is not None:
        return build_action_url(run, "unsubscribe", channel_id=str(channel.id))
    return build_action_url(run, "unsubscribe")


def _topic_email_html(run: AutomationRun, data: dict[str, Any], channel: Optional[NotificationChannel] = None) -> str:
    domain = html.escape(str(data.get("domain") or run.automation.organization.domain or "your site"))
    rows = []
    for index, option in enumerate(_topic_options(data), start=1):
        keyword = html.escape(_option_keyword(option))
        title = html.escape(_option_title(option))
        explanation = html.escape(str(option.get("explanation") or ""))
        approve_url = html.escape(build_action_url(run, "approve_topic", option_index=index - 1))
        rows.append(
            "<li>"
            f"<strong>{title}</strong><br>"
            f"<code>{keyword}</code>"
            f"{'<p>' + explanation + '</p>' if explanation else ''}"
            f'<p><a href="{approve_url}">Approve this topic</a></p>'
            "</li>"
        )
    unsubscribe_url = html.escape(_unsubscribe_url(run, channel))
    return (
        f"<p>Research topics are ready for <strong>{domain}</strong>.</p>"
        f"<ol>{''.join(rows)}</ol>"
        f'<p><a href="{unsubscribe_url}">Pause or unsubscribe</a></p>'
    )


def _recipient_first_name(run: AutomationRun) -> str:
    user = run.automation.user or run.automation.notification_channel.user
    if user is None:
        return ""
    return str(getattr(user, "first_name", "") or "").strip()


def _topic_email_message_data(
    run: AutomationRun,
    data: dict[str, Any],
    channel: Optional[NotificationChannel] = None,
) -> dict[str, Any]:
    """message_data for the Customer.io daily-topics transactional template.

    Field names match docs/customerio-daily-topics-email.html. Each topic's
    confirm_url is the signed one-click approve action (same endpoint Slack and
    the plain-HTML email use), so the template needs no signing logic.
    """
    domain = str(data.get("domain") or run.automation.organization.domain or "your site")
    topics: list[dict[str, Any]] = []
    for index, option in enumerate(_topic_options(data)):
        topics.append(
            {
                "rank": index + 1,
                "keyword": _option_keyword(option),
                "display_title": str(option.get("display_title") or _option_title(option)),
                "volume_display": str(option.get("volume_display") or ""),
                "difficulty": option.get("difficulty"),
                "opportunity_index": option.get("opportunity_index"),
                "tier_label": str(option.get("tier_label") or ""),
                "ai_volume_display": str(option.get("ai_volume_display") or ""),
                "why_recommended": str(option.get("why_recommended") or option.get("explanation") or ""),
                "recommended": bool(option.get("recommended")),
                "confirm_url": build_action_url(run, "approve_topic", option_index=index),
            }
        )
    return {
        "first_name": _recipient_first_name(run),
        "domain": domain,
        "topics": topics,
        "unsubscribe_url": _unsubscribe_url(run, channel),
    }


WHATSAPP_TOPIC_TITLE_SLOTS = 3


def _whatsapp_topic_variables(run: AutomationRun, data: dict[str, Any]) -> dict[str, str]:
    """ContentVariables for the approved daily-topics utility template.

    Template contract: {{1}} domain, {{2}}-{{4}} topic titles (daily runs
    request 3 topics). Twilio rejects sends with missing declared variables,
    so absent options are padded with "-"; extras beyond the slots are cut.
    """
    domain = str(data.get("domain") or run.automation.organization.domain or "your site")
    titles = [_option_title(option) for option in _topic_options(data)]
    while len(titles) < WHATSAPP_TOPIC_TITLE_SLOTS:
        titles.append("-")
    variables = {"1": domain}
    for index, title in enumerate(titles[:WHATSAPP_TOPIC_TITLE_SLOTS], start=2):
        variables[str(index)] = title or "-"
    return variables


def _topic_slack_blocks(run: AutomationRun, data: dict[str, Any], channel: Optional[NotificationChannel] = None) -> list[dict[str, Any]]:
    domain = data.get("domain") or run.automation.organization.domain
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Article Topics Selected", "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Research for *{domain}* is ready. Choose a topic to write:",
            },
        },
    ]
    action_elements = []
    for index, option in enumerate(_topic_options(data), start=1):
        keyword = _option_keyword(option)
        title = _option_title(option)
        explanation = str(option.get("explanation") or "").strip()
        lines = [f"*{index}. {title}*", f"`{keyword}`"]
        if explanation:
            lines.append(f"_{explanation}_")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
        action_elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": f"Approve {index}", "emoji": True},
                "value": f"automation_approve:{run.id}:{index - 1}",
                "url": build_action_url(run, "approve_topic", option_index=index - 1),
                "action_id": f"automation_approve_topic_{index - 1}",
            }
        )
    if action_elements:
        blocks.append({"type": "actions", "elements": action_elements[:5]})
    return blocks


def _delivery_mode_text(run: AutomationRun, data: dict[str, Any]) -> str:
    domain = data.get("domain") or run.automation.organization.domain
    lines = [f"Choose how to deliver the article for {domain}:"]
    for mode, label in (
        ("content_only", "Send content only"),
        ("review_draft", "Open a review draft"),
        ("publish_code", "Publish code"),
    ):
        lines.append(f"{label}: {build_action_url(run, 'delivery_mode', delivery_mode=mode)}")
    return "\n".join(lines)


def _founder_tools_run_url(run_id: str) -> str:
    """Review page for a content-factory run: founder-tools resolves :runId
    to the same id the callbacks carry as job_id/run_id."""
    run_id = str(run_id or "").strip()
    base_url = str(getattr(settings, "FOUNDER_TOOLS_URL", "") or "").rstrip("/")
    if not run_id or not base_url:
        return ""
    return f"{base_url}/founder-tools/marketing/runs/{run_id}"


def _content_ready_text(run: AutomationRun, data: dict[str, Any]) -> str:
    domain = data.get("domain") or run.automation.organization.domain
    title = data.get("title") or data.get("topic") or "Article"
    # publish_pr_url is a relative content-factory API path, not a browsable
    # link — only render pull-request lines for absolute URLs.
    pr_url = str(data.get("pr_url") or data.get("publish_pr_url") or "")
    preview_url = data.get("preview_url") or data.get("primary_review_url") or ""
    review_url = _founder_tools_run_url(run.article_content_factory_run_id or _callback_job_id(data))
    lines = [f"{title} is ready for {domain}."]
    if review_url:
        lines.append(f"Review and approve: {review_url}")
    if preview_url:
        lines.append(f"Preview: {preview_url}")
    if pr_url.startswith("http"):
        lines.append(f"Pull request: {pr_url}")
    return "\n".join(lines)


def _error_text(run: AutomationRun, data: dict[str, Any]) -> str:
    domain = data.get("domain") or run.automation.organization.domain
    error = data.get("error") or data.get("error_message") or "Unknown error"
    return f"Research or article generation failed for {domain}: {error}"


def _send_slack(channel: NotificationChannel, *, text: str, blocks: Optional[list] = None) -> tuple[bool, str, dict]:
    route_id = str(channel.route_id or "").strip()
    if not route_id:
        return False, "", {"error": "missing_slack_route_id"}
    sent, message_ts = SlackService.send_dm(route_id, text, blocks=blocks)
    return sent, str(message_ts or ""), {"message_ts": message_ts or ""}


def _customerio_client():
    from customerio import APIClient

    api_key = str(getattr(settings, "CUSTOMERIO_API_KEY", "") or "").strip()
    return APIClient(api_key) if api_key else None


def _send_email_via_customerio(
    channel: NotificationChannel,
    *,
    subject: str,
    text: str,
    html_body: Optional[str],
    message_data: Optional[dict[str, Any]] = None,
    transactional_message_id: str = "",
) -> tuple[bool, str, dict]:
    client = _customerio_client()
    if client is None:
        return False, "", {"error": "CUSTOMERIO_API_KEY is not configured"}
    to_email = str(channel.route_id or "").strip()
    request_body: dict[str, Any] = {
        "to": to_email,
        # Customer.io requires a person identifier; route by user id when the
        # channel is user-linked (matches core/email_utils magic-link sends).
        "identifiers": {"id": str(channel.user_id)} if channel.user_id else {"email": to_email},
    }
    if transactional_message_id and message_data is not None:
        # Render through a Customer.io transactional template (Liquid + message_data).
        # Subject/body live in the template, so we send neither here.
        request_body["transactional_message_id"] = str(transactional_message_id)
        request_body["message_data"] = message_data
    else:
        # Inline raw body (no template configured): subject/body shipped directly.
        request_body["subject"] = subject
        request_body["body"] = html_body or html.escape(text).replace("\n", "<br>")
        request_body["body_plain"] = text
    from_email = str(getattr(settings, "CUSTOMERIO_FROM_EMAIL", "") or "").strip()
    if from_email:
        request_body["from"] = from_email
    try:
        response = client.send_email(request_body)
    except Exception as exc:
        return False, "", {"error": str(exc)}
    response_payload = response if isinstance(response, dict) else {"response": str(response)}
    return True, str(response_payload.get("delivery_id") or ""), response_payload


def _send_email_via_resend(
    channel: NotificationChannel,
    *,
    subject: str,
    text: str,
    html_body: Optional[str],
    idempotency_key: str,
) -> tuple[bool, str, dict]:
    api_key = str(getattr(settings, "RESEND_API_KEY", "") or "").strip()
    from_email = str(getattr(settings, "RESEND_FROM_EMAIL", "") or "Roo <notifications@mlai.au>").strip()
    to_email = str(channel.route_id or "").strip()
    if not api_key:
        return False, "", {"error": "RESEND_API_KEY is not configured"}
    payload = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "text": text,
    }
    if html_body:
        payload["html"] = html_body
    response = http_client.post(
        "https://api.resend.com/emails",
        json=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        },
        timeout=(3, 8),
    )
    try:
        response_payload = response.json()
    except ValueError:
        response_payload = {"text": response.text}
    if response.status_code >= 300:
        return False, "", response_payload
    return True, str(response_payload.get("id") or ""), response_payload


def _send_email(
    channel: NotificationChannel,
    *,
    subject: str,
    text: str,
    html_body: Optional[str],
    idempotency_key: str,
    message_data: Optional[dict[str, Any]] = None,
    transactional_message_id: str = "",
) -> tuple[bool, str, dict]:
    if str(getattr(settings, "CUSTOMERIO_API_KEY", "") or "").strip():
        return _send_email_via_customerio(
            channel,
            subject=subject,
            text=text,
            html_body=html_body,
            message_data=message_data,
            transactional_message_id=transactional_message_id,
        )
    if str(getattr(settings, "RESEND_API_KEY", "") or "").strip():
        return _send_email_via_resend(
            channel,
            subject=subject,
            text=text,
            html_body=html_body,
            idempotency_key=idempotency_key,
        )
    return False, "", {"error": "email_not_configured"}


def _whatsapp_address(number: str) -> str:
    """Twilio WhatsApp addressing: whatsapp:+E164 (route_ids are stored as +E164)."""
    text = str(number or "").strip()
    if not text:
        return ""
    if text.startswith("whatsapp:"):
        return text
    if not text.startswith("+"):
        text = "+" + text
    return f"whatsapp:{text}"


def _post_whatsapp_message(payload: dict[str, Any]) -> tuple[bool, str, dict]:
    account_sid = str(getattr(settings, "TWILIO_ACCOUNT_SID", "") or "").strip()
    auth_token = str(getattr(settings, "TWILIO_AUTH_TOKEN", "") or "").strip()
    if not account_sid or not auth_token:
        return False, "", {"error": "Twilio WhatsApp credentials are not configured"}
    response = http_client.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
        data=payload,
        auth=(account_sid, auth_token),
        timeout=(3, 8),
    )
    try:
        response_payload = response.json()
    except ValueError:
        response_payload = {"text": response.text}
    if response.status_code >= 300:
        return False, "", response_payload
    return True, str(response_payload.get("sid") or ""), response_payload


def send_whatsapp_text(to_number: str, text: str) -> tuple[bool, str, dict]:
    """Send a free-form WhatsApp text (delivers inside a 24h service window)."""
    return _post_whatsapp_message(
        {
            "From": _whatsapp_address(str(getattr(settings, "TWILIO_WHATSAPP_FROM", "") or "")),
            "To": _whatsapp_address(to_number),
            "Body": text,
        }
    )


def send_whatsapp_template(
    to_number: str,
    *,
    content_sid: str,
    content_variables: Optional[dict[str, str]] = None,
) -> tuple[bool, str, dict]:
    """Send an approved WhatsApp Content template (works outside the 24h window)."""
    payload = {
        "From": _whatsapp_address(str(getattr(settings, "TWILIO_WHATSAPP_FROM", "") or "")),
        "To": _whatsapp_address(to_number),
        "ContentSid": content_sid,
    }
    if content_variables:
        payload["ContentVariables"] = json.dumps(content_variables)
    return _post_whatsapp_message(payload)


def _send_whatsapp(
    channel: NotificationChannel,
    *,
    text: str,
    content_sid: str = "",
    content_variables: Optional[dict[str, str]] = None,
) -> tuple[bool, str, dict]:
    if content_sid:
        return send_whatsapp_template(
            channel.route_id,
            content_sid=content_sid,
            content_variables=content_variables,
        )
    return send_whatsapp_text(channel.route_id, f"{text}\n\nReply STOP to opt out.")


def _send_channel_delivery(
    *,
    run: AutomationRun,
    channel: NotificationChannel,
    delivery: NotificationDelivery,
    event_type: str,
    text: str,
    subject: Optional[str] = None,
    blocks: Optional[list] = None,
    html_body: Optional[str] = None,
    whatsapp_variables: Optional[dict[str, str]] = None,
    email_message_data: Optional[dict[str, Any]] = None,
    email_transactional_message_id: str = "",
) -> NotificationDelivery:
    if channel.consent_state != NotificationConsentState.ACTIVE:
        return record_delivery_status(
            delivery,
            status=NotificationDeliveryStatus.OPTED_OUT,
            error=f"Channel consent state is {channel.consent_state}",
        )

    try:
        if channel.channel_type == NotificationChannelType.SLACK:
            success, provider_id, response_payload = _send_slack(channel, text=text, blocks=blocks)
        elif channel.channel_type == NotificationChannelType.EMAIL:
            success, provider_id, response_payload = _send_email(
                channel,
                subject=subject or "Content research update",
                text=text,
                html_body=html_body,
                idempotency_key=delivery.idempotency_key,
                message_data=email_message_data,
                transactional_message_id=email_transactional_message_id,
            )
        elif channel.channel_type == NotificationChannelType.WHATSAPP:
            content_sid = str(channel.provider_metadata.get(f"{event_type}_content_sid") or "").strip()
            if not content_sid and event_type == "topic_selection":
                content_sid = str(getattr(settings, "TWILIO_WHATSAPP_TOPIC_CONTENT_SID", "") or "").strip()
                if not content_sid:
                    # Business-initiated sends outside a 24h service window need an
                    # approved template; plain text only delivers inside a window.
                    logger.warning(
                        "WhatsApp topic Content template is not configured; attempting plain text for run %s",
                        run.id,
                    )
            success, provider_id, response_payload = _send_whatsapp(
                channel,
                text=text,
                content_sid=content_sid,
                content_variables=whatsapp_variables if content_sid else None,
            )
        else:
            success, provider_id, response_payload = False, "", {"error": f"Unsupported channel {channel.channel_type}"}
    except Exception as exc:
        logger.warning("Notification delivery failed for %s/%s: %s", run.id, event_type, exc)
        return record_delivery_status(
            delivery,
            status=NotificationDeliveryStatus.FAILED,
            error=str(exc),
        )

    if success:
        return record_delivery_status(
            delivery,
            status=NotificationDeliveryStatus.SENT,
            provider_message_id=provider_id,
            response_payload=response_payload,
        )
    return record_delivery_status(
        delivery,
        status=NotificationDeliveryStatus.FAILED,
        response_payload=response_payload,
        error=str(response_payload.get("error") or response_payload),
    )


def _fan_out_event(
    *,
    run: AutomationRun,
    event_type: str,
    request_payload: dict[str, Any],
    build_kwargs,
) -> list[NotificationDelivery]:
    """Deliver one automation event to every active channel, idempotently per channel.

    build_kwargs(channel) returns the _send_channel_delivery content kwargs
    (text/subject/blocks/html_body/whatsapp_variables) for that channel.
    """
    deliveries: list[NotificationDelivery] = []
    for channel in _active_channels_for_run(run):
        delivery, created = _delivery_for_event(
            run=run,
            channel=channel,
            event_type=event_type,
            request_payload=request_payload,
        )
        if not created and delivery.status == NotificationDeliveryStatus.SENT:
            deliveries.append(delivery)
            continue
        deliveries.append(
            _send_channel_delivery(
                run=run,
                channel=channel,
                delivery=delivery,
                event_type=event_type,
                **build_kwargs(channel),
            )
        )
    return deliveries


def send_topic_selection(data: dict[str, Any]) -> list[NotificationDelivery]:
    run = resolve_automation_run(data.get("notification_context"))
    if not run:
        return []
    job_id = _callback_job_id(data)
    run.callback_payload = data
    if job_id and not run.content_factory_run_id:
        run.content_factory_run_id = job_id
    run.status = AutomationRunStatus.TOPIC_SELECTION_SENT
    run.last_error = ""
    run.save(update_fields=["callback_payload", "content_factory_run_id", "status", "last_error", "updated_at"])

    return _fan_out_event(
        run=run,
        event_type="topic_selection",
        request_payload={"event_type": "topic_selection", "job_id": job_id, "options": _topic_options(data)},
        build_kwargs=lambda channel: {
            "text": _plain_topic_message(run, data, channel),
            "subject": f"Article topics for {run.automation.organization.domain}",
            "blocks": _topic_slack_blocks(run, data, channel),
            # Raw-HTML fallback used when no Customer.io template id is configured
            # (or when email goes via Resend).
            "html_body": _topic_email_html(run, data, channel),
            "whatsapp_variables": _whatsapp_topic_variables(run, data),
            "email_message_data": _topic_email_message_data(run, data, channel),
            "email_transactional_message_id": str(
                getattr(settings, "CUSTOMERIO_TOPIC_TEMPLATE_ID", "") or ""
            ).strip(),
        },
    )


def send_delivery_mode_required(data: dict[str, Any]) -> list[NotificationDelivery]:
    run = resolve_automation_run(data.get("notification_context"))
    if not run:
        return []
    if run.selected_delivery_mode and run.status == AutomationRunStatus.GENERATING:
        # A delivery mode was already selected (approval auto-resolves it), so
        # this prompt raced the selection and is stale — don't message anyone.
        return []
    job_id = _callback_job_id(data)
    if job_id:
        run.article_content_factory_run_id = job_id
    run.callback_payload = data
    run.status = AutomationRunStatus.DELIVERY_MODE_REQUIRED
    run.save(update_fields=["article_content_factory_run_id", "callback_payload", "status", "updated_at"])

    text = _delivery_mode_text(run, data)
    return _fan_out_event(
        run=run,
        event_type="delivery_mode_required",
        request_payload={"event_type": "delivery_mode_required", "job_id": job_id},
        build_kwargs=lambda channel: {
            "text": text,
            "subject": f"Choose article delivery mode for {run.automation.organization.domain}",
            "html_body": html.escape(text).replace("\n", "<br>"),
        },
    )


def send_content_ready(data: dict[str, Any]) -> list[NotificationDelivery]:
    run = resolve_automation_run(data.get("notification_context"))
    if not run:
        return []
    job_id = _callback_job_id(data)
    if job_id:
        run.article_content_factory_run_id = job_id
    run.callback_payload = data
    run.status = AutomationRunStatus.COMPLETED
    run.save(update_fields=["article_content_factory_run_id", "callback_payload", "status", "updated_at"])

    text = _content_ready_text(run, data)
    return _fan_out_event(
        run=run,
        event_type="content_ready",
        request_payload={"event_type": "content_ready", "job_id": job_id},
        build_kwargs=lambda channel: {
            "text": text,
            "subject": f"Article ready for {run.automation.organization.domain}",
            "html_body": html.escape(text).replace("\n", "<br>"),
        },
    )


def send_review_ready(data: dict[str, Any]) -> list[NotificationDelivery]:
    """Fan out the review link when a generated article awaits human review.

    Fires for article_review_ready and generation_pr_opened — the terminal
    reviewable outcomes of review_draft/publish_code deliveries, which
    otherwise notify nobody (content_ready only covers content_only runs).
    """
    run = resolve_automation_run(data.get("notification_context"))
    if not run:
        return []
    job_id = _callback_job_id(data)
    if job_id:
        run.article_content_factory_run_id = job_id
    run.callback_payload = data
    run.status = AutomationRunStatus.COMPLETED
    run.save(update_fields=["article_content_factory_run_id", "callback_payload", "status", "updated_at"])

    text = _content_ready_text(run, data)
    return _fan_out_event(
        run=run,
        event_type="review_ready",
        request_payload={"event_type": "review_ready", "job_id": job_id},
        build_kwargs=lambda channel: {
            "text": text,
            "subject": f"Article ready to review for {run.automation.organization.domain}",
            "html_body": html.escape(text).replace("\n", "<br>"),
        },
    )


def send_error(data: dict[str, Any]) -> list[NotificationDelivery]:
    run = resolve_automation_run(data.get("notification_context"))
    if not run:
        return []
    job_id = _callback_job_id(data)
    run.callback_payload = data
    run.status = AutomationRunStatus.FAILED
    run.last_error = str(data.get("error") or data.get("error_message") or "").strip()
    run.save(update_fields=["callback_payload", "status", "last_error", "updated_at"])

    text = _error_text(run, data)
    return _fan_out_event(
        run=run,
        event_type="error",
        request_payload={"event_type": "error", "job_id": job_id, "error": run.last_error},
        build_kwargs=lambda channel: {
            "text": text,
            "subject": f"Content research failed for {run.automation.organization.domain}",
            "html_body": html.escape(text).replace("\n", "<br>"),
        },
    )


def approve_topic_for_run(
    run: AutomationRun,
    *,
    option_index: int,
    delivery_mode: Optional[str] = None,
) -> dict[str, Any]:
    data = dict(run.callback_payload or {})
    options = _topic_options(data)
    if option_index < 0 or option_index >= len(options):
        raise ValueError("Invalid topic option index.")
    option = options[option_index]
    keyword = _option_keyword(option)
    if not keyword:
        raise ValueError("Selected topic option is missing a keyword.")

    if run.article_content_factory_run_id:
        return {
            "status": run.status,
            "job_id": run.article_content_factory_run_id,
            "run_id": run.article_content_factory_run_id,
            "idempotent": True,
        }

    skip_alternatives = [
        _option_keyword(candidate)
        for index, candidate in enumerate(options)
        if index != option_index and _option_keyword(candidate)
    ]
    channel = run.automation.notification_channel
    source_job_id = run.content_factory_run_id or _callback_job_id(data)
    source_job = ContentFactoryJob.objects.filter(job_id=source_job_id).first()
    slack_user_id = (
        channel.route_id
        if channel.channel_type == NotificationChannelType.SLACK
        else (source_job.slack_user_id if source_job else "")
    )
    result = confirm_topic(
        domain=run.automation.organization.domain,
        confirmed_keyword=keyword,
        slack_user_id=slack_user_id or "",
        custom_title=option.get("suggested_title") or None,
        skip_alternatives=skip_alternatives,
        source_run_id=source_job_id,
        delivery_mode=delivery_mode,
        delivery_mode_confirmed=bool(delivery_mode),
        request_source=CONTENT_FACTORY_REQUEST_SOURCE,
        notification_context=notification_context_for_run(run),
    )
    child_job_id = str(result.get("job_id") or result.get("run_id") or "").strip()
    run.selected_topic = option
    if delivery_mode:
        run.selected_delivery_mode = delivery_mode
    if child_job_id:
        run.article_content_factory_run_id = child_job_id
    result_status = str(result.get("status") or "").strip()
    if result_status == "awaiting_delivery_mode" and not delivery_mode and child_job_id:
        # Don't bounce a second "choose delivery mode" prompt through the
        # channels: resolve the org default (review_draft unless configured)
        # and continue. On failure the existing prompt flow still applies.
        try:
            auto_result = set_article_delivery_mode(child_job_id)
        except Exception as exc:
            logger.warning("Auto delivery-mode selection failed for %s: %s", child_job_id, exc)
        else:
            resolved_mode = str(auto_result.get("delivery_mode") or "").strip()
            if resolved_mode:
                run.selected_delivery_mode = resolved_mode
            result_status = str(auto_result.get("status") or "").strip() or "queued"
            result = {**result, "status": result_status, "delivery_mode_autoselected": True}
    run.status = (
        AutomationRunStatus.DELIVERY_MODE_REQUIRED
        if result_status == "awaiting_delivery_mode"
        else AutomationRunStatus.GENERATING
    )
    run.save(
        update_fields=[
            "selected_topic",
            "selected_delivery_mode",
            "article_content_factory_run_id",
            "status",
            "updated_at",
        ]
    )
    return result


def set_delivery_mode_for_run(run: AutomationRun, *, delivery_mode: str) -> dict[str, Any]:
    job_id = run.article_content_factory_run_id or run.content_factory_run_id
    if not job_id:
        raise ValueError("No content-factory run is available for delivery-mode selection.")
    result = set_article_delivery_mode(job_id, delivery_mode)
    run.selected_delivery_mode = delivery_mode
    run.status = AutomationRunStatus.GENERATING
    run.save(update_fields=["selected_delivery_mode", "status", "updated_at"])
    return result


def pause_automations_if_no_active_channels(organization) -> bool:
    """Pause the org's active automations when no consented channel remains."""
    from content_factory.models import OrganizationContentConfig

    has_active = NotificationChannel.objects.filter(
        organization=organization,
        consent_state=NotificationConsentState.ACTIVE,
    ).exists()
    if has_active:
        return False
    paused = ResearchAutomation.objects.filter(
        organization=organization,
        status=ResearchAutomationStatus.ACTIVE,
    ).update(status=ResearchAutomationStatus.PAUSED)
    if paused:
        # Pausing the automation reopens the legacy daily-discovery gate, so
        # the legacy boolean must flip too or Slack sends would resume. Orgs
        # that never had an active automation keep their legacy setting.
        config = OrganizationContentConfig.objects.filter(organization=organization).first()
        if config and config.daily_discovery_enabled:
            config.daily_discovery_enabled = False
            config.save(update_fields=["daily_discovery_enabled", "updated_at"])
    return bool(paused)


def unsubscribe_channel_for_run(run: AutomationRun, *, channel_id: Optional[str] = None) -> dict[str, Any]:
    channel = None
    if channel_id:
        channel = NotificationChannel.objects.filter(
            id=channel_id,
            organization_id=run.automation.organization_id,
        ).first()
    if channel is None:
        # Legacy tokens carry no channel_id; they predate fan-out and always
        # targeted the automation's primary channel.
        channel = run.automation.notification_channel
    channel.consent_state = NotificationConsentState.OPTED_OUT
    channel.opted_out_at = timezone.now()
    channel.save(update_fields=["consent_state", "opted_out_at", "updated_at"])
    automation_paused = pause_automations_if_no_active_channels(run.automation.organization)
    return {
        "status": "unsubscribed",
        "channel_id": str(channel.id),
        "automation_id": str(run.automation_id),
        "automation_paused": automation_paused,
    }


def handle_automation_action_token(token: str) -> dict[str, Any]:
    max_age = int(getattr(settings, "CONTENT_AUTOMATION_ACTION_MAX_AGE_SECONDS", DEFAULT_ACTION_MAX_AGE_SECONDS))
    payload = signing.loads(token, salt=AUTOMATION_ACTION_SALT, max_age=max_age)
    run = (
        AutomationRun.objects.select_related(
            "automation",
            "automation__organization",
            "automation__notification_channel",
        )
        .get(id=payload["automation_run_id"])
    )
    action = str(payload.get("action") or "").strip()
    if action == "approve_topic":
        return approve_topic_for_run(
            run,
            option_index=int(payload.get("option_index") or 0),
            delivery_mode=payload.get("delivery_mode"),
        )
    if action == "delivery_mode":
        return set_delivery_mode_for_run(run, delivery_mode=str(payload.get("delivery_mode") or ""))
    if action == "unsubscribe":
        return unsubscribe_channel_for_run(run, channel_id=payload.get("channel_id"))
    raise ValueError(f"Unsupported automation action: {action}")


def verify_whatsapp_webhook_signature(*, url: str, params: dict[str, Any], signature: str) -> bool:
    """Validate Twilio's X-Twilio-Signature.

    Twilio signs the full public webhook URL with every POST param appended as
    key+value in key-sorted order, HMAC-SHA1 keyed by the auth token, base64.
    An unset auth token accepts everything (local/dev parity with sends).
    """
    auth_token = str(getattr(settings, "TWILIO_AUTH_TOKEN", "") or "").strip()
    if not auth_token:
        return True
    signed = str(url or "") + "".join(key + str(params[key]) for key in sorted(params.keys()))
    digest = hmac.new(auth_token.encode(), signed.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, str(signature or "").strip())


WHATSAPP_OPT_OUT_KEYWORDS = {"STOP", "UNSUBSCRIBE", "PAUSE"}
WHATSAPP_TOPIC_REPLY_WINDOW = timedelta(hours=48)


def _whatsapp_channels_for_sender(sender: str) -> list[NotificationChannel]:
    candidates = {sender, f"+{sender.lstrip('+')}"}
    return list(
        NotificationChannel.objects.filter(
            channel_type=NotificationChannelType.WHATSAPP,
            route_id__in=candidates,
        ).select_related("organization")
    )


def _handle_whatsapp_inbound_message(message: dict[str, Any]) -> dict[str, int]:
    sender = str(message.get("from") or "").strip()
    text = ""
    if isinstance(message.get("text"), dict):
        text = str(message["text"].get("body") or "").strip()
    if not sender or not text:
        return {}

    if text.upper() in WHATSAPP_OPT_OUT_KEYWORDS:
        opted_out = 0
        for channel in _whatsapp_channels_for_sender(sender):
            if channel.consent_state == NotificationConsentState.OPTED_OUT:
                continue
            channel.consent_state = NotificationConsentState.OPTED_OUT
            channel.opted_out_at = timezone.now()
            channel.save(update_fields=["consent_state", "opted_out_at", "updated_at"])
            pause_automations_if_no_active_channels(channel.organization)
            opted_out += 1
        return {"opted_out": opted_out}

    active_channels = [
        channel
        for channel in _whatsapp_channels_for_sender(sender)
        if channel.consent_state == NotificationConsentState.ACTIVE
    ]
    if not active_channels:
        # Unknown senders get no reply: auto-responding would be a spam vector.
        return {}

    if not re.fullmatch(r"[1-4]", text):
        send_whatsapp_text(sender, "Reply 1-3 to pick a topic when one is pending, or STOP to opt out.")
        return {"replied": 1}

    option_index = int(text) - 1
    org_ids = {channel.organization_id for channel in active_channels}
    pending_runs = list(
        AutomationRun.objects.filter(
            automation__organization_id__in=org_ids,
            status=AutomationRunStatus.TOPIC_SELECTION_SENT,
            scheduled_for_at__gte=timezone.now() - WHATSAPP_TOPIC_REPLY_WINDOW,
        )
        .select_related("automation", "automation__organization", "automation__notification_channel")
        .order_by("-scheduled_for_at")
    )
    if not pending_runs:
        send_whatsapp_text(
            sender,
            "There's no topic selection waiting right now. You'll get the next one at your scheduled time.",
        )
        return {"replied": 1}
    if len({run.automation.organization_id for run in pending_runs}) > 1:
        send_whatsapp_text(
            sender,
            "You have pending topics for more than one site. Please use the approval links in the message instead.",
        )
        return {"replied": 1}

    run = pending_runs[0]
    try:
        approve_topic_for_run(run, option_index=option_index)
    except ValueError as exc:
        send_whatsapp_text(sender, f"Couldn't approve option {text}: {exc}")
        return {"replied": 1}
    option = _topic_options(dict(run.callback_payload or {}))[option_index]
    send_whatsapp_text(
        sender,
        f'Locked in: "{_option_title(option)}". We\'ll start writing and message you when it\'s ready.',
    )
    return {"approved": 1, "replied": 1}


def handle_whatsapp_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    """Handle one Twilio inbound-message POST (flat form fields, one message).

    Delivery receipts go to a separately configured status-callback URL, so only
    user messages arrive here. A bad message must never fail the webhook —
    Twilio retries on non-2xx and would replay opt-outs/approvals.
    """
    sender = str(payload.get("WaId") or "").strip()
    if not sender:
        sender = str(payload.get("From") or "").strip().removeprefix("whatsapp:").lstrip("+")
    # Quick-reply button taps deliver the visible title as Body and the
    # developer-defined id as ButtonPayload; the topic template sets ids
    # "1"-"3", so prefer the payload and fall back to typed text.
    body = str(payload.get("ButtonPayload") or "").strip() or str(payload.get("Body") or "")
    message = {"from": sender, "text": {"body": body}}
    try:
        result = _handle_whatsapp_inbound_message(message)
    except Exception as exc:
        logger.warning("Failed handling WhatsApp inbound message: %s", exc)
        result = {}
    return {
        "status": "received",
        "opted_out": result.get("opted_out", 0),
        "approved": result.get("approved", 0),
        "replied": result.get("replied", 0),
    }
