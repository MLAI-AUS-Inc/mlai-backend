from rest_framework import serializers
from .models import Team, Submission
from django.contrib.auth import get_user_model

User = get_user_model()

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'email', 'full_name']

class TeamSerializer(serializers.ModelSerializer):
    members = UserSerializer(many=True, read_only=True)

    class Meta:
        model = Team
        fields = ['team_id', 'team_name', 'members']

class SubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Submission
        fields = ['id', 'user', 'team', 'file_url', 'score', 'submitted_at']
        read_only_fields = ['user', 'team', 'score', 'submitted_at']
