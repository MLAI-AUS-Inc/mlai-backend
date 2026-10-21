"""Run only with the explicitly approved disposable migration inventory."""

from io import StringIO
from unittest.mock import patch

from django.contrib.auth import authenticate
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from core.models import User, UserEmailAlias
from core.password_auth import authenticate_account
from core.views import CheckUserView, CreateUserView
from roo.models import Ledger, PointsAccount, CoworkingBooking, CoworkingBookingOperation


class EmailAliasDatabaseTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email='primary@example.com', password='Synthetic-test-password-786')
        UserEmailAlias.objects.create(user=self.user, email='alt@example.com', verified_at=timezone.now())
        UserEmailAlias.objects.create(user=self.user, email='fake@slack.placeholder.com')

    def test_all_identifiers_resolve_and_authenticate_same_user(self):
        for email in ['primary@example.com', 'ALT@EXAMPLE.COM', 'fake@slack.placeholder.com']:
            with self.subTest(email=email):
                self.assertEqual(User.objects.resolve_email(email).pk, self.user.pk)
                self.assertEqual(authenticate(email=email, password='Synthetic-test-password-786').pk, self.user.pk)
                self.assertEqual(authenticate_account(None, email, 'Synthetic-test-password-786').pk, self.user.pk)

    def test_inactive_account_cannot_authenticate_via_alias(self):
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        self.assertIsNone(authenticate_account(None, 'alt@example.com', 'Synthetic-test-password-786'))

    def test_alias_uniqueness_and_primary_collisions(self):
        other = User.objects.create_user(email='other@example.com')
        for operation in [
            lambda: User.objects.create_user(email='ALT@EXAMPLE.COM'),
            lambda: UserEmailAlias.objects.create(user=other, email='PRIMARY@EXAMPLE.COM'),
            lambda: UserEmailAlias.objects.create(user=other, email='ALT@EXAMPLE.COM'),
        ]:
            with self.assertRaises(IntegrityError), transaction.atomic():
                operation()
        other.email = 'alt@example.com'
        with self.assertRaises(IntegrityError), transaction.atomic():
            other.save()

    def test_public_check_and_signup_cannot_create_duplicate(self):
        factory = APIRequestFactory()
        for email in ['ALT@EXAMPLE.COM', 'fake@slack.placeholder.com']:
            response = CheckUserView.as_view()(factory.post('/', {'email': email, 'app': 'founder-tools'}, format='json'))
            self.assertTrue(response.data['user_exists'])
            response = CreateUserView.as_view()(factory.post('/', {'email': email, 'app': 'founder-tools'}, format='json'))
            self.assertEqual(response.status_code, 400)
        self.assertEqual(User.objects.count(), 1)


class AccountMergeDatabaseTests(TestCase):
    def setUp(self):
        self.actor = User.objects.create_superuser(email='operator@example.com', password='synthetic-operator')
        self.target = User.objects.create_user(email='primary@example.com', password='synthetic-primary', slack_id='mlai_user:999')
        self.source = User.objects.create_user(email='alt@example.com', password='synthetic-old', slack_id='U1111111111', email_verified_at=timezone.now())
        self.placeholder = User.objects.create_user(email='bad@slack.placeholder.com', slack_id='bad')
        self.source_ids = [self.source.pk, self.placeholder.pk]
        self.ledger_ids = set()
        for user, amount in [(self.target, 8), (self.source, 84), (self.placeholder, 108)]:
            PointsAccount.objects.create(user=user, balance=amount, earned_balance=amount, lifetime_earned=amount,
                balance_microroo=amount*1_000_000, earned_balance_microroo=amount*1_000_000,
                lifetime_earned_microroo=amount*1_000_000, microroo_initialized=True)
            self.ledger_ids.add(Ledger.objects.create(user=user, delta=amount, delta_microroo=amount*1_000_000,
                kind='EARN', source='MANUAL', idempotency_key=f'seed-{user.pk}').pk)

    def merge(self, *, commit=False, **kwargs):
        import tempfile
        with tempfile.TemporaryDirectory() as backup:
            with patch('community_chat.adapter.revoke_relay_membership', return_value=('revoked', {})), patch('community_chat.adapter.revoke_member_invite'):
                call_command('merge_accounts_with_aliases',
                    '--source-email', self.source.email, '--source-email', self.placeholder.email,
                    target_email=self.target.email, slack_id='U2222222222', balance=8,
                    actor_email=self.actor.email, repair_key='synthetic-merge', backup_dir=backup,
                    commit=commit, stdout=StringIO(), **kwargs)

    def test_dry_run_rolls_back_every_database_change(self):
        self.merge()
        self.assertEqual(User.objects.filter(pk__in=self.source_ids).count(), 2)
        self.assertFalse(UserEmailAlias.objects.exists())
        self.assertEqual(Ledger.objects.count(), 3)
        self.target.refresh_from_db()
        self.assertEqual(self.target.slack_id, 'mlai_user:999')

    def test_commit_preserves_ledger_books_and_aliases_with_exact_balance(self):
        from datetime import date
        import uuid
        booking = CoworkingBooking.objects.create(user=self.source, date=date(2026, 10, 1), points_cost=8)
        receipt = CoworkingBookingOperation.objects.create(id=uuid.uuid4(), kind='single', request_fingerprint='synthetic', response_payload={'booking_id': str(booking.pk)})
        receipt.subjects.add(self.source)
        self.merge(commit=True)
        self.target.refresh_from_db()
        self.assertEqual(self.target.slack_id, 'U2222222222')
        self.assertFalse(User.objects.filter(pk__in=self.source_ids).exists())
        self.assertEqual(UserEmailAlias.objects.filter(user=self.target).count(), 2)
        self.assertEqual(PointsAccount.objects.get(user=self.target).balance_microroo, 8_000_000)
        self.assertEqual(Ledger.objects.filter(user=self.target).count(), 4)
        self.assertEqual(set(Ledger.objects.filter(pk__in=self.ledger_ids, user=self.target).values_list('pk', flat=True)), self.ledger_ids)
        self.assertEqual(Ledger.objects.get(kind='ADJUST').delta_microroo, -192_000_000)
        booking.refresh_from_db()
        self.assertEqual(booking.user_id, self.target.pk)
        self.assertEqual(list(receipt.subjects.values_list('pk', flat=True)), [self.target.pk])
        self.merge(commit=True)
        self.assertEqual(Ledger.objects.count(), 4)
        self.assertIsNone(authenticate(email=self.source.email, password='synthetic-old'))
        self.assertEqual(authenticate(email=self.source.email, password='synthetic-primary').pk, self.target.pk)

    def test_unreviewed_source_relation_aborts_without_changes(self):
        from core.models import PasswordResetChallenge
        from datetime import timedelta
        PasswordResetChallenge.objects.create(user=self.source, secret_hash='synthetic', expires_at=timezone.now()+timedelta(hours=1))
        with self.assertRaisesMessage(CommandError, 'Unreviewed source relation'):
            self.merge(commit=True)
        self.assertTrue(User.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(Ledger.objects.count(), 3)

    def test_commit_revokes_source_device_and_session_but_preserves_primary(self):
        from datetime import timedelta
        from community_chat.models import CommunityChatDevice, CommunityChatAccountSession
        devices = []
        sessions = []
        for index, user in enumerate([self.target, self.source]):
            device = CommunityChatDevice.objects.create(user=user, public_key=str(index+1)*64,
                status='verified', verified_at=timezone.now())
            devices.append(device)
            sessions.append(CommunityChatAccountSession.objects.create(user=user,
                public_key=device.public_key, installation_id=device.installation_id,
                client_id='synthetic', origin='https://example.test', platform='web',
                access_token_hash=str(index+1)*64, refresh_token_hash=str(index+3)*64,
                auth_version=user.auth_version, access_expires_at=timezone.now()+timedelta(hours=1),
                expires_at=timezone.now()+timedelta(days=1)))
        self.merge(commit=True)
        for device, session in zip(devices, sessions):
            device.refresh_from_db(); session.refresh_from_db()
            self.assertEqual(device.user_id, self.target.pk)
            self.assertEqual(session.user_id, self.target.pk)
        self.assertEqual(devices[0].status, 'verified')
        self.assertIsNone(sessions[0].revoked_at)
        self.assertEqual(devices[1].status, 'revoked')
        self.assertEqual(devices[1].revoked_by_id, self.actor.pk)
        self.assertIsNotNone(sessions[1].revoked_at)
