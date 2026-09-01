import base64
import hashlib
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as datetime_timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import update_last_login
from django.core import signing
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .adapter import (
    MembershipAdapterConflict,
    MembershipAdapterUnavailable,
    get_relay_membership,
    issue_member_invite,
    revoke_member_invite,
    revoke_relay_membership,
)
from hospital.authentication import CustomJWTAuthentication

from .authentication import (
    TOKEN_PREFIX,
    CommunityChatAccountAuthentication,
    CommunityChatBootstrapAuthentication,
)
from .account_cookies import (
    ACCESS_COOKIE as ACCOUNT_ACCESS_COOKIE,
    REFRESH_COOKIE as ACCOUNT_REFRESH_COOKIE,
    clear_account_session_cookies,
    set_account_session_cookies,
)
from .account_sessions import (
    InvalidAccountSession,
    issue_account_session,
    revoke_account_session,
    rotate_account_session,
)
from .models import (
    CommunityChatAccountSession,
    CommunityChatBootstrapToken,
    CommunityChatChallenge,
    CommunityChatDevice,
    CommunityChatDeviceAuthRequest,
    CommunityChatInviteAudit,
    DeviceBindingStatus,
)
from .nostr import (
    DeviceProofExpectation,
    InvalidDeviceProof,
    normalize_public_key,
    verify_device_proof,
)
from .throttles import CommunityChatScopedThrottle, enforce_bootstrap_limits
from core.auth_throttles import (
    client_ip,
    enforce_chat_email_code_request_limits,
    enforce_chat_email_code_verify_limits,
    enforce_chat_password_login_limits,
)
from core.password_auth import authenticate_account, normalize_account_email
from integrations.services.luma import (
    LumaAPIError,
    LumaAttendeeReportService,
    LumaConfigurationError,
    MELBOURNE_TIMEZONE,
)
from roo.models import RewardsCatalog, Task, TaskAssignment
from roo.services import PointsService
from .email_codes import (
    InvalidEmailCode,
    consume_email_code,
    issue_email_code_challenge,
)
from .link_previews import LinkPreviewError, fetch_link_preview, fetch_preview_image
from .slack_file_previews import (
    SlackFilePreviewError,
    fetch_slack_file_image,
    fetch_slack_file_preview,
)
from .serializers import (
    CommunityChatDeviceAuthAuthorizeSerializer,
    CommunityChatDeviceAuthExchangeSerializer,
    CommunityChatDeviceAuthStartSerializer,
    CommunityChatEmailCodeRequestSerializer,
    CommunityChatEmailCodeVerifySerializer,
    CommunityChatPasswordLoginSerializer,
    CommunityChatPublicProfileBatchSerializer,
    CommunityChatSlackDeleteSerializer,
    own_chat_profile,
    public_chat_profile,
)

BOOTSTRAP_ACTION = "community-chat:enrol-device"
AUTHENTICATION_CLASSES = (
    CommunityChatBootstrapAuthentication,
    CustomJWTAuthentication,
)
ACCOUNT_AUTHENTICATION_CLASSES = (
    CommunityChatAccountAuthentication,
    CommunityChatBootstrapAuthentication,
    CustomJWTAuthentication,
)
DESKTOP_AUTH_ORIGINS = (
    "tauri://localhost",
    "http://tauri.localhost",
)
DESKTOP_AUTHORIZATION_CODE_SALT = "community-chat.desktop-authorization.v1"
DESKTOP_AUTHORIZATION_CODE_INVALID_DETAIL = "Desktop authorization code is invalid."
PKCE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
HOME_ITEM_LIMIT = 12
UPCOMING_EVENTS_CACHE_KEY = "community-chat:upcoming-events:v1"
UPCOMING_EVENT_FIELDS = (
    "id",
    "name",
    "url",
    "start_at",
    "end_at",
    "timezone",
)


class _EmailCodeBindingConflict(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _is_eligible(user):
    return bool(user and user.is_authenticated and user.is_active)


def _require_eligible(user):
    if not _is_eligible(user):
        raise PermissionDenied("This MLAI account is not eligible for community chat.")


def _allowlisted_items(items, fields):
    return [
        {field: item.get(field) for field in fields}
        for item in items
        if isinstance(item, dict)
    ]


def _request_origin(request):
    header_origin = str(request.headers.get("Origin") or "").strip().rstrip("/")
    body_origin = str(request.data.get("origin") or "").strip().rstrip("/")
    if header_origin and body_origin and header_origin != body_origin:
        raise PermissionDenied("Request origin does not match the claimed origin.")
    origin = header_origin or body_origin
    allowed = {
        str(item).strip().rstrip("/")
        for item in settings.COMMUNITY_CHAT_ALLOWED_ORIGINS
    }
    if not origin or origin not in allowed:
        raise PermissionDenied("Request origin is not approved for community chat.")
    token_origin = getattr(request, "community_chat_origin", None)
    if (
        token_origin
        and token_origin != "legacy"
        and not secrets.compare_digest(token_origin, origin)
    ):
        raise PermissionDenied("Request origin does not match this authorization.")
    return origin


def _public_key(value):
    try:
        return normalize_public_key(value)
    except InvalidDeviceProof as exc:
        raise ValidationError({"public_key": str(exc)}) from exc


def _require_token_key(request, public_key):
    scoped_key = getattr(request, "community_chat_public_key", None)
    if scoped_key is not None and not secrets.compare_digest(scoped_key, public_key):
        raise PermissionDenied("This authorization is bound to a different device key.")


def _require_current_chat_credential_locked(request, locked_user):
    """Revalidate a Chat credential after taking the shared user row lock."""

    now = timezone.now()
    _require_eligible(locked_user)
    account_session = getattr(request, "community_chat_account_session", None)
    if account_session is not None:
        authorization = str(request.headers.get("Authorization") or "")
        if authorization.startswith("Bearer "):
            raw_access_token = authorization.removeprefix("Bearer ").strip()
        else:
            raw_access_token = str(request.COOKIES.get(ACCOUNT_ACCESS_COOKIE) or "")
        presented_hash = hashlib.sha256(raw_access_token.encode("utf-8")).hexdigest()
        current = (
            CommunityChatAccountSession.objects.select_for_update(of=("self",))
            .filter(
                pk=account_session.pk,
                user_id=locked_user.pk,
                revoked_at__isnull=True,
                access_expires_at__gt=now,
                expires_at__gt=now,
                auth_version=locked_user.auth_version,
            )
            .first()
        )
        if current is None or not secrets.compare_digest(
            current.access_token_hash,
            presented_hash,
        ):
            raise PermissionDenied("MLAI Chat session has expired.")
        request.community_chat_account_session = current

    bootstrap_token = getattr(request, "community_chat_bootstrap_token", None)
    if bootstrap_token is not None:
        authorization = str(request.headers.get("Authorization") or "")
        raw_bootstrap_token = (
            authorization.removeprefix("Bearer ").strip()
            if authorization.startswith("Bearer ")
            else ""
        )
        presented_hash = hashlib.sha256(
            raw_bootstrap_token.encode("utf-8")
        ).hexdigest()
        current = (
            CommunityChatBootstrapToken.objects.select_for_update()
            .filter(
                pk=bootstrap_token.pk,
                user_id=locked_user.pk,
                revoked_at__isnull=True,
                expires_at__gt=now,
            )
            .first()
        )
        if current is None or not secrets.compare_digest(
            current.token_hash,
            presented_hash,
        ):
            raise PermissionDenied("Community chat authorization has expired.")
        request.community_chat_bootstrap_token = current


def _password_auth_origin(request, client_id):
    origin = str(request.headers.get("Origin") or "").strip().rstrip("/")
    allowed = {
        str(item).strip().rstrip("/")
        for item in settings.COMMUNITY_CHAT_ALLOWED_ORIGINS
    }
    if origin and origin not in allowed:
        raise PermissionDenied("Request origin is not approved for community chat.")
    if client_id == "mlai-chat-web":
        if not origin or not origin.startswith(("https://", "http://")):
            raise PermissionDenied(
                "The web client requires an approved browser origin."
            )
    elif client_id == "mlai-chat-desktop":
        if not origin or origin.startswith("mlaichat://"):
            raise PermissionDenied(
                "The desktop client requires an approved application origin."
            )
    else:
        if origin and origin != "mlaichat://callback":
            raise PermissionDenied(
                "Mobile password authentication has an invalid origin."
            )
        origin = origin or "mlaichat://callback"
    return origin


def _token_enrollment_context(request):
    installation_id = getattr(request, "community_chat_installation_id", None)
    client_id = getattr(request, "community_chat_client_id", None)
    if installation_id and client_id:
        return installation_id, client_id
    try:
        installation_id = uuid.UUID(
            str(request.data.get("installation_id") or uuid.uuid4())
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError(
            {"installation_id": "Invalid installation identifier."}
        ) from exc
    return installation_id, str(request.data.get("client_id") or "legacy")[:64]


def _require_token_challenge_context(request, challenge):
    token_installation = getattr(request, "community_chat_installation_id", None)
    token_client = getattr(request, "community_chat_client_id", None)
    if token_installation and token_installation != challenge.installation_id:
        raise PermissionDenied(
            "This authorization is bound to a different installation."
        )
    if token_client and not secrets.compare_digest(token_client, challenge.client_id):
        raise PermissionDenied("This authorization is bound to a different client.")


def _pkce_challenge(verifier):
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest())
        .decode("ascii")
        .rstrip("=")
    )


def _issue_device_auth_authorization_code(auth_request):
    """Return a short-lived signed code that reveals no account credential."""

    if auth_request.user_id is None:
        raise ValueError("Cannot authorize a desktop request without a user.")
    return signing.dumps(
        {
            "request_id": str(auth_request.id),
            "user_id": str(auth_request.user_id),
            "nonce": secrets.token_urlsafe(32),
        },
        salt=DESKTOP_AUTHORIZATION_CODE_SALT,
        compress=False,
    )


def _require_valid_device_auth_authorization_code(code, auth_request):
    """Verify a purpose-bound timestamped code against the locked request."""

    max_age = int(settings.COMMUNITY_CHAT_DEVICE_AUTH_TTL_SECONDS)
    if max_age <= 0:
        raise PermissionDenied(DESKTOP_AUTHORIZATION_CODE_INVALID_DETAIL)
    try:
        payload = signing.loads(
            code,
            salt=DESKTOP_AUTHORIZATION_CODE_SALT,
            max_age=max_age,
        )
    except (signing.BadSignature, signing.SignatureExpired) as exc:
        raise PermissionDenied(DESKTOP_AUTHORIZATION_CODE_INVALID_DETAIL) from exc

    if not isinstance(payload, dict):
        raise PermissionDenied(DESKTOP_AUTHORIZATION_CODE_INVALID_DETAIL)
    request_id = payload.get("request_id")
    user_id = payload.get("user_id")
    nonce = payload.get("nonce")
    valid_payload = all(
        isinstance(value, str) for value in (request_id, user_id, nonce)
    )
    if not valid_payload or not nonce:
        raise PermissionDenied(DESKTOP_AUTHORIZATION_CODE_INVALID_DETAIL)
    if not secrets.compare_digest(request_id, str(auth_request.id)):
        raise PermissionDenied(DESKTOP_AUTHORIZATION_CODE_INVALID_DETAIL)
    if auth_request.user_id is None or not secrets.compare_digest(
        user_id,
        str(auth_request.user_id),
    ):
        raise PermissionDenied(DESKTOP_AUTHORIZATION_CODE_INVALID_DETAIL)


def _lock_device_auth_installation(enrollment):
    """Serialize credential issuance for one native installation in Postgres."""

    if connection.vendor != "postgresql":
        return
    material = (
        f"community-chat-device-auth-v1:{enrollment.client_id}:"
        f"{enrollment.installation_id}"
    ).encode("utf-8")
    lock_id = int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])


@dataclass(frozen=True)
class _DeviceAuthEnrollmentContext:
    public_key: str
    installation_id: uuid.UUID
    client_id: str
    origin: str
    platform: str
    device_name: str


def _device_auth_enrollment_context(data, origin):
    device = data["device"]
    return _DeviceAuthEnrollmentContext(
        public_key=device["public_key"],
        installation_id=device["installation_id"],
        client_id=data["client_id"],
        origin=origin,
        platform=device["platform"],
        device_name=device.get("name", ""),
    )


def _device_auth_state_hash(state, context):
    """Bind the secret state to the complete native enrollment context."""

    digest = hashlib.sha256()
    digest.update(b"mlai-chat-device-auth-v1\0")
    for value in (
        state,
        context.public_key,
        str(context.installation_id),
        context.client_id,
        context.origin,
        context.platform,
        context.device_name,
    ):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _desktop_auth_origin(request):
    origin = _request_origin(request)
    claimed_origins = (
        str(request.headers.get("Origin") or "").strip(),
        str(request.data.get("origin") or "").strip(),
    )
    if any(
        claimed and not secrets.compare_digest(claimed, origin)
        for claimed in claimed_origins
    ):
        raise PermissionDenied("Desktop sign-in requires an exact application origin.")
    if not any(
        secrets.compare_digest(origin, allowed) for allowed in DESKTOP_AUTH_ORIGINS
    ):
        raise PermissionDenied(
            "Desktop sign-in must start from the MLAI Chat application."
        )
    return origin


def _device_payload(device):
    return {
        "id": str(device.id),
        "public_key": device.public_key,
        "installation_id": str(device.installation_id),
        "client_id": device.client_id,
        "platform": device.platform,
        "name": device.name,
        "status": device.status,
        "verified_at": device.verified_at,
        "last_verified_membership_at": device.last_verified_membership_at,
        "last_seen_at": device.last_seen_at,
        "created_at": device.created_at,
    }


class PasswordAuthView(APIView):
    """Authenticate one existing MLAI account and mint one device bootstrap."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not settings.COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED:
            return Response(
                {"error": "password_auth_disabled"}, status=status.HTTP_404_NOT_FOUND
            )
        serializer = CommunityChatPasswordLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        device_data = data["device"]
        origin = _password_auth_origin(request, data["client_id"])
        email = normalize_account_email(data["email"])
        enforce_chat_password_login_limits(request, email, device_data["public_key"])
        user = authenticate_account(request._request, email, data["password"])
        if user is None:
            return Response(
                {"error": "invalid_credentials"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        public_key = device_data["public_key"]
        installation_id = device_data["installation_id"]
        now = timezone.now()
        raw_token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(48)}"
        with transaction.atomic():
            locked_user = get_user_model().objects.select_for_update().get(pk=user.pk)
            # Password verification is deliberately repeated after taking the
            # device-authority lock. A request authenticated before DELETE but
            # queued behind it must prove the password again on the new side of
            # the fence instead of reviving a deleted installation by stale
            # pre-lock authorization.
            if (
                not locked_user.is_active
                or not locked_user.has_usable_password()
                or not locked_user.check_password(data["password"])
            ):
                return Response(
                    {"error": "invalid_credentials"},
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            active_key = (
                CommunityChatDevice.objects.select_for_update()
                .filter(
                    public_key=public_key,
                    status__in=(
                        DeviceBindingStatus.PENDING,
                        DeviceBindingStatus.VERIFIED,
                    ),
                )
                .first()
            )
            if active_key and active_key.user_id != locked_user.id:
                return Response(
                    {"error": "public_key_already_bound"},
                    status=status.HTTP_409_CONFLICT,
                )
            active_installation = (
                CommunityChatDevice.objects.select_for_update()
                .filter(
                    installation_id=installation_id,
                    status__in=(
                        DeviceBindingStatus.PENDING,
                        DeviceBindingStatus.VERIFIED,
                    ),
                )
                .first()
            )
            if active_installation and (
                active_installation.user_id != locked_user.id
                or active_installation.public_key != public_key
            ):
                return Response(
                    {"error": "installation_already_bound"},
                    status=status.HTTP_409_CONFLICT,
                )
            CommunityChatBootstrapToken.objects.filter(
                user=locked_user,
                public_key=public_key,
                revoked_at__isnull=True,
            ).update(revoked_at=now)
            token = CommunityChatBootstrapToken.objects.create(
                user=locked_user,
                public_key=public_key,
                installation_id=installation_id,
                client_id=data["client_id"],
                origin=origin,
                platform=device_data["platform"],
                name=device_data.get("name", ""),
                token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
                expires_at=now
                + timedelta(
                    seconds=settings.COMMUNITY_CHAT_BOOTSTRAP_TOKEN_TTL_SECONDS
                ),
            )
            update_last_login(None, locked_user)

        return Response(
            {
                "status": "authenticated",
                "bootstrap_token": raw_token,
                "expires_at": token.expires_at,
                "relay_url": settings.COMMUNITY_CHAT_RELAY_URL,
                "origin": origin,
                "profile": own_chat_profile(locked_user),
            },
            status=status.HTTP_200_OK,
        )


def _uniform_email_code_delay(started_at):
    minimum = settings.COMMUNITY_CHAT_EMAIL_CODE_MIN_RESPONSE_SECONDS
    jitter = secrets.randbelow(21) / 1000
    remaining = minimum + jitter - (time.monotonic() - started_at)
    if remaining > 0:
        time.sleep(remaining)


def _issue_email_code_bootstrap(user, challenge):
    # Device revocation owns the same user-first boundary before invalidating
    # pending proofs and credentials. Keep issuance on that side of the fence.
    get_user_model().objects.select_for_update().get(pk=user.pk)
    _lock_device_auth_installation(challenge)
    cleanup_grant_ids = ()
    active_key = CommunityChatDevice.objects.filter(
        public_key=challenge.public_key,
        status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
    ).first()
    if active_key and active_key.user_id != user.id:
        raise _EmailCodeBindingConflict("public_key_already_bound")
    active_installation = CommunityChatDevice.objects.filter(
        installation_id=challenge.installation_id,
        status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
    ).first()
    if active_installation and active_installation.user_id != user.id:
        raise _EmailCodeBindingConflict("installation_already_bound")
    if active_installation and active_installation.public_key != challenge.public_key:
        if active_key and active_key.id != active_installation.id:
            raise _EmailCodeBindingConflict("public_key_already_bound")

        # A valid email code proves control of the same MLAI account that owns
        # this installation. Recover from a lost or regenerated browser signer
        # by retiring the old relay identity and preserving it as audit history.
        # The replacement remains pending until the normal signed challenge,
        # invite, and membership-confirmation flow proves the new key.
        from .device_revocation import revoke_device_authority

        revoked_device = revoke_device_authority(
            user,
            device_id=active_installation.pk,
            public_key=active_installation.public_key,
            reason="email_code_identity_recovery",
            revoke_member_invite_callback=revoke_member_invite,
            revoke_relay_membership_callback=revoke_relay_membership,
        )
        if revoked_device is None:
            raise _EmailCodeBindingConflict("installation_already_bound")
        cleanup_grant_ids = tuple(
            getattr(revoked_device, "registration_cleanup_grant_ids", ())
        )
        CommunityChatDevice.objects.create(
            user=user,
            public_key=challenge.public_key,
            installation_id=challenge.installation_id,
            client_id=challenge.client_id,
            platform=challenge.platform,
            name=challenge.device_name,
            status=DeviceBindingStatus.PENDING,
        )

    now = timezone.now()
    raw_token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(48)}"
    CommunityChatBootstrapToken.objects.filter(
        user=user,
        revoked_at__isnull=True,
    ).filter(
        Q(public_key=challenge.public_key)
        | Q(installation_id=challenge.installation_id)
    ).update(
        revoked_at=now
    )
    CommunityChatAccountSession.objects.filter(
        user=user,
        installation_id=challenge.installation_id,
        revoked_at__isnull=True,
    ).update(revoked_at=now)
    token = CommunityChatBootstrapToken.objects.create(
        user=user,
        public_key=challenge.public_key,
        installation_id=challenge.installation_id,
        client_id=challenge.client_id,
        origin=challenge.origin,
        platform=challenge.platform,
        name=challenge.device_name,
        token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
        expires_at=now
        + timedelta(seconds=settings.COMMUNITY_CHAT_BOOTSTRAP_TOKEN_TTL_SECONDS),
    )
    update_last_login(None, user)
    return raw_token, token, cleanup_grant_ids


def _account_session_payload(credentials):
    session = credentials.session
    payload = {
        "id": str(session.id),
        "access_expires_at": session.access_expires_at,
        "refresh_expires_at": session.expires_at,
        "installation_id": str(session.installation_id),
        "client_id": session.client_id,
    }
    if session.client_id != "mlai-chat-web":
        payload.update(
            {
                "access_token": credentials.access_token,
                "refresh_token": credentials.refresh_token,
            }
        )
    return payload


def _attach_account_session(response, credentials):
    if credentials.session.client_id == "mlai-chat-web":
        set_account_session_cookies(response, credentials)
    return response


class EmailCodeRequestView(APIView):
    """Create a uniform, installation-bound MLAI Chat email proof."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not settings.COMMUNITY_CHAT_EMAIL_CODE_AUTH_ENABLED:
            return Response(
                {"error": "email_code_auth_disabled"},
                status=status.HTTP_404_NOT_FOUND,
            )
        started_at = time.monotonic()
        serializer = CommunityChatEmailCodeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        device = data["device"]
        email = normalize_account_email(data["email"])
        origin = _password_auth_origin(request, data["client_id"])
        enforce_chat_email_code_request_limits(
            request,
            email,
            device["installation_id"],
        )
        ip_digest = salted_hmac(
            "community-chat-email-code-ip",
            client_ip(request),
            algorithm="sha256",
        ).hexdigest()
        challenge = issue_email_code_challenge(
            email=email,
            client_id=data["client_id"],
            installation_id=device["installation_id"],
            origin=origin,
            platform=device["platform"],
            device_name=device.get("name", ""),
            public_key=device["public_key"],
            requested_ip_digest=ip_digest,
        )
        _uniform_email_code_delay(started_at)
        resend_available_at = challenge.created_at + timedelta(
            seconds=settings.COMMUNITY_CHAT_EMAIL_CODE_RESEND_SECONDS
        )
        return Response(
            {
                "status": "accepted",
                "challenge_id": str(challenge.id),
                "expires_at": challenge.expires_at,
                "resend_available_at": resend_available_at,
                "message": "If this email is eligible, MLAI has sent a six-digit sign-in code.",
                # Relative values remain for the compatibility window while
                # released clients move to the absolute timestamps above.
                "expires_in": settings.COMMUNITY_CHAT_EMAIL_CODE_TTL_SECONDS,
                "resend_after": settings.COMMUNITY_CHAT_EMAIL_CODE_RESEND_SECONDS,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class EmailCodeVerifyView(APIView):
    """Consume a one-use email proof and mint one device bootstrap."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not settings.COMMUNITY_CHAT_EMAIL_CODE_AUTH_ENABLED:
            return Response(
                {"error": "email_code_auth_disabled"},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = CommunityChatEmailCodeVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        enforce_chat_email_code_verify_limits(
            request,
            data["challenge_id"],
            data["installation_id"],
        )
        invalid_email_code = False
        try:
            # Keep code consumption, device recovery, bootstrap creation, and
            # session replacement in one database transaction. Any binding or
            # adapter failure leaves the one-use code available for a retry.
            with transaction.atomic():
                try:
                    user, challenge = consume_email_code(
                        challenge_id=data["challenge_id"],
                        code=data["code"],
                        client_id=data["client_id"],
                        installation_id=data["installation_id"],
                    )
                except InvalidEmailCode:
                    # Exit this transaction normally so failed-attempt counters
                    # and terminal invalidation remain durable.
                    invalid_email_code = True
                else:
                    raw_token, token, _ = _issue_email_code_bootstrap(user, challenge)
                    account_session = issue_account_session(user, challenge)
        except _EmailCodeBindingConflict as exc:
            return Response(
                {"error": exc.code},
                status=status.HTTP_409_CONFLICT,
            )
        except MembershipAdapterConflict as exc:
            return Response({"error": exc.code}, status=status.HTTP_409_CONFLICT)
        except MembershipAdapterUnavailable:
            return Response(
                {"error": "membership_service_unavailable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if invalid_email_code:
            return Response(
                {"error": "invalid_or_expired_code"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Device rotation has already fenced every old Slack registration in
        # the committed transaction above. Adapter DELETEs are content-free,
        # durable work drained by the community-bridge maintenance worker.
        # Never hold the login response open while a large DM archive drains.
        response = Response(
            {
                "status": "authenticated",
                "bootstrap_token": raw_token,
                "expires_at": token.expires_at,
                "relay_url": settings.COMMUNITY_CHAT_RELAY_URL,
                "origin": challenge.origin,
                "profile": own_chat_profile(user),
                "session": _account_session_payload(account_session),
            },
            status=status.HTTP_200_OK,
        )
        return _attach_account_session(response, account_session)


class AccountSessionRefreshView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        explicit_token = str(request.data.get("refresh_token") or "").strip()
        raw_token = (
            explicit_token
            or str(request.COOKIES.get(ACCOUNT_REFRESH_COOKIE) or "").strip()
        )
        required_origin = None
        if not explicit_token:
            required_origin = (
                str(request.headers.get("Origin") or "").strip().rstrip("/")
            )
            if not required_origin:
                return Response(
                    {"error": "invalid_session"},
                    status=status.HTTP_401_UNAUTHORIZED,
                )
        try:
            credentials = rotate_account_session(
                raw_token,
                required_origin=required_origin,
            )
        except InvalidAccountSession:
            response = Response(
                {"error": "invalid_session"},
                status=status.HTTP_401_UNAUTHORIZED,
            )
            return clear_account_session_cookies(response)
        response = Response(
            {
                "status": "refreshed",
                "session": _account_session_payload(credentials),
            },
            status=status.HTTP_200_OK,
        )
        return _attach_account_session(response, credentials)


class AccountSessionLogoutView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        explicit_token = str(request.data.get("refresh_token") or "").strip()
        raw_token = (
            explicit_token
            or str(request.COOKIES.get(ACCOUNT_REFRESH_COOKIE) or "").strip()
        )
        required_origin = None
        if not explicit_token:
            required_origin = (
                str(request.headers.get("Origin") or "").strip().rstrip("/")
            )
        try:
            revoke_account_session(raw_token, required_origin=required_origin)
        except InvalidAccountSession:
            response = Response(
                {"error": "invalid_session"},
                status=status.HTTP_401_UNAUTHORIZED,
            )
            return clear_account_session_cookies(response)
        response = Response({"status": "signed_out"}, status=status.HTTP_200_OK)
        return clear_account_session_cookies(response)


class AccountView(APIView):
    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = [IsAuthenticated]

    def get(self, request):
        account_session = request.community_chat_account_session
        devices = CommunityChatDevice.objects.filter(
            user=request.user,
            status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
        )
        return Response(
            {
                "authenticated": True,
                "profile": own_chat_profile(request.user),
                "public_profile": public_chat_profile(request.user),
                "session": {
                    "id": str(account_session.id),
                    "installation_id": str(account_session.installation_id),
                    "client_id": account_session.client_id,
                    "platform": account_session.platform,
                    "name": account_session.name,
                    "access_expires_at": account_session.access_expires_at,
                    "refresh_expires_at": account_session.expires_at,
                },
                "devices": [_device_payload(device) for device in devices],
            }
        )


class HomeView(APIView):
    """Return member-scoped Roo dashboard data for MLAI Chat Home.

    Only the caller's aggregate balance and public/volunteer catalog fields
    cross this boundary. Slack ids, assignment/reviewer details, internal
    tasks, redemption records, and other members' balances are excluded.
    """

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_home"

    def get(self, request):
        balance = PointsService.get_balance(request.user)
        today = timezone.localdate(timezone=ZoneInfo(MELBOURNE_TIMEZONE))
        opportunities = (
            Task.objects.filter(
                status="open",
                volunteer_ready=True,
                visibility__in=("volunteer", "public"),
            )
            .filter(Q(due_date__isnull=True) | Q(due_date__gte=today))
            .exclude(assignments__status__in=TaskAssignment.ACTIVE_STATUSES)
            .distinct()
            .order_by("due_date", "id")[:HOME_ITEM_LIMIT]
        )
        rewards = (
            RewardsCatalog.objects.filter(is_active=True)
            .filter(Q(stock_remaining__isnull=True) | Q(stock_remaining__gt=0))
            .order_by("cost_points", "name")[:HOME_ITEM_LIMIT]
        )

        earn_actions = [
            {
                "id": "intro",
                "name": "Introduce yourself",
                "description": "Post your first message in #_start-here.",
                "points": 4,
            }
        ]
        monthly_update_points = int(
            getattr(settings, "ROO_POINTS_MONTHLY_UPDATE_REWARD", 0)
        )
        if monthly_update_points > 0:
            earn_actions.append(
                {
                    "id": "monthly_update",
                    "name": "Complete your monthly startup update",
                    "description": (
                        "Complete and save a ready monthly update for your "
                        "verified company."
                    ),
                    "points": monthly_update_points,
                }
            )
        earn_actions.extend(
            {
                "id": f"task:{task.task_code}",
                "name": task.title,
                "description": task.description,
                "points": task.points_estimate or task.points,
                "command": f"@Roo task claim {task.task_code}",
            }
            for task in opportunities
            if task.task_code
        )

        response = Response(
            {
                "points": {
                    "balance": balance["balance"],
                    "earned_balance": balance["earned_balance"],
                    "purchased_topup_balance": balance["purchased_topup_balance"],
                    "lifetime_earned": balance["lifetime_earned"],
                    "lifetime_spent": balance["lifetime_spent"],
                },
                "earn_actions": earn_actions,
                "rewards": [
                    {
                        "code": reward.code,
                        "name": reward.name,
                        "description": reward.description,
                        "cost_points": reward.cost_points,
                        "stock_remaining": reward.stock_remaining,
                        "can_afford": balance["balance"] >= reward.cost_points,
                    }
                    for reward in rewards
                ],
                "feature_flags": {
                    "link_love": False,
                    "meeting_rooms": bool(
                        getattr(settings, "MEETING_ROOM_BOOKING_ENABLED", False)
                    ),
                },
            }
        )
        response["Cache-Control"] = "private, no-store"
        return response


class UpcomingEventsView(APIView):
    """Return cached, public Luma event cards to signed-in chat members."""

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_upcoming_events"

    def get(self, request):
        raw_limit = request.query_params.get("limit") or 5
        try:
            requested_limit = int(raw_limit)
        except (TypeError, ValueError):
            return Response(
                {"error": "invalid_limit"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if requested_limit < 1:
            return Response(
                {"error": "invalid_limit"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        requested_limit = min(requested_limit, 10)

        events = cache.get(UPCOMING_EVENTS_CACHE_KEY)
        if not isinstance(events, list):
            try:
                events = LumaAttendeeReportService(
                    timeout=settings.LUMA_API_TIMEOUT_SECONDS,
                ).list_upcoming_events(limit=10)
            except LumaConfigurationError:
                return Response(
                    {"error": "upcoming_events_unavailable"},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            except LumaAPIError as exc:
                response_status = (
                    status.HTTP_429_TOO_MANY_REQUESTS
                    if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS
                    else status.HTTP_502_BAD_GATEWAY
                )
                return Response(
                    {"error": "upcoming_events_unavailable"},
                    status=response_status,
                )
            events = _allowlisted_items(events, UPCOMING_EVENT_FIELDS)
            cache.set(
                UPCOMING_EVENTS_CACHE_KEY,
                events,
                timeout=settings.LUMA_UPCOMING_EVENTS_CACHE_SECONDS,
            )
        else:
            events = _allowlisted_items(events, UPCOMING_EVENT_FIELDS)

        response = Response(
            {
                "calendar_url": settings.LUMA_CALENDAR_URL,
                "events": events[:requested_limit],
            }
        )
        response["Cache-Control"] = "private, max-age=60"
        return response


class PublicProfileBatchView(APIView):
    """Resolve current and historical verified chat keys to public profiles.

    Revoking a device removes its access, but it must not erase attribution on
    messages and membership events that the key signed while it was verified.
    Pending or otherwise never-verified keys remain private and unresolved.
    """

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_session"

    def post(self, request):
        serializer = CommunityChatPublicProfileBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        public_keys = serializer.validated_data["public_keys"]

        devices = (
            CommunityChatDevice.objects.select_related("user")
            .filter(
                public_key__in=public_keys,
                status__in=(
                    DeviceBindingStatus.VERIFIED,
                    DeviceBindingStatus.REVOKED,
                ),
                verified_at__isnull=False,
                user__is_active=True,
            )
            .order_by("public_key", "-verified_at", "-created_at")
        )
        devices_by_key = {}
        for device in devices:
            devices_by_key.setdefault(device.public_key, device)
        profiles = {
            public_key: public_chat_profile(devices_by_key[public_key].user)
            for public_key in public_keys
            if public_key in devices_by_key
        }
        missing = [
            public_key for public_key in public_keys if public_key not in profiles
        ]
        return Response({"profiles": profiles, "missing": missing})


class LinkPreviewView(APIView):
    """Return bounded Open Graph metadata through the authenticated API."""

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_link_preview"

    def get(self, request):
        raw_url = str(request.query_params.get("url") or "").strip()
        try:
            slack_preview = fetch_slack_file_preview(raw_url)
            preview = slack_preview or fetch_link_preview(raw_url)
        except (LinkPreviewError, SlackFilePreviewError) as exc:
            return Response(
                {"error": "preview_unavailable", "detail": str(exc)},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        payload = preview.as_payload()
        if slack_preview and slack_preview.is_image:
            image_path = reverse("community_chat_link_preview_image")
            payload["image_url"] = request.build_absolute_uri(
                f"{image_path}?{urlencode({'slack_file': slack_preview.file_id})}"
            )
        elif preview.image_url:
            image_path = reverse("community_chat_link_preview_image")
            payload["image_url"] = request.build_absolute_uri(
                f"{image_path}?{urlencode({'url': preview.image_url})}"
            )
        response = Response(payload)
        response["Cache-Control"] = "private, max-age=3600"
        return response


class LinkPreviewImageView(APIView):
    """Proxy one validated preview image so MLAI Chat keeps a narrow CSP."""

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_link_preview"

    def get(self, request):
        try:
            slack_file_id = str(request.query_params.get("slack_file") or "").strip()
            if slack_file_id:
                content_type, body = fetch_slack_file_image(slack_file_id)
            else:
                content_type, body = fetch_preview_image(
                    str(request.query_params.get("url") or "").strip()
                )
        except (LinkPreviewError, SlackFilePreviewError) as exc:
            return Response(
                {"error": "preview_image_unavailable", "detail": str(exc)},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        response = HttpResponse(body, content_type=content_type)
        response["Cache-Control"] = "private, max-age=21600"
        response["Cross-Origin-Resource-Policy"] = "same-site"
        return response


class SlackOriginMessageDeleteView(APIView):
    """Request deletion of the caller's Slack-authored mirrored message."""

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_slack_delete"

    def post(self, request):
        from integrations.services.community_bridge.deletion import (
            SlackDeletionError,
            delete_slack_origin_message,
        )
        from integrations.services.external_connectors import (
            ConnectorConfigurationError,
            build_community_chat_slack_authorization_url,
        )

        serializer = CommunityChatSlackDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = delete_slack_origin_message(
                user=request.user,
                device_public_key=request.community_chat_public_key,
                buzz_event_id=serializer.validated_data["buzz_event_id"],
                idempotency_key=serializer.validated_data["idempotency_key"],
            )
        except SlackDeletionError as exc:
            body = {"error": exc.code, "detail": exc.detail}
            if exc.code == "slack_reauthorization_required":
                try:
                    body["connect_url"] = build_community_chat_slack_authorization_url(
                        request.user
                    )
                except ConnectorConfigurationError:
                    body["error"] = "slack_oauth_unavailable"
                    body["detail"] = "Slack reconnection is temporarily unavailable."
            return Response(body, status=exc.http_status)

        response_status = (
            status.HTTP_202_ACCEPTED
            if result.status in {"processing", "succeeded"}
            else status.HTTP_200_OK
        )
        return Response(
            {
                "request_id": result.request_id,
                "status": result.status,
            },
            status=response_status,
        )


class DeviceAuthStartView(APIView):
    """Create a desktop-only origin/device/state/PKCE-bound login request."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not settings.COMMUNITY_CHAT_DEVICE_AUTH_ENABLED:
            return Response(
                {"error": "device_auth_disabled"},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = CommunityChatDeviceAuthStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        origin = _desktop_auth_origin(request)
        enrollment = _device_auth_enrollment_context(data, origin)
        enforce_bootstrap_limits(
            request,
            action="auth-start",
            public_key=enrollment.public_key,
            user_limit=20,
            key_limit=10,
            ip_limit=30,
        )
        auth_request = CommunityChatDeviceAuthRequest.objects.create(
            public_key=enrollment.public_key,
            origin=origin,
            state_hash=_device_auth_state_hash(data["state"], enrollment),
            code_challenge=data["code_challenge"],
            expires_at=timezone.now()
            + timedelta(seconds=settings.COMMUNITY_CHAT_DEVICE_AUTH_TTL_SECONDS),
        )
        callback_path = f"/auth/desktop?request={auth_request.id}"
        response = Response(
            {
                "request_id": str(auth_request.id),
                "callback_path": callback_path,
                "expires_at": auth_request.expires_at,
            },
            status=status.HTTP_201_CREATED,
        )
        response["Cache-Control"] = "no-store"
        response["Pragma"] = "no-cache"
        return response


class DeviceAuthAuthorizeView(APIView):
    """Explicitly authorize a pending app handoff with a Chat browser session."""

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not settings.COMMUNITY_CHAT_DEVICE_AUTH_ENABLED:
            return Response(
                {"error": "device_auth_disabled"},
                status=status.HTTP_404_NOT_FOUND,
            )
        _require_eligible(request.user)
        cookie_access = str(request.COOKIES.get(ACCOUNT_ACCESS_COOKIE) or "")
        cookie_hash = hashlib.sha256(cookie_access.encode("utf-8")).hexdigest()
        if not cookie_access or not secrets.compare_digest(
            cookie_hash,
            request.community_chat_account_session.access_token_hash,
        ):
            raise PermissionDenied("Device approval requires a browser sign-in cookie.")
        if request.community_chat_account_session.client_id != "mlai-chat-web":
            raise PermissionDenied(
                "Device approval requires an MLAI Chat browser session."
            )
        origin = _request_origin(request)
        browser_origin = str(settings.COMMUNITY_CHAT_FRONTEND_URL).strip().rstrip("/")
        if not secrets.compare_digest(origin, browser_origin):
            raise PermissionDenied(
                "Device approval must come from the MLAI Chat browser origin."
            )
        serializer = CommunityChatDeviceAuthAuthorizeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request_id = serializer.validated_data["request_id"]
        try:
            with transaction.atomic():
                locked_user = get_user_model().objects.select_for_update().get(
                    pk=request.user.pk
                )
                _require_current_chat_credential_locked(request, locked_user)
                current_session = request.community_chat_account_session
                if not secrets.compare_digest(
                    cookie_hash,
                    current_session.access_token_hash,
                ):
                    raise PermissionDenied(
                        "Device approval requires a current browser sign-in cookie."
                    )
                if current_session.client_id != "mlai-chat-web":
                    raise PermissionDenied(
                        "Device approval requires an MLAI Chat browser session."
                    )
                auth_request = (
                    CommunityChatDeviceAuthRequest.objects.select_for_update().get(
                        id=request_id
                    )
                )
                now = timezone.now()
                if auth_request.expires_at <= now:
                    return Response(
                        {"error": "authorization_expired"},
                        status=status.HTTP_410_GONE,
                    )
                if auth_request.consumed_at is not None:
                    return Response(
                        {"error": "authorization_consumed"},
                        status=status.HTTP_409_CONFLICT,
                    )
                if (
                    auth_request.user_id is not None
                    and auth_request.user_id != locked_user.id
                ):
                    raise PermissionDenied(
                        "Login request belongs to another MLAI account."
                    )
                auth_request.user = locked_user
                auth_request.authorized_at = now
                auth_request.save(update_fields=("user", "authorized_at"))
                authorization_code = _issue_device_auth_authorization_code(auth_request)
        except CommunityChatDeviceAuthRequest.DoesNotExist:
            return Response(
                {"error": "authorization_not_found"}, status=status.HTTP_404_NOT_FOUND
            )
        response = Response(
            {
                "status": "authorized",
                "request_id": str(auth_request.id),
                "authorization_code": authorization_code,
            }
        )
        response["Cache-Control"] = "no-store"
        response["Pragma"] = "no-cache"
        return response


class DeviceAuthExchangeView(APIView):
    """Exchange desktop state + PKCE for bootstrap and native account sessions."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not settings.COMMUNITY_CHAT_DEVICE_AUTH_ENABLED:
            return Response(
                {"error": "device_auth_disabled"},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = CommunityChatDeviceAuthExchangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        request_id = data["request_id"]
        origin = _desktop_auth_origin(request)
        enrollment = _device_auth_enrollment_context(data, origin)
        auth_request_identity = (
            CommunityChatDeviceAuthRequest.objects.filter(id=request_id)
            .values("user_id")
            .first()
        )
        if auth_request_identity is None:
            return Response(
                {"error": "authorization_not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        authorized_user_id = auth_request_identity["user_id"]
        if authorized_user_id is None:
            raise PermissionDenied(DESKTOP_AUTHORIZATION_CODE_INVALID_DETAIL)
        try:
            with transaction.atomic():
                # Device DELETE locks user before invalidating pending handoff
                # rows. Mirror that order so an exchange authorized before the
                # delete either commits credentials that DELETE then revokes,
                # or observes the expired handoff after DELETE wins.
                locked_user = get_user_model().objects.select_for_update().get(
                    pk=authorized_user_id
                )
                auth_request = (
                    CommunityChatDeviceAuthRequest.objects.select_for_update().get(
                        id=request_id
                    )
                )
                if auth_request.user_id != locked_user.pk:
                    raise PermissionDenied(DESKTOP_AUTHORIZATION_CODE_INVALID_DETAIL)
                now = timezone.now()
                enforce_bootstrap_limits(
                    request,
                    action="auth-exchange",
                    public_key=auth_request.public_key,
                    user_limit=40,
                    key_limit=30,
                    ip_limit=60,
                )
                if auth_request.origin != origin:
                    raise PermissionDenied("Login request origin does not match.")
                if not secrets.compare_digest(
                    auth_request.public_key,
                    enrollment.public_key,
                ):
                    raise PermissionDenied(
                        "Login request belongs to a different device key."
                    )
                _require_valid_device_auth_authorization_code(
                    data["authorization_code"],
                    auth_request,
                )
                if auth_request.expires_at <= now:
                    return Response(
                        {"error": "authorization_expired"},
                        status=status.HTTP_410_GONE,
                    )
                if auth_request.consumed_at is not None:
                    return Response(
                        {"error": "authorization_consumed"},
                        status=status.HTTP_409_CONFLICT,
                    )
                state_hash = _device_auth_state_hash(data["state"], enrollment)
                verifier_challenge = _pkce_challenge(data["code_verifier"])
                valid_state = secrets.compare_digest(
                    auth_request.state_hash, state_hash
                )
                valid_verifier = secrets.compare_digest(
                    auth_request.code_challenge,
                    verifier_challenge,
                )
                if not valid_state or not valid_verifier:
                    raise PermissionDenied("Login state or verifier is invalid.")
                if auth_request.authorized_at is None:
                    raise PermissionDenied(DESKTOP_AUTHORIZATION_CODE_INVALID_DETAIL)
                _require_eligible(locked_user)

                raw_token, token, _ = _issue_email_code_bootstrap(
                    locked_user,
                    enrollment,
                )
                account_session = issue_account_session(
                    locked_user,
                    enrollment,
                )
                auth_request.consumed_at = now
                auth_request.save(update_fields=("consumed_at",))
        except _EmailCodeBindingConflict as exc:
            return Response(
                {"error": exc.code},
                status=status.HTTP_409_CONFLICT,
            )
        except MembershipAdapterConflict as exc:
            return Response({"error": exc.code}, status=status.HTTP_409_CONFLICT)
        except MembershipAdapterUnavailable:
            return Response(
                {"error": "membership_service_unavailable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except CommunityChatDeviceAuthRequest.DoesNotExist:
            return Response(
                {"error": "authorization_not_found"}, status=status.HTTP_404_NOT_FOUND
            )

        # See EmailCodeVerifyView: authentication succeeds once the privacy
        # fence is durable; the periodic worker owns adapter cleanup.
        response = Response(
            {
                "status": "authenticated",
                "bootstrap_token": raw_token,
                "expires_at": token.expires_at,
                "relay_url": settings.COMMUNITY_CHAT_RELAY_URL,
                "origin": enrollment.origin,
                "profile": own_chat_profile(auth_request.user),
                "session": _account_session_payload(account_session),
            }
        )
        response["Cache-Control"] = "no-store"
        response["Pragma"] = "no-cache"
        return response


class SessionView(APIView):
    authentication_classes = ACCOUNT_AUTHENTICATION_CLASSES
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_session"

    def get(self, request):
        devices = CommunityChatDevice.objects.filter(
            user=request.user,
            status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
        )
        scoped_key = getattr(request, "community_chat_public_key", None)
        if scoped_key:
            devices = devices.filter(public_key=scoped_key)
        return Response(
            {
                "authenticated": True,
                "eligible": _is_eligible(request.user),
                "relay_url": settings.COMMUNITY_CHAT_RELAY_URL,
                "devices": [_device_payload(device) for device in devices],
                "profile": own_chat_profile(request.user),
                "public_profile": public_chat_profile(request.user),
            }
        )


class ChallengeView(APIView):
    # Browser reloads restore the durable, device-bound account session after
    # the one-use bootstrap token has been consumed. Accept that session for
    # re-enrollment while `_require_token_key` and the origin checks below keep
    # every request bound to the same key, installation, client, and origin.
    authentication_classes = ACCOUNT_AUTHENTICATION_CLASSES
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_challenge"

    def post(self, request):
        _require_eligible(request.user)
        public_key = _public_key(request.data.get("public_key"))
        _require_token_key(request, public_key)
        origin = _request_origin(request)
        installation_id, client_id = _token_enrollment_context(request)
        enforce_bootstrap_limits(
            request,
            action="challenge",
            public_key=public_key,
            user_limit=20,
            key_limit=10,
            ip_limit=30,
        )

        nonce = secrets.token_urlsafe(32)
        expires_at = timezone.now() + timedelta(
            seconds=settings.COMMUNITY_CHAT_CHALLENGE_TTL_SECONDS
        )
        with transaction.atomic():
            locked_user = get_user_model().objects.select_for_update().get(
                pk=request.user.pk
            )
            _require_current_chat_credential_locked(request, locked_user)
            active = (
                CommunityChatDevice.objects.select_for_update()
                .filter(
                    public_key=public_key,
                    status__in=(
                        DeviceBindingStatus.PENDING,
                        DeviceBindingStatus.VERIFIED,
                    ),
                )
                .first()
            )
            if active and active.user_id != locked_user.id:
                return Response(
                    {"error": "public_key_already_bound"},
                    status=status.HTTP_409_CONFLICT,
                )
            active_installation = (
                CommunityChatDevice.objects.select_for_update()
                .filter(
                    installation_id=installation_id,
                    status__in=(
                        DeviceBindingStatus.PENDING,
                        DeviceBindingStatus.VERIFIED,
                    ),
                )
                .first()
            )
            if (
                active_installation
                and active_installation.public_key != public_key
            ):
                return Response(
                    {"error": "installation_already_bound"},
                    status=status.HTTP_409_CONFLICT,
                )

            challenge = CommunityChatChallenge.objects.create(
                user=locked_user,
                public_key=public_key,
                installation_id=installation_id,
                client_id=client_id,
                action=BOOTSTRAP_ACTION,
                audience=settings.COMMUNITY_CHAT_API_AUDIENCE,
                origin=origin,
                nonce_hash=hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
                expires_at=expires_at,
            )
        expectation = DeviceProofExpectation(
            challenge_id=str(challenge.id),
            public_key=public_key,
            nonce=nonce,
            action=challenge.action,
            audience=challenge.audience,
            origin=challenge.origin,
        )
        return Response(
            {
                "challenge_id": str(challenge.id),
                "nonce": nonce,
                "expires_at": expires_at,
                "audience": challenge.audience,
                "origin": challenge.origin,
                "action": challenge.action,
                "unsigned_event": expectation.unsigned_event(),
            },
            status=status.HTTP_201_CREATED,
        )


class InviteView(APIView):
    authentication_classes = ACCOUNT_AUTHENTICATION_CLASSES
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_invite"

    def post(self, request):
        _require_eligible(request.user)
        origin = _request_origin(request)
        challenge_id = request.data.get("challenge_id")
        nonce = str(request.data.get("nonce") or "")
        event = request.data.get("event")
        if not challenge_id or not nonce or not isinstance(event, dict):
            raise ValidationError("challenge_id, nonce, and event are required.")

        now = timezone.now()
        try:
            with transaction.atomic():
                # Device revocation uses this same user-first boundary before
                # resolving the current binding. Serialize same-key
                # re-enrollment so a completed revoke cannot be followed by a
                # stale device INSERT.
                locked_user = get_user_model().objects.select_for_update().get(
                    pk=request.user.pk
                )
                _require_current_chat_credential_locked(request, locked_user)
                challenge = CommunityChatChallenge.objects.select_for_update().get(
                    id=challenge_id, user_id=locked_user.pk
                )
                _require_token_challenge_context(request, challenge)
                public_key = challenge.public_key
                _require_token_key(request, public_key)
                enforce_bootstrap_limits(
                    request,
                    action="invite",
                    public_key=public_key,
                    user_limit=10,
                    key_limit=5,
                    ip_limit=20,
                )
                if challenge.used_at is not None:
                    return Response(
                        {"error": "challenge_replayed"}, status=status.HTTP_409_CONFLICT
                    )
                if challenge.expires_at <= now:
                    return Response(
                        {"error": "challenge_expired"}, status=status.HTTP_410_GONE
                    )
                if (
                    challenge.origin != origin
                    or challenge.audience != settings.COMMUNITY_CHAT_API_AUDIENCE
                ):
                    raise PermissionDenied(
                        "Challenge context does not match this request."
                    )
                nonce_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
                if not secrets.compare_digest(challenge.nonce_hash, nonce_hash):
                    raise PermissionDenied("Challenge nonce is invalid.")

                expectation = DeviceProofExpectation(
                    challenge_id=str(challenge.id),
                    public_key=public_key,
                    nonce=nonce,
                    action=challenge.action,
                    audience=challenge.audience,
                    origin=challenge.origin,
                )
                try:
                    signed_at = verify_device_proof(event, expectation)
                except InvalidDeviceProof as exc:
                    raise PermissionDenied("Device signature is invalid.") from exc
                signed_time = datetime.fromtimestamp(
                    signed_at, tz=datetime_timezone.utc
                )
                if (
                    abs((now - signed_time).total_seconds())
                    > settings.COMMUNITY_CHAT_CHALLENGE_TTL_SECONDS
                ):
                    raise PermissionDenied(
                        "Device signature is outside the allowed time window."
                    )

                existing = (
                    CommunityChatDevice.objects.select_for_update()
                    .filter(
                        public_key=public_key,
                        status__in=(
                            DeviceBindingStatus.PENDING,
                            DeviceBindingStatus.VERIFIED,
                        ),
                    )
                    .first()
                )
                if existing and existing.user_id != locked_user.id:
                    return Response(
                        {"error": "public_key_already_bound"},
                        status=status.HTTP_409_CONFLICT,
                    )
                if existing and existing.status == DeviceBindingStatus.VERIFIED:
                    challenge.used_at = now
                    challenge.save(update_fields=("used_at",))
                    return Response(
                        {
                            "status": "already_member",
                            "device": _device_payload(existing),
                        }
                    )
                device = existing or CommunityChatDevice.objects.create(
                    user=locked_user,
                    public_key=public_key,
                    installation_id=challenge.installation_id,
                    client_id=challenge.client_id,
                    platform=getattr(request, "community_chat_platform", ""),
                    name=getattr(request, "community_chat_device_name", ""),
                )
                challenge.used_at = now
                challenge.save(update_fields=("used_at",))
                try:
                    # Keep the user/device authority lock through remote mint
                    # and durable audit. Device DELETE takes the same boundary,
                    # then cancels every unconfirmed audit before member DELETE.
                    invite = issue_member_invite(public_key)
                except MembershipAdapterConflict as exc:
                    if exc.code == "already_member":
                        return Response(
                            {
                                "status": "already_member",
                                "device": _device_payload(device),
                            }
                        )
                    return Response(
                        {"error": exc.code},
                        status=status.HTTP_409_CONFLICT,
                    )
                except MembershipAdapterUnavailable:
                    return Response(
                        {"error": "membership_service_unavailable"},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE,
                    )

                CommunityChatInviteAudit.objects.create(
                    device=device,
                    challenge=challenge,
                    adapter_invite_id=invite.invite_id,
                    adapter_request_id=invite.request_id,
                    expires_at=invite.expires_at,
                )
        except CommunityChatChallenge.DoesNotExist:
            return Response(
                {"error": "challenge_not_found"}, status=status.HTTP_404_NOT_FOUND
            )
        except (ValueError, OverflowError, OSError):
            raise PermissionDenied("Device signature timestamp is invalid.")
        except IntegrityError:
            return Response(
                {"error": "public_key_already_bound"}, status=status.HTTP_409_CONFLICT
            )

        return Response(
            {
                "status": "invite_issued",
                "invite_code": invite.code,
                "expires_at": invite.expires_at,
                "relay_url": settings.COMMUNITY_CHAT_RELAY_URL,
                "device": _device_payload(device),
            }
        )


class ConfirmView(APIView):
    authentication_classes = ACCOUNT_AUTHENTICATION_CLASSES
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_confirm"

    def post(self, request):
        _require_eligible(request.user)
        public_key = _public_key(request.data.get("public_key"))
        _require_token_key(request, public_key)
        _request_origin(request)
        enforce_bootstrap_limits(
            request,
            action="confirm",
            public_key=public_key,
            user_limit=30,
            key_limit=20,
            ip_limit=40,
        )
        try:
            with transaction.atomic():
                # Device DELETE takes the same user-first boundary. Keep the
                # exact row locked through the membership observation and
                # VERIFIED write so a stale GET cannot resurrect a completed
                # revocation.
                locked_user = get_user_model().objects.select_for_update().get(
                    pk=request.user.pk
                )
                _require_current_chat_credential_locked(request, locked_user)
                device = (
                    CommunityChatDevice.objects.select_for_update()
                    .filter(
                        user_id=locked_user.pk,
                        public_key=public_key,
                        status__in=(
                            DeviceBindingStatus.PENDING,
                            DeviceBindingStatus.VERIFIED,
                        ),
                        revoked_at__isnull=True,
                    )
                    .order_by("-id")
                    .first()
                )
                if not device:
                    return Response(
                        {"error": "device_not_found"},
                        status=status.HTTP_404_NOT_FOUND,
                    )

                membership = get_relay_membership(public_key)
                if not membership.is_member:
                    return Response(
                        {"error": "membership_not_found"},
                        status=status.HTTP_409_CONFLICT,
                    )
                if membership.role != "member":
                    return Response(
                        {"error": "unexpected_relay_role"},
                        status=status.HTTP_409_CONFLICT,
                    )

                now = timezone.now()
                device.status = DeviceBindingStatus.VERIFIED
                device.verified_at = device.verified_at or now
                device.last_verified_membership_at = now
                device.last_seen_at = now
                device.save(
                    update_fields=(
                        "status",
                        "verified_at",
                        "last_verified_membership_at",
                        "last_seen_at",
                        "updated_at",
                    )
                )
                CommunityChatInviteAudit.objects.filter(
                    device=device,
                    confirmed_at__isnull=True,
                ).update(confirmed_at=now)
                bootstrap_token = getattr(
                    request, "community_chat_bootstrap_token", None
                )
                if bootstrap_token is not None and bootstrap_token.revoked_at is None:
                    bootstrap_token.revoked_at = now
                    bootstrap_token.save(update_fields=("revoked_at",))
        except MembershipAdapterUnavailable:
            return Response(
                {"error": "membership_service_unavailable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            {
                "status": "verified",
                "device": _device_payload(device),
                "public_profile": public_chat_profile(request.user),
            }
        )


class DeviceView(APIView):
    authentication_classes = ACCOUNT_AUTHENTICATION_CLASSES
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_revoke"

    def delete(self, request, public_key):
        _require_eligible(request.user)
        _request_origin(request)
        public_key = _public_key(public_key)
        _require_token_key(request, public_key)
        enforce_bootstrap_limits(
            request,
            action="revoke",
            public_key=public_key,
            user_limit=10,
            key_limit=10,
            ip_limit=20,
        )
        reason = str(request.data.get("reason") or "user_requested").strip()[:500]
        try:
            with transaction.atomic():
                from .device_revocation import (
                    revoke_device_authority,
                    revoke_device_credentials_locked,
                )

                locked_user = get_user_model().objects.select_for_update().get(
                    pk=request.user.pk
                )
                # Authentication occurs before the transaction. Revalidate the
                # exact Chat credential after taking the authority lock so a
                # duplicate stale DELETE cannot wait behind an earlier delete
                # and later revoke a freshly reauthorized same-key binding.
                _require_current_chat_credential_locked(request, locked_user)
                # The shared boundary locks user->grant->conversation->device
                # before resolving the current binding and revoking relay
                # membership. This prevents a stale pre-lock row id from
                # skipping a same-key re-enrollment.
                device = revoke_device_authority(
                    locked_user,
                    # Resolve the current binding only after the shared user
                    # lock. An Invite POST may still be minting the first
                    # binding, and a key may have been re-enrolled after an old
                    # row was revoked.
                    device_id=None,
                    public_key=public_key,
                    reason=reason,
                    allow_already_revoked=True,
                    revoke_member_invite_callback=revoke_member_invite,
                    revoke_relay_membership_callback=revoke_relay_membership,
                )
                if device is None:
                    # A relay invite can outlive the local device row if the
                    # adapter committed its mint immediately before this
                    # service crashed and rolled back the enclosing Invite
                    # transaction. Only a credential cryptographically scoped
                    # to this exact key may close that orphan-capability gap;
                    # an unscoped account/JWT must not gain arbitrary-key
                    # revocation authority.
                    active_owner_id = (
                        CommunityChatDevice.objects.select_for_update()
                        .filter(
                            public_key=public_key,
                            status__in=(
                                DeviceBindingStatus.PENDING,
                                DeviceBindingStatus.VERIFIED,
                            ),
                        )
                        .values_list("user_id", flat=True)
                        .first()
                    )
                    scoped_key = getattr(request, "community_chat_public_key", None)
                    if scoped_key is None or not secrets.compare_digest(
                        scoped_key,
                        public_key,
                    ) or active_owner_id not in (None, request.user.pk):
                        return Response(
                            {"error": "device_not_found"},
                            status=status.HTTP_404_NOT_FOUND,
                        )
                    revoke_device_credentials_locked(
                        locked_user,
                        public_key=public_key,
                        installation_id=getattr(
                            request,
                            "community_chat_installation_id",
                            None,
                        ),
                    )
                    relay_status, _ = revoke_relay_membership(public_key)
                    now = timezone.now()
                    cleanup_grant_ids = ()
                else:
                    relay_status = device.relay_revocation_status
                    now = device.revoked_at or timezone.now()
                    cleanup_grant_ids = tuple(
                        getattr(device, "registration_cleanup_grant_ids", ())
                    )

                bootstrap_token = getattr(
                    request, "community_chat_bootstrap_token", None
                )
                if bootstrap_token is not None and bootstrap_token.revoked_at is None:
                    bootstrap_token.revoked_at = now
                    bootstrap_token.save(update_fields=("revoked_at",))
                account_session = getattr(
                    request, "community_chat_account_session", None
                )
                if account_session is not None and account_session.revoked_at is None:
                    account_session.revoked_at = now
                    account_session.save(update_fields=("revoked_at", "updated_at"))
        except MembershipAdapterConflict as exc:
            return Response({"error": exc.code}, status=status.HTTP_409_CONFLICT)
        except MembershipAdapterUnavailable:
            return Response(
                {"error": "membership_service_unavailable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        from integrations.services.slack_dm_registration_ledger import (
            reconcile_registration_cleanup,
        )

        for grant_id in cleanup_grant_ids:
            reconcile_registration_cleanup(
                grant_id,
                # The privacy boundary above is already durable: the device,
                # queued bodies, and every registration containing its key
                # are fenced atomically. Adapter cleanup is content-free and
                # retried by the periodic worker, so do not consume the
                # caller's now-revoked auth credential with a non-retryable
                # 5xx response when the adapter is temporarily unavailable.
                raise_on_pending=False,
            )
        return Response({"status": "revoked", "relay_status": relay_status})
