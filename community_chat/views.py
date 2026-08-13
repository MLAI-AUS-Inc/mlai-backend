import hashlib
import base64
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.contrib.auth.models import update_last_login
from django.db import IntegrityError, transaction
from django.db.models import Q
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
    revoke_relay_membership,
)
from hospital.authentication import CustomJWTAuthentication

from .authentication import (
    TOKEN_PREFIX,
    CommunityChatAccountAuthentication,
    CommunityChatBootstrapAuthentication,
)
from .account_cookies import (
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
from .email_codes import (
    InvalidEmailCode,
    consume_email_code,
    issue_email_code_challenge,
)
from .serializers import (
    CommunityChatEmailCodeRequestSerializer,
    CommunityChatEmailCodeVerifySerializer,
    CommunityChatPasswordLoginSerializer,
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
PKCE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class _EmailCodeBindingConflict(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _is_eligible(user):
    return bool(user and user.is_authenticated and user.is_active)


def _require_eligible(user):
    if not _is_eligible(user):
        raise PermissionDenied("This MLAI account is not eligible for community chat.")


def _request_origin(request):
    header_origin = str(request.headers.get("Origin") or "").strip().rstrip("/")
    body_origin = str(request.data.get("origin") or "").strip().rstrip("/")
    if header_origin and body_origin and header_origin != body_origin:
        raise PermissionDenied("Request origin does not match the claimed origin.")
    origin = header_origin or body_origin
    allowed = {str(item).strip().rstrip("/") for item in settings.COMMUNITY_CHAT_ALLOWED_ORIGINS}
    if not origin or origin not in allowed:
        raise PermissionDenied("Request origin is not approved for community chat.")
    token_origin = getattr(request, "community_chat_origin", None)
    if token_origin and token_origin != "legacy" and not secrets.compare_digest(token_origin, origin):
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


def _password_auth_origin(request, client_id):
    origin = str(request.headers.get("Origin") or "").strip().rstrip("/")
    allowed = {str(item).strip().rstrip("/") for item in settings.COMMUNITY_CHAT_ALLOWED_ORIGINS}
    if origin and origin not in allowed:
        raise PermissionDenied("Request origin is not approved for community chat.")
    if client_id == "mlai-chat-web":
        if not origin or not origin.startswith(("https://", "http://")):
            raise PermissionDenied("The web client requires an approved browser origin.")
    elif client_id == "mlai-chat-desktop":
        if not origin or origin.startswith("mlaichat://"):
            raise PermissionDenied("The desktop client requires an approved application origin.")
    else:
        if origin and origin != "mlaichat://callback":
            raise PermissionDenied("Mobile password authentication has an invalid origin.")
        origin = origin or "mlaichat://callback"
    return origin


def _token_enrollment_context(request):
    installation_id = getattr(request, "community_chat_installation_id", None)
    client_id = getattr(request, "community_chat_client_id", None)
    if installation_id and client_id:
        return installation_id, client_id
    try:
        installation_id = uuid.UUID(str(request.data.get("installation_id") or uuid.uuid4()))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError({"installation_id": "Invalid installation identifier."}) from exc
    return installation_id, str(request.data.get("client_id") or "legacy")[:64]


def _require_token_challenge_context(request, challenge):
    token_installation = getattr(request, "community_chat_installation_id", None)
    token_client = getattr(request, "community_chat_client_id", None)
    if token_installation and token_installation != challenge.installation_id:
        raise PermissionDenied("This authorization is bound to a different installation.")
    if token_client and not secrets.compare_digest(token_client, challenge.client_id):
        raise PermissionDenied("This authorization is bound to a different client.")


def _pkce_challenge(verifier):
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("utf-8")).digest()
    ).decode("ascii").rstrip("=")


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
            return Response({"error": "password_auth_disabled"}, status=status.HTTP_404_NOT_FOUND)
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
        active_key = CommunityChatDevice.objects.filter(
            public_key=public_key,
            status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
        ).first()
        if active_key and active_key.user_id != user.id:
            return Response({"error": "public_key_already_bound"}, status=status.HTTP_409_CONFLICT)
        active_installation = CommunityChatDevice.objects.filter(
            installation_id=installation_id,
            status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
        ).first()
        if active_installation and (
            active_installation.user_id != user.id
            or active_installation.public_key != public_key
        ):
            return Response({"error": "installation_already_bound"}, status=status.HTTP_409_CONFLICT)

        now = timezone.now()
        raw_token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(48)}"
        with transaction.atomic():
            CommunityChatBootstrapToken.objects.filter(
                user=user,
                public_key=public_key,
                revoked_at__isnull=True,
            ).update(revoked_at=now)
            token = CommunityChatBootstrapToken.objects.create(
                user=user,
                public_key=public_key,
                installation_id=installation_id,
                client_id=data["client_id"],
                origin=origin,
                platform=device_data["platform"],
                name=device_data.get("name", ""),
                token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
                expires_at=now
                + timedelta(seconds=settings.COMMUNITY_CHAT_BOOTSTRAP_TOKEN_TTL_SECONDS),
            )
            update_last_login(None, user)

        return Response(
            {
                "status": "authenticated",
                "bootstrap_token": raw_token,
                "expires_at": token.expires_at,
                "relay_url": settings.COMMUNITY_CHAT_RELAY_URL,
                "origin": origin,
                "profile": own_chat_profile(user),
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
    active_key = CommunityChatDevice.objects.select_for_update().filter(
        public_key=challenge.public_key,
        status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
    ).first()
    if active_key and active_key.user_id != user.id:
        raise _EmailCodeBindingConflict("public_key_already_bound")
    active_installation = CommunityChatDevice.objects.select_for_update().filter(
        installation_id=challenge.installation_id,
        status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
    ).first()
    if active_installation and active_installation.user_id != user.id:
        raise _EmailCodeBindingConflict("installation_already_bound")
    if (
        active_installation
        and active_installation.public_key != challenge.public_key
    ):
        if active_key and active_key.id != active_installation.id:
            raise _EmailCodeBindingConflict("public_key_already_bound")

        # A valid email code proves control of the same MLAI account that owns
        # this installation. Recover from a lost or regenerated browser signer
        # by retiring the old relay identity and preserving it as audit history.
        # The replacement remains pending until the normal signed challenge,
        # invite, and membership-confirmation flow proves the new key.
        revoke_relay_membership(active_installation.public_key)
        now = timezone.now()
        active_installation.status = DeviceBindingStatus.REVOKED
        active_installation.revoked_at = now
        active_installation.revoked_by = user
        active_installation.revocation_reason = "email_code_identity_recovery"
        active_installation.save(
            update_fields=(
                "status",
                "revoked_at",
                "revoked_by",
                "revocation_reason",
                "updated_at",
            )
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
    ).update(revoked_at=now)
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
    return raw_token, token


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
                    raw_token, token = _issue_email_code_bootstrap(user, challenge)
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
        raw_token = explicit_token or str(
            request.COOKIES.get(ACCOUNT_REFRESH_COOKIE) or ""
        ).strip()
        required_origin = None
        if not explicit_token:
            required_origin = str(request.headers.get("Origin") or "").strip().rstrip("/")
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
        raw_token = explicit_token or str(
            request.COOKIES.get(ACCOUNT_REFRESH_COOKIE) or ""
        ).strip()
        required_origin = None
        if not explicit_token:
            required_origin = str(request.headers.get("Origin") or "").strip().rstrip("/")
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


class DeviceAuthStartView(APIView):
    """Create an origin/key/state/PKCE-bound browser-to-app login request."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not settings.COMMUNITY_CHAT_DEVICE_AUTH_ENABLED:
            return Response(
                {"error": "device_auth_disabled"},
                status=status.HTTP_404_NOT_FOUND,
            )
        public_key = _public_key(request.data.get("public_key"))
        origin = _request_origin(request)
        state = str(request.data.get("state") or "")
        code_challenge = str(request.data.get("code_challenge") or "")
        if len(state) < 32 or len(state) > 256:
            raise ValidationError({"state": "State must contain at least 32 characters."})
        if not PKCE_CHALLENGE_RE.fullmatch(code_challenge):
            raise ValidationError({"code_challenge": "Invalid PKCE S256 challenge."})
        enforce_bootstrap_limits(
            request,
            action="auth-start",
            public_key=public_key,
            user_limit=20,
            key_limit=10,
            ip_limit=30,
        )
        auth_request = CommunityChatDeviceAuthRequest.objects.create(
            public_key=public_key,
            origin=origin,
            state_hash=hashlib.sha256(state.encode("utf-8")).hexdigest(),
            code_challenge=code_challenge,
            expires_at=timezone.now()
            + timedelta(seconds=settings.COMMUNITY_CHAT_DEVICE_AUTH_TTL_SECONDS),
        )
        callback_path = f"/auth/callback?request={auth_request.id}"
        return Response(
            {
                "request_id": str(auth_request.id),
                "callback_path": callback_path,
                "expires_at": auth_request.expires_at,
            },
            status=status.HTTP_201_CREATED,
        )


class DeviceAuthAuthorizeView(APIView):
    """Authorize a pending app handoff using the browser's MLAI session."""

    authentication_classes = (CustomJWTAuthentication,)
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not settings.COMMUNITY_CHAT_DEVICE_AUTH_ENABLED:
            return Response(
                {"error": "device_auth_disabled"},
                status=status.HTTP_404_NOT_FOUND,
            )
        _require_eligible(request.user)
        origin = _request_origin(request)
        browser_origin = str(settings.COMMUNITY_CHAT_FRONTEND_URL).strip().rstrip("/")
        if not secrets.compare_digest(origin, browser_origin):
            raise PermissionDenied("Device approval must come from the MLAI Chat browser origin.")
        request_id = request.data.get("request_id")
        now = timezone.now()
        try:
            with transaction.atomic():
                auth_request = CommunityChatDeviceAuthRequest.objects.select_for_update().get(
                    id=request_id
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
                if auth_request.user_id and auth_request.user_id != request.user.id:
                    raise PermissionDenied("Login request belongs to another MLAI account.")
                auth_request.user = request.user
                auth_request.authorized_at = now
                auth_request.save(update_fields=("user", "authorized_at"))
        except (CommunityChatDeviceAuthRequest.DoesNotExist, ValueError):
            return Response({"error": "authorization_not_found"}, status=status.HTTP_404_NOT_FOUND)
        return Response({"status": "authorized", "request_id": str(auth_request.id)})


class DeviceAuthExchangeView(APIView):
    """Exchange the state + PKCE verifier for a one-purpose bootstrap token."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not settings.COMMUNITY_CHAT_DEVICE_AUTH_ENABLED:
            return Response(
                {"error": "device_auth_disabled"},
                status=status.HTTP_404_NOT_FOUND,
            )
        request_id = request.data.get("request_id")
        state_value = str(request.data.get("state") or "")
        verifier = str(request.data.get("code_verifier") or "")
        origin = _request_origin(request)
        now = timezone.now()
        try:
            with transaction.atomic():
                auth_request = CommunityChatDeviceAuthRequest.objects.select_for_update().get(
                    id=request_id
                )
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
                state_hash = hashlib.sha256(state_value.encode("utf-8")).hexdigest()
                verifier_challenge = _pkce_challenge(verifier)
                if not secrets.compare_digest(auth_request.state_hash, state_hash) or not secrets.compare_digest(
                    auth_request.code_challenge, verifier_challenge
                ):
                    raise PermissionDenied("Login state or verifier is invalid.")
                if auth_request.authorized_at is None or auth_request.user_id is None:
                    return Response(
                        {"status": "pending"},
                        status=status.HTTP_202_ACCEPTED,
                    )

                raw_token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(48)}"
                token = CommunityChatBootstrapToken.objects.create(
                    user=auth_request.user,
                    public_key=auth_request.public_key,
                    origin=auth_request.origin,
                    token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
                    expires_at=now
                    + timedelta(seconds=settings.COMMUNITY_CHAT_BOOTSTRAP_TOKEN_TTL_SECONDS),
                )
                auth_request.consumed_at = now
                auth_request.save(update_fields=("consumed_at",))
        except (CommunityChatDeviceAuthRequest.DoesNotExist, ValueError):
            return Response({"error": "authorization_not_found"}, status=status.HTTP_404_NOT_FOUND)

        return Response(
            {
                "status": "authorized",
                "access_token": raw_token,
                "expires_at": token.expires_at,
                "public_key": token.public_key,
            }
        )


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

        active = CommunityChatDevice.objects.filter(
            public_key=public_key,
            status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
        ).first()
        if active and active.user_id != request.user.id:
            return Response(
                {"error": "public_key_already_bound"},
                status=status.HTTP_409_CONFLICT,
            )
        active_installation = CommunityChatDevice.objects.filter(
            installation_id=installation_id,
            status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
        ).first()
        if active_installation and active_installation.public_key != public_key:
            return Response(
                {"error": "installation_already_bound"},
                status=status.HTTP_409_CONFLICT,
            )

        nonce = secrets.token_urlsafe(32)
        expires_at = timezone.now() + timedelta(seconds=settings.COMMUNITY_CHAT_CHALLENGE_TTL_SECONDS)
        challenge = CommunityChatChallenge.objects.create(
            user=request.user,
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
                challenge = CommunityChatChallenge.objects.select_for_update().get(
                    id=challenge_id,
                    user=request.user,
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
                    return Response({"error": "challenge_replayed"}, status=status.HTTP_409_CONFLICT)
                if challenge.expires_at <= now:
                    return Response({"error": "challenge_expired"}, status=status.HTTP_410_GONE)
                if challenge.origin != origin or challenge.audience != settings.COMMUNITY_CHAT_API_AUDIENCE:
                    raise PermissionDenied("Challenge context does not match this request.")
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
                signed_time = datetime.fromtimestamp(signed_at, tz=datetime_timezone.utc)
                if abs((now - signed_time).total_seconds()) > settings.COMMUNITY_CHAT_CHALLENGE_TTL_SECONDS:
                    raise PermissionDenied("Device signature is outside the allowed time window.")

                existing = CommunityChatDevice.objects.select_for_update().filter(
                    public_key=public_key,
                    status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
                ).first()
                if existing and existing.user_id != request.user.id:
                    return Response(
                        {"error": "public_key_already_bound"},
                        status=status.HTTP_409_CONFLICT,
                    )
                if existing and existing.status == DeviceBindingStatus.VERIFIED:
                    challenge.used_at = now
                    challenge.save(update_fields=("used_at",))
                    return Response({"status": "already_member", "device": _device_payload(existing)})
                device = existing or CommunityChatDevice.objects.create(
                    user=request.user,
                    public_key=public_key,
                    installation_id=challenge.installation_id,
                    client_id=challenge.client_id,
                    platform=getattr(request, "community_chat_platform", ""),
                    name=getattr(request, "community_chat_device_name", ""),
                )
                challenge.used_at = now
                challenge.save(update_fields=("used_at",))
        except CommunityChatChallenge.DoesNotExist:
            return Response({"error": "challenge_not_found"}, status=status.HTTP_404_NOT_FOUND)
        except (ValueError, OverflowError, OSError):
            raise PermissionDenied("Device signature timestamp is invalid.")
        except IntegrityError:
            return Response({"error": "public_key_already_bound"}, status=status.HTTP_409_CONFLICT)

        try:
            invite = issue_member_invite(public_key)
        except MembershipAdapterConflict as exc:
            if exc.code == "already_member":
                return Response({"status": "already_member", "device": _device_payload(device)})
            return Response({"error": exc.code}, status=status.HTTP_409_CONFLICT)
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
        device = CommunityChatDevice.objects.filter(
            user=request.user,
            public_key=public_key,
            status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
        ).first()
        if not device:
            return Response({"error": "device_not_found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            membership = get_relay_membership(public_key)
        except MembershipAdapterUnavailable:
            return Response(
                {"error": "membership_service_unavailable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if not membership.is_member:
            return Response({"error": "membership_not_found"}, status=status.HTTP_409_CONFLICT)
        if membership.role != "member":
            return Response({"error": "unexpected_relay_role"}, status=status.HTTP_409_CONFLICT)

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
        bootstrap_token = getattr(request, "community_chat_bootstrap_token", None)
        if bootstrap_token is not None and bootstrap_token.revoked_at is None:
            bootstrap_token.revoked_at = now
            bootstrap_token.save(update_fields=("revoked_at",))
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
        device = CommunityChatDevice.objects.filter(
            user=request.user,
            public_key=public_key,
            status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
        ).first()
        if not device:
            return Response({"error": "device_not_found"}, status=status.HTTP_404_NOT_FOUND)
        reason = str(request.data.get("reason") or "user_requested").strip()[:500]
        try:
            relay_status, _ = revoke_relay_membership(public_key)
        except MembershipAdapterConflict as exc:
            return Response({"error": exc.code}, status=status.HTTP_409_CONFLICT)
        except MembershipAdapterUnavailable:
            return Response(
                {"error": "membership_service_unavailable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        now = timezone.now()
        device.status = DeviceBindingStatus.REVOKED
        device.revoked_at = now
        device.revoked_by = request.user
        device.revocation_reason = reason
        device.save(
            update_fields=(
                "status",
                "revoked_at",
                "revoked_by",
                "revocation_reason",
                "updated_at",
            )
        )
        bootstrap_token = getattr(request, "community_chat_bootstrap_token", None)
        if bootstrap_token is not None and bootstrap_token.revoked_at is None:
            bootstrap_token.revoked_at = now
            bootstrap_token.save(update_fields=("revoked_at",))
        account_session = getattr(request, "community_chat_account_session", None)
        if account_session is not None and account_session.revoked_at is None:
            account_session.revoked_at = now
            account_session.save(update_fields=("revoked_at", "updated_at"))
        return Response({"status": "revoked", "relay_status": relay_status})
