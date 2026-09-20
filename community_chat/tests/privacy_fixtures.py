"""Synthetic, explicitly consented test accounts for existing Roo regressions."""

from django.utils import timezone
from community_chat.models import AiConsentRecord
from community_chat.privacy import ai_disclosure

PROVIDERS = [{"name": "Test provider", "purpose": "Answer Roo requests",
              "data": "Messages, attachments and conversation context", "privacy_url": "https://example.com/privacy"}]


def grant_test_ai_consent(user):
    disclosure = ai_disclosure()
    assert disclosure["available"], "Test must configure its synthetic disclosure"
    AiConsentRecord.objects.update_or_create(user=user, purpose="roo_chat", defaults={
        "disclosure_version": disclosure["version"], "provider_digest": disclosure["provider_digest"],
        "granted_at": timezone.now(), "withdrawn_at": None,
    })
