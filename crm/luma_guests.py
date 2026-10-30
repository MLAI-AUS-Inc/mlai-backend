"""Turn Luma event and guest payloads into plain records, without the database."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .emails import normalize_contact_email

APPROVED = "approved"
WAITLIST = "waitlist"
_CONSENT_YES = {"yes", "y", "true", "1", "on", "checked", "agree", "i agree"}


@dataclass(frozen=True)
class LumaEventRecord:
    luma_event_id: str
    name: str
    url: str
    cover_url: str
    start_at: Optional[datetime]
    end_at: Optional[datetime]
    timezone: str
    visibility: str
    raw: dict


@dataclass(frozen=True)
class LumaGuestRecord:
    luma_guest_id: str
    email: str
    first_name: str
    last_name: str
    display_name: str
    phone: str
    company: str
    job_title: str
    approval_status: str
    registered_at: Optional[datetime]
    checked_in_at: Optional[datetime]
    ticket_names: tuple[str, ...]
    registration_answers: tuple[dict, ...]
    utm_source: str
    custom_source: str
    consent_granted: bool
    whatsapp_opt_in: bool


def event_record(raw: Any) -> Optional[LumaEventRecord]:
    """Return a managed Luma event, or None for external or malformed listings.

    External events are only listed on the calendar; their guests belong to
    another host and cannot be read with the MLAI key.
    """

    if not isinstance(raw, dict):
        return None
    event_id = _text(raw.get("id"), 255)
    if not event_id:
        return None
    if str(raw.get("platform") or "luma").strip() != "luma":
        return None
    if str(raw.get("access") or "manage").strip() != "manage":
        return None
    return LumaEventRecord(
        luma_event_id=event_id,
        name=_text(raw.get("name"), 500) or event_id,
        url=_https_url(raw.get("url")),
        cover_url=_https_url(raw.get("cover_url")),
        start_at=parse_luma_datetime(raw.get("start_at")),
        end_at=parse_luma_datetime(raw.get("end_at")),
        timezone=_text(raw.get("timezone"), 64),
        visibility=_text(raw.get("visibility"), 32),
        raw=raw,
    )


def guest_record(
    raw: Any,
    *,
    consent_labels: Iterable[str] = (),
    whatsapp_labels: Iterable[str] = (),
) -> Optional[LumaGuestRecord]:
    """Return one guest, or None when Luma gave no usable email."""

    if not isinstance(raw, dict):
        return None
    guest = raw.get("guest") if isinstance(raw.get("guest"), dict) else raw
    email = normalize_contact_email(guest.get("user_email"))
    if not email:
        return None

    tickets = _tickets(guest)
    check_ins = [
        parsed
        for parsed in (
            parse_luma_datetime(guest.get("checked_in_at")),
            *(parse_luma_datetime(ticket.get("checked_in_at")) for ticket in tickets),
        )
        if parsed is not None
    ]
    answers = tuple(_answers(guest.get("registration_answers")))
    company, job_title = _company_answer(guest.get("registration_answers"))
    first_name = _text(guest.get("user_first_name"), 150)
    last_name = _text(guest.get("user_last_name"), 150)
    display_name = _text(guest.get("user_name"), 300) or " ".join(
        part for part in (first_name, last_name) if part
    )
    return LumaGuestRecord(
        luma_guest_id=_text(guest.get("id") or guest.get("api_id"), 255),
        email=email,
        first_name=first_name,
        last_name=last_name,
        display_name=display_name,
        phone=_text(guest.get("phone_number"), 64),
        company=company,
        job_title=job_title,
        approval_status=_text(guest.get("approval_status"), 32),
        registered_at=parse_luma_datetime(guest.get("registered_at")),
        checked_in_at=min(check_ins) if check_ins else None,
        ticket_names=tuple(
            name for name in (_text(ticket.get("name"), 255) for ticket in tickets) if name
        ),
        registration_answers=answers,
        utm_source=_text(guest.get("utm_source"), 255),
        custom_source=_text(guest.get("custom_source"), 255),
        consent_granted=_consent_granted(answers, consent_labels),
        whatsapp_opt_in=_consent_granted(answers, whatsapp_labels),
    )


def one_guest_per_email(records: Iterable[LumaGuestRecord]) -> list[LumaGuestRecord]:
    """Keep one record per email: checked in, then approved, then earliest."""

    def rank(record: LumaGuestRecord):
        registered = record.registered_at or datetime.max.replace(tzinfo=timezone.utc)
        return (record.checked_in_at is None, record.approval_status != APPROVED, registered)

    chosen: dict[str, LumaGuestRecord] = {}
    for record in records:
        current = chosen.get(record.email)
        if current is None or rank(record) < rank(current):
            chosen[record.email] = record
    return list(chosen.values())


def parse_luma_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _tickets(guest: dict) -> list[dict]:
    tickets = guest.get("event_tickets")
    if isinstance(tickets, list):
        return [ticket for ticket in tickets if isinstance(ticket, dict)]
    ticket = guest.get("event_ticket")
    return [ticket] if isinstance(ticket, dict) else []


def _answers(raw_answers: Any) -> Iterable[dict]:
    for answer in raw_answers if isinstance(raw_answers, list) else ():
        if not isinstance(answer, dict):
            continue
        label = _text(answer.get("label"), 500) or _text(answer.get("question_id"), 255)
        if not label:
            continue
        yield {
            "label": label,
            "question_id": _text(answer.get("question_id"), 255),
            "question_type": _text(answer.get("question_type"), 64),
            "value": _answer_value(answer),
        }


def _company_answer(raw_answers: Any) -> tuple[str, str]:
    for answer in raw_answers if isinstance(raw_answers, list) else ():
        if isinstance(answer, dict) and answer.get("question_type") == "company":
            company = _text(answer.get("answer_company") or answer.get("answer"), 300)
            return company, _text(answer.get("answer_job_title"), 300)
    return "", ""


def _answer_value(answer: dict) -> str:
    if answer.get("question_type") == "company":
        parts = (
            _text(answer.get("answer_company") or answer.get("answer"), 300),
            _text(answer.get("answer_job_title"), 300),
        )
        return " - ".join(part for part in parts if part)
    value = answer.get("answer")
    if value is None:
        value = answer.get("value")
    return _stringify(value)[:2000]


def _consent_granted(answers: Iterable[dict], consent_labels: Iterable[str]) -> bool:
    wanted = {" ".join(label.split()).casefold() for label in consent_labels if label.strip()}
    if not wanted:
        return False
    return any(
        " ".join(answer["label"].split()).casefold() in wanted
        and answer["value"].strip().casefold() in _CONSENT_YES
        for answer in answers
    )


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "; ".join(_stringify(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


def _https_url(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.startswith("https://") and len(text) <= 2048 else ""
