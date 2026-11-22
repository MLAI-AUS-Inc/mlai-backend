"""Durable cosmetic preferences; contribution authority remains in Volunteer."""

import uuid

from django.conf import settings
from django.db import models

from .policy import choices


class CommunityRooAvatar(models.Model):
    """One outfit per canonical account and trusted community, across devices."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="roo_avatars",
    )
    community = models.CharField(max_length=255)
    fur = models.CharField(max_length=32, choices=choices("fur"), null=True, blank=True)
    hair = models.CharField(
        max_length=32, choices=choices("hair"), null=True, blank=True
    )
    glasses = models.CharField(
        max_length=32, choices=choices("glasses"), null=True, blank=True
    )
    jacket = models.CharField(
        max_length=32, choices=choices("jacket"), null=True, blank=True
    )
    shoes = models.CharField(
        max_length=32, choices=choices("shoes"), null=True, blank=True
    )
    owned_items = models.JSONField(default=list, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("user", "community"), name="roo_avatar_account_community"
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1), name="roo_avatar_version_positive"
            ),
        ]
