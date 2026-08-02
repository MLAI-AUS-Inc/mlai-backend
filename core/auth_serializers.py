from rest_framework import serializers


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)


class PasswordResetConfirmSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=200, trim_whitespace=False)
    new_password = serializers.CharField(
        max_length=128,
        trim_whitespace=False,
        write_only=True,
    )


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(
        max_length=128,
        trim_whitespace=False,
        write_only=True,
    )
    new_password = serializers.CharField(
        max_length=128,
        trim_whitespace=False,
        write_only=True,
    )
