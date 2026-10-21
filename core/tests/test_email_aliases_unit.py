"""Identity routing tests: no database construction, mail or external calls."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.db import IntegrityError
from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from core import email_aliases, password_auth
from core.models import User, UserEmailAlias
from core.views import CheckUserView, CreateUserView, SendMagicLinkView
from core.slack_users import ensure_slack_user


class EmailAliasAdminTests(SimpleTestCase):
    def setUp(self):
        from django.contrib import admin
        from core.admin import UserAdmin

        self.request = APIRequestFactory().get('/admin/core/user/add/')
        self.request.user = User(pk=1, email='operator@example.test',
            is_active=True, is_staff=True, is_superuser=True)
        self.model_admin = UserAdmin(User, admin.site)

    def test_add_user_does_not_require_an_alias_inline_formset(self):
        self.assertEqual(list(self.model_admin.get_formsets_with_inlines(self.request, obj=None)), [])

    def test_existing_user_retains_read_only_alias_history(self):
        user = User(pk=2, email='existing@example.test')
        inlines = self.model_admin.get_inline_instances(self.request, obj=user)
        self.assertEqual(len(inlines), 1)
        self.assertIs(inlines[0].model, UserEmailAlias)
        self.assertFalse(inlines[0].has_add_permission(self.request, user))
        self.assertFalse(inlines[0].can_delete)
        self.assertEqual(inlines[0].get_readonly_fields(self.request, user),
            ('email', 'verified_at', 'created_at'))


class EmailAliasUnitTests(SimpleTestCase):
    def setUp(self):
        self.user = User(pk=648, email='primary@example.com', is_active=True, slack_id='U1234567890')
        self.factory = APIRequestFactory()

    def test_lookup_normalizes_and_combines_primary_and_alias(self):
        with patch.object(User.objects, 'filter') as query:
            User.objects.for_email('  ALT@EXAMPLE.COM ')
        predicate = query.call_args.args[0]
        self.assertEqual(predicate.children[0], ('email__iexact', 'alt@example.com'))
        alias_query = predicate.children[1][1].query
        self.assertEqual(alias_query.model._meta.db_table, 'core_useremailalias')
        self.assertEqual(alias_query.where.children[0].rhs, 'alt@example.com')

    def test_natural_key_resolves_alias(self):
        with patch.object(User.objects, 'for_email') as query:
            query.return_value.get.return_value = self.user
            self.assertIs(User.objects.get_by_natural_key('ALT@example.com'), self.user)
            query.assert_called_once_with('ALT@example.com')

    def test_password_alias_authenticates_canonical_email(self):
        with patch.object(User.objects, 'resolve_email', return_value=self.user), patch.object(User, 'has_usable_password', return_value=True), patch.object(password_auth, 'authenticate', return_value=self.user) as auth:
            self.assertIs(password_auth.authenticate_account(None, 'alt@example.com', 'synthetic-password'), self.user)
            auth.assert_called_once_with(request=None, email=self.user.email, password='synthetic-password')

    def test_inactive_alias_does_not_authenticate(self):
        self.user.is_active = False
        with patch.object(User.objects, 'resolve_email', return_value=self.user), patch.object(User, 'has_usable_password', return_value=True), patch.object(User, 'check_password'), patch.object(password_auth, 'authenticate') as auth:
            self.assertIsNone(password_auth.authenticate_account(None, 'alt@example.com', 'synthetic'))
            auth.assert_not_called()

    def test_missing_alias_uses_timing_padding(self):
        with patch.object(User.objects, 'resolve_email', return_value=None), patch.object(password_auth, 'check_password') as check, patch.object(password_auth, 'authenticate') as auth:
            self.assertIsNone(password_auth.authenticate_account(None, 'missing@example.com', 'synthetic'))
            check.assert_called_once()
            auth.assert_not_called()

    def test_primary_cannot_reclaim_alias(self):
        with patch.object(email_aliases, 'lock_email_namespace'), patch.object(UserEmailAlias.objects, 'using') as aliases:
            aliases.return_value.filter.return_value.exists.return_value = True
            with self.assertRaises(IntegrityError):
                email_aliases.guard_primary_email(self.user)

    def test_alias_cannot_reclaim_primary(self):
        alias = UserEmailAlias(email='taken@example.com', user=self.user)
        with patch.object(email_aliases, 'lock_email_namespace'), patch.object(User.objects, 'using') as users:
            users.return_value.filter.return_value.exists.return_value = True
            with self.assertRaises(IntegrityError):
                email_aliases.guard_alias_email(alias)

    def test_placeholder_cannot_be_marked_verified(self):
        alias = UserEmailAlias(email='fake@slack.placeholder.com', user=self.user, verified_at='synthetic')
        with patch.object(email_aliases, 'lock_email_namespace'), patch.object(User.objects, 'using') as users:
            users.return_value.filter.return_value.exists.return_value = False
            with self.assertRaises(IntegrityError):
                email_aliases.guard_alias_email(alias)

    def test_shared_postgres_lock_is_stable_and_parameterized(self):
        connection = MagicMock(vendor='postgresql')
        with patch.object(email_aliases, 'connections', {'default': connection}):
            email_aliases.lock_email_namespace('alt@example.com')
            email_aliases.lock_email_namespace('alt@example.com')
        calls = connection.cursor.return_value.__enter__.return_value.execute.call_args_list
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0].args[0], 'SELECT pg_advisory_xact_lock(%s)')
        self.assertIsInstance(calls[0].args[1][0], int)

    def test_signup_existing_alias_never_creates_user(self):
        with patch('django.db.transaction.atomic', side_effect=lambda **kw: nullcontext()), patch.object(email_aliases, 'lock_email_namespace'), patch.object(User.objects, 'resolve_email', return_value=self.user), patch.object(User.objects, 'create') as create:
            user, created = User.objects.get_or_create_for_email('ALT@example.com')
            self.assertIs(user, self.user)
            self.assertFalse(created)
            create.assert_not_called()

    def test_check_user_reports_alias_exists(self):
        request = self.factory.post('/', {'email': 'alt@example.com', 'app': 'vibe-raising'})
        with patch.object(User.objects, 'resolve_email', return_value=self.user):
            response = CheckUserView().post(request=SimpleNamespace(data=request.POST))
        self.assertEqual(response.data, {'user_exists': True})

    def test_magic_link_alias_uses_primary_destination_and_identity(self):
        request = SimpleNamespace(data={'email': 'fake@slack.placeholder.com', 'app': 'vibe-raising'})
        with patch.object(User.objects, 'resolve_email', return_value=self.user), patch('core.views.generate_magic_link', return_value='https://example.com/synthetic') as generate, patch('core.views.send_magic_link_email') as send:
            response = SendMagicLinkView().post(request)
        self.assertEqual(response.status_code, 200)
        self.assertIs(generate.call_args.args[0], self.user)
        self.assertIs(send.call_args.args[0], self.user)
        self.assertEqual(send.call_args.args[0].email, 'primary@example.com')

    def test_public_signup_rejects_existing_alias(self):
        request = SimpleNamespace(data={'email': 'alt@example.com', 'app': 'vibe-raising'})
        with patch('django.db.transaction.atomic', side_effect=lambda **kw: nullcontext()), patch.object(User.objects, 'for_email') as lookup, patch.object(User.objects, 'create_user') as create:
            lookup.return_value.exists.return_value = True
            response = CreateUserView().post(request)
        self.assertEqual(response.status_code, 400)
        create.assert_not_called()

    def test_slack_alias_keeps_selected_identity_and_prevents_placeholder(self):
        fake = MagicMock(email='primary@example.com', slack_id='U1234567890', is_active=True)
        fake.email_aliases.filter.return_value.exists.return_value = True
        with patch('django.db.transaction.atomic', side_effect=lambda **kw: nullcontext()), patch.object(User.objects, 'select_for_update') as locked, patch.object(User.objects, 'resolve_email', return_value=fake), patch.object(User.objects, 'create_user') as create:
            locked.return_value.filter.return_value.first.return_value = None
            result = ensure_slack_user(slack_id='U0000000000', email='alt@example.com')
        self.assertIs(result.user, fake)
        self.assertEqual(fake.slack_id, 'U1234567890')
        self.assertFalse(result.created)
        create.assert_not_called()
