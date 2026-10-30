"""Private account onboarding; admission remains mandatory for chat access."""

import unicodedata

from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.client_ip import trusted_client_ip

from .authentication import CommunityChatOnboardingAuthentication
from .invite_tokens import TOKEN_MAX_LENGTH, InvalidInviteToken, invite_tokens_enabled, resolve_invite_token
from .onboarding import (
    CITIES, INTERESTS, JOIN_INTENTS, join_next, one_step_join_enabled, onboarding_payload, save_onboarding,
)
from .serializers import own_chat_profile
from .throttles import CommunityChatScopedThrottle

JOIN_ONLY_FIELDS = ("adult_and_terms", "intent", "invite")
INVITE_INVALID_DETAIL = "This invite link has expired."


class StrictBooleanField(serializers.BooleanField):
    """Consent must be a JSON boolean, not a truthy string or number."""

    def to_internal_value(self, data):
        if type(data) is not bool:
            self.fail("invalid", input=data)
        return data


class MemberOnboardingSerializer(serializers.Serializer):
    step = serializers.ChoiceField(choices=("basics", "complete"))
    first_name = serializers.CharField(max_length=80, required=False)
    last_name = serializers.CharField(max_length=80, required=False)
    adult_confirmed = StrictBooleanField(required=False)
    accept_rules = StrictBooleanField(required=False)
    policy_version = serializers.CharField(max_length=80, required=False)
    city = serializers.ChoiceField(choices=("", *CITIES), required=False)
    interests = serializers.ListField(
        child=serializers.ChoiceField(choices=tuple(key for key, _ in INTERESTS)), max_length=3, required=False,
    )
    marketing_opt_in = StrictBooleanField(required=False)
    skip_personalisation = StrictBooleanField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if one_step_join_enabled():
            # One-screen join (P1.3). With the switch off none of these fields
            # exist, so a "join" step or a join field is refused as today.
            self.fields["step"] = serializers.ChoiceField(choices=("basics", "complete", "join"))
            self.fields["last_name"].allow_blank = True
            self.fields["adult_and_terms"] = StrictBooleanField(required=False)
            self.fields["intent"] = serializers.ChoiceField(choices=JOIN_INTENTS, required=False)
            self.fields["invite"] = serializers.CharField(max_length=TOKEN_MAX_LENGTH, required=False)

    def validate(self, attrs):
        if set(self.initial_data) - set(self.fields):
            raise serializers.ValidationError("Unknown onboarding fields.")
        if attrs["step"] == "join":
            missing = [key for key in ("first_name", "adult_and_terms", "policy_version", "intent") if key not in attrs]
            if "first_name" in missing or not attrs.get("first_name", "").strip():
                raise serializers.ValidationError({"first_name": "Enter your name."})
            if missing:
                raise serializers.ValidationError({missing[0]: "This field is required."})
        elif any(key in attrs for key in JOIN_ONLY_FIELDS):
            raise serializers.ValidationError("Unknown onboarding fields.")
        for key in ("first_name", "last_name"):
            if attrs["step"] == "basics" and not attrs.get(key, "").strip():
                raise serializers.ValidationError({key: "Enter your first name." if key == "first_name" else "Enter your surname."})
            if any(unicodedata.category(char) in {"Cc", "Cs"} for char in attrs.get(key, "")):
                raise serializers.ValidationError({key: "Remove control characters from your name."})
        if len(attrs.get("interests", [])) != len(set(attrs.get("interests", []))):
            raise serializers.ValidationError({"interests": "Choose each interest once."})
        return attrs


class MemberOnboardingView(APIView):
    """Read or save only the calling account's private membership application."""

    authentication_classes = (CommunityChatOnboardingAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_home"

    def get(self, request):
        return self._response(request.user)

    def put(self, request):
        serializer = MemberOnboardingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        user = save_onboarding(
            authenticated_session=request.community_chat_account_session,
            values=values,
        )
        extra = {"next": join_next(values.get("intent"))} if values["step"] == "join" else {}
        return self._response(user, **extra)

    def _response(self, user, **extra):
        response = Response({"onboarding": onboarding_payload(user), "profile": own_chat_profile(user), **extra})
        response["Cache-Control"] = "no-store"
        return response


class InviteResolveSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=TOKEN_MAX_LENGTH, trim_whitespace=True)


class InviteResolveThrottle(CommunityChatScopedThrottle):
    """Anonymous resolve is keyed by the trusted client IP, never a spoofable header."""

    def get_ident(self, request):
        return trusted_client_ip(request)


class InviteResolveView(APIView):
    """Resolve an opaque invite token for prefill and routing; never blocks sign-in."""

    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (InviteResolveThrottle,)
    community_chat_throttle_scope = "community_chat_invite_resolve"

    def post(self, request):
        if not invite_tokens_enabled():
            return self._no_store(Response({"error": "invite_tokens_disabled"}, status=status.HTTP_404_NOT_FOUND))
        serializer = InviteResolveSerializer(data=request.data)
        if not serializer.is_valid():
            return self._invalid()
        try:
            claims = resolve_invite_token(serializer.validated_data["token"])
        except InvalidInviteToken:
            return self._invalid()
        return self._no_store(Response(claims.public_payload()))

    def _invalid(self):
        return self._no_store(Response(
            {"detail": INVITE_INVALID_DETAIL, "code": "invite_invalid"},
            status=status.HTTP_400_BAD_REQUEST,
        ))

    @staticmethod
    def _no_store(response):
        response["Cache-Control"] = "no-store"
        return response
