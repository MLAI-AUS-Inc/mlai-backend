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


from .models import GeneratedComponent, ComponentMapping


class GeneratedComponentSerializer(serializers.ModelSerializer):
    """Serializer for individual generated components."""
    
    class Meta:
        model = GeneratedComponent
        fields = [
            'id', 'name', 'content', 'source', 'original_path',
            'similarity_score', 'matched_component', 'adaptation_notes',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class GeneratedComponentListSerializer(serializers.ModelSerializer):
    """Lighter serializer for component listings (without full content)."""
    
    class Meta:
        model = GeneratedComponent
        fields = ['name', 'source', 'similarity_score', 'updated_at']


class ComponentMappingSerializer(serializers.ModelSerializer):
    """Serializer for component mapping summary."""
    
    class Meta:
        model = ComponentMapping
        fields = [
            'mapping_data', 'total_components', 'matched_count', 'generated_count',
            'generation_status', 'design_guide_path', 'storage_local_path',
            'storage_pr_url', 'storage_branch_url', 'failed_components',
            'last_scan_commit', 'last_scan_at'
        ]
        read_only_fields = ['last_scan_at']
