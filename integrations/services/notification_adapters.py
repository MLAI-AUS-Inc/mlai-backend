from __future__ import annotations

import html
import hmac
import hashlib
import logging
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


def _delivery_for_event(
    *,
    run: AutomationRun,
    event_type: str,
    request_payload: dict[str, Any],
) -> tuple[NotificationDelivery, bool]:
    channel = run.automation.notification_channel
    delivery, created = NotificationDelivery.objects.get_or_create(
        automation_run=run,
        channel=channel,
        event_type=event_type,
        idempotency_key=f"{run.id}:{event_type}",
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


def _plain_topic_message(run: AutomationRun, data: dict[str, Any]) -> str:
    domain = data.get("domain") or run.automation.organization.domain
    lines = [f"Research topics are ready for {domain}:"]
    for index, option in enumerate(_topic_options(data), start=1):
        keyword = _option_keyword(option)
        title = _option_title(option)
        score = option.get("opportunity_index")
        score_suffix = f" (score {score})" if score not in (None, "") else ""
        lines.append(f"{index}. {title} - {keyword}{score_suffix}")
        lines.append(f"Approve: {build_action_url(run, 'approve_topic', option_index=index - 1)}")
    lines.append(f"Pause these notifications: {build_action_url(run, 'unsubscribe')}")
    return "\n".join(lines)


def _topic_email_html(run: AutomationRun, data: dict[str, Any]) -> str:
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
    unsubscribe_url = html.escape(build_action_url(run, "unsubscribe"))
    return (
        f"<p>Research topics are ready for <strong>{domain}</strong>.</p>"
        f"<ol>{''.join(rows)}</ol>"
        f'<p><a href="{unsubscribe_url}">Pause or unsubscribe</a></p>'
    )


def _topic_slack_blocks(run: AutomationRun, data: dict[str, Any]) -> list[dict[str, Any]]:
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


def _content_ready_text(run: AutomationRun, data: dict[str, Any]) -> str:
    domain = data.get("domain") or run.automation.organization.domain
    title = data.get("title") or data.get("topic") or "Article"
    pr_url = data.get("pr_url") or data.get("publish_pr_url") or ""
    preview_url = data.get("preview_url") or data.get("primary_review_url") or ""
    lines = [f"{title} is ready for {domain}."]
    if preview_url:
        lines.append(f"Preview: {preview_url}")
    if pr_url:
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


def _send_email(
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


def _send_whatsapp(
    channel: NotificationChannel,
    *,
    text: str,
    template_name: str = "",
) -> tuple[bool, str, dict]:
    token = str(getattr(settings, "WHATSAPP_CLOUD_API_TOKEN", "") or "").strip()
    phone_number_id = str(getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "") or "").strip()
    to_number = str(channel.route_id or "").strip().lstrip("+")
    if not token or not phone_number_id:
        return False, "", {"error": "WhatsApp Cloud API credentials are not configured"}

    if template_name:
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": str(getattr(settings, "WHATSAPP_TEMPLATE_LANGUAGE", "en_US"))},
            },
        }
    else:
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {"body": f"{text}\n\nReply STOP to opt out."},
        }
    response = http_client.post(
        f"https://graph.facebook.com/v20.0/{phone_number_id}/messages",
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=(3, 8),
    )
    try:
        response_payload = response.json()
    except ValueError:
        response_payload = {"text": response.text}
    if response.status_code >= 300:
        return False, "", response_payload
    messages = response_payload.get("messages") if isinstance(response_payload.get("messages"), list) else []
    provider_id = str((messages[0] or {}).get("id") if messages else "")
    return True, provider_id, response_payload


def _send_channel_delivery(
    *,
    run: AutomationRun,
    delivery: NotificationDelivery,
    event_type: str,
    text: str,
    subject: Optional[str] = None,
    blocks: Optional[list] = None,
    html_body: Optional[str] = None,
) -> NotificationDelivery:
    channel = run.automation.notification_channel
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
            )
        elif channel.channel_type == NotificationChannelType.WHATSAPP:
            template_name = str(channel.provider_metadata.get(f"{event_type}_template") or "").strip()
            success, provider_id, response_payload = _send_whatsapp(
                channel,
                text=text,
                template_name=template_name,
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


def send_topic_selection(data: dict[str, Any]) -> Optional[NotificationDelivery]:
    run = resolve_automation_run(data.get("notification_context"))
    if not run:
        return None
    job_id = _callback_job_id(data)
    run.callback_payload = data
    if job_id and not run.content_factory_run_id:
        run.content_factory_run_id = job_id
    run.status = AutomationRunStatus.TOPIC_SELECTION_SENT
    run.last_error = ""
    run.save(update_fields=["callback_payload", "content_factory_run_id", "status", "last_error", "updated_at"])

    request_payload = {"event_type": "topic_selection", "job_id": job_id, "options": _topic_options(data)}
    delivery, created = _delivery_for_event(run=run, event_type="topic_selection", request_payload=request_payload)
    if not created and delivery.status == NotificationDeliveryStatus.SENT:
        return delivery
    text = _plain_topic_message(run, data)
    return _send_channel_delivery(
        run=run,
        delivery=delivery,
        event_type="topic_selection",
        text=text,
        subject=f"Article topics for {run.automation.organization.domain}",
        blocks=_topic_slack_blocks(run, data),
        html_body=_topic_email_html(run, data),
    )


def send_delivery_mode_required(data: dict[str, Any]) -> Optional[NotificationDelivery]:
    run = resolve_automation_run(data.get("notification_context"))
    if not run:
        return None
    job_id = _callback_job_id(data)
    if job_id:
        run.article_content_factory_run_id = job_id
    run.callback_payload = data
    run.status = AutomationRunStatus.DELIVERY_MODE_REQUIRED
    run.save(update_fields=["article_content_factory_run_id", "callback_payload", "status", "updated_at"])

    delivery, created = _delivery_for_event(
        run=run,
        event_type="delivery_mode_required",
        request_payload={"event_type": "delivery_mode_required", "job_id": job_id},
    )
    if not created and delivery.status == NotificationDeliveryStatus.SENT:
        return delivery
    text = _delivery_mode_text(run, data)
    return _send_channel_delivery(
        run=run,
        delivery=delivery,
        event_type="delivery_mode_required",
        text=text,
        subject=f"Choose article delivery mode for {run.automation.organization.domain}",
        html_body=html.escape(text).replace("\n", "<br>"),
    )


def send_content_ready(data: dict[str, Any]) -> Optional[NotificationDelivery]:
    run = resolve_automation_run(data.get("notification_context"))
    if not run:
        return None
    job_id = _callback_job_id(data)
    if job_id:
        run.article_content_factory_run_id = job_id
    run.callback_payload = data
    run.status = AutomationRunStatus.COMPLETED
    run.save(update_fields=["article_content_factory_run_id", "callback_payload", "status", "updated_at"])

    delivery, created = _delivery_for_event(
        run=run,
        event_type="content_ready",
        request_payload={"event_type": "content_ready", "job_id": job_id},
    )
    if not created and delivery.status == NotificationDeliveryStatus.SENT:
        return delivery
    text = _content_ready_text(run, data)
    return _send_channel_delivery(
        run=run,
        delivery=delivery,
        event_type="content_ready",
        text=text,
        subject=f"Article ready for {run.automation.organization.domain}",
        html_body=html.escape(text).replace("\n", "<br>"),
    )


def send_error(data: dict[str, Any]) -> Optional[NotificationDelivery]:
    run = resolve_automation_run(data.get("notification_context"))
    if not run:
        return None
    job_id = _callback_job_id(data)
    run.callback_payload = data
    run.status = AutomationRunStatus.FAILED
    run.last_error = str(data.get("error") or data.get("error_message") or "").strip()
    run.save(update_fields=["callback_payload", "status", "last_error", "updated_at"])

    delivery, created = _delivery_for_event(
        run=run,
        event_type="error",
        request_payload={"event_type": "error", "job_id": job_id, "error": run.last_error},
    )
    if not created and delivery.status == NotificationDeliveryStatus.SENT:
        return delivery
    text = _error_text(run, data)
    return _send_channel_delivery(
        run=run,
        delivery=delivery,
        event_type="error",
        text=text,
        subject=f"Content research failed for {run.automation.organization.domain}",
        html_body=html.escape(text).replace("\n", "<br>"),
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


def unsubscribe_channel_for_run(run: AutomationRun) -> dict[str, Any]:
    channel = run.automation.notification_channel
    channel.consent_state = NotificationConsentState.OPTED_OUT
    channel.opted_out_at = timezone.now()
    channel.save(update_fields=["consent_state", "opted_out_at", "updated_at"])
    run.automation.status = "paused"
    run.automation.save(update_fields=["status", "updated_at"])
    return {"status": "unsubscribed", "channel_id": str(channel.id), "automation_id": str(run.automation_id)}


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
        return unsubscribe_channel_for_run(run)
    raise ValueError(f"Unsupported automation action: {action}")


def verify_whatsapp_webhook_signature(*, body: bytes, signature: str) -> bool:
    app_secret = str(getattr(settings, "WHATSAPP_APP_SECRET", "") or "").strip()
    if not app_secret:
        return True
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(signature or "").strip())


def handle_whatsapp_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    opted_out = 0
    status_updates = 0
    entries = payload.get("entry") if isinstance(payload.get("entry"), list) else []
    for entry in entries:
        changes = entry.get("changes") if isinstance(entry, dict) and isinstance(entry.get("changes"), list) else []
        for change in changes:
            value = change.get("value") if isinstance(change, dict) else {}
            if not isinstance(value, dict):
                continue
            statuses = value.get("statuses") if isinstance(value.get("statuses"), list) else []
            status_updates += len(statuses)
            messages = value.get("messages") if isinstance(value.get("messages"), list) else []
            for message in messages:
                sender = str(message.get("from") or "").strip()
                text = ""
                if isinstance(message.get("text"), dict):
                    text = str(message["text"].get("body") or "").strip()
                if text.upper() not in {"STOP", "UNSUBSCRIBE", "PAUSE"}:
                    continue
                candidates = {sender, f"+{sender.lstrip('+')}"}
                updated = NotificationChannel.objects.filter(
                    channel_type=NotificationChannelType.WHATSAPP,
                    route_id__in=candidates,
                ).exclude(consent_state=NotificationConsentState.OPTED_OUT).update(
                    consent_state=NotificationConsentState.OPTED_OUT,
                    opted_out_at=timezone.now(),
                )
                opted_out += updated
    return {"status": "received", "opted_out": opted_out, "status_updates": status_updates}
