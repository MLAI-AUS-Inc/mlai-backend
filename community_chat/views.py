import hashlib
import secrets
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .adapter import (
    MembershipAdapterConflict,
    MembershipAdapterUnavailable,
    get_relay_membership,
    issue_member_invite,
    revoke_relay_membership,
)
from .models import (
    CommunityChatChallenge,
    CommunityChatDevice,
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


def _device_payload(device):
    return {
        "id": str(device.id),
        "public_key": device.public_key,
        "status": device.status,
        "verified_at": device.verified_at,
        "last_verified_membership_at": device.last_verified_membership_at,
        "created_at": device.created_at,
    }


class SessionView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_session"

    def get(self, request):
        devices = CommunityChatDevice.objects.filter(
            user=request.user,
            status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
        )
        return Response(
            {
                "authenticated": True,
                "eligible": _is_eligible(request.user),
                "relay_url": settings.COMMUNITY_CHAT_RELAY_URL,
                "devices": [_device_payload(device) for device in devices],
            }
        )


class ChallengeView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_challenge"

    def post(self, request):
        _require_eligible(request.user)
        public_key = _public_key(request.data.get("public_key"))
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
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_confirm"

    def post(self, request):
        _require_eligible(request.user)
        public_key = _public_key(request.data.get("public_key"))
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
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_revoke"

    def delete(self, request, public_key):
        _require_eligible(request.user)
        public_key = _public_key(public_key)
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

