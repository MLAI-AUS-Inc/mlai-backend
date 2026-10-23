"""Signed-in clients and synthetic contacts for the contacts API database tests."""

from datetime import timedelta

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from community_chat.account_sessions import issue_account_session
from community_chat.models import ChatRole, CommunityChatDevice, CommunityChatEmailCodeChallenge
from core.models import AccountBan, User
from crm.models import Contact, CrmEvent, EventRegistration

PREFIX = "/api/v1/community-chat/crm/"

crm_enabled = override_settings(
    COMMUNITY_CHAT_CRM_ENABLED=True,
    CRM_LUMA_SYNC_ENABLED=False,
    CRM_SMS_ALLOWED_PREFIXES=["+614"],
)


class CrmApiMixin:
    """An admin and a member, each signed in on their own verified device."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.admin = self.chat_user("admin@example.test", "a", role="admin")
        self.member = self.chat_user("member@example.test", "b")
        self.client = self.account_client(self.admin)
        self.member_client = self.account_client(self.member)

    def chat_user(self, email, key, role=None):
        user = User.objects.create_user(email)
        if role:
            ChatRole.objects.create(user=user, role=role)
        CommunityChatDevice.objects.create(user=user, public_key=key * 64, status="verified")
        return user

    def account_client(self, user):
        device = CommunityChatDevice.objects.get(user=user)
        challenge = CommunityChatEmailCodeChallenge.objects.create(
            user=user,
            email_digest="c" * 64,
            code_digest="d" * 64,
            public_key=device.public_key,
            client_id="mlai-chat-ios",
            installation_id=device.installation_id,
            origin="mlaichat://callback",
            platform="ios",
            device_name="Synthetic test phone",
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        credentials = issue_account_session(user, challenge)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {credentials.access_token}")
        return client

    def ids(self, rows, key="id"):
        return {row[key] for row in rows}


def make_contact(email, **fields):
    fields.setdefault("source", Contact.Source.LUMA)
    return Contact.objects.create(email=email, **fields)


def make_event(luma_event_id, *, start, name=None, hours=2):
    return CrmEvent.objects.create(
        luma_event_id=luma_event_id,
        name=name or luma_event_id,
        url=f"https://luma.com/{luma_event_id}",
        start_at=start,
        end_at=start + timedelta(hours=hours),
        timezone="Australia/Melbourne",
    )


def register(event, contact, status="approved", **fields):
    return EventRegistration.objects.create(
        event=event, contact=contact, approval_status=status, **fields
    )


def ban(email):
    user = User.objects.create_user(f"banned-account-{email}")
    return AccountBan.objects.create(user=user, email=email, revocation_pending=False)
