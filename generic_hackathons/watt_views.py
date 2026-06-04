import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from django.conf import settings
from django.core.cache import caches
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from core.firebase_utils import create_firebase_custom_token
from core.models import Hackathon
from .models import GenericHackathonTeam


WATT_HACKATHON_SLUGS = ("watt", "watt-the-hack")
UNITY_TICKET_CACHE_PREFIX = "watt_unity_session_ticket"
FIREBASE_SEGMENT_RE = re.compile(r"[\.\#\$\[\]/\s]+")

# Unity session tickets live in a DB-backed cache (settings.CACHES["watt_session"]) so a ticket
# minted by one gunicorn worker can be redeemed by any other. The default LocMemCache is
# per-process, which 404s the redeem when mint and redeem land on different workers.
cache = caches["watt_session"]

# A team may enter the streamed game only with 2..6 members. Max mirrors
# GENERIC_HACKATHON_MAX_TEAM_MEMBERS in views.py; the min is enforced here and in the web gate.
WATT_MIN_TEAM_MEMBERS = 2
WATT_MAX_TEAM_MEMBERS = 6


class WattUnityTicketRedeemThrottle(AnonRateThrottle):
    scope = "watt_unity_ticket_redeem"


def _get_watt_hackathon():
    hackathon = Hackathon.objects.filter(slug__in=WATT_HACKATHON_SLUGS).order_by("slug").first()
    if hackathon is None:
        return get_object_or_404(Hackathon, slug="watt")
    return hackathon


def _current_team(user, hackathon):
    # Deterministic resolution: a user on >1 Watt team must always resolve to the SAME
    # household, or their stream/deploy/shop could hop between teams across requests and
    # laptops. Order by pk (earliest team the user joined wins) so it is stable everywhere.
    return (
        GenericHackathonTeam.objects
        .filter(hackathon=hackathon, members=user)
        .order_by("pk")
        .prefetch_related("members")
        .first()
    )


def _firebase_segment(value, fallback):
    cleaned = FIREBASE_SEGMENT_RE.sub("_", str(value or "").strip()).strip("_")
    return cleaned or fallback


def _class_id():
    return _firebase_segment(getattr(settings, "WATT_HACKATHON_CLASS_ID", "WATT"), "WATT")


def _household_id(team):
    return _firebase_segment(team.code or f"TEAM{team.pk}", f"TEAM{team.pk}")


def _team_size_gate(team):
    """Return a 403 ``Response`` when the team can't enter the game (needs 2..6 members), else ``None``.

    Enforces the same rule as the web ``requireValidWattTeam`` gate at the source, so stream /
    Firebase tokens and smart-home writes are never issued to an invalidly-sized team (rather
    than merely hidden in the UI).
    """
    count = team.members.count()
    if WATT_MIN_TEAM_MEMBERS <= count <= WATT_MAX_TEAM_MEMBERS:
        return None
    return Response(
        {
            "error": (
                f"Your team needs between {WATT_MIN_TEAM_MEMBERS} and "
                f"{WATT_MAX_TEAM_MEMBERS} members to enter the game (currently {count})."
            ),
            "member_count": count,
            "min_members": WATT_MIN_TEAM_MEMBERS,
            "max_members": WATT_MAX_TEAM_MEMBERS,
        },
        status=status.HTTP_403_FORBIDDEN,
    )


def _ticket_ttl_seconds():
    # Default 15 min so a slow Vagon cold-boot can still redeem before the ticket expires.
    return max(30, int(getattr(settings, "WATT_UNITY_SESSION_TICKET_TTL_SECONDS", 900)))


def _ticket_key(ticket):
    return f"{UNITY_TICKET_CACHE_PREFIX}:{ticket}"


def _ticket_used_key(ticket):
    return f"{UNITY_TICKET_CACHE_PREFIX}:used:{ticket}"


def _api_base_url(request):
    configured = str(getattr(settings, "WATT_HACKATHON_API_BASE_URL", "") or "").strip()
    if configured:
        return configured.rstrip("/")
    return request.build_absolute_uri("/").rstrip("/")


def _vagon_stream_base_url():
    configured_url = str(getattr(settings, "VAGON_STREAM_URL", "") or "").strip()
    if configured_url:
        parts = urlsplit(configured_url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))

    stream_id = str(getattr(settings, "VAGON_STREAM_ID", "") or "").strip()
    if stream_id:
        return f"https://app.vagon.io/streams/{quote(stream_id, safe='')}"

    return ""


def _campaign_window_ms():
    """Return ``(start_ms, end_ms)`` for the global campaign window, or ``(None, None)``.

    The campaign maps linearly onto ``[START, START + LENGTH * DAY_SECONDS]``. The same epoch-ms
    values are handed to every team and every session, so the in-game day is a pure function of
    wall-clock time. ``WATT_CAMPAIGN_START`` must be an ISO-8601 datetime WITH an explicit UTC
    offset; a naive (timezone-less) value is rejected so the window can't drift with the server tz.
    """
    raw = str(getattr(settings, "WATT_CAMPAIGN_START", "") or "").strip()
    if not raw:
        return None, None
    try:
        start_dt = datetime.fromisoformat(raw)
    except ValueError:
        return None, None
    if start_dt.tzinfo is None:
        return None, None

    length_days = max(1, int(getattr(settings, "WATT_CAMPAIGN_LENGTH_DAYS", 46)))
    day_seconds = float(getattr(settings, "WATT_CAMPAIGN_DAY_SECONDS", 700.0))
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = start_ms + int(round(length_days * day_seconds * 1000))
    return start_ms, end_ms


def _mint_firebase_token(role, class_id, household_id, uid_suffix):
    claims = {
        "role": role,
        "class_id": class_id,
        "household_id": household_id,
    }
    uid = f"{role}:{household_id}:{uid_suffix}"[:128]
    return create_firebase_custom_token(uid, claims)


def _build_stream_url(base_url, household_id, ticket, api_base_url, start_ms=None, end_ms=None):
    launch_flags = (
        f"--household-id {household_id} "
        f"--session-ticket {ticket} "
        f"--backend-url {api_base_url}"
    )
    if start_ms is not None and end_ms is not None:
        launch_flags += f" --campaign-start-ms {start_ms} --campaign-end-ms {end_ms}"
    query = urlencode(
        {
            "launchFlags": launch_flags,
            "newSession": "true",
        },
        quote_via=quote,
    )
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{query}"


class WattUnitySessionCurrentView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        hackathon = _get_watt_hackathon()
        team = _current_team(request.user, hackathon)
        if team is None:
            return Response(
                {"error": "Join or create a Watt team before starting a Unity session."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        gate = _team_size_gate(team)
        if gate is not None:
            return gate

        base_url = _vagon_stream_base_url()
        if not base_url:
            return Response(
                {"error": "Vagon stream is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        class_id = _class_id()
        household_id = _household_id(team)
        campaign_start_ms, campaign_end_ms = _campaign_window_ms()
        expires_at = timezone.now() + timedelta(seconds=_ticket_ttl_seconds())
        ticket = secrets.token_urlsafe(32)
        try:
            unity_token = _mint_firebase_token(
                "watt_unity",
                class_id,
                household_id,
                uuid4().hex[:12],
            )
        except Exception as exc:
            return Response(
                {"error": f"Firebase token minting failed: {exc}"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        cache.set(
            _ticket_key(ticket),
            {
                "firebase_custom_token": unity_token,
                "class_id": class_id,
                "household_id": household_id,
                "expires_at": expires_at.isoformat(),
                "expires_at_epoch": expires_at.timestamp(),
            },
            timeout=_ticket_ttl_seconds(),
        )

        return Response(
            {
                "stream_url": _build_stream_url(
                    base_url,
                    household_id,
                    ticket,
                    _api_base_url(request),
                    campaign_start_ms,
                    campaign_end_ms,
                ),
                "household_id": household_id,
                "expires_at": expires_at.isoformat(),
                "campaign_start_ms": campaign_start_ms,
                "campaign_end_ms": campaign_end_ms,
            },
            status=status.HTTP_200_OK,
        )


class WattUnitySessionRedeemTicketView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = [WattUnityTicketRedeemThrottle]

    def post(self, request):
        ticket = str(request.data.get("ticket") or "").strip()
        household_id = str(request.data.get("household_id") or "").strip()
        if not ticket or not household_id:
            return Response(
                {"error": "ticket and household_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = cache.get(_ticket_key(ticket))
        if not isinstance(payload, dict):
            if cache.get(_ticket_used_key(ticket)):
                return Response({"error": "Ticket has already been used."}, status=status.HTTP_409_CONFLICT)
            return Response({"error": "Ticket is invalid or expired."}, status=status.HTTP_404_NOT_FOUND)

        if float(payload.get("expires_at_epoch") or 0) <= timezone.now().timestamp():
            cache.delete(_ticket_key(ticket))
            return Response({"error": "Ticket is expired."}, status=status.HTTP_410_GONE)

        if payload.get("household_id") != household_id:
            return Response({"error": "Ticket household mismatch."}, status=status.HTTP_403_FORBIDDEN)

        remaining_ttl = max(1, int(float(payload["expires_at_epoch"]) - timezone.now().timestamp()))
        if not cache.add(_ticket_used_key(ticket), True, timeout=remaining_ttl):
            return Response({"error": "Ticket has already been used."}, status=status.HTTP_409_CONFLICT)

        cache.delete(_ticket_key(ticket))
        return Response(
            {
                "firebase_custom_token": payload["firebase_custom_token"],
                "class_id": payload["class_id"],
                "household_id": payload["household_id"],
            },
            status=status.HTTP_200_OK,
        )


class WattParticipantFirebaseTokenView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        hackathon = _get_watt_hackathon()
        team = _current_team(request.user, hackathon)
        if team is None:
            return Response(
                {"error": "Join or create a Watt team before requesting a Firebase token."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        gate = _team_size_gate(team)
        if gate is not None:
            return gate

        class_id = _class_id()
        household_id = _household_id(team)
        try:
            participant_token = _mint_firebase_token(
                "watt_participant",
                class_id,
                household_id,
                str(request.user.id),
            )
        except Exception as exc:
            return Response(
                {"error": f"Firebase token minting failed: {exc}"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "firebase_custom_token": participant_token,
                "class_id": class_id,
                "household_id": household_id,
            },
            status=status.HTTP_200_OK,
        )


class WattUnitySessionDevTokenView(APIView):
    """DEV-ONLY: mint a watt_unity custom token for local Unity editor runs.

    Production launches obtain this token through the ticket flow
    (`unity-sessions/current/` -> `redeem-ticket/`). Local in-editor runs have no
    ticket, so this endpoint hands a watt_unity token straight to the editor for a
    given class/household. It is disabled unless ``settings.WATT_ALLOW_DEV_TOKEN``
    (defaulting to ``settings.DEBUG``) is true, so it never functions in production.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        if not getattr(settings, "WATT_ALLOW_DEV_TOKEN", settings.DEBUG):
            return Response(
                {"error": "Dev token endpoint is disabled."},
                status=status.HTTP_403_FORBIDDEN,
            )

        class_id = _firebase_segment(request.data.get("class_id"), "CLASS")
        household_id = _firebase_segment(request.data.get("household_id"), "household_001")
        try:
            unity_token = _mint_firebase_token(
                "watt_unity",
                class_id,
                household_id,
                "dev-local",
            )
        except Exception as exc:
            return Response(
                {"error": f"Firebase token minting failed: {exc}"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "firebase_custom_token": unity_token,
                "class_id": class_id,
                "household_id": household_id,
            },
            status=status.HTTP_200_OK,
        )
