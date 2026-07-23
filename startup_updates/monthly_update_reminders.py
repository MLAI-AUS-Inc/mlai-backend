"""Idempotent Customer.io reminders for monthly-update discount renewal."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from founder_tools.models import VibeRaisingCompany
from integrations.services.notification_adapters import _customerio_client

from .models import (
    MonthlyUpdateDraft,
    MonthlyUpdateDraftStatus,
    MonthlyUpdateReminderDelivery,
    MonthlyUpdateReminderKind,
    MonthlyUpdateReminderStatus,
    UserStartupBinding,
)

logger = logging.getLogger(__name__)

DISCOUNT_WINDOW_DAYS = 28
REMINDER_DAY_OFFSETS = {
    MonthlyUpdateReminderKind.SEVEN_DAY: 7,
    MonthlyUpdateReminderKind.ONE_DAY: 1,
}
TERMINAL_STATUSES = {
    MonthlyUpdateReminderStatus.SENT,
    MonthlyUpdateReminderStatus.DRAFTED,
    MonthlyUpdateReminderStatus.UNKNOWN,
    MonthlyUpdateReminderStatus.SUPPRESSED,
    MonthlyUpdateReminderStatus.SENDING,
}


@dataclass(frozen=True)
class MonthlyUpdateReminderTarget:
    user_id: Any
    recipient_email: str
    first_name: str
    organization_id: int
    organization_name: str
    company_id: str
    company_name: str
    domain: str
    source_update_id: int
    ready_date: date
    valid_through: date
    expires_on: date
    reminder_kind: str
    reminder_date: date
    update_url: str


def _template_id(reminder_kind: str) -> str:
    setting_name = (
        "CUSTOMERIO_MONTHLY_UPDATE_7D_TEMPLATE_ID"
        if reminder_kind == MonthlyUpdateReminderKind.SEVEN_DAY
        else "CUSTOMERIO_MONTHLY_UPDATE_1D_TEMPLATE_ID"
    )
    return str(getattr(settings, setting_name, "") or "").strip()


def _display_date(value: date) -> str:
    return f"{value.day} {value.strftime('%B %Y')}"


def _update_url(company_id: str, reminder_kind: str) -> str:
    next_query = urlencode(
        {
            "companyId": company_id,
            "source": "monthly-update-reminder",
            "reminder": reminder_kind,
        }
    )
    next_path = f"/founder-tools/updates/create?{next_query}"
    login_query = urlencode({"app": "founder-tools", "next": next_path})
    app_url = str(getattr(settings, "MONTHLY_UPDATE_REMINDER_APP_URL", "https://mlai.au") or "").rstrip("/")
    return f"{app_url}/platform/login?{login_query}"


def collect_monthly_update_reminder_targets(reminder_date: date) -> list[MonthlyUpdateReminderTarget]:
    """Return only exact-day reminders; rollout never catches up old reminders."""
    companies = list(
        VibeRaisingCompany.objects.filter(
            organization__isnull=False,
            profile__user__is_active=True,
            registered=True,
            abr_verified_at__isnull=False,
        )
        .exclude(acn__isnull=True)
        .exclude(acn="")
        .exclude(profile__user__email="")
        .select_related("profile__user", "organization")
        .order_by("profile__user_id", "organization_id", "created_at")
    )
    if not companies:
        return []

    user_org_pairs = {(company.profile.user_id, company.organization_id) for company in companies}
    eligible_pairs = set(
        UserStartupBinding.objects.filter(
            user_id__in={user_id for user_id, _ in user_org_pairs},
            organization_id__in={organization_id for _, organization_id in user_org_pairs},
            coworking_discount_eligible=True,
        )
        .values_list("user_id", "organization_id")
    )
    owned_eligible_pairs = user_org_pairs & eligible_pairs
    if not owned_eligible_pairs:
        return []

    organization_ids = {organization_id for _, organization_id in owned_eligible_pairs}
    latest_ready_by_org: dict[int, MonthlyUpdateDraft] = {}
    ready_updates = MonthlyUpdateDraft.objects.filter(
        organization_id__in=organization_ids,
        status=MonthlyUpdateDraftStatus.READY,
        ready_at__isnull=False,
    ).order_by("organization_id", "-ready_at", "-id")
    for update in ready_updates:
        latest_ready_by_org.setdefault(update.organization_id, update)

    targets: list[MonthlyUpdateReminderTarget] = []
    seen_pairs: set[tuple[Any, int]] = set()
    for company in companies:
        pair = (company.profile.user_id, company.organization_id)
        if pair in seen_pairs or pair not in owned_eligible_pairs:
            continue
        seen_pairs.add(pair)
        update = latest_ready_by_org.get(company.organization_id)
        if update is None or update.ready_at is None:
            continue

        ready_date = update.ready_at.date()
        valid_through = ready_date + timedelta(days=DISCOUNT_WINDOW_DAYS)
        expires_on = valid_through + timedelta(days=1)
        due_kinds = [
            reminder_kind
            for reminder_kind, days_before in REMINDER_DAY_OFFSETS.items()
            if expires_on - timedelta(days=days_before) == reminder_date
        ]
        user = company.profile.user
        for reminder_kind in due_kinds:
            targets.append(
                MonthlyUpdateReminderTarget(
                    user_id=user.pk,
                    recipient_email=user.email.strip(),
                    first_name=(user.first_name or "").strip(),
                    organization_id=company.organization_id,
                    organization_name=(company.organization.name or company.name).strip(),
                    company_id=str(company.pk),
                    company_name=company.name.strip(),
                    domain=(company.domain or company.organization.domain or "").strip(),
                    source_update_id=update.pk,
                    ready_date=ready_date,
                    valid_through=valid_through,
                    expires_on=expires_on,
                    reminder_kind=reminder_kind,
                    reminder_date=reminder_date,
                    update_url=_update_url(str(company.pk), reminder_kind),
                )
            )
    return targets


def _group_targets(targets: list[MonthlyUpdateReminderTarget]):
    grouped: dict[tuple[Any, str], list[MonthlyUpdateReminderTarget]] = defaultdict(list)
    for target in targets:
        grouped[(target.user_id, target.reminder_kind)].append(target)
    return grouped


def _snapshot(targets: list[MonthlyUpdateReminderTarget]) -> list[dict[str, Any]]:
    return json.loads(json.dumps([asdict(target) for target in targets], default=str))


def _message_data(targets: list[MonthlyUpdateReminderTarget]) -> dict[str, Any]:
    first = targets[0]
    return {
        "first_name": first.first_name,
        "reminder_type": first.reminder_kind,
        "startups": [
            {
                "name": target.company_name,
                "domain": target.domain,
                "valid_through": target.valid_through.isoformat(),
                "valid_through_display": _display_date(target.valid_through),
                "expires_on": target.expires_on.isoformat(),
                "expires_on_display": _display_date(target.expires_on),
                "update_url": target.update_url,
            }
            for target in targets
        ],
    }


def _dispatch_group(targets: list[MonthlyUpdateReminderTarget]) -> dict[str, Any]:
    first = targets[0]
    template_id = _template_id(first.reminder_kind)
    idempotency_key = (
        f"monthly-update-reminder:{first.user_id}:{first.reminder_kind}:{first.reminder_date.isoformat()}"
    )
    snapshot = _snapshot(targets)
    with transaction.atomic():
        delivery, _ = MonthlyUpdateReminderDelivery.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults={
                "user_id": first.user_id,
                "reminder_kind": first.reminder_kind,
                "reminder_date": first.reminder_date,
                "recipient_email": first.recipient_email,
                "template_id": template_id,
                "target_snapshot": snapshot,
            },
        )
        delivery = MonthlyUpdateReminderDelivery.objects.select_for_update().get(pk=delivery.pk)
        if delivery.status in TERMINAL_STATUSES:
            return {"status": "skipped", "reason": f"already_{delivery.status}", "delivery_id": delivery.pk}

        current_group = _group_targets(collect_monthly_update_reminder_targets(first.reminder_date)).get(
            (first.user_id, first.reminder_kind), []
        )
        current_update_ids = {target.source_update_id for target in current_group}
        targets = [target for target in targets if target.source_update_id in current_update_ids]
        if not targets:
            delivery.status = MonthlyUpdateReminderStatus.SUPPRESSED
            delivery.last_error = "Eligibility or latest monthly update changed before dispatch."
            delivery.save(update_fields=["status", "last_error", "updated_at"])
            return {"status": "suppressed", "delivery_id": delivery.pk}

        delivery.status = MonthlyUpdateReminderStatus.SENDING
        delivery.recipient_email = first.recipient_email
        delivery.template_id = template_id
        delivery.target_snapshot = _snapshot(targets)
        delivery.attempt_count += 1
        delivery.last_error = ""
        delivery.save(
            update_fields=[
                "status",
                "recipient_email",
                "template_id",
                "target_snapshot",
                "attempt_count",
                "last_error",
                "updated_at",
            ]
        )

    request_body: dict[str, Any] = {
        "to": first.recipient_email,
        "identifiers": {"id": str(first.user_id)},
        "transactional_message_id": template_id,
        "message_data": _message_data(targets),
        "send_to_unsubscribed": False,
        "tracked": True,
        "queue_draft": bool(getattr(settings, "MONTHLY_UPDATE_REMINDERS_QUEUE_DRAFT", True)),
    }
    from_email = str(getattr(settings, "CUSTOMERIO_FROM_EMAIL", "") or "").strip()
    if from_email:
        request_body["from"] = from_email

    try:
        client = _customerio_client()
        if client is None:
            raise RuntimeError("CUSTOMERIO_API_KEY is not configured")
        response = client.send_email(request_body)
        response_payload = response if isinstance(response, dict) else {"response": str(response)}
        response_payload = json.loads(json.dumps(response_payload, default=str))
    except Exception as exc:
        # A network timeout may occur after Customer.io accepted the request.
        # Quarantine the row instead of automatically retrying a possible send.
        MonthlyUpdateReminderDelivery.objects.filter(pk=delivery.pk).update(
            status=MonthlyUpdateReminderStatus.UNKNOWN,
            last_error=str(exc),
        )
        logger.exception("Monthly-update reminder delivery became unknown: %s", idempotency_key)
        return {"status": "unknown", "delivery_id": delivery.pk, "error": str(exc)}

    queued_as_draft = bool(getattr(settings, "MONTHLY_UPDATE_REMINDERS_QUEUE_DRAFT", True))
    final_status = MonthlyUpdateReminderStatus.DRAFTED if queued_as_draft else MonthlyUpdateReminderStatus.SENT
    provider_delivery_id = str(response_payload.get("delivery_id") or "")
    MonthlyUpdateReminderDelivery.objects.filter(pk=delivery.pk).update(
        status=final_status,
        customerio_delivery_id=provider_delivery_id,
        provider_response=response_payload,
        dispatched_at=timezone.now(),
    )
    return {"status": final_status, "delivery_id": delivery.pk, "customerio_delivery_id": provider_delivery_id}


def run_monthly_update_reminder_scheduler(
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not dry_run and not bool(getattr(settings, "MONTHLY_UPDATE_REMINDERS_ENABLED", False)):
        return {"status": "skipped", "reason": "disabled"}

    timezone_name = str(getattr(settings, "MONTHLY_UPDATE_REMINDER_TIMEZONE", "Australia/Melbourne"))
    local_now = (now or timezone.now()).astimezone(ZoneInfo(timezone_name))
    scheduled_time = (
        int(getattr(settings, "MONTHLY_UPDATE_REMINDER_HOUR", 9)),
        int(getattr(settings, "MONTHLY_UPDATE_REMINDER_MINUTE", 0)),
    )
    if not dry_run and (local_now.hour, local_now.minute) < scheduled_time:
        return {"status": "skipped", "reason": "before_schedule_window", "local_date": local_now.date().isoformat()}

    targets = collect_monthly_update_reminder_targets(local_now.date())
    groups = _group_targets(targets)
    if dry_run:
        return {
            "status": "dry_run",
            "local_date": local_now.date().isoformat(),
            "recipient_count": len(groups),
            "target_count": len(targets),
            "recipients": [
                {
                    "email": group[0].recipient_email,
                    "reminder_kind": group[0].reminder_kind,
                    "companies": [target.company_name for target in group],
                }
                for group in groups.values()
            ],
        }

    missing_templates = sorted({kind for _, kind in groups if not _template_id(kind)})
    if groups and missing_templates:
        return {"status": "failed", "reason": "missing_template_ids", "reminder_kinds": missing_templates}
    if groups and not str(getattr(settings, "CUSTOMERIO_API_KEY", "") or "").strip():
        return {"status": "failed", "reason": "missing_customerio_api_key"}

    outcomes = [_dispatch_group(group) for group in groups.values()]
    return {
        "status": "completed",
        "local_date": local_now.date().isoformat(),
        "recipient_count": len(groups),
        "target_count": len(targets),
        "outcomes": outcomes,
    }
