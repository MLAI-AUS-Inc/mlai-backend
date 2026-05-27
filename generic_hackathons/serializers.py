from rest_framework import serializers

from core.user_compat import DEFAULT_USER_ROLE
from .models import (
    GenericHackathonAnnouncement,
    GenericHackathonResource,
    GenericHackathonSubmission,
    GenericHackathonTeam,
)


class GenericHackathonMemberSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    email = serializers.EmailField()
    full_name = serializers.CharField()
    avatar_url = serializers.URLField(allow_null=True, required=False)
    role = serializers.CharField(default=DEFAULT_USER_ROLE)


class GenericHackathonTeamSerializer(serializers.ModelSerializer):
    code = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()
    members = serializers.SerializerMethodField()

    class Meta:
        model = GenericHackathonTeam
        fields = [
            'id',
            'team_id',
            'code',
            'team_name',
            'avatar_url',
            'member_count',
            'members',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'team_id', 'code', 'member_count', 'members', 'created_at', 'updated_at']

    def get_code(self, obj):
        return obj.code

    def get_member_count(self, obj):
        return obj.members.count()

    def get_members(self, obj):
        return [
            {
                'id': member.id,
                'email': member.email,
                'full_name': member.full_name,
                'avatar_url': member.avatar_url,
                'role': DEFAULT_USER_ROLE,
            }
            for member in obj.members.all()
        ]


class GenericHackathonSubmissionSerializer(serializers.ModelSerializer):
    team = GenericHackathonTeamSerializer(read_only=True)
    submitted_by = serializers.SerializerMethodField()
    submitted_at = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = GenericHackathonSubmission
        fields = [
            'id',
            'title',
            'summary',
            'repository_url',
            'demo_url',
            'slides_url',
            'attachment_url',
            'attachment_name',
            'attachment_content_type',
            'attachment_size',
            'team',
            'submitted_by',
            'submitted_at',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'attachment_url',
            'attachment_name',
            'attachment_content_type',
            'attachment_size',
            'team',
            'submitted_by',
            'submitted_at',
            'created_at',
            'updated_at',
        ]

    def get_submitted_by(self, obj):
        return {
            'id': obj.user_id,
            'email': obj.user.email,
            'full_name': obj.user.full_name,
            'avatar_url': obj.user.avatar_url,
        }


class GenericHackathonAnnouncementSerializer(serializers.ModelSerializer):
    author = serializers.SerializerMethodField()
    date = serializers.SerializerMethodField()
    datetime = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = GenericHackathonAnnouncement
        fields = ['id', 'title', 'body', 'date', 'datetime', 'author']

    def get_author(self, obj):
        if obj.author is None:
            return {
                'name': 'MLAI',
                'imageUrl': 'https://ui-avatars.com/api/?name=MLAI',
                'href': '/',
            }
        return {
            'name': obj.author.full_name or obj.author.email,
            'imageUrl': obj.author.avatar_url or f"https://ui-avatars.com/api/?name={obj.author.full_name or obj.author.email}",
            'href': f"/users/{obj.author_id}",
        }

    def get_date(self, obj):
        return obj.created_at.strftime("%Y-%m-%d %H:%M")


class GenericHackathonResourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = GenericHackathonResource
        fields = [
            'id',
            'title',
            'summary',
            'body',
            'url',
            'category',
            'order',
        ]
