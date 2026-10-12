"""Identity grants belong to the issuer; staging mappings never contain passwords."""
import uuid
from django.conf import settings
from django.db import models


class ChatFederationGrant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client_id = models.CharField(max_length=64)
    callback_url = models.URLField(max_length=255)
    state = models.CharField(max_length=128)
    code_challenge = models.CharField(max_length=43)
    expires_at = models.DateTimeField()
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.CASCADE)
    code_hash = models.CharField(max_length=64, null=True, unique=True)
    code_expires_at = models.DateTimeField(null=True)
    consumed_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ChatFederationIdentity(models.Model):
    issuer = models.URLField(max_length=255)
    subject = models.CharField(max_length=128)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    last_checked_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=('issuer', 'subject'), name='chat_federation_identity_unique')]


class ChatFederationTransaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    cookie_hash = models.CharField(max_length=64, unique=True)
    state_hash = models.CharField(max_length=64)
    verifier = models.CharField(max_length=128)
    public_key = models.CharField(max_length=64)
    installation_id = models.UUIDField()
    client_id = models.CharField(max_length=64, default='mlai-chat-web')
    origin = models.URLField(max_length=255)
    platform = models.CharField(max_length=32, default='web')
    device_name = models.CharField(max_length=120, blank=True)
    request_id = models.UUIDField(null=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True)
