"""Bounded Luma operations for Roo's event-specific discount redemptions.

The public Luma API does not provide an idempotency key for coupon creation.
Callers must persist a code before sending a write and reconcile an uncertain
result with ``find_coupon``. This module never retries a remote mutation.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlsplit, urlunsplit

import requests
from django.conf import settings

from roo.event_ticket_policy import EVENT_TICKET_MAX_CENTS_EXCLUSIVE


class LumaEventDiscountError(RuntimeError):
    """Safe provider error, including whether a remote write may have happened."""

    def __init__(self, code, message, *, uncertain=False, retryable=False, status_code=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.uncertain = uncertain
        self.retryable = retryable
        self.status_code = status_code


def _datetime(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            value = None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LumaEventDiscountError("invalid_provider_time", "Luma returned an invalid event or ticket time.")
    return value.astimezone(timezone.utc)


def _iso(value):
    # Use millisecond precision for portable provider timestamps. Truncate
    # before persisting or comparing terms so losing sub-millisecond precision
    # cannot look like mismatched terms after a write. Never extend the expiry.
    value = _datetime(value)
    value = value.replace(microsecond=(value.microsecond // 1000) * 1000)
    return value.isoformat().replace("+00:00", "Z")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _discount(value):
    if not isinstance(value, dict):
        raise LumaEventDiscountError("invalid_discount", "The event discount is not configured correctly.")
    if value.get("discount_type") == "percent":
        amount = _number(value.get("percent_off"))
        if amount is not None and 0 < amount <= 100:
            return {"discount_type": "percent", "percent_off": float(amount)}
    if value.get("discount_type") == "amount":
        amount = _number(value.get("cents_off"))
        if amount is not None and amount > 0 and amount == amount.to_integral_value() and value.get("currency") == "aud":
            return {"discount_type": "amount", "cents_off": int(amount), "currency": "aud"}
    raise LumaEventDiscountError("invalid_discount", "The event discount is not configured correctly.")


def _code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,20}", value):
        raise LumaEventDiscountError("invalid_coupon_code", "The coupon code is not valid.")
    return value.upper()


class LumaEventDiscountService:
    """Validate eligible events and issue or reconcile one-use ticket coupons."""

    def __init__(self, *, api_key=None, base_url=None, session=None, timeout=10.0, max_pages=10):
        self.api_key = str(api_key if api_key is not None else getattr(settings, "LUMA_API_KEY", "") or "").strip()
        self.base_url = str(base_url if base_url is not None else getattr(settings, "LUMA_BASE_URL", "https://public-api.luma.com")).rstrip("/")
        origin = urlsplit(self.base_url)
        if origin.scheme != "https" or not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment:
            raise LumaEventDiscountError("luma_configuration", "The Luma API origin is not configured correctly.")
        if not self.api_key:
            raise LumaEventDiscountError("luma_configuration", "Event discounts are temporarily unavailable.")
        self.session = session or requests.Session()
        self.timeout = float(timeout)
        self.max_pages = max(1, min(int(max_pages), 50))
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 60:
            raise LumaEventDiscountError("luma_configuration", "The Luma request timeout is not configured correctly.")

    def _request(self, method, path, *, params=None, body=None):
        write = method == "POST"
        try:
            response = self.session.request(
                method, self.base_url + path, headers={"x-luma-api-key": self.api_key},
                params=params, json=body, timeout=self.timeout, allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise LumaEventDiscountError(
                "luma_unavailable", "Unable to reach Luma. Please try again later.",
                uncertain=write, retryable=True,
            ) from exc
        status = response.status_code
        if not 200 <= status < 300:
            uncertain = write and (status >= 500 or status in (408, 409, 425, 429) or 300 <= status < 400)
            raise LumaEventDiscountError(
                "luma_rejected" if 400 <= status < 500 else "luma_unavailable",
                "Luma could not complete the event discount request.", uncertain=uncertain,
                retryable=status >= 500 or status in (408, 409, 425, 429), status_code=status,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise LumaEventDiscountError("invalid_luma_response", "Luma returned an invalid response.", uncertain=write, retryable=True) from exc
        if not isinstance(payload, dict):
            raise LumaEventDiscountError("invalid_luma_response", "Luma returned an invalid response.", uncertain=write, retryable=True)
        return payload

    def _pages(self, path, *, params):
        cursor = None
        seen = set()
        for _ in range(self.max_pages):
            page_params = {**params, "pagination_limit": 100}
            if cursor:
                page_params["pagination_cursor"] = cursor
            page = self._request("GET", path, params=page_params)
            entries = page.get("entries")
            if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries) or not isinstance(page.get("has_more"), bool):
                raise LumaEventDiscountError("invalid_luma_response", "Luma returned an incomplete result.", retryable=True)
            yield entries
            if page["has_more"] is False:
                return
            cursor = page.get("next_cursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise LumaEventDiscountError("incomplete_luma_pagination", "Luma returned an incomplete result.", retryable=True)
            seen.add(cursor)
        raise LumaEventDiscountError("luma_pagination_limit", "The Luma verification request exceeded its page budget.", retryable=True)

    def get_event(self, event_id):
        """Read the configured event, without retaining host or guest details."""
        event = self._request("GET", "/v1/events/get", params={"event_id": event_id})
        if event.get("id") != event_id:
            raise LumaEventDiscountError("invalid_luma_event", "Luma returned a different event.")
        return event

    def get_ticket_types(self, event_id):
        """Read ticket types for the exact event; hidden types remain excluded."""
        payload = self._request("GET", "/v1/events/ticket-types/list", params={"event_id": event_id})
        entries = payload.get("entries")
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            raise LumaEventDiscountError("invalid_luma_response", "Luma returned invalid ticket types.")
        return entries

    def _approved_ticket_count(self, event_id, ticket_type_id):
        ticket_ids = set()
        for entries in self._pages("/v1/events/guests/list", params={"event_id": event_id, "approval_status": "approved"}):
            for guest in entries:
                tickets = guest.get("event_tickets")
                if guest.get("approval_status") != "approved" or not isinstance(tickets, list):
                    raise LumaEventDiscountError("ticket_capacity_unverified", "Ticket availability could not be verified.", retryable=True)
                for ticket in tickets:
                    if (not isinstance(ticket, dict)
                            or not isinstance(ticket.get("id"), str) or not ticket["id"]
                            or not isinstance(ticket.get("event_ticket_type_id"), str)
                            or not ticket["event_ticket_type_id"]):
                        raise LumaEventDiscountError("ticket_capacity_unverified", "Ticket availability could not be verified.", retryable=True)
                    if ticket["event_ticket_type_id"] == ticket_type_id:
                        ticket_ids.add(ticket["id"])
        return len(ticket_ids)

    def validate_offer(self, terms, *, now=None):
        """Return a public snapshot only for a currently purchasable AUD ticket.

        Availability is an observation, not a seat reservation. Date-only sales
        windows cannot be verified precisely and fail closed until configured
        with explicit timestamps in Luma.
        """
        now = _datetime(now or datetime.now(timezone.utc))
        event_id = terms.get("event_id")
        ticket_id = terms.get("event_ticket_type_id")
        discount = _discount(terms.get("discount"))
        expires = _datetime(terms.get("valid_end_at"))
        event = self.get_event(event_id)
        starts = _datetime(event.get("start_at"))
        if event.get("access") != "manage" or event.get("platform") != "luma":
            raise LumaEventDiscountError("event_not_managed", "This event does not support Roo discounts.")
        if event.get("visibility") != "public" or event.get("registration_open") is not True:
            raise LumaEventDiscountError("event_registration_closed", "Registration for this event is not open.")
        if event.get("can_register_for_multiple_tickets") is not False:
            raise LumaEventDiscountError("group_registration_enabled", "This event does not yet support a one-ticket Roo discount.")
        if starts <= now or expires <= now or expires > starts:
            raise LumaEventDiscountError("event_offer_expired", "This event discount has expired or is not configured correctly.")
        remaining = event.get("spots_remaining")
        if ("spots_remaining" not in event
                or (remaining is None and event.get("max_capacity") is not None)
                or (remaining is not None and (_number(remaining) is None or _number(remaining) <= 0))):
            raise LumaEventDiscountError("event_sold_out", "This event has no verified places available.")
        tickets = [ticket for ticket in self.get_ticket_types(event_id) if ticket.get("id") == ticket_id]
        if len(tickets) != 1:
            raise LumaEventDiscountError("ticket_unavailable", "The discount ticket type is unavailable.")
        ticket = tickets[0]
        cents = _number(ticket.get("cents"))
        if ticket.get("type") != "paid" or ticket.get("currency") != "aud" or cents is None or cents <= 0 or cents != cents.to_integral_value():
            raise LumaEventDiscountError("ticket_not_paid_aud", "This discount requires a paid AUD ticket.")
        if cents >= EVENT_TICKET_MAX_CENTS_EXCLUSIVE:
            raise LumaEventDiscountError("ticket_price_limit", "Roo ticket rewards only cover tickets priced below A$100.")
        requires_approval = ticket.get("require_approval", False)
        if ticket.get("is_hidden", False) is not False or type(requires_approval) is not bool or ticket.get("is_flexible", False) is not False:
            raise LumaEventDiscountError("ticket_not_supported", "This ticket type does not support Roo discounts.")
        if "ticket" in terms and terms["ticket"].get("requires_approval", False) != requires_approval:
            raise LumaEventDiscountError("ticket_terms_changed", "The ticket's approval requirements changed. Ask Roo for a new quote.")
        for field, is_end in (("valid_start_at", False), ("valid_end_at", True)):
            value = ticket.get(field)
            if value is not None:
                deadline = _datetime(value)
                if (is_end and deadline <= now) or (not is_end and deadline > now):
                    raise LumaEventDiscountError("ticket_sales_closed", "This ticket is not currently on sale.")
                if is_end and expires > deadline:
                    raise LumaEventDiscountError("invalid_coupon_expiry", "The discount expires after ticket sales close.")
        capacity = ticket.get("max_capacity")
        if capacity is not None:
            maximum = _number(capacity)
            if maximum is None or maximum < 1 or maximum != maximum.to_integral_value():
                raise LumaEventDiscountError("ticket_capacity_unverified", "Ticket availability could not be verified.")
            if self._approved_ticket_count(event_id, ticket_id) >= maximum:
                raise LumaEventDiscountError("ticket_sold_out", "This ticket type is sold out.")
        if discount["discount_type"] == "amount" and discount["cents_off"] > cents:
            raise LumaEventDiscountError("invalid_discount", "The discount exceeds this ticket's price.")
        url = self.coupon_url(event.get("url"), "ROO-VALIDATE").split("?", 1)[0]
        return {
            "event_id": event_id, "event_ticket_type_id": ticket_id, "discount": discount,
            "valid_end_at": _iso(expires), "id": event_id, "name": str(event.get("name") or ""),
            "url": url, "starts_at": _iso(starts), "timezone": str(event.get("timezone") or ""),
            "ticket": {"id": ticket_id, "name": str(ticket.get("name") or ""), "cents": int(cents), "currency": "aud", "requires_approval": requires_approval},
        }

    def find_coupon(self, event_id, code):
        """Find a saved code, rejecting incomplete scans and duplicate matches."""
        code = _code(code)
        matches = []
        for entries in self._pages("/v1/events/coupons/list", params={"event_id": event_id}):
            matches.extend(coupon for coupon in entries if str(coupon.get("code") or "").upper() == code)
        if len(matches) > 1:
            raise LumaEventDiscountError("duplicate_luma_coupon", "Luma returned duplicate coupon codes.")
        return matches[0] if matches else None

    @staticmethod
    def matches_coupon(coupon, snapshot, code, *, allow_consumed=True):
        """Check remote immutable terms before settling a persisted redemption."""
        if not isinstance(coupon, dict):
            return False
        try:
            discount = _discount(snapshot.get("discount"))
            remaining = coupon.get("remaining_count")
            valid = (
                isinstance(coupon.get("id"), str) and bool(coupon["id"])
                and str(coupon.get("code") or "").upper() == _code(code)
                and coupon.get("event_ticket_type_id") == snapshot.get("event_ticket_type_id")
                and type(remaining) is int and remaining in ((0, 1) if allow_consumed else (1,))
                and coupon.get("valid_start_at") is None
                and _datetime(coupon.get("valid_end_at")) == _datetime(_iso(snapshot.get("valid_end_at")))
            )
            if discount["discount_type"] == "percent":
                return bool(valid and _number(coupon.get("percent_off")) == _number(discount["percent_off"]) and coupon.get("cents_off") is None and coupon.get("currency") is None)
            return bool(valid and _number(coupon.get("cents_off")) == _number(discount["cents_off"]) and coupon.get("percent_off") is None and coupon.get("currency") == "aud")
        except LumaEventDiscountError:
            return False

    def create_coupon(self, snapshot, code):
        """Attempt creation exactly once; uncertain outcomes require lookup."""
        code = _code(code)
        body = {
            "event_id": snapshot["event_id"], "event_ticket_type_id": snapshot["event_ticket_type_id"],
            "code": code, "remaining_count": 1, "discount": _discount(snapshot["discount"]),
            "valid_end_at": _iso(snapshot["valid_end_at"]),
        }
        coupon = self._request("POST", "/v1/events/coupons/create", body=body)
        if not self.matches_coupon(coupon, snapshot, code):
            raise LumaEventDiscountError("coupon_terms_unverified", "The created coupon terms could not be verified.", uncertain=True, retryable=True)
        return coupon

    def disable_coupon(self, event_id, code):
        """Disable the exact event coupon and verify before a caller refunds."""
        code = _code(code)
        self._request("POST", "/v1/events/coupons/update", body={"event_id": event_id, "code": code, "remaining_count": 0})
        coupon = self.find_coupon(event_id, code)
        if coupon is None or type(coupon.get("remaining_count")) is not int or coupon["remaining_count"] != 0:
            raise LumaEventDiscountError("coupon_disable_unverified", "Coupon cancellation could not be verified.", uncertain=True, retryable=True)

    @staticmethod
    def coupon_url(event_url, code):
        """Build a canonical HTTPS Luma link for one nonempty event slug."""
        code = _code(code)
        if not isinstance(event_url, str) or any(ord(char) < 33 or ord(char) == 127 for char in event_url):
            raise LumaEventDiscountError("invalid_luma_url", "Luma returned an invalid event link.")
        try:
            parsed = urlsplit(event_url)
            safe = parsed.scheme == "https" and parsed.hostname in ("luma.com", "lu.ma", "www.luma.com", "www.lu.ma") and parsed.port in (None, 443)
        except ValueError:
            safe = False
        if (not safe or parsed.username or parsed.password
                or not re.fullmatch(r"/[A-Za-z0-9_-]+", parsed.path)
                or "?" in event_url or "#" in event_url):
            raise LumaEventDiscountError("invalid_luma_url", "Luma returned an invalid event link.")
        hostname = parsed.hostname.removeprefix("www.")
        return urlunsplit(("https", hostname, parsed.path, urlencode({"coupon": code}), ""))
