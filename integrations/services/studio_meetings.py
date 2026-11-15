"""Project calendar enrichment and separately approved participant time."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from django.db import transaction

from integrations.models import StudioMeetingOccurrence
from mentoring.calendars.google import GoogleCalendarClient
from .linear_meeting_actions import (create_linear_meeting_issue, _create_linear_channel_issue_comment,
                                    list_issue_labels, _label_is_compatible_with_team)
from .studio_payment_rules import ReviewError, digest
from .studio_treasury import scope_for
from .studio_treasury_sources import StudioSources, SIZES

TZ = ZoneInfo("Australia/Melbourne")


def planned_sizing(project, settings, seconds):
    """Use the existing upper-bound rubric for planning, with no payable time."""
    effort = next((name for name, maximum in SIZES.items() if seconds <= maximum), None)
    if effort is None:
        raise ReviewError("meeting_size_needs_review")
    compatible = [label for label in list_issue_labels() if label.get("name") == effort
                  and not label.get("archivedAt") and _label_is_compatible_with_team(label, settings["team_id"])]
    if len(compatible) != 1:
        raise ReviewError("meeting_effort_label_needs_review")
    return {"label_ids": [compatible[0]["id"]], "sizing_metadata": {
        "projectId": project, "projectNameAtAssessment": settings["name"],
        "rubricVersion": "studio-upper-bound-v1", "effortLabel": effort,
        "rationale": f"Planned check-in for {seconds/60:g} minutes, with attendance confirmed after it finishes."}}


def instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ReviewError("event_timezone_missing")
    return parsed.astimezone(timezone.utc)


def occurrence_key(calendar_id, event):
    identity = event.get("iCalUID") or calendar_id + ":" + event["id"]
    original = event.get("originalStartTime", {}).get("dateTime")
    if event.get("recurringEventId"):
        if not original:
            raise ReviewError("recurrence_identity_missing")
        identity += ":" + instant(original).isoformat()
    return digest(identity)


def calendar_client(binding):
    """Use only an explicitly provisioned Studio calendar grant."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    config = json.loads(Path(binding["credentials_path"]).read_text())
    scopes = set(config.get("scopes", []))
    if not scopes.intersection({"https://www.googleapis.com/auth/calendar.readonly",
                               "https://www.googleapis.com/auth/calendar.events.readonly"}):
        raise ReviewError("calendar_read_scope_missing")
    credentials = Credentials(token=None, refresh_token=config["refresh_token"], client_id=config["client_id"],
        client_secret=config["client_secret"], token_uri="https://oauth2.googleapis.com/token")
    credentials.refresh(Request())
    return GoogleCalendarClient(credentials.token)


def events(binding, start, end, *, client_factory=calendar_client):
    client, cursor = client_factory(binding), None
    for _ in range(10):
        params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                  "showDeleted": "true", "maxResults": 250, "timeZone": str(TZ)}
        if cursor:
            params["pageToken"] = cursor
        page = client.api("GET", client.events_path(binding["calendar_id"]), params=params)
        yield from page["items"]
        cursor = page.get("nextPageToken")
        if not cursor:
            return
    raise ReviewError("calendar_page_limit")


def matching_tickets(issues, project, event, day):
    result = []
    for issue in issues:
        if (issue.get("project") or {}).get("id") != project:
            continue
        text = issue["title"] + "\n" + (issue.get("description") or "")
        marker = "Studio meeting " + occurrence_key(event["_calendar"], event)
        if marker in text:
            return [issue]
        # Conservative historical matching includes multi-date titles.
        local = day.astimezone(TZ)
        dates = (local.date().isoformat(), local.strftime("%d/%m"),
                 str(local.day) + local.strftime(" %B %Y"))
        if re.search(r"meeting|check.in|project meetings", issue["title"], re.I) and any(d in text for d in dates):
            result.append(issue)
    return result


def enrich(workspace, actor, projects, *, mode="review", now=None, start=None, end=None,
           source_factory=StudioSources, event_reader=events):
    scope = scope_for(workspace, actor, treasury=False)
    now = now or datetime.now(timezone.utc)
    start = instant(start) if start else now - timedelta(days=1)
    end = instant(end) if end else now + timedelta(days=14)
    if end <= start or end - start > timedelta(days=32) or mode not in {"review", "create"}:
        raise ReviewError("invalid_calendar_window")
    issues = source_factory(scope).issues()
    rows, seen = [], set()
    for project in projects:
        settings = scope.configuration["projects"].get(project)
        if not settings:
            continue
        for binding in settings.get("calendars", []):
            for event in event_reader(binding, start, end):
                if event.get("status") == "cancelled":
                    known = StudioMeetingOccurrence.objects.filter(ledger=scope, project_id=project,
                        calendar_evidence__event_id=event["id"], calendar_evidence__calendar_id=binding["calendar_id"]).first()
                    if known:
                        if mode == "create":
                            known.cancelled = True
                            known.save(update_fields=["cancelled", "updated_at"])
                        rows.append({"key": known.occurrence_key, "project": project, "title": known.calendar_evidence["title"],
                            "start": known.starts_at.isoformat(), "end": known.ends_at.isoformat(), "action": "cancelled",
                            "issue": known.linear_issue, "confirmation_needed": False})
                    continue
                if (event.get("visibility") == "private" or event.get("eventType", "default") != "default"
                        or "dateTime" not in event.get("start", {})):
                    continue
                title = event.get("summary", "")
                if not any(re.fullmatch(pattern, title, re.I) for pattern in binding["title_patterns"]):
                    continue
                key = occurrence_key(binding["calendar_id"], event)
                if key in seen:
                    continue
                seen.add(key)
                starts, ends = instant(event["start"]["dateTime"]), instant(event["end"]["dateTime"])
                if ends <= starts:
                    continue
                cancelled = event.get("status") == "cancelled"
                event["_calendar"] = binding["calendar_id"]
                matches = matching_tickets(issues, project, event, starts)
                stored = StudioMeetingOccurrence.objects.filter(ledger=scope, occurrence_key=key).first()
                linked = stored.linear_issue if stored else {}
                action = "linked" if linked or len(matches) == 1 else "needs_review" if matches else "planned"
                if mode == "create":
                    create_input = None
                    with transaction.atomic():
                        row, _ = StudioMeetingOccurrence.objects.select_for_update().get_or_create(
                            ledger=scope, occurrence_key=key, defaults={"project_id": project,
                                "starts_at": starts, "ends_at": ends})
                        if row.project_id != project:
                            raise ReviewError("calendar_project_conflict")
                        row.starts_at, row.ends_at, row.cancelled = starts, ends, cancelled
                        row.calendar_evidence = {"url": event.get("htmlLink", ""), "updated": event.get("updated"),
                            "event_id": event["id"], "calendar_id": binding["calendar_id"],
                            "candidate_issues": [{k: match[k] for k in ("id", "identifier", "title")} for match in matches],
                            "title": title, "attendees": [a["email"].casefold() for a in event.get("attendees", [])
                                if a.get("responseStatus") != "declined" and a.get("email")]}
                        if not row.linear_issue and len(matches) == 1:
                            row.linear_issue = {k: matches[0][k] for k in ("id", "identifier", "title")}
                        if not row.linear_issue and not matches and not cancelled:
                            create_input = {"title": title + " — " + starts.astimezone(TZ).strftime("%d %B %Y"),
                                "team_id": settings["team_id"], "project_id": project,
                                "description": "Studio meeting " + key + "\n\nScheduled start: " + starts.isoformat()
                                    + "\nScheduled end: " + ends.isoformat()
                                    + "\n\nPlanned meeting. Each participant confirms their own time after the meeting.",
                                "idempotency_key": digest([scope.workspace_id, key])}
                        row.save()
                        linked = row.linear_issue
                    if create_input:
                        create_input.update(planned_sizing(project, settings, int((ends-starts).total_seconds())))
                        # The existing Linear receipt must commit outside our row transaction.
                        # A crash after the provider write can then replay its exact issue.
                        issue = create_linear_meeting_issue(create_input)
                        with transaction.atomic():
                            row = StudioMeetingOccurrence.objects.select_for_update().get(pk=row.pk)
                            if row.linear_issue and row.linear_issue.get("id") != issue["id"]:
                                raise ReviewError("meeting_link_changed")
                            row.linear_issue = issue
                            row.save(update_fields=["linear_issue", "updated_at"])
                            linked, action = issue, "added"
                rows.append({"key": key, "project": project, "title": title, "start": starts.isoformat(),
                             "end": ends.isoformat(), "action": "cancelled" if cancelled else action,
                             "issue": linked, "confirmation_needed": ends <= now and not cancelled,
                             "notify_slack_ids": [identity["slack_id"] for identity in scope.configuration["builders"].values()
                                 if set(identity.get("senders", [])) & {a.get("email", "").casefold() for a in event.get("attendees", [])
                                     if a.get("responseStatus") != "declined"}]})
                if len(rows) >= 20:
                    return rows
    return rows


def confirm(workspace, actor, key, minutes, *, builder=None, approved=False):
    scope = scope_for(workspace, actor, treasury=False)
    own = next((k for k, b in scope.configuration["builders"].items() if b["slack_id"] == actor), None)
    builder = builder or own
    if type(approved) is not bool or builder not in scope.configuration["builders"]:
        raise ReviewError("invalid_participant_approval")
    if not builder or (builder != own or approved) and actor not in scope.configuration["treasurers"]:
        raise PermissionError("attendance_approval_not_authorized")
    if type(minutes) is not int or not 0 < minutes <= 24 * 60:
        raise ReviewError("invalid_attended_minutes")
    with transaction.atomic():
        row = StudioMeetingOccurrence.objects.select_for_update().get(ledger=scope, occurrence_key=key)
        if row.cancelled or row.ends_at > datetime.now(timezone.utc) or not row.linear_issue:
            raise ReviewError("meeting_not_ready_for_time")
        current = row.participation.get(builder)
        if current and current.get("approved"):
            if current["minutes"] != minutes:
                raise ReviewError("approved_time_requires_correction")
            return current
        if (current and not current.get("comment_pending") and current["minutes"] == minutes
                and current["actor"] == actor and current.get("approval_requested") == approved):
            return current
        value = {"minutes": minutes, "approved": False, "approval_requested": approved, "actor": actor,
                 "work_date": (current or {}).get("work_date") or row.starts_at.astimezone(TZ).date().isoformat(),
                 "comment_pending": True,
                 "recorded_at": datetime.now(timezone.utc).isoformat()}
        if current and current.get("comment_pending"):
            raise ReviewError("meeting_time_note_needs_review")
        row.participation[builder] = value
        row.save(update_fields=["participation", "updated_at"])
    note = ("Studio meeting attendance " + key + ":" + builder + "\nParticipant: " + builder
            + "\nActual time: " + str(minutes) + " minutes\nRecorded by: " + actor
            + "\nWork date: " + value["work_date"]
            + "\nApproval: " + ("approved" if approved else "awaiting treasurer"))
    issue_id = row.linear_issue.get("id")
    comment = _create_linear_channel_issue_comment(issue_id, note)["comment"]
    with transaction.atomic():
        row = StudioMeetingOccurrence.objects.select_for_update().get(pk=row.pk)
        row.participation[builder].update(approved=approved, comment_pending=False, comment_id=comment["id"], note_hash=digest(note))
        row.save(update_fields=["participation", "updated_at"])
    return row.participation[builder]


def pending_times(workspace, actor):
    scope = scope_for(workspace, actor, treasury=False)
    own = next((key for key, value in scope.configuration["builders"].items() if value["slack_id"] == actor), None)
    rows = StudioMeetingOccurrence.objects.filter(ledger=scope, cancelled=False,
        ends_at__lte=datetime.now(timezone.utc)).order_by("-starts_at")[:30]
    output = []
    for row in rows:
        if not row.linear_issue:
            continue
        participants = [own] if own else []
        if actor in scope.configuration["treasurers"]:
            participants = list(set(participants) | set(row.participation))
        for builder in participants:
            current = row.participation.get(builder)
            identity = scope.configuration["builders"][builder]
            if current and current.get("approved"):
                continue
            if not current and not set(identity["senders"]) & set(row.calendar_evidence.get("attendees", [])):
                continue
            output.append({"key": row.occurrence_key, "builder": builder, "title": row.calendar_evidence["title"],
                "issue": row.linear_issue, "minutes": current["minutes"] if current else max(1, min(1440, int((row.ends_at-row.starts_at).total_seconds()/60))),
                "approve": bool(current and actor in scope.configuration["treasurers"]),
                "awaiting_approval": bool(current)})
    return output


def pending_links(workspace, actor):
    scope = scope_for(workspace, actor, treasury=False)
    return [{"key": row.occurrence_key, "title": row.calendar_evidence["title"],
             "candidates": row.calendar_evidence.get("candidate_issues", [])[:5]}
            for row in StudioMeetingOccurrence.objects.filter(ledger=scope, cancelled=False,
                linear_issue={}).order_by("starts_at")[:20]]


def link(workspace, actor, key, issue_id, source_factory=StudioSources):
    scope = scope_for(workspace, actor, treasury=False)
    row = StudioMeetingOccurrence.objects.get(ledger=scope, occurrence_key=key)
    issue = next((issue for issue in source_factory(scope).issues() if issue["id"] == issue_id
                  and issue.get("project", {}).get("id") == row.project_id), None)
    if not issue:
        raise ReviewError("meeting_ticket_project_mismatch")
    with transaction.atomic():
        row = StudioMeetingOccurrence.objects.select_for_update().get(pk=row.pk)
        if row.linear_issue and row.linear_issue["id"] != issue_id:
            raise ReviewError("meeting_already_linked")
        row.linear_issue = {k: issue[k] for k in ("id", "identifier", "title")}
        row.save(update_fields=["linear_issue", "updated_at"])
    return row.linear_issue
