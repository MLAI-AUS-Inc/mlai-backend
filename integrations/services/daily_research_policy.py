"""Delivery-based daily research memory. Existing JSON fields keep this schema-free."""
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

MEMORY_KEY = "daily_research"
PAUSE_REASON = "three_unanswered_days"
COOLDOWN_DAYS = 7


def zone(name):
    try:
        return ZoneInfo(name or "Australia/Melbourne")
    except ZoneInfoNotFoundError:
        return ZoneInfo("Australia/Melbourne")


def unanswered_days(deliveries, *, since, now, timezone_name):
    """Fan-out, retries and twice-daily slots count as one delivered local day."""
    local_zone = zone(timezone_name)
    today = now.astimezone(local_zone).date()
    return sorted({sent.astimezone(local_zone).date() for sent in deliveries
                   if sent and sent > since and sent.astimezone(local_zone).date() < today})


def record_engagement(organization, *, resume=False, now=None):
    from content_factory.models import ResearchAutomation, OrganizationContentConfig
    current = now or timezone.now()
    with transaction.atomic():
        for automation in ResearchAutomation.objects.select_for_update().filter(organization=organization):
            metadata = dict(automation.metadata or {})
            memory = dict(metadata.get(MEMORY_KEY) or {})
            memory["last_engaged_at"] = current.isoformat()
            if resume and (memory.get("pause_reason") == PAUSE_REASON or automation.status == "active"):
                automation.status = "active"
                memory.pop("pause_reason", None)
                memory.pop("paused_at", None)
                OrganizationContentConfig.objects.filter(organization=organization).update(daily_discovery_enabled=True)
            metadata[MEMORY_KEY] = memory
            automation.metadata = metadata
            automation.save(update_fields=["metadata", "status", "updated_at"])


def record_manual_pause(organization):
    from content_factory.models import ResearchAutomation
    with transaction.atomic():
        for automation in ResearchAutomation.objects.select_for_update().filter(organization=organization):
            metadata = dict(automation.metadata or {})
            metadata[MEMORY_KEY] = {**metadata.get(MEMORY_KEY, {}), "pause_reason": "user_paused"}
            automation.metadata, automation.status = metadata, "paused"
            automation.save(update_fields=["metadata", "status", "updated_at"])


def pause_if_unanswered(organization, *, now=None):
    from content_factory.models import AutomationRun, NotificationDelivery, OrganizationContentConfig, ResearchAutomation
    current = now or timezone.now()
    with transaction.atomic():
        automations = list(ResearchAutomation.objects.select_for_update().filter(organization=organization).order_by("created_at"))
        active = [a for a in automations if a.status == "active"]
        if not active:
            return any((a.metadata or {}).get(MEMORY_KEY, {}).get("pause_reason") == PAUSE_REASON for a in automations)
        boundaries = [a.created_at for a in automations]
        for a in automations:
            stamp = parse_datetime(str((a.metadata or {}).get(MEMORY_KEY, {}).get("last_engaged_at") or ""))
            if stamp and timezone.is_aware(stamp):
                boundaries.append(stamp)
        # Existing approvals from before this policy also establish engagement.
        latest_selection = AutomationRun.objects.filter(automation__organization=organization).exclude(selected_topic={}).order_by("-updated_at").first()
        if latest_selection and not any((a.metadata or {}).get(MEMORY_KEY, {}).get("last_engaged_at") for a in automations):
            boundaries.append(latest_selection.updated_at)
        since = max(boundaries)
        dates = unanswered_days(NotificationDelivery.objects.filter(
            automation_run__automation__organization=organization, event_type="topic_selection",
            status="sent", delivered_at__gt=since,
            automation_run__slot_index__lt=100,
        ).values_list("delivered_at", flat=True), since=since, now=current, timezone_name=active[0].timezone)
        if len(dates) < 3:
            return False
        for a in active:
            metadata = dict(a.metadata or {})
            metadata[MEMORY_KEY] = {**metadata.get(MEMORY_KEY, {}), "pause_reason": PAUSE_REASON,
                                    "paused_at": current.isoformat(), "unanswered_days": len(dates)}
            a.metadata, a.status = metadata, "paused"
            a.save(update_fields=["metadata", "status", "updated_at"])
        OrganizationContentConfig.objects.filter(organization=organization).update(daily_discovery_enabled=False)
        AutomationRun.objects.filter(automation__organization=organization, status="scheduled", slot_index__lt=100).update(
            status="cancelled", last_error=PAUSE_REASON, updated_at=current)
        return True


def daily_topic_policy(run, *, now=None):
    """Use actual sent cards, not research attempts, to expire novelty/preferences."""
    from content_factory.models import NotificationDelivery
    from workflow_runs.models import ContentFactoryRun
    current = now or timezone.now()
    organization = run.automation.organization
    deliveries = list(NotificationDelivery.objects.filter(
        automation_run__automation__organization=organization, event_type="topic_selection", status="sent",
        delivered_at__gte=current - timedelta(days=30),
    ).only("request_payload", "delivered_at"))
    recent, consumed = {}, set()
    for delivery in deliveries:
        payload = delivery.request_payload or {}
        consumed.update(payload.get("daily_preference_ids") or [])
        if delivery.delivered_at < current - timedelta(days=COOLDOWN_DAYS):
            continue
        for option in payload.get("options") or []:
            keyword = str(option.get("keyword") or option.get("primary_keyword") or "").strip()
            if keyword:
                recent[keyword.casefold()] = {"keyword": keyword, "suggested_title": option.get("suggested_title", "")}
    preferences = []
    for source in ContentFactoryRun.objects.filter(organization=organization, updated_at__gte=current - timedelta(days=30)).order_by("created_at"):
        request, result = source.run_request or {}, source.result or {}
        state = result.get("island_research_selection", {})
        events = state.get("daily_priority_events", [])
        if not events and state.get("selected_ids"):
            events = [{"id": f"island-selection:{source.run_id}:initial", "created_at": source.created_at.isoformat(),
                       "keywords": [k["keyword"] for p in result.get("suggested_islands", [])
                                    if p.get("id") in state["selected_ids"] for k in p.get("keywords", [])][:100]}]
        for event in events:
            created = parse_datetime(event.get("created_at", ""))
            if event.get("id") not in consumed and created and created >= current - timedelta(days=30):
                preferences.append(event)
        seed = request.get("custom_topic_seed") or {}
        keyword = request.get("custom_topic_keyword") or seed.get("keyword") or request.get("custom_topic_title") or seed.get("title")
        key = f"custom-topic:{source.run_id}"
        if keyword and key not in consumed and source.created_at >= current - timedelta(days=30) and source.status not in {"failed", "blocked", "cancelled"}:
            options = (result.get("selection") or {}).get("options") or []
            preferences.append({"id": key, "keywords": list(dict.fromkeys([keyword] + [o["keyword"] for o in options if o.get("keyword")]))[:40]})
    return {"version": 1, "cooldown_days": COOLDOWN_DAYS, "recent_topics": list(recent.values()),
            "preferences": preferences[:20]}
