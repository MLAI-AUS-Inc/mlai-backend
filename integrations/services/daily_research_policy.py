"""Delivery-based daily research memory. Existing JSON fields keep this schema-free."""
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.db.models import Q
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
            if resume and (memory.get("pause_reason") in {PAUSE_REASON, POINTS_PAUSE_REASON}
                           or automation.status == "active"):
                automation.status = "active"
                for key in ("pause_reason", "paused_at", "points_required", "points_balance"):
                    memory.pop(key, None)
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
    from content_factory.models import ContentIsland, NotificationDelivery
    from content_factory.research_workspace import current_research_runs, research_config, research_cutoff
    from workflow_runs.models import ContentFactoryRun
    current = now or timezone.now()
    organization = run.automation.organization
    config = research_config(organization)
    cutoff = research_cutoff(config)
    deliveries = list(NotificationDelivery.objects.filter(
        automation_run__automation__organization=organization, event_type="topic_selection", status="sent",
        delivered_at__gte=max(current - timedelta(days=30), cutoff) if cutoff else current - timedelta(days=30),
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
    preferences, managed_slugs = [], set()
    sources = ContentFactoryRun.objects.filter(organization=organization).filter(
        Q(result__island_research_selection__isnull=False)
        | Q(run_request__custom_topic_keyword__isnull=False)
        | Q(run_request__custom_topic_title__isnull=False)
        | Q(run_request__custom_topic_seed__isnull=False)
    ).only("run_id", "created_at", "status", "run_request", "result").order_by("created_at")
    if cutoff:
        sources = sources.filter(created_at__gte=cutoff)
    for source in current_research_runs(sources, config):
        request, result = source.run_request or {}, source.result or {}
        state = result.get("island_research_selection", {})
        managed_slugs.update(state.get("managed_slugs", []))
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
    # Older direct custom-island creation predates researched selection runs.
    for island in ContentIsland.objects.filter(organization=organization, origin="manual", status="visible",
            created_at__gte=current - timedelta(days=30)).exclude(slug__in=managed_slugs):
        key = f"custom-island:{island.pk}"
        if key not in consumed:
            preferences.append({"id": key, "keywords": [island.pillar_keyword or island.name]})
    return {"version": 1, "cooldown_days": COOLDOWN_DAYS, "recent_topics": list(recent.values()),
            "preferences": preferences[:20]}


# B23: research that finds nothing new on a scheduled night is an empty,
# completed run. After this many consecutive empty local nights the founder
# hears once, with a suggestion to add seed topics or competitors.
EMPTY_NIGHTS_BEFORE_NOTICE = 3
EMPTY_STREAK_KEY = "empty_nights"
EMPTY_NOTICE_KEY = "empty_notice_sent_for"


def record_empty_night(run, *, now=None):
    """Record one empty scheduled night and say whether its streak's notice is now due.

    Nights are local calendar dates in the automation's time zone, so a
    callback replay or two empty slots on one night count once. A gap of a
    night starts a new streak. The notice is due once per streak: the first
    time the streak reaches ``EMPTY_NIGHTS_BEFORE_NOTICE``.
    """
    from content_factory.models import ResearchAutomation
    current = now or timezone.now()
    night = (run.scheduled_for_at or current).astimezone(zone(run.automation.timezone)).date()
    with transaction.atomic():
        automation = ResearchAutomation.objects.select_for_update().get(pk=run.automation_id)
        metadata = dict(automation.metadata or {})
        memory = dict(metadata.get(MEMORY_KEY) or {})
        nights = [str(value) for value in memory.get(EMPTY_STREAK_KEY) or []]
        last = nights[-1] if nights else ""
        if night.isoformat() == last:
            return False
        if last and (night - timedelta(days=1)).isoformat() != last:
            nights = []
            memory.pop(EMPTY_NOTICE_KEY, None)
        nights = (nights + [night.isoformat()])[-EMPTY_NIGHTS_BEFORE_NOTICE:]
        due = len(nights) >= EMPTY_NIGHTS_BEFORE_NOTICE and not memory.get(EMPTY_NOTICE_KEY)
        if due:
            memory[EMPTY_NOTICE_KEY] = nights[0]
        memory[EMPTY_STREAK_KEY] = nights
        metadata[MEMORY_KEY] = memory
        automation.metadata = metadata
        automation.save(update_fields=["metadata", "updated_at"])
        return due


def reset_empty_streak(organization):
    """A night with topics ends the empty streak, so a later streak may notify again."""
    from content_factory.models import ResearchAutomation
    with transaction.atomic():
        for automation in ResearchAutomation.objects.select_for_update().filter(organization=organization):
            metadata = dict(automation.metadata or {})
            memory = dict(metadata.get(MEMORY_KEY) or {})
            if EMPTY_STREAK_KEY not in memory and EMPTY_NOTICE_KEY not in memory:
                continue
            memory.pop(EMPTY_STREAK_KEY, None)
            memory.pop(EMPTY_NOTICE_KEY, None)
            metadata[MEMORY_KEY] = memory
            automation.metadata = metadata
            automation.save(update_fields=["metadata", "updated_at"])


# B18: one insufficient-points result pauses the automation instead of
# retrying (and failing) every night. The founder hears once per pause.
POINTS_PAUSE_REASON = "insufficient_points"


def pause_for_insufficient_points(organization, *, required_points=None, current_balance=None, now=None):
    """Pause every active automation of ``organization`` after a points refusal.

    Returns True only when this call paused something, so the caller sends the
    one founder notice; a replay or a second refusal while paused returns False.
    Scheduled nightly slots are cancelled. A manual "Run today now" slot is left
    to its own result.
    """
    from content_factory.models import AutomationRun, OrganizationContentConfig, ResearchAutomation
    current = now or timezone.now()
    with transaction.atomic():
        active = list(ResearchAutomation.objects.select_for_update().filter(organization=organization, status="active"))
        if not active:
            return False
        for automation in active:
            metadata = dict(automation.metadata or {})
            memory = {**metadata.get(MEMORY_KEY, {}), "pause_reason": POINTS_PAUSE_REASON,
                      "paused_at": current.isoformat()}
            if required_points is not None:
                memory["points_required"] = int(required_points)
            if current_balance is not None:
                memory["points_balance"] = int(current_balance)
            metadata[MEMORY_KEY] = memory
            automation.metadata, automation.status = metadata, "paused"
            automation.save(update_fields=["metadata", "status", "updated_at"])
        OrganizationContentConfig.objects.filter(organization=organization).update(daily_discovery_enabled=False)
        AutomationRun.objects.filter(automation__organization=organization, status="scheduled", slot_index__lt=100).update(
            status="cancelled", last_error=POINTS_PAUSE_REASON, updated_at=current)
        return True


def resume_after_points(organization, *, balance, now=None):
    """Resume automations paused for points once the wallet covers the next run.

    Only a points pause is lifted here; a founder's own pause or the
    unanswered-days pause stays until the founder acts.
    """
    from content_factory.models import OrganizationContentConfig, ResearchAutomation
    with transaction.atomic():
        resumed = 0
        for automation in ResearchAutomation.objects.select_for_update().filter(organization=organization, status="paused"):
            metadata = dict(automation.metadata or {})
            memory = dict(metadata.get(MEMORY_KEY) or {})
            if memory.get("pause_reason") != POINTS_PAUSE_REASON:
                continue
            required = int(memory.get("points_required") or 0)
            if int(balance or 0) < max(required, 1):
                continue
            for key in ("pause_reason", "paused_at", "points_required", "points_balance"):
                memory.pop(key, None)
            metadata[MEMORY_KEY] = memory
            automation.metadata, automation.status = metadata, "active"
            automation.save(update_fields=["metadata", "status", "updated_at"])
            resumed += 1
        if resumed:
            OrganizationContentConfig.objects.filter(organization=organization).update(daily_discovery_enabled=True)
        return resumed
