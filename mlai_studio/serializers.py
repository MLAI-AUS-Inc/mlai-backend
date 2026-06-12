from rest_framework import serializers

from .models import StudioApplication


class StudioApplicationSerializer(serializers.ModelSerializer):
    # The form's multi-selects are lists of short strings; don't accept
    # arbitrary JSON blobs into the JSONFields.
    skills = serializers.ListField(child=serializers.CharField(max_length=255), required=False)
    skills_other = serializers.ListField(child=serializers.CharField(max_length=255), required=False)
    ai_tools = serializers.ListField(child=serializers.CharField(max_length=255), required=False)
    ai_tools_other = serializers.ListField(child=serializers.CharField(max_length=255), required=False)
    interests = serializers.ListField(child=serializers.CharField(max_length=255), required=False)
    interests_other = serializers.ListField(child=serializers.CharField(max_length=255), required=False)

    class Meta:
        model = StudioApplication
        fields = [
            'client_ref', 'stage',
            'full_name', 'email', 'phone',
            'location', 'legal_work', 'visa',
            'linkedin', 'github', 'portfolio',
            'skills', 'skills_other',
            'ai_tools', 'ai_tools_other',
            'interests', 'interests_other',
            'availability', 'availability_other',
            'start_date', 'start_date_other', 'rate',
            'projects', 'anything_else', 'consent',
        ]

    def validate(self, attrs):
        stage = attrs.get('stage', getattr(self.instance, 'stage', StudioApplication.STAGE_LEAD))
        consent = attrs.get('consent', getattr(self.instance, 'consent', False))
        if stage == StudioApplication.STAGE_COMPLETE and not consent:
            raise serializers.ValidationError(
                {'consent': 'Consent is required to submit an application.'}
            )
        return attrs
