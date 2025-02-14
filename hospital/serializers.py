# serializers.py
import logging
import uuid
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from .models import User


class MyTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token['role'] = user.role
        return token

class RegisterSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['email', 'full_name', 'is_account_manager']
        extra_kwargs = {
            'email': {'required': True},
            'full_name': {'required': False},
            'is_account_manager': {'default': False},
        }

    def create(self, validated_data):
        email = validated_data['email']
        full_name = validated_data.get('full_name', '')
        user, created = User.objects.get_or_create(email=email)
        if created:
            user.full_name = full_name
            user.is_active = False
            user.save()
        return user