"""API for the community token leaderboard.

Two halves. The ingest endpoints speak the published ``tokenmaxer`` reporter's
wire protocol, so members run the unmodified npm package pointed at MLAI. The
member-facing endpoints (mint, visibility, leave, read) sit behind the account
session MLAI Chat already holds, which is what scopes the board to members.

Counts are self-reported. That is the intended trust model for a community
board and it is stated plainly in the UI — there are no prizes attached.
"""

import hashlib
import secrets
from datetime import date as calendar_date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import CharField, Count, Sum, Value
from django.db.models.functions import Concat
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import (
    USAGE_TOKEN_PREFIX,
    CommunityChatAccountAuthentication,
    CommunityChatBootstrapAuthentication,
    TokenUsageAuthentication,
)
from hospital.authentication import CustomJWTAuthentication

from .models import (
    CommunityChatDevice,
    DeviceBindingStatus,
    TokenUsageAccount,
    TokenUsageSession,
)
from .serializers import public_chat_profile
from .throttles import CommunityChatScopedThrottle
from .token_usage import (
    MAX_HISTORY_SESSIONS,
    MAX_INGEST_SESSIONS,
    IngestError,
    normalized_token_total,
    parse_sessions,
    upsert_sessions,
)
from .tokenmaxer_federation import fetch_public_tokenmaxer_entries


ACCOUNT_AUTHENTICATION_CLASSES = (
    CommunityChatAccountAuthentication,
    CommunityChatBootstrapAuthentication,
    CustomJWTAuthentication,
)

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
)

WINDOWS = ("today", "7d", "30d", "all")
DEFAULT_WINDOW = "today"
DEFAULT_LIMIT = 100
MAX_LIMIT = 200


def _window_dates(window, anchor):
    """Inclusive calendar bounds, or ``(None, None)`` for all time."""
    if window == "today":
        return anchor, anchor
    if window == "7d":
        return anchor - timedelta(days=6), anchor
    if window == "30d":
        return anchor - timedelta(days=29), anchor
    return None, None


def _parse_anchor(raw, local_today):
    if raw in (None, ""):
        return local_today
    try:
        anchor = calendar_date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if anchor > local_today:
        return None
    return anchor


def _leaderboard_date(at):
    return at.astimezone(
        ZoneInfo(settings.TOKEN_USAGE_LEADERBOARD_TIME_ZONE)
    ).date()


def _session_window(rows, date_from, date_to):
    """Filter cumulative sessions by their start date for every time window."""
    if date_from is None:
        return rows
    zone = ZoneInfo(settings.TOKEN_USAGE_LEADERBOARD_TIME_ZONE)
    start = datetime.combine(date_from, time.min, tzinfo=zone)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=zone)
    return rows.filter(started_at__gte=start, started_at__lt=end)


def _empty_entry(account_id, *, has_reported):
    return {
        "account_id": account_id,
        "sessions": 0,
        "grand_total": 0,
        "has_reported": has_reported,
        **{field: 0 for field in TOKEN_FIELDS},
    }


def _aggregate_entries(rows, initial):
    """Fold source-aware aggregates so inclusive caches are counted once."""
    session_identity = Concat(
        "source",
        Value(":"),
        "session_id",
        output_field=CharField(),
    )
    grouped = rows.values("account_id", "source").annotate(
        sessions=Count(session_identity, distinct=True),
        **{field: Sum(field) for field in TOKEN_FIELDS},
    )
    entries = {account_id: dict(entry) for account_id, entry in initial.items()}
    for group in grouped:
        account_id = group["account_id"]
        entry = entries.setdefault(
            account_id,
            _empty_entry(account_id, has_reported=True),
        )
        totals = {field: int(group[field] or 0) for field in TOKEN_FIELDS}
        entry["sessions"] += int(group["sessions"] or 0)
        entry["grand_total"] += normalized_token_total(group["source"], totals)
        entry["has_reported"] = True
        for field, value in totals.items():
            entry[field] += value
    return list(entries.values())


def _external_payload(entry):
    return {
        "public_id": entry["external_id"],
        "display_name": entry["display_name"],
        "avatar_url": None,
        "public_key": None,
        "profile_url": entry["profile_url"],
        "origin": "tokenmaxer",
        "has_reported": True,
        **{key: entry[key] for key in ("sessions", "grand_total", *TOKEN_FIELDS)},
    }


class _TokenUsageIngestBase(APIView):
    """Shared ingest handler for live reporting and history backfill."""

    authentication_classes = (TokenUsageAuthentication,)
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    max_sessions = MAX_INGEST_SESSIONS
    attribute_daily = True

    def post(self, request):
        account = getattr(request, "token_usage_account", None)
        if account is None:
            return Response({"error": "unauthorized"}, status=401)

        try:
            source, rows, rejected = parse_sessions(
                request.data, self.max_sessions
            )
        except IngestError as exc:
            return Response({"error": str(exc)}, status=400)

        if rows:
            reported_at = timezone.now()
            upsert_sessions(
                account,
                source,
                rows,
                reported_at=reported_at,
                attribute_daily=self.attribute_daily,
            )
            TokenUsageAccount.objects.filter(pk=account.pk).update(
                last_report_at=reported_at
            )
        return Response({"accepted": len(rows), "rejected": rejected})


class TokenUsageIngestView(_TokenUsageIngestBase):
    """POST {api_base}/api/ingest — live reporting from agent session hooks."""

    community_chat_throttle_scope = "token_usage_ingest"


class TokenUsageHistoryView(_TokenUsageIngestBase):
    """POST {api_base}/api/history — one-time backfill of past sessions."""

    community_chat_throttle_scope = "token_usage_history"
    max_sessions = MAX_HISTORY_SESSIONS
    attribute_daily = False


class TokenUsageTokenView(APIView):
    """Mint, adjust, or surrender a member's place on the board."""

    authentication_classes = ACCOUNT_AUTHENTICATION_CLASSES
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "token_usage_token"

    def get(self, request):
        account = TokenUsageAccount.objects.filter(user=request.user).first()
        if account is None:
            return Response(
                {"connected": False, "api_base": settings.TOKEN_USAGE_API_BASE}
            )
        return Response(
            {
                "connected": True,
                "api_base": settings.TOKEN_USAGE_API_BASE,
                "is_public": account.is_public,
                "last_report_at": account.last_report_at,
            }
        )

    def post(self, request):
        """Mint or rotate. The raw token is shown exactly once."""
        raw_token = USAGE_TOKEN_PREFIX + secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        account, created = TokenUsageAccount.objects.update_or_create(
            user=request.user,
            defaults={"token_hash": token_hash},
        )
        return Response(
            {
                "token": raw_token,
                "api_base": settings.TOKEN_USAGE_API_BASE,
                "is_public": account.is_public,
            },
            status=201 if created else 200,
        )

    def patch(self, request):
        """Hide or show the member's row without discarding their history."""
        account = TokenUsageAccount.objects.filter(user=request.user).first()
        if account is None:
            return Response({"error": "not connected"}, status=404)
        is_public = request.data.get("is_public")
        if not isinstance(is_public, bool):
            return Response({"error": "is_public must be a boolean"}, status=400)
        account.is_public = is_public
        account.save(update_fields=["is_public", "updated_at"])
        return Response({"is_public": account.is_public})

    def delete(self, request):
        """Leave the board. Cascades, so the member's rows are really gone."""
        TokenUsageAccount.objects.filter(user=request.user).delete()
        return Response(status=204)


class TokenUsageLeaderboardView(APIView):
    """The MLAI board. Members only — this is not a public ranking."""

    authentication_classes = ACCOUNT_AUTHENTICATION_CLASSES
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "token_usage_leaderboard"

    def get(self, request):
        window = request.query_params.get("window", DEFAULT_WINDOW)
        if window not in WINDOWS:
            return Response(
                {"error": "window must be one of: " + ", ".join(WINDOWS)},
                status=400,
            )
        try:
            limit = int(request.query_params.get("limit", DEFAULT_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT
        limit = min(max(limit, 1), MAX_LIMIT)

        now = timezone.now()
        leaderboard_today = _leaderboard_date(now)
        anchor = _parse_anchor(request.query_params.get("date"), leaderboard_today)
        if anchor is None:
            return Response(
                {"error": "date must be a valid, non-future YYYY-MM-DD value"},
                status=400,
            )
        date_from, date_to = _window_dates(window, anchor)
        rows = _session_window(
            TokenUsageSession.objects.filter(
                account__is_public=True,
                started_at__lte=now,
            ),
            date_from,
            date_to,
        )

        # An opted-in member should not disappear merely because they have not
        # emitted a positive delta inside the selected window. Keep every public
        # reporter account on the board and fill the selected window with zeroes
        # when it has no matching buckets. `has_reported` distinguishes an
        # installed-but-silent reporter from a historical contributor who is
        # simply inactive in this period.
        public_accounts = list(
            TokenUsageAccount.objects.filter(is_public=True).select_related("user")
        )
        reported_account_ids = set(
            TokenUsageSession.objects.filter(
                account__is_public=True,
                started_at__lte=now,
            ).values_list("account_id", flat=True)
        )
        entries = _aggregate_entries(
            rows,
            {
                account.id: _empty_entry(
                    account.id,
                    has_reported=account.id in reported_account_ids,
                )
                for account in public_accounts
            },
        )

        own = TokenUsageAccount.objects.filter(user=request.user).first()
        hidden_own_entry = None
        if own is not None and not own.is_public:
            own_rows = _session_window(
                TokenUsageSession.objects.filter(
                    account=own,
                    started_at__lte=now,
                ),
                date_from,
                date_to,
            )
            has_reported = TokenUsageSession.objects.filter(
                account=own,
                started_at__lte=now,
            ).exists()
            hidden_own_entry = _aggregate_entries(
                own_rows,
                {
                    own.id: _empty_entry(
                        own.id,
                        has_reported=has_reported,
                    )
                },
            )[0]

        accounts = {account.id: account for account in public_accounts}
        if own is not None and own.id not in accounts:
            accounts[own.id] = own

        own_account_id = None
        if own is not None:
            own_account_id = own.id

        pubkeys = _public_keys_for(
            [account.user_id for account in accounts.values()]
        )

        local_payloads = []
        own_public_id = None
        for entry in entries:
            account = accounts.get(entry["account_id"])
            if account is None:
                continue
            totals = {
                key: value for key, value in entry.items() if key != "account_id"
            }
            payload = dict(
                totals,
                profile_url=None,
                origin="mlai",
                **_member_payload(account, pubkeys),
            )
            if account.id == own_account_id:
                own_public_id = payload["public_id"]
            if account.is_public:
                local_payloads.append(payload)

        # The upstream API can only describe its current windows. Historical
        # anchor queries remain local rather than presenting mismatched dates.
        external_payloads = []
        if anchor == leaderboard_today:
            external_payloads = [
                _external_payload(entry)
                for entry in fetch_public_tokenmaxer_entries(window)
            ]

        combined = [*local_payloads, *external_payloads]
        combined.sort(
            key=lambda entry: (
                -entry["grand_total"],
                entry["origin"],
                entry["public_id"],
            )
        )
        for rank, payload in enumerate(combined, start=1):
            payload["rank"] = rank

        ranked = combined[:limit]
        you = next(
            (
                payload
                for payload in combined
                if own_public_id is not None
                and payload["public_id"] == own_public_id
            ),
            None,
        )

        if hidden_own_entry is not None and own is not None:
            hidden_totals = {
                key: value
                for key, value in hidden_own_entry.items()
                if key != "account_id"
            }
            hidden_payload = dict(
                hidden_totals,
                profile_url=None,
                origin="mlai",
                **_member_payload(own, pubkeys),
            )
            hidden_sort_key = (
                -hidden_payload["grand_total"],
                hidden_payload["origin"],
                hidden_payload["public_id"],
            )
            hidden_payload["rank"] = 1 + sum(
                (
                    -entry["grand_total"],
                    entry["origin"],
                    entry["public_id"],
                )
                < hidden_sort_key
                for entry in combined
            )
            you = hidden_payload

        return Response(
            {
                "window": window,
                "timezone": settings.TOKEN_USAGE_LEADERBOARD_TIME_ZONE,
                "window_basis": "session_started_at",
                "total_basis": "source_normalized",
                "date_from": date_from.isoformat() if date_from else None,
                "date_to": date_to.isoformat() if date_to else None,
                "entries": ranked,
                # A member outside the cut still sees where they stand;
                # otherwise connecting appears to have done nothing.
                "you": you,
                "connected": own is not None,
            }
        )


def _public_keys_for(user_ids):
    """Map user id to active Nostr pubkey in one query, for click-to-DM.

    A member may have several devices; any active one identifies them on the
    relay, so the most recently verified wins.
    """
    devices = (
        CommunityChatDevice.objects.filter(
            user_id__in=user_ids,
            status=DeviceBindingStatus.VERIFIED,
        )
        .order_by("user_id", "-verified_at", "-created_at")
        .values_list("user_id", "public_key")
    )
    resolved = {}
    for user_id, public_key in devices:
        resolved.setdefault(user_id, public_key)
    return resolved


def _member_payload(account, pubkeys):
    """Identity for one row, resolved from the member's MLAI profile."""
    profile = public_chat_profile(account.user)
    return {
        "public_id": profile["public_id"],
        "display_name": profile["display_name"],
        "avatar_url": profile["avatar_url"],
        "public_key": pubkeys.get(account.user_id),
    }
