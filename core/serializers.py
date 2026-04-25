from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth import get_user_model

User = get_user_model()

class MyTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token['role'] = user.role
        return token

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
    team_avatar = serializers.FileField(write_only=True, required=False)
    
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
    ResearchSessionSerializer,
    ResearchedKeywordDetailSerializer,
    ResearchedKeywordListSerializer,
    SEODashboardSerializer,
    SemanticClusterSerializer,
    TopicMapSerializer,
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
