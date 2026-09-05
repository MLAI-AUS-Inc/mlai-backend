from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenRefreshSerializer
from django.contrib.auth import get_user_model
from .refresh_sessions import (
    add_auth_version_claim,
    add_refresh_session_claim,
    ensure_refresh_session_active,
    ensure_token_auth_version,
)
from .user_compat import get_compat_user_role

User = get_user_model()

class MyTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        add_refresh_session_claim(token)
        add_auth_version_claim(token, user)
        token['role'] = get_compat_user_role(user)
        return token


class RevocableTokenRefreshSerializer(TokenRefreshSerializer):
    """Reject every rotated token in a family after explicit logout."""

    def validate(self, attrs):
        refresh = self.token_class(attrs['refresh'])
        ensure_refresh_session_active(refresh)
        ensure_token_auth_version(refresh)
        return super().validate(attrs)

class RegisterSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['email', 'first_name', 'last_name']
        extra_kwargs = {
            'email': {'required': True},
            'first_name': {'required': False},
            'last_name': {'required': False},
        } 

    def create(self, validated_data):
        email = validated_data['email']
        first_name = validated_data.get('first_name', '')
        last_name = validated_data.get('last_name', '')
        user, created = User.objects.get_or_create(email=email)
        if created:
            user.first_name = first_name
            user.last_name = last_name
            user.is_active = False
            user.save()
        return user

from .models import Hackathon

class HackathonSerializer(serializers.ModelSerializer):
    class Meta:
        model = Hackathon
        fields = ['name', 'slug', 'description', 'start_date', 'end_date', 'bg_image_url']

class UserSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()
    team_avatar = serializers.FileField(write_only=True, required=False)

    def get_role(self, obj):
        return get_compat_user_role(obj)
    
    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'full_name', 'phone', 'about', 'role', 'avatar_url', 'is_superuser', 'team_avatar']
        read_only_fields = ['email', 'role', 'is_superuser']

# Compatibility imports for one release after the Content Factory app split.
from content_factory.serializers import (
    AISaturationSerializer,
    ClusterBulkUpsertSerializer,
    ComponentMappingSerializer,
    ContentFactoryHealingRecordSerializer,
    GeneratedComponentListSerializer,
    GeneratedComponentSerializer,
    KeywordBulkUpsertSerializer,
    KeywordStatusUpdateSerializer,
    KeywordVelocitySerializer,
    PAQuestionSerializer,
    ResearchFeedbackSerializer,
    ResearchedKeywordDetailSerializer,
    ResearchedKeywordListSerializer,
    SEODashboardSerializer,
    SemanticClusterSerializer,
    WrittenArticleCreateSerializer,
    WrittenArticleSerializer,
)
from workflow_runs.serializers import (
    ContentFactoryRunAttemptSerializer,
    ContentFactoryRunControlSerializer,
    ContentFactoryRunStepSerializer,
    ContentFactoryRunSyncSerializer,
    ContentFactoryRunValleyJobSerializer,
)
