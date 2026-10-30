"""Member-scoped event discounts with durable holds and recoverable Luma writes."""

from datetime import timedelta
import hashlib
import json
import re
import secrets
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from integrations.services.luma_event_discounts import LumaEventDiscountError, LumaEventDiscountService
from integrations.services.luma import LumaAPIError, LumaAttendeeReportService, LumaConfigurationError
from .models import EventDiscountOffer, EventDiscountQuote, EventDiscountRedemption, PointsAccount, RewardsCatalog
from .permissions import InsufficientBalanceError
from .services import PointsService
from .event_ticket_policy import EVENT_TICKET_POINTS


class EventDiscountError(ValueError):
    """A safe member-facing error with a stable API code."""

    def __init__(self, code, detail, status_code=409, *, extra=None):
        super().__init__(detail)
        self.code, self.detail, self.status_code = code, detail, status_code
        self.extra = extra or {}


def feature_enabled():
    """Advertise the capability only with an explicitly configured service."""
    return bool(
        getattr(settings, "ROO_EVENT_DISCOUNTS_ENABLED", False)
        and re.fullmatch(r"T[A-Z0-9]+", getattr(settings, "ROO_EVENT_DISCOUNTS_SLACK_TEAM_ID", ""))
        and str(getattr(settings, "LUMA_API_KEY", "") or "").strip()
        and str(getattr(settings, "ROO_API_KEY", "") or "").strip()
    )


def _enabled():
    if not feature_enabled():
        raise EventDiscountError("event_discounts_disabled", "Event discounts are not available yet.", 503)


def _scope(scope):
    scope = scope or {}
    result = {name: scope.get(name, "") for name in ("team_id", "channel_id", "thread_ts")}
    if (
        not all(isinstance(value, str) for value in result.values())
        or result["team_id"] != getattr(settings, "ROO_EVENT_DISCOUNTS_SLACK_TEAM_ID", "")
        or not re.fullmatch(r"T[A-Z0-9]{1,29}", result["team_id"])
        or not re.fullmatch(r"D[A-Z0-9]{1,29}", result["channel_id"])
        or (result["thread_ts"] and not re.fullmatch(r"[0-9]{10}\.[0-9]{6}", result["thread_ts"]))
    ):
        raise EventDiscountError("private_conversation_required", "Ask Roo in your private conversation.", 403)
    return result


def _member(user):
    if not user or not user.is_active:
        raise EventDiscountError("inactive_member", "An active MLAI account is required.", 403)


def _offer_terms(offer):
    discount = {"discount_type": offer.discount_type}
    if offer.discount_type == "percent":
        discount["percent_off"] = offer.percent_off
    else:
        discount.update(cents_off=offer.cents_off, currency=offer.currency)
    return {
        "event_id": offer.event_id, "event_ticket_type_id": offer.event_ticket_type_id,
        "discount": discount, "valid_end_at": offer.valid_end_at.isoformat(),
    }


def _fingerprint(offer):
    terms = {
        **_offer_terms(offer), "active": offer.is_active,
        "start": offer.valid_start_at.isoformat() if offer.valid_start_at else None,
        "reward_code": offer.reward_id, "cost": offer.reward.cost_points,
        "reward_active": offer.reward.is_active, "max_per_user": offer.reward.max_per_user,
    }
    return hashlib.sha256(json.dumps(terms, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _available_offer(offer, now):
    reward = offer.reward
    if (reward.code == "EVENT_TICKET" or reward.code.startswith("EVENT_TICKET_")) and (
        reward.cost_points != EVENT_TICKET_POINTS
        or offer.discount_type != "percent" or offer.percent_off != 100
    ):
        raise EventDiscountError("offer_unavailable", "A community event ticket requires 32 Roo Points for a 100% ticket discount.")
    if (
        not offer.is_active or not reward.is_active or reward.cost_points <= 0
        or (offer.valid_start_at and offer.valid_start_at > now) or offer.valid_end_at <= now
        or (reward.stock_remaining is not None and reward.stock_remaining <= 0)
    ):
        raise EventDiscountError("offer_unavailable", "This event discount is unavailable.")


def _name_candidates(offers, query):
    """Match canonical public event titles as well as administrator reward labels."""
    key_fingerprint = hashlib.sha256(str(getattr(settings, "LUMA_API_KEY", "")).encode()).hexdigest()[:16]
    cache_key = "roo:event-discount-discovery:v1:" + key_fingerprint
    events = cache.get(cache_key)
    if events is None:
        try:
            events = LumaAttendeeReportService(timeout=10).list_upcoming_events(now=timezone.now())
        except (LumaAPIError, LumaConfigurationError) as exc:
            raise EventDiscountError("event_discovery_unavailable", "Event details are temporarily unavailable. Try the event ID or ask again shortly.", 503) from exc
        cache.set(cache_key, events, timeout=60)
    needle = query.casefold()
    titles = {event["id"]: event["name"] for event in events if needle in event["name"].casefold()}
    candidates = list(offers.filter(Q(reward__name__icontains=query) | Q(event_id__in=titles)).order_by("event_id")[:11])
    return candidates, titles


def _description(snapshot):
    discount = snapshot["discount"]
    if discount["discount_type"] == "percent":
        description = f"{discount['percent_off']:g}% off one {snapshot['ticket']['name']} ticket"
    else:
        description = f"A${discount['cents_off'] / 100:.2f} off one {snapshot['ticket']['name']} ticket"
    if snapshot["ticket"].get("requires_approval", False):
        description += ". Organiser approval is required and is not guaranteed. Points are spent when the coupon is issued, before approval."
    return description


def quote_payload(quote):
    """Present exactly the saved offer, without exposing private upstream data."""
    snapshot = quote.snapshot
    return {
        "quote_id": str(quote.pk), "expires_at": quote.expires_at.isoformat(),
        "event": {key: snapshot[key] for key in ("id", "name", "url", "starts_at")},
        "points_cost": snapshot["points_cost"], "discount_description": snapshot["discount_description"],
        "eligible_balance": PointsService.microroo_to_legacy_whole(PointsService.get_available_microroo(quote.user)),
    }


def serialize_redemption(row):
    """Return one private receipt; only fulfilled rows disclose the coupon."""
    snapshot = row.quote.snapshot
    fulfilled = row.status == EventDiscountRedemption.Status.FULFILLED
    return {
        "id": str(row.pk), "status": row.status,
        "event_id": row.event_id, "event_name": snapshot["name"],
        "event": {key: snapshot[key] for key in ("id", "name", "url", "starts_at")},
        "points_cost": snapshot["points_cost"],
        "coupon_code": row.coupon_code if fulfilled else None,
        "checkout_url": LumaEventDiscountService.coupon_url(snapshot["url"], row.coupon_code) if fulfilled else None,
        "expires_at": snapshot["valid_end_at"], "created_at": row.created_at.isoformat(),
        "remaining_balance": PointsService.microroo_to_legacy_whole(PointsService.get_available_microroo(row.user)),
    }


def list_redemptions(user):
    """Retain this account's receipts even when new issuance is switched off."""
    _member(user)
    rows = EventDiscountRedemption.objects.filter(user=user).select_related("quote", "user").order_by("-created_at")[:50]
    return {"enabled": feature_enabled(), "redemptions": [serialize_redemption(row) for row in rows]}


class EventDiscountService:
    """Coordinate quote, reserve, provider reconciliation, and exact settlement."""

    @staticmethod
    def quote(*, user, event_query, scope):
        _enabled()
        _member(user)
        scope = _scope(scope)
        if not isinstance(event_query, str) or not 1 <= len(event_query.strip()) <= 300:
            raise EventDiscountError("invalid_event", "Name an event or provide its Luma event ID.", 400)
        query = event_query.strip()
        offers = EventDiscountOffer.objects.select_related("reward").filter(is_active=True, reward__is_active=True)
        exact = offers.filter(Q(event_id=query) | Q(reward__code__iexact=query)).first()
        candidates, titles = ([exact], {}) if exact else _name_candidates(offers, query)
        if not candidates:
            raise EventDiscountError("offer_unavailable", "No eligible event discount matches that request.", 404)
        if len(candidates) != 1:
            raise EventDiscountError("ambiguous_event", "Choose one event by its event ID.", extra={
                "candidates": [{"offer_id": str(row.pk), "event_id": row.event_id, "event_name": titles.get(row.event_id, row.reward.name)} for row in candidates[:10]],
            })
        offer = candidates[0]
        now = timezone.now()
        _available_offer(offer, now)
        if EventDiscountRedemption.objects.filter(user=user, event_id=offer.event_id, status__in=("processing", "fulfilled")).exists():
            raise EventDiscountError("already_redeemed", "You already have a redemption for this event. Ask Roo to show your event discounts.")
        fingerprint = _fingerprint(offer)
        try:
            snapshot = LumaEventDiscountService().validate_offer(_offer_terms(offer), now=now)
        except LumaEventDiscountError as exc:
            raise EventDiscountError(exc.code, exc.message, 503 if exc.retryable else 409) from exc
        snapshot.update(points_cost=offer.reward.cost_points, reward_code=offer.reward_id)
        snapshot["discount_description"] = _description(snapshot)
        # Freeze a fresh policy check after the upstream read. No points or stock
        # are reserved by a quote; concurrent changes are checked again on confirm.
        with transaction.atomic():
            user = get_user_model().objects.select_for_update().get(pk=user.pk)
            _member(user)
            offer = EventDiscountOffer.objects.select_related("reward").get(pk=offer.pk)
            if fingerprint != _fingerprint(offer):
                raise EventDiscountError("stale_offer", "The offer changed. Ask Roo for a new quote.")
            _available_offer(offer, timezone.now())
            cost = PointsService.roo_to_microroo(snapshot["points_cost"])
            if PointsService.get_available_microroo(user) < cost:
                raise EventDiscountError("insufficient_points", "You do not have enough eligible Roo Points for this offer.")
            return EventDiscountQuote.objects.create(
                user=user, offer=offer, offer_fingerprint=fingerprint, snapshot=snapshot,
                slack_team_id=scope["team_id"], slack_channel_id=scope["channel_id"], slack_thread_ts=scope["thread_ts"],
                expires_at=min(timezone.now() + timedelta(minutes=10), parse_datetime(snapshot["valid_end_at"])),
            )

    @staticmethod
    @transaction.atomic
    def reserve(*, user, quote_id, request_id, scope):
        _enabled()
        _member(user)
        scope = _scope(scope)
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id):
            raise EventDiscountError("invalid_request_id", "A stable redemption request ID is required.", 400)
        try:
            quote_uuid = uuid.UUID(str(quote_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise EventDiscountError("invalid_quote", "Ask Roo for a new event quote.", 400) from exc
        user = get_user_model().objects.select_for_update().get(pk=user.pk)
        _member(user)
        quote = EventDiscountQuote.objects.select_related("offer__reward").filter(pk=quote_uuid, user=user).first()
        if quote is None:
            raise EventDiscountError("quote_not_found", "This quote was not found.", 404)
        if (quote.slack_team_id, quote.slack_channel_id, quote.slack_thread_ts) != tuple(scope[key] for key in ("team_id", "channel_id", "thread_ts")):
            raise EventDiscountError("quote_scope_mismatch", "Confirm this quote in the same private Roo thread.", 403)
        existing = EventDiscountRedemption.objects.filter(Q(request_id=request_id) | Q(quote=quote)).first()
        if existing:
            if existing.user_id != user.pk or existing.quote_id != quote.pk or existing.request_id != request_id:
                raise EventDiscountError("idempotency_conflict", "That request ID or quote belongs to another redemption.")
            return existing
        reward = RewardsCatalog.objects.select_for_update().get(pk=quote.offer.reward_id)
        offer = EventDiscountOffer.objects.select_for_update().select_related("reward").get(pk=quote.offer_id)
        offer.reward = reward
        now = timezone.now()
        if quote.expires_at <= now or quote.offer_fingerprint != _fingerprint(offer):
            raise EventDiscountError("stale_quote", "This quote expired or changed. Ask Roo for a new quote.")
        _available_offer(offer, now)
        if EventDiscountRedemption.objects.filter(user=user, event_id=offer.event_id, status__in=("processing", "fulfilled")).exists():
            raise EventDiscountError("already_redeemed", "You already have a redemption for this event.")
        if reward.max_per_user is not None and reward.max_per_user < 1:
            raise EventDiscountError("offer_limit", "This offer is not available for redemption.")
        cost = PointsService.roo_to_microroo(quote.snapshot["points_cost"])
        account, _ = PointsAccount.objects.get_or_create(user=user)
        PointsAccount.objects.select_for_update().get(pk=account.pk)
        if PointsService.get_available_microroo(user) < cost:
            raise EventDiscountError("insufficient_points", "You do not have enough eligible Roo Points for this offer.")
        # Persist the opaque code before any provider write. All retries reuse it.
        try:
            with transaction.atomic():
                row = EventDiscountRedemption.objects.create(
                    user=user, quote=quote, offer=offer, event_id=offer.event_id, request_id=request_id,
                    coupon_code="ROO-" + "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for _ in range(16)),
                    reserved_microroo=cost, next_attempt_at=now,
                )
        except IntegrityError as exc:
            # A cross-account request-key collision can race despite the member
            # lock. The savepoint preserves the outer transaction for a clean409.
            raise EventDiscountError("idempotency_conflict", "This redemption request conflicts with an existing request.") from exc
        if reward.stock_remaining is not None:
            reward.stock_remaining -= 1
            reward.save(update_fields=("stock_remaining", "updated_at"))
        return row

    @staticmethod
    def _locked(redemption_id):
        # A row's owner is protected against generic account merges while an
        # event receipt exists. The global points lock order begins with User.
        owner = EventDiscountRedemption.objects.values_list("user_id", flat=True).get(pk=redemption_id)
        user = get_user_model().objects.select_for_update().get(pk=owner)
        row = EventDiscountRedemption.objects.select_for_update(of=("self",)).select_related("quote", "offer", "user").get(pk=redemption_id)
        row.user = user
        return row

    @staticmethod
    @transaction.atomic
    def _claim(redemption_id):
        row = EventDiscountService._locked(redemption_id)
        now = timezone.now()
        if (row.status != "processing" or row.needs_review
                or (row.lease_until and row.lease_until > now)
                or (row.next_attempt_at and row.next_attempt_at > now)):
            return row, None
        row.lease_token = uuid.uuid4()
        row.lease_until = now + timedelta(minutes=10)
        row.attempts += 1
        row.save(update_fields=("lease_token", "lease_until", "attempts", "updated_at"))
        return row, row.lease_token

    @staticmethod
    @transaction.atomic
    def _checkpoint_attempt(redemption_id, token):
        row = EventDiscountService._locked(redemption_id)
        if row.status != "processing" or row.lease_token != token:
            return False
        _member(row.user)
        row.creation_attempted_at = row.creation_attempted_at or timezone.now()
        row.save(update_fields=("creation_attempted_at", "updated_at"))
        return True

    @staticmethod
    @transaction.atomic
    def _defer(redemption_id, token, code, *, review=False):
        row = EventDiscountService._locked(redemption_id)
        if row.status == "processing" and row.lease_token == token:
            row.last_error_code = str(code)[:64]
            row.needs_review = review
            row.next_attempt_at = timezone.now() + timedelta(seconds=min(30 * (2 ** min(row.attempts, 7)), 3600))
            row.lease_token = row.lease_until = None
            row.save(update_fields=("last_error_code", "needs_review", "next_attempt_at", "lease_token", "lease_until", "updated_at"))
        return row

    @staticmethod
    @transaction.atomic
    def _fail(redemption_id, token, code):
        row = EventDiscountService._locked(redemption_id)
        if row.status == "processing" and row.lease_token == token:
            reward = RewardsCatalog.objects.select_for_update().get(pk=row.offer.reward_id)
            row.status, row.reserved_microroo = "failed", 0
            row.last_error_code = str(code)[:64]
            row.lease_token = row.lease_until = row.next_attempt_at = None
            row.save()
            if reward.stock_remaining is not None:
                reward.stock_remaining += 1
                reward.save(update_fields=("stock_remaining", "updated_at"))
        return row

    @staticmethod
    @transaction.atomic
    def _settle(redemption_id, token, coupon):
        row = EventDiscountService._locked(redemption_id)
        if row.status != "processing" or row.lease_token != token:
            return row
        _member(row.user)
        if parse_datetime(row.quote.snapshot["valid_end_at"]) <= timezone.now():
            raise EventDiscountError("offer_expired", "The event discount expired before it could be issued.")
        cost = row.quote.snapshot["points_cost"]
        account = PointsAccount.objects.select_for_update().get(user=row.user)
        PointsService._ensure_microroo_account(account)
        if parse_datetime(row.quote.snapshot["valid_end_at"]) <= timezone.now():
            raise EventDiscountError("offer_expired", "The event discount expired before it could be issued.")
        purchased = min(account.purchased_topup_balance_microroo, row.reserved_microroo)
        ledger, _ = PointsService.spend(
            user=row.user, delta=cost, source="EVENT",
            description=f"Event discount: {row.quote.snapshot['name']}",
            created_by_slack_id=row.user.slack_id or "", idempotency_key=f"event_discount_spend:{row.pk}",
            reference_type="EVENT_DISCOUNT", reference_id=str(row.pk),
            purchased_delta_microroo=purchased, allow_event_redemption_id=row.pk,
        )
        # The append-only ledger records this exact allocation when created.
        row.status, row.reserved_microroo = "fulfilled", 0
        row.purchased_microroo, row.ledger_entry = purchased, ledger
        row.luma_coupon_id = str(coupon["id"])
        row.fulfilled_at = timezone.now()
        row.lease_token = row.lease_until = row.next_attempt_at = None
        row.last_error_code = ""
        row.save()
        return row

    @staticmethod
    def process(redemption_id, *, gateway=None):
        """Attempt once; unknown external results keep their hold for reconciliation."""
        row, token = EventDiscountService._claim(redemption_id)
        if token is None:
            return row
        attempted_before = row.creation_attempted_at is not None
        try:
            gateway = gateway or LumaEventDiscountService()
            snapshot = row.quote.snapshot
            coupon = gateway.find_coupon(row.event_id, row.coupon_code)
            if coupon is not None:
                if not attempted_before:
                    return EventDiscountService._fail(row.pk, token, "coupon_code_collision")
                if not gateway.matches_coupon(coupon, snapshot, row.coupon_code, allow_consumed=False):
                    return EventDiscountService._defer(row.pk, token, "coupon_terms_need_review", review=True)
            else:
                if attempted_before:
                    # An absent read is not proof an earlier timed-out mutation
                    # never happened. Keep the same hold/code and reconcile;
                    # prolonged uncertainty becomes an operator review.
                    return EventDiscountService._defer(
                        row.pk, token, "coupon_result_unknown", review=row.attempts >= 6,
                    )
                _member(row.user)
                if not feature_enabled():
                    return EventDiscountService._fail(row.pk, token, "event_discounts_disabled")
                gateway.validate_offer(snapshot, now=timezone.now())
                if not EventDiscountService._checkpoint_attempt(row.pk, token):
                    return EventDiscountRedemption.objects.select_related("quote", "user").get(pk=row.pk)
                coupon = gateway.create_coupon(snapshot, row.coupon_code)
            if not gateway.matches_coupon(coupon, snapshot, row.coupon_code, allow_consumed=False):
                return EventDiscountService._defer(row.pk, token, "coupon_terms_need_review", review=True)
            if parse_datetime(snapshot["valid_end_at"]) <= timezone.now() or not row.user.is_active:
                # A remote disable cannot be fenced by a DB lease. A stale
                # worker must never invalidate a newer worker's fulfilled code;
                # uncertain consumption/expiry requires reviewed reconciliation.
                return EventDiscountService._defer(row.pk, token, "offer_expired_or_member_inactive", review=True)
            return EventDiscountService._settle(row.pk, token, coupon)
        except LumaEventDiscountError as exc:
            # A prior uncertain write cannot be disproved by a later auth or
            # read failure. Never release such a hold just because a retry failed.
            latest = EventDiscountRedemption.objects.get(pk=row.pk)
            if exc.uncertain or exc.retryable or (attempted_before and latest.creation_attempted_at):
                return EventDiscountService._defer(row.pk, token, exc.code)
            return EventDiscountService._fail(row.pk, token, exc.code)
        except EventDiscountError as exc:
            latest = EventDiscountRedemption.objects.get(pk=row.pk)
            if latest.creation_attempted_at:
                return EventDiscountService._defer(row.pk, token, exc.code, review=True)
            return EventDiscountService._fail(row.pk, token, exc.code)
        except InsufficientBalanceError:
            return EventDiscountService._defer(row.pk, token, "settlement_requires_review", review=True)


def process_pending(*, limit=20):
    """Reconcile bounded durable work; an expired lease never releases its hold."""
    now = timezone.now()
    ids = list(EventDiscountRedemption.objects.filter(status="processing", needs_review=False)
               .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
               .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
               .order_by("next_attempt_at").values_list("pk", flat=True)[:max(1, min(int(limit), 100))])
    for redemption_id in ids:
        EventDiscountService.process(redemption_id)
    return len(ids)
