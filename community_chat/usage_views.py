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
from datetime import date as calendar_date, timedelta

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
    TokenUsageDailyBucket,
    TokenUsageSession,
)
from .serializers import public_chat_profile
from .throttles import CommunityChatScopedThrottle
from .token_usage import (
    MAX_HISTORY_SESSIONS,
    MAX_INGEST_SESSIONS,
    IngestError,
    local_usage_date,
    parse_sessions,
    upsert_sessions,
)


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
        local_today = local_usage_date(now)
        anchor = _parse_anchor(request.query_params.get("date"), local_today)
        if anchor is None:
            return Response(
                {"error": "date must be a valid, non-future YYYY-MM-DD value"},
                status=400,
            )
        date_from, date_to = _window_dates(window, anchor)
        if date_from is None:
            rows = TokenUsageSession.objects.filter(
                account__is_public=True,
                started_at__lte=now,
            )
        else:
            rows = TokenUsageDailyBucket.objects.filter(
                account__is_public=True,
                usage_date__range=(date_from, date_to),
            )

        session_identity = Concat(
            "source",
            Value(":"),
            "session_id",
            output_field=CharField(),
        )
        grouped = rows.values("account_id").annotate(
            sessions=Count(session_identity, distinct=True),
            **{field: Sum(field) for field in TOKEN_FIELDS}
        )

        # A reporting member should not disappear merely because they have not
        # emitted a positive delta inside the selected daily window.  Keep every
        # public account that has reported at least one session on the board and
        # fill the selected window with zeroes when it has no matching buckets.
        # This makes Today/7d/30d useful as slices of the same contributor set
        # instead of looking like the user's connected peers have vanished.
        contributor_account_ids = set(
            TokenUsageSession.objects.filter(
                account__is_public=True,
                started_at__lte=now,
            ).values_list("account_id", flat=True)
        )
        entries_by_account = {
            account_id: {
                "account_id": account_id,
                "sessions": 0,
                "grand_total": 0,
                **{field: 0 for field in TOKEN_FIELDS},
            }
            for account_id in contributor_account_ids
        }
        for group in grouped:
            totals = {field: int(group[field] or 0) for field in TOKEN_FIELDS}
            entries_by_account[group["account_id"]] = {
                "account_id": group["account_id"],
                "sessions": group["sessions"],
                "grand_total": sum(totals.values()),
                **totals,
            }
        entries = list(entries_by_account.values())

        own = TokenUsageAccount.objects.filter(user=request.user).first()
        hidden_own_entry = None
        if own is not None and not own.is_public:
            if date_from is None:
                own_rows = TokenUsageSession.objects.filter(
                    account=own,
                    started_at__lte=now,
                )
            else:
                own_rows = TokenUsageDailyBucket.objects.filter(
                    account=own,
                    usage_date__range=(date_from, date_to),
                )
            own_group = own_rows.aggregate(
                sessions=Count(session_identity, distinct=True),
                **{field: Sum(field) for field in TOKEN_FIELDS},
            )
            own_totals = {
                field: int(own_group[field] or 0) for field in TOKEN_FIELDS
            }
            has_reported = TokenUsageSession.objects.filter(
                account=own,
                started_at__lte=now,
            ).exists()
            if has_reported:
                hidden_own_entry = {
                    "account_id": own.id,
                    "sessions": own_group["sessions"],
                    "grand_total": sum(own_totals.values()),
                    **own_totals,
                }
        entries.sort(
            key=lambda entry: (-entry["grand_total"], str(entry["account_id"]))
        )

        accounts = {
            account.id: account
            for account in TokenUsageAccount.objects.filter(
                id__in=[
                    *[entry["account_id"] for entry in entries],
                    *([own.id] if hidden_own_entry is not None else []),
                ]
            ).select_related("user")
        }

        own_account_id = None
        if own is not None:
            own_account_id = own.id

        pubkeys = _public_keys_for(
            [account.user_id for account in accounts.values()]
        )

        ranked = []
        you = None
        for rank, entry in enumerate(entries, start=1):
            account = accounts.get(entry["account_id"])
            if account is None:
                continue
            totals = {
                key: value for key, value in entry.items() if key != "account_id"
            }
            payload = dict(
                totals, rank=rank, **_member_payload(account, pubkeys)
            )
            if account.id == own_account_id:
                you = payload
            if account.is_public and rank <= limit:
                ranked.append(payload)

        if hidden_own_entry is not None and own is not None:
            hidden_sort_key = (
                -hidden_own_entry["grand_total"],
                str(hidden_own_entry["account_id"]),
            )
            hidden_rank = 1 + sum(
                (
                    -entry["grand_total"],
                    str(entry["account_id"]),
                )
                < hidden_sort_key
                for entry in entries
            )
            hidden_totals = {
                key: value
                for key, value in hidden_own_entry.items()
                if key != "account_id"
            }
            you = dict(
                hidden_totals,
                rank=hidden_rank,
                **_member_payload(own, pubkeys),
            )

        return Response(
            {
                "window": window,
                "timezone": settings.TOKEN_USAGE_TIME_ZONE,
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
