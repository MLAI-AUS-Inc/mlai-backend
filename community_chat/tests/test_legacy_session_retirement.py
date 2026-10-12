from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
import uuid

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from community_chat.account_sessions import issue_account_session
from community_chat.models import CommunityChatAccountSession


class LegacySessionRetirementTests(TestCase):
    def test_dry_run_then_cutoff_preserves_new_and_native_sessions(self):
        user = get_user_model().objects.create_user(email='retire@example.test')
        def session(client):
            return issue_account_session(user, SimpleNamespace(client_id=client,
                installation_id=uuid.uuid4(), public_key='a'*64, origin='https://chat.mlai.au',
                platform='web', device_name='Test')).session
        old, new, native = session('mlai-chat-web'), session('mlai-chat-web'), session('mlai-chat-ios')
        cutoff = timezone.now() - timedelta(minutes=1)
        CommunityChatAccountSession.objects.filter(pk__in=[old.pk, native.pk]).update(created_at=cutoff-timedelta(minutes=1))
        output = StringIO()
        call_command('retire_legacy_chat_sessions', before=cutoff.isoformat(), stdout=output)
        old.refresh_from_db()
        self.assertIsNone(old.revoked_at)
        self.assertIn('1', output.getvalue())
        call_command('retire_legacy_chat_sessions', before=cutoff.isoformat(), apply=True, stdout=StringIO())
        for obj in (old, new, native): obj.refresh_from_db()
        self.assertIsNotNone(old.revoked_at)
        self.assertIsNone(new.revoked_at)
        self.assertIsNone(native.revoked_at)
