from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase

from .admin import PointsAdminAdmin
from .models import PointsAdmin


User = get_user_model()


class PointsAdminAdminTests(TestCase):
    def setUp(self):
        self.model_admin = PointsAdminAdmin(PointsAdmin, admin.site)

    def test_admin_name_uses_linked_user_full_name(self):
        user = User.objects.create_user(
            email='jane@example.com',
            first_name='Jane',
            last_name='Admin',
            slack_id='UADMINNAME',
        )
        points_admin = PointsAdmin.objects.create(
            slack_user_id='UADMINNAME',
            user=user,
            role='admin',
        )

        self.assertEqual(self.model_admin.admin_name(points_admin), 'Jane Admin')

    def test_admin_name_falls_back_to_email_when_user_has_no_name(self):
        user = User.objects.create_user(
            email='nameless@example.com',
            slack_id='UNAMELESS',
        )
        points_admin = PointsAdmin.objects.create(
            slack_user_id='UNAMELESS',
            user=user,
            role='admin',
        )

        self.assertEqual(self.model_admin.admin_name(points_admin), 'nameless@example.com')

    def test_admin_name_falls_back_to_slack_id_without_user(self):
        points_admin = PointsAdmin.objects.create(
            slack_user_id='UNOLINK',
            role='admin',
        )

        self.assertEqual(self.model_admin.admin_name(points_admin), 'UNOLINK')

    def test_changelist_uses_admin_name_as_primary_link_and_keeps_existing_fields(self):
        self.assertEqual(
            self.model_admin.list_display,
            ('admin_name', 'slack_user_id', 'role', 'portfolio', 'is_active', 'created_at'),
        )
        self.assertEqual(self.model_admin.list_display_links, ('admin_name',))
