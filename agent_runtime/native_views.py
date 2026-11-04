"""Owner management and private registry for the separately gated Dot pilot."""

import secrets
from urllib.parse import urlsplit

from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .authentication import OwnerAuthentication, bearer_value
from .models import AgentEnrollment, AgentInstallation
from . import native_installations, native_sources, serializers as schema
from .views import RuntimeView


class NativeOwnerView(RuntimeView):
    """Read disabled setup without consulting an uncreated runtime schema."""
    authentication_classes = (OwnerAuthentication,)

    def initial(self, request, *args, **kwargs):
        # Deliberately skip RuntimeView's feature gate; mutations enforce their own.
        if len(request.body) > 32 * 1024:
            raise ValidationError("Request is too large.")
        APIView.initial(self, request, *args, **kwargs)
        if not request.user or not request.user.is_authenticated or not request.user.is_active:
            raise PermissionDenied("Sign in to your MLAI account.")


class NativeAppsView(NativeOwnerView):
    def get(self, request):
        return Response(native_installations.catalog(request.user, relay_url=request.query_params.get("relay_url")))


class NativeInstallationsView(NativeOwnerView):
    def get(self, request):
        community, _ = native_sources.community_config(request.query_params.get("community"))
        if not getattr(settings, "AGENT_RUNTIME_ENABLED", False):
            return Response({"community_id": community, "installations": [],
                "limits": {"personal_limit": max(0, getattr(settings, "AGENT_RUNTIME_NATIVE_PERSONAL_LIMIT", 1)),
                           "personal_used": 0}})
        rows = AgentInstallation.objects.filter(owner=request.user, community_id=community,
            app_id__in=("openai-dot", "mlai-community")).select_related("owner_device").order_by("-created_at")
        # Revoked public identities remain owner-visible tombstones so search
        # results cannot mistake them for ordinary users when directory data is absent.
        return Response({"community_id": community,
            "installations": [native_installations.installation_payload(row) for row in rows],
            "limits": native_installations.limits(request.user, community)})


class NativeEnrollmentView(NativeOwnerView):
    def post(self, request):
        data = schema.validated(schema.EnrollmentSerializer, request.data)
        row = native_installations.enroll(request.user, data)
        return Response(native_installations.enrollment_payload(row), status=201)


class NativeEnrollmentDetailView(NativeOwnerView):
    def get(self, request, enrollment_id):
        native_sources.require_native()
        row = get_object_or_404(AgentEnrollment.objects.select_related("installation__owner_device"),
                               pk=enrollment_id, installation__owner=request.user)
        return Response(native_installations.enrollment_payload(row))


class NativeEnrollmentConfirmView(NativeOwnerView):
    def post(self, request, enrollment_id):
        data = schema.validated(schema.EnrollmentConfirmSerializer, request.data)
        row = native_installations.confirm(request.user, enrollment_id, **data)
        return Response(native_installations.installation_payload(row))


class NativeInstallationStateView(NativeOwnerView):
    def post(self, request, installation_id):
        native_sources.require_native()
        data = schema.validated(schema.InstallationStateSerializer, request.data)
        row = native_installations.change_state(request.user, installation_id, data["state"])
        return Response(native_installations.installation_payload(row))


class NativePersonalMentionView(NativeOwnerView):
    """Submit identifiers for an owner's signed, per-message context consent."""

    def post(self, request, installation_id):
        from . import native_invocations, personal_context
        data = schema.validated(schema.PersonalMentionSerializer, request.data)
        row = personal_context.submit(request.user, installation_id, data)
        return Response(native_invocations.payload(row), status=202)


class NativeRegistryView(APIView):
    """Keeps bot tombstones available even when the operator stops native work.

    Relay cache contract: registered=true is permanent for a key. Installations
    are never deleted or re-keyed; revocation retains their tombstones. Keys are
    generated server-side in native_installations.enroll, never from client input.
    MLAI Chat's crates/buzz-relay/src/native_agent.rs caches agent verdicts for
    24 hours and ordinary verdicts for up to ten minutes. Change that cache
    policy before changing either invariant.
    """
    authentication_classes = ()
    permission_classes = ()
    # Internal service classification is on ordinary relay admission/fanout.
    # Charging it to the public 60/min agent bucket would block human chat.
    throttle_classes = ()

    def get(self, request, public_key):
        expected = str(getattr(settings, "AGENT_RUNTIME_NATIVE_REGISTRY_KEY", ""))
        if len(expected) < 32 or not secrets.compare_digest(bearer_value(request), expected):
            raise AuthenticationFailed("Registry credential is invalid.")
        if not native_crypto_public_key(public_key):
            raise ValidationError("Invalid public key.")
        # Classification is global and grants no native operation access. Shared
        # relay hosts may serve communities outside the enrollment allowlist.
        relay = canonical_registry_relay(request.query_params.get("relay_url"))
        # All native keys remain agent keys forever, including expired reservations.
        registered = AgentInstallation.objects.filter(public_key=public_key, community_id__isnull=False).exists()
        response = Response({"public_key": public_key, "relay_url": relay, "registered": registered})
        installation = AgentInstallation.objects.filter(public_key=public_key, community_id__isnull=False).first() if registered else None
        if getattr(installation, "app_id", None) in {"openai-dot", "mlai-community"}:
            response.data["app_id"] = installation.app_id
        response["Cache-Control"] = "no-store"
        return response


def native_crypto_public_key(value):
    from community_chat.nostr import PUBLIC_KEY_RE
    return isinstance(value, str) and PUBLIC_KEY_RE.fullmatch(value) is not None


def canonical_registry_relay(value):
    raw = str(value or "")
    if raw.startswith("wss://"):
        raw = "https://" + raw[6:]
    try:
        parts = urlsplit(raw)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.path not in {"", "/"} or parts.query or parts.fragment):
            raise ValueError()
    except ValueError as exc:
        raise ValidationError("Registry requires a canonical relay origin.") from exc
    return raw.rstrip("/")
