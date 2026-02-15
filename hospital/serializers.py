# serializers.py
import logging
import uuid
from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import Team, Submission, Announcement

User = get_user_model()

# Auth serializers have been moved to core/serializers.py

class TeamSerializer(serializers.ModelSerializer):
    code = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = Team
        fields = ['id', 'team_id', 'code', 'team_name', 'members', 'member_count']

    def get_code(self, obj):
        if obj.team_id is None:
            return None
        return f"TEAM{obj.team_id}"

    def get_member_count(self, obj):
        return obj.members.count()

class SubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Submission
        fields = ['id', 'user', 'team', 'participant_name', 'score', 'accuracy', 'submitted_at']

class AuthorSerializer(serializers.ModelSerializer):
    imageUrl = serializers.SerializerMethodField()
    href = serializers.SerializerMethodField()
    name = serializers.CharField(source='full_name')

    class Meta:
        model = User
        fields = ['name', 'imageUrl', 'href']

    def get_imageUrl(self, obj):
        if obj.avatar_url:
            return obj.avatar_url
        return "https://ui-avatars.com/api/?name=" + (obj.full_name or obj.email)

    def get_href(self, obj):
        return f"/users/{obj.id}"

class AnnouncementSerializer(serializers.ModelSerializer):
    author = AuthorSerializer(read_only=True)
    date = serializers.SerializerMethodField()
    datetime = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = Announcement
        fields = ['id', 'title', 'body', 'date', 'datetime', 'author']

    def get_date(self, obj):
        return obj.created_at.strftime("%Y-%m-%d %H:%M")
