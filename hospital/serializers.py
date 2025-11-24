# serializers.py
import logging
import uuid
from rest_framework import serializers
from django.contrib.auth import get_user_model

User = get_user_model()

# Auth serializers have been moved to core/serializers.py