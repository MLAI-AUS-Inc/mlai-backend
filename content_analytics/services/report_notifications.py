""""Report ready" delivery for daily article-performance briefs.

Sends one idempotent notification per (report, channel) through the existing
consented channels (Slack / email / WhatsApp), reusing the provider senders
and status recording from ``integrations.services.notification_adapters``.
The delivery rows reference ``performance_report`` instead of an
``AutomationRun`` — the fan-out engine's other family stays untouched.
"""
from __future__ import annotations

import html
import logging
from typing import Any, Optional

from django.conf import settings

from content_analytics.models import ArticlePerformanceReport
from content_factory.models import (
    NotificationChannel,
    NotificationChannelType,
    NotificationConsentState,
    NotificationDelivery,
    NotificationDeliveryStatus,
)
from integrations.services.notification_adapters import (
    _send_email,
    _send_slack,
    _send_whatsapp,
    record_delivery_status,
)

logger = logging.getLogger(__name__)

REPORT_READY_EVENT = "report_ready"


def report_notifications_enabled() -> bool:
    return bool(getattr(settings, "CONTENT_ANALYTICS_REPORT_NOTIFICATIONS_ENABLED", False))


def _brief_url() -> str:
    base_url = str(getattr(settings, "FOUNDER_TOOLS_URL", "") or "").rstrip("/")
    if not base_url:
        return ""
    return f"{base_url}/founder-tools/marketing#analytics"


def _pct_display(rate: Any, *, decimals: int = 0) -> str:
    try:
        value = float(rate or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    return f"{value * 100:.{decimals}f}%"


def _delta_display(value: Any) -> str:
    try:
        delta = int(value or 0)
    except (TypeError, ValueError):
        delta = 0
    return f"+{delta}" if delta > 0 else str(delta)


def _headline(report: ArticlePerformanceReport) -> dict[str, Any]:
    payload = report.payload if isinstance(report.payload, dict) else {}
    headline = payload.get("headline")
    return headline if isinstance(headline, dict) else {}


def _categories(report: ArticlePerformanceReport) -> dict[str, int]:
    payload = report.payload if isinstance(report.payload, dict) else {}
    raw = payload.get("categoriesSummary")
    raw = raw if isinstance(raw, dict) else {}
    return {key: int(raw.get(key) or 0) for key in (
        "top_performer",
        "high_interest",
        "needs_attention",
        "gathering_data",
    )}


def _window_days(report: ArticlePerformanceReport) -> int:
    payload = report.payload if isinstance(report.payload, dict) else {}
    window = payload.get("window")
    if isinstance(window, dict):
        try:
            return int(window.get("days") or 0) or 7
        except (TypeError, ValueError):
            pass
    return 7


def _report_text(report: ArticlePerformanceReport) -> str:
    headline = _headline(report)
    categories = _categories(report)
    domain = report.organization.domain
    lines = [
        f"Your article performance brief for {domain} — {report.report_date.isoformat()}.",
        (
            f"Last {_window_days(report)} days: {int(headline.get('humanVisits') or 0)} visits"
            f" ({_delta_display(headline.get('visitsDelta'))} vs prior)"
            f" · {_pct_display(headline.get('engagedReaderRate'))} engaged"
            f" · {int(headline.get('ctaClickers') or 0)} CTA clickers"
            f" ({_pct_display(headline.get('ctaConversionRate'), decimals=1)} conversion)."
        ),
        (
            f"{categories['top_performer']} top performers"
            f" · {categories['high_interest']} high-interest"
            f" · {categories['needs_attention']} needs attention"
            f" · {categories['gathering_data']} gathering data."
        ),
    ]
    url = _brief_url()
    if url:
        lines.append(f"View the full brief: {url}")
    return "\n".join(lines)


def _report_email_html(report: ArticlePerformanceReport) -> str:
    headline = _headline(report)
    categories = _categories(report)
    domain = html.escape(report.organization.domain)
    url = html.escape(_brief_url())
    link_html = f'<p><a href="{url}">Open the full brief</a></p>' if url else ""
    return (
        f"<p>Your article performance brief for <strong>{domain}</strong> — "
        f"{report.report_date.isoformat()}.</p>"
        f"<p>Last {_window_days(report)} days: "
        f"<strong>{int(headline.get('humanVisits') or 0)}</strong> visits "
        f"({html.escape(_delta_display(headline.get('visitsDelta')))} vs prior) · "
        f"{_pct_display(headline.get('engagedReaderRate'))} engaged · "
        f"<strong>{int(headline.get('ctaClickers') or 0)}</strong> CTA clickers "
        f"({_pct_display(headline.get('ctaConversionRate'), decimals=1)} conversion).</p>"
        f"<p>{categories['top_performer']} top performers · "
        f"{categories['high_interest']} high-interest · "
        f"{categories['needs_attention']} needs attention · "
        f"{categories['gathering_data']} gathering data.</p>"
        f"{link_html}"
    )


def _report_email_message_data(
    report: ArticlePerformanceReport,
    channel: Optional[NotificationChannel] = None,
) -> dict[str, Any]:
    """message_data for the Customer.io report transactional template."""
    headline = _headline(report)
    payload = report.payload if isinstance(report.payload, dict) else {}
    articles = payload.get("articles")
    articles = articles if isinstance(articles, list) else []
    top_articles = [
        {
            "title": str(row.get("title") or ""),
            "visits": int((row.get("metrics") or {}).get("visits") or 0),
            "conversion_display": _pct_display(
                (row.get("metrics") or {}).get("ctaConversionRate"), decimals=1
            ),
            "category_label": str(row.get("categoryLabel") or ""),
        }
        for row in articles[:3]
        if isinstance(row, dict)
    ]
    user = channel.user if channel else None
    return {
        "first_name": str(getattr(user, "first_name", "") or "").strip(),
        "domain": report.organization.domain,
        "report_date": report.report_date.isoformat(),
        "window_days": _window_days(report),
        "human_visits": int(headline.get("humanVisits") or 0),
        "visits_delta_display": _delta_display(headline.get("visitsDelta")),
        "engaged_rate_display": _pct_display(headline.get("engagedReaderRate")),
        "cta_clickers": int(headline.get("ctaClickers") or 0),
        "conversion_display": _pct_display(headline.get("ctaConversionRate"), decimals=1),
        "categories": _categories(report),
        "top_articles": top_articles,
        "brief_url": _brief_url(),
    }


def _active_channels_for_report(report: ArticlePerformanceReport) -> list[NotificationChannel]:
    """Same targeting rule as the automation fan-out: consented + selected."""
    channels = list(
        NotificationChannel.objects.filter(
            organization_id=report.organization_id,
            consent_state=NotificationConsentState.ACTIVE,
            delivery_enabled=True,
        )
    )
    channels.sort(key=lambda channel: (str(channel.channel_type), str(channel.route_id)))
    return channels


def send_report_ready(report: ArticlePerformanceReport) -> list[NotificationDelivery]:
    """Deliver the brief to every active channel, idempotently per channel.

    Calling this is explicit intent — the scheduler gates on
    ``CONTENT_ANALYTICS_REPORT_NOTIFICATIONS_ENABLED`` before invoking it.
    Already-SENT deliveries are never resent; PENDING/FAILED ones retry.
    """
    text = _report_text(report)
    subject = (
        f"Article performance brief — {report.organization.domain} "
        f"{report.report_date.isoformat()}"
    )
    html_body = _report_email_html(report)
    template_id = str(getattr(settings, "CUSTOMERIO_REPORT_TEMPLATE_ID", "") or "").strip()

    deliveries: list[NotificationDelivery] = []
    for channel in _active_channels_for_report(report):
        delivery, created = NotificationDelivery.objects.get_or_create(
            performance_report=report,
            channel=channel,
            event_type=REPORT_READY_EVENT,
            idempotency_key=f"report:{report.pk}:{channel.pk}:{REPORT_READY_EVENT}",
            defaults={
                "status": NotificationDeliveryStatus.PENDING,
                "request_payload": {
                    "domain": report.organization.domain,
                    "report_date": report.report_date.isoformat(),
                },
            },
        )
        if not created and delivery.status == NotificationDeliveryStatus.SENT:
            deliveries.append(delivery)
            continue
        if channel.consent_state != NotificationConsentState.ACTIVE:
            deliveries.append(
                record_delivery_status(
                    delivery,
                    status=NotificationDeliveryStatus.OPTED_OUT,
                    error=f"Channel consent state is {channel.consent_state}",
                )
            )
            continue
        try:
            if channel.channel_type == NotificationChannelType.SLACK:
                success, provider_id, response_payload = _send_slack(channel, text=text)
            elif channel.channel_type == NotificationChannelType.EMAIL:
                success, provider_id, response_payload = _send_email(
                    channel,
                    subject=subject,
                    text=text,
                    html_body=html_body,
                    idempotency_key=delivery.idempotency_key,
                    message_data=(
                        _report_email_message_data(report, channel) if template_id else None
                    ),
                    transactional_message_id=template_id,
                )
            elif channel.channel_type == NotificationChannelType.WHATSAPP:
                content_sid = str(
                    channel.provider_metadata.get(f"{REPORT_READY_EVENT}_content_sid") or ""
                ).strip()
                success, provider_id, response_payload = _send_whatsapp(
                    channel,
                    text=text,
                    content_sid=content_sid,
                    content_variables=None,
                )
            else:
                success, provider_id, response_payload = (
                    False,
                    "",
                    {"error": f"Unsupported channel {channel.channel_type}"},
                )
        except Exception as exc:
            logger.warning(
                "Report notification delivery failed for report %s: %s", report.pk, exc
            )
            deliveries.append(
                record_delivery_status(
                    delivery,
                    status=NotificationDeliveryStatus.FAILED,
                    error=str(exc),
                )
            )
            continue
        if success:
            deliveries.append(
                record_delivery_status(
                    delivery,
                    status=NotificationDeliveryStatus.SENT,
                    provider_message_id=provider_id,
                    response_payload=response_payload,
                )
            )
        else:
            deliveries.append(
                record_delivery_status(
                    delivery,
                    status=NotificationDeliveryStatus.FAILED,
                    response_payload=response_payload,
                    error=str(response_payload.get("error") or response_payload),
                )
            )
    return deliveries
