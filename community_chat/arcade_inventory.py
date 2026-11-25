"""Private, single-use MLAI Arcade stock and a globally paced Codex award clock."""

import hashlib
import hmac
import re
import secrets
from datetime import timedelta, timezone as dt_timezone
from urllib.parse import urlsplit

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.account_bans import account_is_banned
from crm.models import Contact, CrmAuditEvent
from integrations.fields import decrypt_credential_value, encrypt_credential_value
from system_email import transport_resend
from system_email.models import SystemEmailSuppression

from .admin_access import require_mlai_admin
from .crm.errors import CrmError
from .volunteer.access import community_id
from .volunteer.models import VolunteerSourceReceipt as Receipt


ORIGIN = "mlai_arcade"
KINDS = {"codex": "stock_codex", "ticket": "stock_ticket"}
POOL_COMMUNITY = "__mlai_arcade_inventory__"
CODEX_INTERVAL = timedelta(hours=36)
CODE = re.compile(r"[a-zA-Z0-9_-]{1,160}\Z")
CODEX_PREFIX = "https://chatgpt.com/codex/p/"


def _stamp(now):
    return now.astimezone(dt_timezone.utc).isoformat(timespec="microseconds")


def _code(raw, prize):
    value = raw.strip() if isinstance(raw, str) else ""
    if prize == "codex" and value.startswith(CODEX_PREFIX):
        value = value[len(CODEX_PREFIX):].rstrip("/")
    if not CODE.fullmatch(value):
        raise CrmError("invalid_arcade_code")
    return value


def _terms(data, prize, now):
    expires = data.get("expiresAt")
    if expires:
        try:
            when = parse_datetime(expires)
        except (ValueError, TypeError, OverflowError):
            when = None
        if not when or timezone.is_naive(when) or when <= now:
            raise CrmError("invalid_arcade_expiry")
    elif prize == "ticket":
        raise CrmError("invalid_arcade_expiry")
    result = {"expiresAt": _stamp(when) if expires else ""}
    if prize == "ticket":
        name, url = data.get("eventName"), data.get("eventUrl")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200 or not isinstance(url, str) or len(url) > 500:
            raise CrmError("invalid_arcade_event")
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise CrmError("invalid_arcade_event") from None
        if (parsed.scheme != "https" or parsed.netloc not in {"lu.ma", "luma.com"}
                or not parsed.path.strip("/") or parsed.query or parsed.fragment or re.search(r"[\s\\]", url)):
            raise CrmError("invalid_arcade_event")
        result.update(eventName=name.strip(), eventUrl=url)
    return result


def _pool():
    return Receipt.objects.select_for_update().filter(community=POOL_COMMUNITY, source_key="arcade:inventory-lock", origin=ORIGIN)


def _stock(community):
    return Receipt.objects.filter(community=community, origin=ORIGIN, kind__in=KINDS.values())


def _usable(stock, now):
    return stock.filter(Q(source__expiresAt="") | Q(source__expiresAt__gt=_stamp(now)), status="available")


def stock_summary():
    """Return counts only. Codes, links containing codes and recipients stay private."""
    stock = _stock(community_id())
    eligible = _usable(stock, timezone.now())
    result = {}
    for prize, kind in KINDS.items():
        result[prize] = {"available": eligible.filter(kind=kind).count(),
                         "reserved": stock.filter(kind=kind).exclude(status="available").count()}
    statuses = dict(stock.filter(kind=KINDS["codex"]).values_list("status").annotate(n=Count("pk")))
    result["delivery"] = {"pending": statuses.get("email_pending", 0), "sent": statuses.get("sent", 0),
                          "needsReview": statuses.get("needs_review", 0)}
    result["review"] = [
        {"reservation": str(row.pk), "reason": row.error}
        for row in stock.filter(kind=KINDS["codex"], status="needs_review").order_by("-updated_at")[:10]
    ]
    result["codexIntervalHours"] = int(CODEX_INTERVAL.total_seconds() // 3600)
    return result


@transaction.atomic
def import_stock(actor, data):
    """Validate the entire paste, then encrypt and de-duplicate it under the pool lock."""
    require_mlai_admin(actor)
    if not isinstance(data, dict):
        raise CrmError("invalid_arcade_import")
    prize, raw = data.get("prize"), data.get("codes")
    if not isinstance(prize, str) or prize not in KINDS or not isinstance(raw, list) or not 1 <= len(raw) <= 200:
        raise CrmError("invalid_arcade_import")
    now = timezone.now()
    terms = _terms(data, prize, now)
    codes = [_code(value, prize) for value in raw]
    pool = _pool().first()
    if pool is None:
        # The existing unique community/source constraint also serializes first imports.
        Receipt.objects.get_or_create(
            community=POOL_COMMUNITY, source_key="arcade:inventory-lock",
            defaults={"origin": ORIGIN, "kind": "stock_pool", "actor": actor, "occurred_at": now,
                      "metadata": {"pepper": encrypt_credential_value(secrets.token_hex(32))}, "status": "active"},
        )
        pool = _pool().get()
    pepper = decrypt_credential_value(pool.metadata["pepper"]).encode()
    inserted = 0
    for value in codes:
        identity = hmac.new(pepper, f"{prize}\0{value}".encode(), hashlib.sha256).hexdigest()
        key = f"arcade:stock:{identity}"
        if Receipt.objects.filter(origin=ORIGIN, source_key=key).exists():
            continue
        Receipt.objects.create(community=community_id(), source_key=key, origin=ORIGIN, kind=KINDS[prize],
                               actor=actor, occurred_at=now, status="available", source=terms,
                               metadata={"code": encrypt_credential_value(value)})
        inserted += 1
    counts = {"inserted": inserted, "duplicates": len(raw) - inserted}
    CrmAuditEvent.objects.create(actor=actor, action="import", target_type="arcade_stock",
                                 target_id=prize, detail=counts)
    return {**stock_summary(), **counts}


def can_email_winner(user, *, check_sender=True):
    """Give Codex only when its account recipient is eligible for a private prize email."""
    if check_sender and not (transport_resend.api_key() and transport_resend.from_address()):
        return False
    if not (user.is_active and user.email_verified_at and user.email) or account_is_banned(user):
        return False
    if SystemEmailSuppression.objects.filter(email=user.email, cleared_at__isnull=True).exists():
        return False
    return not Contact.objects.filter(email__iexact=user.email).exclude(email_status=Contact.EmailStatus.ACTIVE).exists()


def reserve_prize(arcade_round, prize, now):
    """Atomically reserve a real code alongside its round. Caller owns the spin transaction."""
    is_codex = prize == "codex_credits"
    if not is_codex and prize != "event_ticket":
        return None
    kind = KINDS["codex" if is_codex else "ticket"]
    pool = _pool().first()
    if not pool:
        return None
    if is_codex:
        available_from = pool.metadata.get("nextCodexAt", "")
        if available_from > _stamp(now) or not can_email_winner(arcade_round.actor):
            return None
    stock = _usable(_stock(arcade_round.community), now).filter(kind=kind).order_by("created_at", "pk").first()
    if stock is None:
        return None
    stock.source = {**stock.source, "roundId": str(arcade_round.pk)}
    stock.target_id = arcade_round.actor_id
    stock.occurred_at = now
    stock.status = "email_pending" if is_codex else "reserved"
    if is_codex:
        pool.metadata = {**pool.metadata, "nextCodexAt": _stamp(now + CODEX_INTERVAL)}
        pool.save(update_fields=("metadata", "updated_at"))
        stock.metadata = {**stock.metadata, "recipient": encrypt_credential_value(arcade_round.actor.email)}
    stock.save(update_fields=("source", "metadata", "target", "status", "occurred_at", "updated_at"))
    return {"stockId": str(stock.pk)}


def ticket_payload(arcade_round):
    """Only return the coupon bound to this ticket round and winner."""
    stock = _stock(arcade_round.community).filter(
        kind=KINDS["ticket"], status="reserved", target_id=arcade_round.actor_id,
        source__roundId=str(arcade_round.pk),
    ).first()
    if stock is None:
        return {}
    return {"eventName": stock.source["eventName"], "eventUrl": stock.source["eventUrl"],
            "ticketCode": decrypt_credential_value(stock.metadata["code"])}
