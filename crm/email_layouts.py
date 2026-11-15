"""The HTML for invite emails, laid out like a Luma invite but in MLAI's brand.

The layout is email-safe HTML: tables, inline styles and hosted PNG icons, so
it holds up in Gmail, Outlook and phone mail apps. It is wrapped around the
staff member's Markdown message as raw HTML blocks, which listmonk's Markdown
renderer passes through unchanged. Each block has no blank lines, so the
Markdown parser never ends one early, and the message sits between two of
them as ordinary Markdown.

Everything in the card comes from the event record, so neither staff nor Roo
can put a wrong date, place or link in it. Event text is HTML-escaped and its
braces are turned into entities, so nothing in it can open a listmonk
template action or an HTML tag.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone as dt_timezone
from typing import Optional
from urllib.parse import quote, urlencode

from django.conf import settings

from . import merge_tags

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
INK = "#111827"
MUTED = "#6b7280"
FAINT = "#9ca3af"
LINE = "#e5e7eb"
TEAL = "#2fd8bf"  # The MLAI roo's teal.
HOST_NAME = "MLAI"
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def asset(name: str) -> str:
    """A hosted email image, served from the backend's static files."""

    base = str(getattr(settings, "CRM_EMAIL_ASSET_BASE_URL", "") or "").rstrip("/")
    return f"{base}/{name}"


def _text(value) -> str:
    """Event text inside HTML: escaped, one line, and no template braces."""

    text = html.escape(" ".join(str(value or "").split()), quote=True)
    return text.replace("{", "&#123;").replace("}", "&#125;")


def _href(url: str) -> str:
    return _text(url.replace(" ", "%20"))


# Event facts


def _local(value, zone_name: str) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_timezone.utc)
    return value.astimezone(merge_tags._zone(zone_name))


def _clock(value: datetime) -> str:
    hour = value.hour % 12 or 12
    return f"{hour}:{value.minute:02d} {'AM' if value.hour < 12 else 'PM'}"


def event_when(event) -> Optional[dict]:
    """The date tile and time row: ``NOV`` / ``20``, ``Friday, 20 November``, ``6:00 PM – 9:00 PM AEDT``."""

    zone = getattr(event, "timezone", "")
    start = _local(getattr(event, "start_at", None), zone)
    if start is None:
        return None
    end = _local(getattr(event, "end_at", None), zone)
    times = _clock(start)
    if end is not None and end > start:
        if end.date() == start.date():
            times += f" – {_clock(end)}"
        else:
            times += f" – {_WEEKDAYS[end.weekday()][:3]} {end.day} {_MONTHS[end.month - 1][:3]}, {_clock(end)}"
    abbreviation = start.strftime("%Z")
    if abbreviation:
        times += f" {abbreviation}"
    return {
        "month": _MONTHS[start.month - 1][:3].upper(),
        "day": str(start.day),
        "date": f"{_WEEKDAYS[start.weekday()]}, {start.day} {_MONTHS[start.month - 1]}",
        "times": times,
    }


def event_where(event) -> Optional[dict]:
    """The location row: a venue and its city, a hidden address, or an online event."""

    raw = event.raw_payload if isinstance(getattr(event, "raw_payload", None), dict) else {}
    geo = raw.get("geo_address_json") if isinstance(raw.get("geo_address_json"), dict) else {}
    city = " ".join(str(geo.get("city_state") or geo.get("city") or "").split())
    full = " ".join(str(geo.get("full_address") or "").split())
    venue = " ".join(str(geo.get("address") or raw.get("location") or "").split())
    if raw.get("geo_address_visibility") == "guests-only" and (venue or full or city):
        return {"title": "Register to see location", "detail": city, "icon": "pin.png", "url": ""}
    if venue or full:
        title = venue or full
        detail = city if city and city not in title else ("" if title == full else full)
        query = {"api": "1", "query": full or venue}
        place_id = geo.get("google_maps_place_id") or geo.get("place_id")
        if place_id:
            query["query_place_id"] = str(place_id)
        return {"title": title[:200], "detail": detail[:200], "icon": "pin.png",
                "url": "https://www.google.com/maps/search/?" + urlencode(query)}
    if raw.get("meeting_url") or raw.get("zoom_meeting_url"):
        return {"title": "Online event", "detail": "You'll get the link when you register",
                "icon": "video.png", "url": ""}
    return None


# Pieces


def _tile(inner: str) -> str:
    return (f'<td width="48" valign="top" style="width:48px;padding:0">'
            f'<table role="presentation" width="48" cellpadding="0" cellspacing="0" border="0" '
            f'style="width:48px;border:1px solid {LINE};border-radius:8px;border-collapse:separate">'
            f'{inner}</table></td>')


def _row(tile: str, title: str, detail: str) -> str:
    detail_html = (f'<div style="font-size:15px;line-height:22px;color:{MUTED};padding-top:2px">{detail}</div>'
                   if detail else "")
    return (f'<tr><td style="padding:0 0 14px 0">'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>{tile}'
            f'<td valign="middle" style="padding:2px 0 0 14px;font-family:{FONT}">'
            f'<div style="font-size:17px;line-height:24px;font-weight:600;color:{INK}">{title}</div>'
            f'{detail_html}</td></tr></table></td></tr>')


def _date_row(when: dict) -> str:
    tile = _tile(
        f'<tr><td align="center" style="background:#f9fafb;border-radius:8px 8px 0 0;border-bottom:1px solid {LINE};'
        f'padding:3px 0 2px 0;font-family:{FONT};font-size:10px;line-height:12px;font-weight:600;'
        f'letter-spacing:0.5px;color:{MUTED}">{_text(when["month"])}</td></tr>'
        f'<tr><td align="center" style="padding:4px 0 5px 0;font-family:{FONT};font-size:18px;line-height:22px;'
        f'font-weight:500;color:{INK}">{_text(when["day"])}</td></tr>'
    )
    return _row(tile, _text(when["date"]), _text(when["times"]))


def _place_row(where: dict) -> str:
    tile = _tile(
        f'<tr><td align="center" valign="middle" height="46" style="height:46px;padding:0">'
        f'<img src="{_href(asset(where["icon"]))}" width="20" height="20" alt="" '
        f'style="display:block;width:20px;height:20px;border:0"></td></tr>'
    )
    title = _text(where["title"])
    if where["url"]:
        title = (f'<a href="{_href(where["url"])}" style="color:{INK};text-decoration:none">{title}'
                 f'&nbsp;<span style="color:{FAINT}">&#8599;</span></a>')
    return _row(tile, title, _text(where["detail"]))


def _button(label: str, url: str, *, primary: bool) -> str:
    background, color = (TEAL, "#0a0a0a") if primary else ("#f3f4f6", "#4b5563")
    return (f'<td style="background:{background};border-radius:8px;mso-padding-alt:12px 22px">'
            f'<a href="{_href(url)}" style="display:inline-block;padding:12px 22px;font-family:{FONT};'
            f'font-size:16px;line-height:20px;font-weight:600;color:{color};text-decoration:none;'
            f'border-radius:8px">{label}</a></td>')


def _share_links(event, share_url: str) -> str:
    text = f"Join me at {event.name}"
    links = (
        ("Facebook", "share-facebook.png", "https://www.facebook.com/sharer/sharer.php?" + urlencode({"u": share_url})),
        ("X", "share-x.png", "https://twitter.com/intent/tweet?" + urlencode({"url": share_url, "text": text})),
        ("LinkedIn", "share-linkedin.png",
         "https://www.linkedin.com/sharing/share-offsite/?" + urlencode({"url": share_url})),
        ("WhatsApp", "share-whatsapp.png", "https://wa.me/?text=" + quote(f"{text} {share_url}", safe="")),
    )
    cells = "".join(
        f'<td style="padding:0 18px 0 0"><a data-notrack href="{_href(url)}" style="text-decoration:none">'
        f'<img src="{_href(asset(icon))}" width="24" height="24" alt="Share on {name}" '
        f'style="display:block;width:24px;height:24px;border:0"></a></td>'
        for name, icon, url in links
    )
    return (f'<tr><td style="padding:36px 0 0 0;font-family:{FONT};font-size:14px;line-height:20px;color:{FAINT}">'
            f'Share with friends</td></tr>'
            f'<tr><td style="padding:12px 0 0 0"><table role="presentation" cellpadding="0" cellspacing="0" '
            f'border="0"><tr>{cells}</tr></table></td></tr>')


def _divider(padding: str) -> str:
    return (f'<tr><td style="padding:{padding}"><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" border="0"><tr><td style="border-top:1px solid {LINE};font-size:0;line-height:0;'
            f'height:1px">&nbsp;</td></tr></table></td></tr>')


# The invite


def invite(event, body: str, *, event_url: str, share_url: str) -> str:
    """An invite: host, title, date and place, the message, the buttons, then sharing.

    ``body`` is the already-rendered Markdown message; ``event_url`` is the
    tracked event link (empty when the event has no public link).
    """

    table = ('<table role="presentation" class="mlai-invite" width="100%" cellpadding="0" cellspacing="0" '
             f'border="0" style="width:100%;max-width:560px;font-family:{FONT};color:{INK}">')
    header = (
        f'{table}'
        f'<tr><td style="padding:0 0 14px 0"><table role="presentation" cellpadding="0" cellspacing="0" '
        f'border="0"><tr><td valign="middle" style="padding:0 8px 0 0">'
        f'<img src="{_href(asset("mlai-roo.png"))}" width="24" height="24" alt="" '
        f'style="display:block;width:24px;height:24px;border:0;border-radius:12px;background:{TEAL}"></td>'
        f'<td valign="middle" style="font-family:{FONT};font-size:16px;line-height:20px;color:{MUTED}">'
        f'{HOST_NAME}</td></tr></table></td></tr>'
        f'<tr><td style="font-family:{FONT};font-size:26px;line-height:32px;color:{FAINT};padding:0">'
        f'You&rsquo;re invited to</td></tr>'
        f'<tr><td class="mlai-invite-title" style="font-family:{FONT};font-size:28px;line-height:34px;'
        f'font-weight:700;color:{INK};padding:2px 0 0 0">{_text(event.name)}</td></tr>'
        f'{_divider("20px 0 20px 0")}'
    )
    when, where = event_when(event), event_where(event)
    if when:
        header += _date_row(when)
    if where:
        header += _place_row(where)
    if when or where:
        header += _divider("6px 0 8px 0")
    header += f'<tr><td class="mlai-invite-body" style="font-family:{FONT};font-size:16px;line-height:26px;color:{INK}">'

    footer = '</td></tr>'
    if event_url:
        footer += (
            f'<tr><td style="padding:24px 0 0 0"><table role="presentation" cellpadding="0" cellspacing="0" '
            f'border="0"><tr>{_button("Accept Invite", event_url, primary=True)}'
            f'<td style="width:10px;font-size:0;line-height:0">&nbsp;</td>'
            f'{_button("View Event", event_url, primary=False)}</tr></table></td></tr>'
        )
    footer += (
        f'<tr><td style="padding:24px 0 0 0;font-family:{FONT};font-size:15px;line-height:22px;color:{MUTED}">'
        f'Can&rsquo;t make it? No worries, this is the only invite we&rsquo;ll send you for this event.</td></tr>'
    )
    if share_url:
        footer += _share_links(event, share_url)
    footer += '</table>'
    # Raw HTML blocks must start at the line's first column and contain no blank line.
    return f"{header}\n\n{body}\n\n{footer}"
