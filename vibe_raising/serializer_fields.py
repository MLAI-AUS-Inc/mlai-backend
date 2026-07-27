from rest_framework import serializers

from vibe_raising.audience_visibility import (
    DEFAULT_AUDIENCE_VISIBILITY,
    normalize_audience_visibility,
)


class AudienceVisibilityField(serializers.Field):
    def to_internal_value(self, data):
        try:
            return normalize_audience_visibility(data)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def to_representation(self, value):
        try:
            return normalize_audience_visibility(value)
        except ValueError:
            return list(DEFAULT_AUDIENCE_VISIBILITY)
