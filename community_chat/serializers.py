from rest_framework import serializers

from .nostr import InvalidDeviceProof, normalize_public_key


COMMUNITY_CHAT_CLIENT_IDS = (
    'mlai-chat-web',
    'mlai-chat-desktop',
    'mlai-chat-ios',
    'mlai-chat-android',
)

COMMUNITY_CHAT_PUBLIC_PROFILE_BATCH_SIZE = 200


class CommunityChatPublicProfileBatchSerializer(serializers.Serializer):
    public_keys = serializers.ListField(
        child=serializers.CharField(min_length=64, max_length=64),
        allow_empty=True,
        max_length=COMMUNITY_CHAT_PUBLIC_PROFILE_BATCH_SIZE,
    )

    def validate_public_keys(self, values):
        normalized = []
        seen = set()
        for index, value in enumerate(values):
            try:
                public_key = normalize_public_key(value)
            except InvalidDeviceProof as exc:
                raise serializers.ValidationError(
                    {index: str(exc)},
                ) from exc
            if public_key not in seen:
                normalized.append(public_key)
                seen.add(public_key)
        return normalized


class CommunityChatDeviceLoginSerializer(serializers.Serializer):
    installation_id = serializers.UUIDField()
    public_key = serializers.CharField(min_length=64, max_length=64)
    platform = serializers.ChoiceField(
        choices=('web', 'macos', 'windows', 'linux', 'ios', 'android'),
    )
    name = serializers.CharField(max_length=120, required=False, allow_blank=True)

    def validate_public_key(self, value):
        try:
            return normalize_public_key(value)
        except InvalidDeviceProof as exc:
            raise serializers.ValidationError(str(exc)) from exc


class CommunityChatPasswordLoginSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)
    client_id = serializers.ChoiceField(choices=COMMUNITY_CHAT_CLIENT_IDS)
    device = CommunityChatDeviceLoginSerializer()

    def validate(self, attrs):
        client_id = attrs['client_id']
        platform = attrs['device']['platform']
        allowed_platforms = {
            'mlai-chat-web': {'web'},
            'mlai-chat-desktop': {'macos', 'windows', 'linux'},
            'mlai-chat-ios': {'ios'},
            'mlai-chat-android': {'android'},
        }
        if platform not in allowed_platforms[client_id]:
            raise serializers.ValidationError(
                {'device': {'platform': 'Platform does not match the registered client.'}}
            )
        return attrs


class CommunityChatEmailCodeRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    client_id = serializers.ChoiceField(choices=COMMUNITY_CHAT_CLIENT_IDS)
    device = CommunityChatDeviceLoginSerializer()

    def validate(self, attrs):
        return CommunityChatPasswordLoginSerializer().validate(attrs)


class CommunityChatEmailCodeVerifySerializer(serializers.Serializer):
    challenge_id = serializers.UUIDField()
    code = serializers.CharField(min_length=6, max_length=16, trim_whitespace=True)
    client_id = serializers.ChoiceField(choices=COMMUNITY_CHAT_CLIENT_IDS)
    installation_id = serializers.UUIDField()

    def validate_code(self, value):
        normalized = value.replace(" ", "").replace("-", "")
        if len(normalized) != 6 or not normalized.isdigit():
            raise serializers.ValidationError("Enter the six-digit code.")
        return normalized


def display_name_for_user(user):
    return user.full_name or 'MLAI member'


def profile_version_for_user(user):
    timestamp = user.updated_at or user.date_joined
    return timestamp.isoformat() if timestamp else None


def own_chat_profile(user):
    return {
        'id': str(user.id),
        'public_id': str(user.community_chat_profile_id),
        'email': user.email,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'display_name': display_name_for_user(user),
        'avatar_url': user.avatar_url,
        'about': user.about or '',
        'profile_version': profile_version_for_user(user),
    }


def public_chat_profile(user):
    return {
        'public_id': str(user.community_chat_profile_id),
        'display_name': display_name_for_user(user),
        'avatar_url': user.avatar_url,
        'about': user.about or '',
        'role': 'member',
        'profile_version': profile_version_for_user(user),
    }
