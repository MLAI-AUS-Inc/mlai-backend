"""Bounded PII-free event imports for Pulse, using the existing event model."""

from django.utils.dateparse import parse_datetime

from startup_updates.activity_scope import activity_window
from startup_updates.evidence_contract import content_hash
from startup_updates.models import StartupEvent
from startup_updates.source_evidence import complete_source, stage_source


def fetch_event_page(run, binding, provider, state):
    """Read one authorized event catalog page without holding database locks."""
    from integrations.models import ExternalServiceConnection

    pins = (run.run_request or {}).get("source_connection_pins") or {}
    connection = (
        ExternalServiceConnection.objects.filter(
            pk=pins.get(provider),
            user=binding.user,
            organization_id=binding.organization_id,
            provider=provider,
        )
        .exclude(status="disconnected")
        .first()
    )
    if connection is None:
        raise PermissionError("The selected event source changed.")
    rows = []
    if provider == "humanitix":
        from integrations.services.humanitix import (
            HumanitixClient,
            sanitise_event_payload,
        )

        page = int(state.get("page", 1))
        payload = HumanitixClient(api_key=connection.access_token).list_events_page(
            page=page, page_size=100
        )
        if int(payload.get("page", page)) != page:
            raise ValueError("Humanitix pagination did not advance.")
        rows = [sanitise_event_payload(item) for item in payload.get("events", [])]
        more = bool(rows) and page * int(payload.get("pageSize", 100)) < int(
            payload.get("total", len(rows))
        )
        next_state = {"page": page + 1, "complete": not more}
    else:
        from integrations.services.luma import LumaAttendeeReportService

        payload = LumaAttendeeReportService(
            api_key=connection.access_token
        ).list_ended_events(
            cursor=state.get("cursor"),
            limit=100,
        )
        rows = [
            {
                "event_id": item.get("id"),
                "name": item.get("name"),
                "start_at": item.get("start_at"),
                "url": item.get("url"),
            }
            for item in payload.get("events", [])
        ]
        cursor = payload.get("next_cursor")
        if payload.get("has_more") and not cursor:
            raise ValueError("Luma omitted its next resource page.")
        visited = state.get("visited", [])
        if cursor and (cursor == state.get("cursor") or cursor in visited):
            raise ValueError("Luma pagination did not advance.")
        next_state = {
            "cursor": cursor,
            "visited": [*visited, cursor] if cursor else visited,
            "complete": not bool(cursor),
        }
    return rows, next_state


def commit_event_page(run, provider, rows, state):
    """Import only dated catalog facts; never infer attendance, sales or success."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(run.run_request.get("reporting_timezone") or "UTC")
    start, end = activity_window(run.run_request["narrative_period"])
    for row in rows:
        stamp = parse_datetime(str(row.get("start_at") or row.get("startDate") or ""))
        if (
            stamp is None
            or stamp.tzinfo is None
            or not start <= stamp < end
            or not row.get("event_id")
        ):
            continue
        source_id = str(row["event_id"])
        text = f"Event listed: {row.get('name') or source_id}. Scheduled start: {stamp.isoformat()}."
        staged = stage_source(
            run,
            provider,
            source_id,
            {
                "cleaned_text": text,
                "event": row,
                "source_provider": provider,
                "source_record_id": source_id,
                "reporting_window": run.run_request["narrative_period"],
            },
        )
        if staged is not None:
            complete_source(
                run,
                provider,
                source_id,
                {
                    "source_fingerprint": staged["source_fingerprint"],
                    "facts": [{"text": text, "source_record_id": source_id}],
                    "extraction_method": "event_catalog_v1",
                },
            )
        StartupEvent.objects.update_or_create(
            organization_id=run.organization_id,
            canonical_key=f"{provider}:catalog:{run.pk}:{content_hash({'id': source_id})}",
            defaults={
                "run": run,
                "source_thread_ids": [f"{provider}:{source_id}"],
                "evidence_message_ids": [f"{provider}:{source_id}"],
                "event_type": "product_milestone",
                "title": str(row.get("name") or source_id)[:255],
                "summary": text,
                "event_date": stamp.astimezone(zone).date(),
                "month_bucket": start.astimezone(zone).date().replace(day=1),
                "confidence": 1.0,
                "needs_review": False,
            },
        )
    run.result = {
        **(run.result or {}),
        "event_source_progress": {
            **((run.result or {}).get("event_source_progress") or {}),
            provider: state,
        },
    }
