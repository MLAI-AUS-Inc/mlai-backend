from rest_framework import serializers
from .models import Team, Submission, Announcement
from django.contrib.auth import get_user_model

User = get_user_model()

class TeamSerializer(serializers.ModelSerializer):
    class Meta:
        model = Team
        fields = ['id', 'team_id', 'team_name', 'members', 'avatar_url']

class SubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Submission
        fields = ['id', 'user', 'team', 'file_url', 'score', 'submitted_at']

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
