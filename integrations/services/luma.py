from __future__ import annotations

import base64
import csv
import io
import json
import re
from datetime import date, datetime, time, timezone
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import requests
from django.conf import settings


MELBOURNE_TIMEZONE = "Australia/Melbourne"


class LumaConfigurationError(RuntimeError):
    """Raised when the backend is missing Luma configuration."""


class LumaAPIError(RuntimeError):
    """Raised when Luma rejects or fails an API request."""

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class LumaAttendeeReportService:
    """Fetch Luma events/guests and build attendee report payloads for Roo."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timeout: float = 30.0,
    ):
        raw_api_key = api_key if api_key is not None else getattr(settings, "LUMA_API_KEY", None)
        raw_base_url = (
            base_url
            if base_url is not None
            else getattr(settings, "LUMA_BASE_URL", "https://public-api.luma.com")
        )
        self.api_key = str(raw_api_key or "").strip()
        self.base_url = str(raw_base_url or "https://public-api.luma.com").rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout

    def build_attendee_report(
        self,
        *,
        event_count: int = 3,
        event_date: Optional[date] = None,
        approval_status: str = "approved",
        include_csv: bool = False,
    ) -> Dict[str, Any]:
        if not self.api_key:
            raise LumaConfigurationError("LUMA_API_KEY is not configured on mlai-backend.")

        count = max(1, min(int(event_count or 3), 10))
        if event_date:
            events = self.get_ended_events_for_date(event_date, count=count)
        else:
            events = self.get_recent_ended_events(count=count)

        report_events: List[Dict[str, Any]] = []
        total_guest_count = 0
        for event in events:
            guests = self.list_guests(
                event_id=str(event.get("id") or ""),
                approval_status=approval_status,
            )
            guest_count = len(guests)
            total_guest_count += guest_count

            event_report: Dict[str, Any] = {
                "event_id": event.get("id", ""),
                "event_name": event.get("name", ""),
                "event_url": event.get("url", ""),
                "start_at": event.get("start_at", ""),
                "end_at": event.get("end_at", ""),
                "approval_status": approval_status,
                "guest_count": guest_count,
                "checked_in_count": sum(1 for guest in guests if _guest_checked_in(guest)),
            }
            if include_csv:
                csv_content = self.build_attendee_csv(event, guests)
                event_report["csv"] = {
                    "filename": self.build_csv_filename(event),
                    "content_base64": base64.b64encode(csv_content.encode("utf-8")).decode("ascii"),
                    "content_type": "text/csv",
                }
            report_events.append(event_report)

        return {
            "events": report_events,
            "total_guest_count": total_guest_count,
        }

    def get_recent_ended_events(
        self,
        *,
        count: int = 3,
        now: Optional[datetime] = None,
        timezone_name: str = MELBOURNE_TIMEZONE,
    ) -> List[Dict[str, Any]]:
        now_utc = _local_now_utc(now, timezone_name)
        events: List[Dict[str, Any]] = []
        cursor = None
        while len(events) < count:
            params: Dict[str, Any] = {
                "before": _isoformat_z(now_utc),
                "pagination_limit": 100,
                "sort_column": "start_at",
                "sort_direction": "desc",
                "status": "approved",
            }
            if cursor:
                params["pagination_cursor"] = cursor

            page = self._get("/v1/calendar/list-events", params=params)
            for event in page.get("entries", []):
                end_at = _parse_datetime(event.get("end_at"))
                if end_at and end_at <= now_utc:
                    events.append(event)
                    if len(events) >= count:
                        break

            if len(events) >= count or not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
            if not cursor:
                break

        return events[:count]

    def get_ended_events_for_date(
        self,
        event_date: date,
        *,
        count: int = 1,
        now: Optional[datetime] = None,
        timezone_name: str = MELBOURNE_TIMEZONE,
    ) -> List[Dict[str, Any]]:
        tz = ZoneInfo(timezone_name)
        now_utc = _local_now_utc(now, timezone_name)
        before_utc = datetime.combine(event_date, time.max, tzinfo=tz).astimezone(timezone.utc)
        if before_utc > now_utc:
            before_utc = now_utc

        events: List[Dict[str, Any]] = []
        cursor = None
        while len(events) < count:
            params: Dict[str, Any] = {
                "before": _isoformat_z(before_utc),
                "pagination_limit": 100,
                "sort_column": "start_at",
                "sort_direction": "desc",
                "status": "approved",
            }
            if cursor:
                params["pagination_cursor"] = cursor

            page = self._get("/v1/calendar/list-events", params=params)
            stop = False
            for event in page.get("entries", []):
                start_at = _parse_datetime(event.get("start_at"))
                end_at = _parse_datetime(event.get("end_at"))
                if not start_at or not end_at:
                    continue

                start_local_date = start_at.astimezone(tz).date()
                if start_local_date == event_date and end_at <= now_utc:
                    events.append(event)
                    if len(events) >= count:
                        break
                elif start_local_date < event_date:
                    stop = True
                    break

            if len(events) >= count or stop or not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
            if not cursor:
                break

        return events[:count]

    def list_guests(self, *, event_id: str, approval_status: str = "approved") -> List[Dict[str, Any]]:
        params = {
            "event_id": event_id,
            "approval_status": approval_status,
            "pagination_limit": 100,
            "sort_column": "registered_at",
            "sort_direction": "asc",
        }
        return self._paginate("/v1/event/get-guests", params=params)

    def _paginate(self, path: str, *, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        cursor = None
        while True:
            page_params = dict(params)
            if cursor:
                page_params["pagination_cursor"] = cursor

            page = self._get(path, params=page_params)
            entries.extend(page.get("entries", []))

            if not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
            if not cursor:
                break

        return entries

    def _get(self, path: str, *, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"x-luma-api-key": self.api_key}
        try:
            response = self.session.get(url, headers=headers, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise LumaAPIError("Unable to reach Luma.") from exc

        status_code = getattr(response, "status_code", None)
        if status_code in (401, 403):
            raise LumaAPIError("Luma rejected the configured API key.", status_code=status_code)
        if status_code == 429:
            raise LumaAPIError("Luma rate-limited the attendee report request.", status_code=status_code)

        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise LumaAPIError("Luma returned an error.", status_code=status_code) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise LumaAPIError("Luma returned an invalid JSON response.", status_code=status_code) from exc
        return payload if isinstance(payload, dict) else {}

    def build_attendee_csv(self, event: Dict[str, Any], guests: Iterable[Dict[str, Any]]) -> str:
        rows: List[Dict[str, Any]] = []
        question_headers: List[str] = []
        seen_questions = set()

        for guest in guests:
            row = self._guest_to_row(event, guest)
            for header in row:
                if header.startswith("question: ") and header not in seen_questions:
                    seen_questions.add(header)
                    question_headers.append(header)
            rows.append(row)

        headers = [
            "event_id",
            "event_name",
            "event_url",
            "event_start_at",
            "event_end_at",
            "guest_id",
            "user_id",
            "name",
            "first_name",
            "last_name",
            "email",
            "phone_number",
            "approval_status",
            "registered_at",
            "checked_in_at",
            "ticket_count",
            "ticket_names",
            "ticket_ids",
            "ticket_checked_in_at",
            "utm_source",
            "custom_source",
            "check_in_qr_code",
        ] + question_headers

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        return output.getvalue()

    def build_csv_filename(self, event: Dict[str, Any]) -> str:
        start_at = _parse_datetime(event.get("start_at"))
        date_label = start_at.date().isoformat() if start_at else "unknown-date"
        slug = _slugify(str(event.get("name") or "event"))
        return f"luma-mlai-{date_label}-{slug}.csv"

    def _guest_to_row(self, event: Dict[str, Any], guest: Dict[str, Any]) -> Dict[str, Any]:
        guest_data = guest.get("guest") if isinstance(guest.get("guest"), dict) else guest
        tickets = _tickets_for_guest(guest_data)
        checked_in_values = [
            str(ticket.get("checked_in_at") or "").strip()
            for ticket in tickets
            if str(ticket.get("checked_in_at") or "").strip()
        ]
        checked_in_at = "; ".join(checked_in_values) or str(guest_data.get("checked_in_at") or "")

        row: Dict[str, Any] = {
            "event_id": event.get("id", ""),
            "event_name": event.get("name", ""),
            "event_url": event.get("url", ""),
            "event_start_at": event.get("start_at", ""),
            "event_end_at": event.get("end_at", ""),
            "guest_id": guest_data.get("id", ""),
            "user_id": guest_data.get("user_id", ""),
            "name": guest_data.get("user_name") or "",
            "first_name": guest_data.get("user_first_name") or "",
            "last_name": guest_data.get("user_last_name") or "",
            "email": guest_data.get("user_email") or "",
            "phone_number": guest_data.get("phone_number") or "",
            "approval_status": guest_data.get("approval_status") or "",
            "registered_at": guest_data.get("registered_at") or "",
            "checked_in_at": checked_in_at,
            "ticket_count": len(tickets),
            "ticket_names": "; ".join(_clean_string(ticket.get("name")) for ticket in tickets if _clean_string(ticket.get("name"))),
            "ticket_ids": "; ".join(_clean_string(ticket.get("id")) for ticket in tickets if _clean_string(ticket.get("id"))),
            "ticket_checked_in_at": "; ".join(checked_in_values),
            "utm_source": guest_data.get("utm_source") or "",
            "custom_source": guest_data.get("custom_source") or "",
            "check_in_qr_code": guest_data.get("check_in_qr_code") or "",
        }

        for answer in guest_data.get("registration_answers") or []:
            if not isinstance(answer, dict):
                continue
            label = _clean_string(answer.get("label")) or _clean_string(answer.get("question_id"))
            if not label:
                continue
            row[f"question: {label}"] = _answer_value(answer)

        return row


def _local_now_utc(now: Optional[datetime], timezone_name: str) -> datetime:
    tz = ZoneInfo(timezone_name)
    now_local = now or datetime.now(tz)
    if now_local.tzinfo is None:
        now_local = now_local.replace(tzinfo=tz)
    return now_local.astimezone(timezone.utc)


def _guest_checked_in(guest: Dict[str, Any]) -> bool:
    guest_data = guest.get("guest") if isinstance(guest.get("guest"), dict) else guest
    if str(guest_data.get("checked_in_at") or "").strip():
        return True
    return any(str(ticket.get("checked_in_at") or "").strip() for ticket in _tickets_for_guest(guest_data))


def _tickets_for_guest(guest: Dict[str, Any]) -> List[Dict[str, Any]]:
    tickets = guest.get("event_tickets")
    if isinstance(tickets, list):
        return [ticket for ticket in tickets if isinstance(ticket, dict)]
    ticket = guest.get("event_ticket")
    if isinstance(ticket, dict):
        return [ticket]
    return []


def _answer_value(answer: Dict[str, Any]) -> str:
    if answer.get("question_type") == "company":
        company = _clean_string(answer.get("answer_company") or answer.get("value") or answer.get("answer"))
        job_title = _clean_string(answer.get("answer_job_title"))
        return " - ".join(part for part in [company, job_title] if part)

    value = answer.get("answer")
    if value is None:
        value = answer.get("value")
    return _stringify_csv_value(value)


def _stringify_csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "; ".join(_stringify_csv_value(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "event"
