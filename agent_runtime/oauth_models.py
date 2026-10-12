"""One-use OAuth codes and client-bound refresh families; hashes only."""
import uuid
from django.db import models


class AgentOAuthCode(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code_hash = models.CharField(max_length=64, unique=True)
    grant = models.ForeignKey("agent_runtime.AgentGrant", on_delete=models.PROTECT)
    generation = models.PositiveIntegerField()
    client_id = models.CharField(max_length=255)
    redirect_uri = models.CharField(max_length=2000)
    code_challenge = models.CharField(max_length=43)
    audience = models.CharField(max_length=500)
    scopes = models.JSONField(default=list)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class AgentOAuthBinding(models.Model):
    credential = models.OneToOneField("agent_runtime.AgentCredential", primary_key=True,
                                     on_delete=models.CASCADE, related_name="oauth_binding")
    client_id = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
