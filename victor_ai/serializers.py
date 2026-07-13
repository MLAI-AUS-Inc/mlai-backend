from rest_framework import serializers

from .models import VictorApplication


class VictorApplicationSerializer(serializers.ModelSerializer):
    class Meta:
        model = VictorApplication
        fields = [
            'client_ref', 'stage',
            'first_name', 'last_name', 'email',
            'team_name', 'role', 'startup_stage', 'industry_sector', 'location',
            'idea', 'support', 'consent',
        ]

    def _current(self, attrs, field, default):
        return attrs.get(field, getattr(self.instance, field, default))

    def validate(self, attrs):
        stage = self._current(attrs, 'stage', VictorApplication.STAGE_LEAD)
        if stage != VictorApplication.STAGE_COMPLETE:
            return attrs
        errors = {}
        if not self._current(attrs, 'consent', False):
            errors['consent'] = 'Consent is required to submit a registration.'
        for field in ('role', 'startup_stage', 'industry_sector', 'idea'):
            if not str(self._current(attrs, field, '')).strip():
                errors[field] = 'This field is required to submit a registration.'
        if errors:
            raise serializers.ValidationError(errors)
        return attrs
