import hashlib
import base64
import re
import secrets
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
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
    CommunityChatBootstrapAuthentication,
)
from .models import (
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


BOOTSTRAP_ACTION = "community-chat:enrol-device"
AUTHENTICATION_CLASSES = (
    CommunityChatBootstrapAuthentication,
    CustomJWTAuthentication,
)
PKCE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


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


def _pkce_challenge(verifier):
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("utf-8")).digest()
    ).decode("ascii").rstrip("=")


def _device_payload(device):
    return {
        "id": str(device.id),
        "public_key": device.public_key,
        "status": device.status,
        "verified_at": device.verified_at,
        "last_verified_membership_at": device.last_verified_membership_at,
        "created_at": device.created_at,
    }


class DeviceAuthStartView(APIView):
    """Create an origin/key/state/PKCE-bound browser-to-app login request."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
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
    authentication_classes = AUTHENTICATION_CLASSES
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
            }
        )


class ChallengeView(APIView):
    authentication_classes = AUTHENTICATION_CLASSES
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_challenge"

    def post(self, request):
        _require_eligible(request.user)
        public_key = _public_key(request.data.get("public_key"))
        _require_token_key(request, public_key)
        origin = _request_origin(request)
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

        nonce = secrets.token_urlsafe(32)
        expires_at = timezone.now() + timedelta(seconds=settings.COMMUNITY_CHAT_CHALLENGE_TTL_SECONDS)
        challenge = CommunityChatChallenge.objects.create(
            user=request.user,
            public_key=public_key,
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
    authentication_classes = AUTHENTICATION_CLASSES
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
    authentication_classes = AUTHENTICATION_CLASSES
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
        device.save(update_fields=("status", "verified_at", "last_verified_membership_at", "updated_at"))
        CommunityChatInviteAudit.objects.filter(
            device=device,
            confirmed_at__isnull=True,
        ).update(confirmed_at=now)
        return Response({"status": "verified", "device": _device_payload(device)})


class DeviceView(APIView):
    authentication_classes = AUTHENTICATION_CLASSES
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_revoke"

    def delete(self, request, public_key):
        _require_eligible(request.user)
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
        return Response({"status": "revoked", "relay_status": relay_status})
