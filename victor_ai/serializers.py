import re

from rest_framework import serializers

from .models import VictorApplication

# Stages at (or beyond) which the form collects recent revenue.
# Keep in sync with REVENUE_STAGES in the victorai.win frontend (src/App.tsx).
REVENUE_STAGES = {
    'We have paying users',
    'Pre-seed',
    'Seed',
    'Series A',
    'Series B or later',
}

MONTH_KEY_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')

MAX_TEAM_SIZE = 50


class TeamMemberSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=255)
    last_name = serializers.CharField(max_length=255)
    email = serializers.EmailField()
    role = serializers.CharField(max_length=64)


class VictorApplicationSerializer(serializers.ModelSerializer):
    # The form caps the problem statement at 240 characters; mirror it here.
    idea = serializers.CharField(max_length=240, required=False, allow_blank=True)
    team_size = serializers.IntegerField(
        min_value=1, max_value=MAX_TEAM_SIZE, required=False, allow_null=True
    )
    team_members = TeamMemberSerializer(many=True, required=False)
    revenue_last_3_months = serializers.DictField(
        child=serializers.FloatField(min_value=0), required=False
    )

    class Meta:
        model = VictorApplication
        fields = [
            'client_ref', 'stage',
            'first_name', 'last_name', 'email', 'linkedin',
            'team_name', 'role', 'startup_stage', 'industry_sector', 'location',
            'team_size', 'team_members', 'revenue_last_3_months',
            'idea', 'support', 'consent',
        ]

    def validate_team_members(self, value):
        if len(value) > MAX_TEAM_SIZE - 1:
            raise serializers.ValidationError(
                f'List at most {MAX_TEAM_SIZE - 1} other team members.'
            )
        return value

    def validate_revenue_last_3_months(self, value):
        for key in value:
            if not MONTH_KEY_RE.match(key):
                raise serializers.ValidationError(
                    f"Invalid month key '{key}' (expected YYYY-MM)."
                )
        return value

    def _current(self, attrs, field, default):
        return attrs.get(field, getattr(self.instance, field, default))

    def validate(self, attrs):
        stage = self._current(attrs, 'stage', VictorApplication.STAGE_LEAD)
        if stage != VictorApplication.STAGE_COMPLETE:
            return attrs
        errors = {}
        if not self._current(attrs, 'consent', False):
            errors['consent'] = 'Consent is required to submit a registration.'
        for field in ('team_name', 'role', 'startup_stage', 'industry_sector', 'idea'):
            if not str(self._current(attrs, field, '') or '').strip():
                errors[field] = 'This field is required to submit a registration.'

        team_size = self._current(attrs, 'team_size', None)
        team_members = self._current(attrs, 'team_members', []) or []
        if not team_size:
            errors['team_size'] = 'Team size (including you) is required to submit a registration.'
        elif len(team_members) != team_size - 1:
            errors['team_members'] = (
                f'List first name, last name, and email for each other team member: '
                f'expected {team_size - 1}, got {len(team_members)}.'
            )

        startup_stage = str(self._current(attrs, 'startup_stage', '') or '')
        if startup_stage in REVENUE_STAGES:
            revenue = self._current(attrs, 'revenue_last_3_months', {}) or {}
            if len(revenue) != 3:
                errors['revenue_last_3_months'] = (
                    'Provide revenue for each of the last three months.'
                )

        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class VictorApplicationListSerializer(serializers.ModelSerializer):
    class Meta:
        model = VictorApplication
        fields = [
            'id', 'first_name', 'last_name', 'email', 'stage', 'role',
            'startup_stage', 'industry_sector', 'team_name', 'team_size',
            'created_at',
        ]


class VictorApplicationDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = VictorApplication
        fields = [
            'id', 'stage', 'first_name', 'last_name', 'email', 'linkedin',
            'team_name', 'role', 'startup_stage', 'industry_sector', 'location',
            'team_size', 'team_members', 'revenue_last_3_months', 'idea',
            'support', 'consent', 'created_at', 'updated_at',
        ]
