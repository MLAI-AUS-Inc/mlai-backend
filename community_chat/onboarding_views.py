"""Private account onboarding; admission remains mandatory for chat access."""

import unicodedata

from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import CommunityChatOnboardingAuthentication
from .onboarding import CITIES, INTERESTS, onboarding_payload, save_onboarding
from .serializers import own_chat_profile
from .throttles import CommunityChatScopedThrottle


class StrictBooleanField(serializers.BooleanField):
    """Consent must be a JSON boolean, not a truthy string or number."""

    def to_internal_value(self, data):
        if type(data) is not bool:
            self.fail("invalid", input=data)
        return data


class MemberOnboardingSerializer(serializers.Serializer):
    step = serializers.ChoiceField(choices=("basics", "complete"))
    first_name = serializers.CharField(max_length=80, required=False)
    last_name = serializers.CharField(max_length=80, required=False, allow_blank=True)
    adult_confirmed = StrictBooleanField(required=False)
    accept_rules = StrictBooleanField(required=False)
    policy_version = serializers.CharField(max_length=80, required=False)
    city = serializers.ChoiceField(choices=("", *CITIES), required=False)
    interests = serializers.ListField(
        child=serializers.ChoiceField(choices=tuple(key for key, _ in INTERESTS)), max_length=3, required=False,
    )
    marketing_opt_in = StrictBooleanField(required=False)
    skip_personalisation = StrictBooleanField(required=False)

    def validate(self, attrs):
        if set(self.initial_data) - set(self.fields):
            raise serializers.ValidationError("Unknown onboarding fields.")
        for key in ("first_name", "last_name"):
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
        user = save_onboarding(
            authenticated_session=request.community_chat_account_session,
            values=serializer.validated_data,
        )
        return self._response(user)

    def _response(self, user):
        response = Response({"onboarding": onboarding_payload(user), "profile": own_chat_profile(user)})
        response["Cache-Control"] = "no-store"
        return response
