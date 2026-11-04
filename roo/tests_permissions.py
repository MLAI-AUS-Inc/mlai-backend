from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.test import TestCase

from .models import PointsAdmin
from .permissions import is_mlai_admin_user, is_points_admin_user, mlai_admin_role

User = get_user_model()


class MlaiAdminRoleTests(TestCase):
    """Only linked, active admin and committee rows (or superusers) open MLAI Chat's Admin area."""

    def linked(self, role, *, active=True, user_active=True):
        slack_id = f'UMLAI{role.upper()}{int(active)}{int(user_active)}'
        user = User.objects.create_user(email=f'{slack_id.lower()}@example.com', slack_id=slack_id)
        if not user_active:
            user.is_active = False
            user.save(update_fields=['is_active'])
        PointsAdmin.objects.create(slack_user_id=slack_id, user=user, role=role, is_active=active)
        return user

    def test_admin_and_committee_rows_count(self):
        for role in ('admin', 'committee'):
            with self.subTest(role=role):
                user = self.linked(role)
                self.assertEqual(mlai_admin_role(user), role)
                self.assertTrue(is_mlai_admin_user(user))

    def test_superusers_count(self):
        user = User.objects.create_user(email='super-mlai@example.com')
        user.is_superuser = True
        user.save(update_fields=['is_superuser'])
        self.assertEqual(mlai_admin_role(user), 'superuser')

    def test_portfolio_leads_and_partners_do_not(self):
        for role in ('portfolio_lead', 'partner'):
            with self.subTest(role=role):
                self.assertIsNone(mlai_admin_role(self.linked(role)))

    def test_an_inactive_row_does_not(self):
        self.assertIsNone(mlai_admin_role(self.linked('committee', active=False)))

    def test_a_row_without_a_linked_account_does_not(self):
        PointsAdmin.objects.create(slack_user_id='UMLAIUNLINKED', role='committee', is_active=True)
        user = User.objects.create_user(email='unlinked-mlai@example.com', slack_id='UMLAIUNLINKED')
        self.assertIsNone(mlai_admin_role(user))

    def test_an_inactive_account_does_not(self):
        self.assertIsNone(mlai_admin_role(self.linked('admin', user_active=False)))

    def test_anonymous_does_not(self):
        self.assertIsNone(mlai_admin_role(None))
        self.assertIsNone(mlai_admin_role(AnonymousUser()))
        self.assertFalse(is_mlai_admin_user(AnonymousUser()))

    def test_bootstrap_slack_ids_do_not(self):
        user = User.objects.create_user(email='bootstrap-mlai@example.com', slack_id='UBOOTSTRAPMLAI')
        with self.settings(POINTS_BOOTSTRAP_ADMIN_SLACK_IDS=['UBOOTSTRAPMLAI']):
            self.assertIsNone(mlai_admin_role(user))


class IsPointsAdminUserTests(TestCase):
    def test_none_is_not_admin(self):
        self.assertFalse(is_points_admin_user(None))

    def test_plain_user_is_not_admin(self):
        user = User.objects.create_user(email='plain@example.com')
        self.assertFalse(is_points_admin_user(user))

    def test_superuser_is_always_admin(self):
        user = User.objects.create_user(email='super@example.com')
        user.is_superuser = True
        user.save(update_fields=['is_superuser'])
        self.assertTrue(is_points_admin_user(user))

    def test_linked_active_full_admin_is_admin(self):
        for role in ('admin', 'committee', 'portfolio_lead'):
            with self.subTest(role=role):
                slack_id = f'UFULL{role.upper()}'
                user = User.objects.create_user(email=f'{role}@example.com', slack_id=slack_id)
                PointsAdmin.objects.create(
                    slack_user_id=slack_id, user=user, role=role, is_active=True,
                )
                self.assertTrue(is_points_admin_user(user))

    def test_partner_role_is_not_full_admin(self):
        user = User.objects.create_user(email='partner@example.com', slack_id='UPARTNER')
        PointsAdmin.objects.create(
            slack_user_id='UPARTNER', user=user, role='partner', is_active=True,
        )
        self.assertFalse(is_points_admin_user(user))

    def test_inactive_admin_is_not_admin(self):
        user = User.objects.create_user(email='inactive@example.com', slack_id='UINACTIVE')
        PointsAdmin.objects.create(
            slack_user_id='UINACTIVE', user=user, role='admin', is_active=False,
        )
        self.assertFalse(is_points_admin_user(user))

    def test_unlinked_points_admin_does_not_grant_admin(self):
        # A PointsAdmin row whose user FK is null must not grant admin to a user
        # who merely shares the Slack ID -- the FK has to be backfilled first.
        PointsAdmin.objects.create(slack_user_id='UUNLINKED', role='admin', is_active=True)
        user = User.objects.create_user(email='someone@example.com', slack_id='UUNLINKED')
        self.assertFalse(is_points_admin_user(user))


class LinkPointsAdminsToUsersCommandTests(TestCase):
    def test_links_points_admin_to_user_by_slack_id(self):
        user = User.objects.create_user(email='link@example.com', slack_id='ULINK')
        points_admin = PointsAdmin.objects.create(
            slack_user_id='ULINK', role='admin', is_active=True,
        )
        self.assertIsNone(points_admin.user)

        call_command('link_points_admins_to_users', stdout=StringIO())

        points_admin.refresh_from_db()
        self.assertEqual(points_admin.user, user)

    def test_dry_run_does_not_write(self):
        User.objects.create_user(email='dry@example.com', slack_id='UDRY')
        points_admin = PointsAdmin.objects.create(
            slack_user_id='UDRY', role='admin', is_active=True,
        )

        call_command('link_points_admins_to_users', '--dry-run', stdout=StringIO())

        points_admin.refresh_from_db()
        self.assertIsNone(points_admin.user)

    def test_skips_rows_without_a_matching_user(self):
        points_admin = PointsAdmin.objects.create(
            slack_user_id='UNOMATCH', role='admin', is_active=True,
        )

        call_command('link_points_admins_to_users', stdout=StringIO())

        points_admin.refresh_from_db()
        self.assertIsNone(points_admin.user)
