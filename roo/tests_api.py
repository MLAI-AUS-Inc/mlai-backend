from datetime import date, timedelta
import threading

from django.db import close_old_connections
from django.test import TransactionTestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from .models import ChannelFirstPost, CoworkingBooking, Ledger, PointsAccount, PointsAdmin, PointsRequest
from django.contrib.auth import get_user_model
from unittest.mock import patch
from .services import PointsService

User = get_user_model()

class ManualAwardViewTests(APITestCase):
    def setUp(self):
        self.url = reverse('manual-award')
        self.admin_slack_id = 'UADMIN123'
        self.target_slack_id = 'UTARGET456'
        
        # Create admin
        PointsAdmin.objects.create(
            slack_user_id=self.admin_slack_id,
            role='admin',
            is_active=True
        )

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_award_with_slack_user_id_success(self, mock_permission):
        """Test award works with slack_user_id parameter."""
        data = {
            'slack_user_id': self.admin_slack_id,
            'target_slack_id': self.target_slack_id,
            'points': 10,
            'reason': 'Test award'
        }
        
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['success'], True)

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_award_with_whitespace_stripped(self, mock_permission):
        """Test that whitespace is stripped from IDs."""
        data = {
            'slack_user_id': f"  {self.admin_slack_id}  ",
            'target_slack_id': f" {self.target_slack_id} ",
            'points': 10,
            'reason': 'Test whitespace'
        }
        
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['success'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_award_with_legacy_admin_slack_id(self, mock_permission):
        """Test that legacy admin_slack_id still works."""
        data = {
            'admin_slack_id': self.admin_slack_id,
            'target_slack_id': self.target_slack_id,
            'points': 10
        }
        
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['success'])


class PointsAdminManagementViewSetTests(APITestCase):
    def setUp(self):
        self.super_admin_slack_id = 'U05QPB483K9'
        self.other_slack_id = 'UNOTSUPER'
        self.target_slack_id = 'UTARGET456'
        self.list_url = reverse('points-admin-list')
        self.detail_url = reverse(
            'points-admin-detail',
            kwargs={'slack_user_id': self.target_slack_id},
        )
        self.target_user = User.objects.create_user(
            email='target@example.com',
            slack_id=self.target_slack_id,
        )

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_promote_points_admin_creates_admin(self, mock_permission):
        response = self.client.post(
            self.list_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'target_slack_id': self.target_slack_id,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        admin = PointsAdmin.objects.get(slack_user_id=self.target_slack_id)
        self.assertTrue(admin.is_active)
        self.assertEqual(admin.role, 'committee')
        self.assertEqual(admin.user, self.target_user)
        self.assertEqual(admin.added_by_slack_id, self.super_admin_slack_id)
        self.assertEqual(response.data['target_slack_id'], self.target_slack_id)
        self.assertFalse(response.data['already_admin'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_promote_points_admin_is_idempotent_when_already_active(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=True,
            added_by_slack_id='UOLDER',
        )

        response = self.client.post(
            self.list_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'target_slack_id': self.target_slack_id,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(PointsAdmin.objects.filter(slack_user_id=self.target_slack_id).count(), 1)
        self.assertTrue(response.data['already_admin'])
        self.assertFalse(response.data['created'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_promote_points_admin_reactivates_inactive_admin_and_preserves_allowance(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=False,
            added_by_slack_id='UOLDER',
            weekly_allowance=175,
        )

        response = self.client.post(
            self.list_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'target_slack_id': self.target_slack_id,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        admin = PointsAdmin.objects.get(slack_user_id=self.target_slack_id)
        self.assertTrue(admin.is_active)
        self.assertEqual(admin.weekly_allowance, 175)
        self.assertEqual(admin.added_by_slack_id, self.super_admin_slack_id)
        self.assertFalse(response.data['already_admin'])
        self.assertFalse(response.data['created'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_promote_points_admin_requires_super_admin_requester(self, mock_permission):
        response = self.client.post(
            self.list_url,
            {
                'requester_slack_id': self.other_slack_id,
                'target_slack_id': self.target_slack_id,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(PointsAdmin.objects.filter(slack_user_id=self.target_slack_id).exists())
        self.assertIn('super admin', response.data['error'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_patch_points_admin_weekly_allowance(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=True,
            weekly_allowance=100,
        )

        response = self.client.patch(
            self.detail_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'weekly_allowance': 150,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        admin = PointsAdmin.objects.get(slack_user_id=self.target_slack_id)
        self.assertEqual(admin.weekly_allowance, 150)
        self.assertEqual(response.data['target_slack_id'], self.target_slack_id)
        self.assertEqual(response.data['weekly_allowance'], 150)

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_patch_points_admin_weekly_allowance_rejects_non_positive_values(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=True,
        )

        response = self.client.patch(
            self.detail_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'weekly_allowance': 0,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['error'], 'weekly_allowance must be positive')

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_patch_points_admin_weekly_allowance_requires_existing_admin(self, mock_permission):
        response = self.client.patch(
            self.detail_url,
            {
                'requester_slack_id': self.super_admin_slack_id,
                'weekly_allowance': 150,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data['error'], 'Not a points admin')

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_delete_points_admin_revokes_active_admin(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=True,
        )

        response = self.client.delete(
            self.detail_url,
            {'requester_slack_id': self.super_admin_slack_id},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        admin = PointsAdmin.objects.get(slack_user_id=self.target_slack_id)
        self.assertFalse(admin.is_active)
        self.assertEqual(response.data['target_slack_id'], self.target_slack_id)
        self.assertTrue(response.data['revoked'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_delete_points_admin_is_idempotent_when_already_inactive(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=False,
        )

        response = self.client.delete(
            self.detail_url,
            {'requester_slack_id': self.super_admin_slack_id},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['target_slack_id'], self.target_slack_id)
        self.assertTrue(response.data['already_revoked'])
        self.assertFalse(response.data['revoked'])

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_delete_points_admin_requires_existing_admin(self, mock_permission):
        response = self.client.delete(
            self.detail_url,
            {'requester_slack_id': self.super_admin_slack_id},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data['error'], 'Not a points admin')

    @patch('core.permissions.HasRooApiKey.has_permission', return_value=True)
    def test_delete_points_admin_requires_super_admin_requester(self, mock_permission):
        PointsAdmin.objects.create(
            slack_user_id=self.target_slack_id,
            user=self.target_user,
            role='committee',
            is_active=True,
        )

        response = self.client.delete(
            self.detail_url,
            {'requester_slack_id': self.other_slack_id},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        admin = PointsAdmin.objects.get(slack_user_id=self.target_slack_id)
        self.assertTrue(admin.is_active)
        self.assertIn('super admin', response.data['error'])


class PointsRequestViewSetTests(APITestCase):
    def setUp(self):
        self.requester = User.objects.create_user(
            email='requester@example.com',
            slack_id='UREQUESTER',
        )
        self.admin = User.objects.create_user(
            email='admin@example.com',
            slack_id='UADMIN123',
        )
        PointsAdmin.objects.create(
            slack_user_id='UADMIN123',
            user=self.admin,
            role='admin',
            is_active=True,
        )
        self.list_url = reverse('points-request-list')

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_create_points_request(self, mock_permission):
        response = self.client.post(
            self.list_url,
            {
                'requester_slack_id': 'UREQUESTER',
                'target_slack_id': 'UREQUESTER',
                'points': 12,
                'reason': 'Running the 21st x MLAI event',
                'slack_channel_id': 'C123',
                'slack_thread_ts': '111.222',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        points_request = PointsRequest.objects.get()
        self.assertEqual(points_request.status, 'pending')
        self.assertEqual(points_request.points, 12)
        self.assertEqual(points_request.slack_channel_id, 'C123')

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_attach_and_lookup_points_request_by_slack_message(self, mock_permission):
        points_request = PointsRequest.objects.create(
            requester_slack_id='UREQUESTER',
            target_slack_id='UREQUESTER',
            points=12,
            reason='Running the 21st x MLAI event',
            slack_channel_id='C123',
            slack_thread_ts='111.222',
        )

        attach_url = reverse('points-request-attach-slack-summary', args=[points_request.id])
        response = self.client.patch(
            attach_url,
            {
                'slack_channel_id': 'C123',
                'slack_thread_ts': '111.222',
                'slack_summary_message_ts': '222.333',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        points_request.refresh_from_db()
        self.assertEqual(points_request.slack_summary_message_ts, '222.333')

        lookup_url = reverse('points-request-by-slack-message')
        response = self.client.get(
            lookup_url,
            {
                'slack_channel_id': 'C123',
                'slack_message_ts': '222.333',
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['id'], points_request.id)
        self.assertEqual(response.data['status'], 'pending')

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_approve_points_request_awards_points(self, mock_permission):
        points_request = PointsRequest.objects.create(
            requester_slack_id='UREQUESTER',
            target_slack_id='UREQUESTER',
            points=12,
            reason='Running the 21st x MLAI event',
            slack_channel_id='C123',
            slack_thread_ts='111.222',
            slack_summary_message_ts='222.333',
        )

        approve_url = reverse('points-request-approve', args=[points_request.id])
        response = self.client.post(
            approve_url,
            {'admin_slack_id': 'UADMIN123'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        points_request.refresh_from_db()
        self.requester.refresh_from_db()
        self.assertEqual(points_request.status, 'approved')
        self.assertEqual(points_request.approved_by_slack_id, 'UADMIN123')
        self.assertEqual(response.data['points_awarded'], 12)
        self.assertEqual(response.data['new_balance'], 12)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_approve_points_request_requires_points_admin(self, mock_permission):
        points_request = PointsRequest.objects.create(
            requester_slack_id='UREQUESTER',
            target_slack_id='UREQUESTER',
            points=12,
            reason='Running the 21st x MLAI event',
        )

        approve_url = reverse('points-request-approve', args=[points_request.id])
        response = self.client.post(
            approve_url,
            {'admin_slack_id': 'UNOTADMIN'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['error'], 'Not a points admin')


class CoworkingViewSetTests(APITestCase):
    def setUp(self):
        self.url = reverse('coworking-book')
        self.user = User.objects.create_user(
            email='coworking@example.com',
            slack_id='UCOBOOK',
        )
        PointsService.award(
            user=self.user,
            delta=10,
            source='MANUAL',
            description='Coworking setup',
            created_by_slack_id='UADMIN',
            idempotency_key='coworking_api_setup',
        )

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_book_endpoint_is_idempotent_for_existing_booking(self, mock_permission):
        booking_date = (date.today() + timedelta(days=1)).isoformat()

        first_response = self.client.post(
            self.url,
            {
                'slack_user_id': self.user.slack_id,
                'date': booking_date,
                'slack_channel_id': 'C123',
            },
            format='json',
        )
        second_response = self.client.post(
            self.url,
            {
                'slack_user_id': self.user.slack_id,
                'date': booking_date,
                'slack_channel_id': 'C123',
            },
            format='json',
        )

        self.assertEqual(first_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertTrue(second_response.data['already_booked'])
        self.assertTrue(second_response.data['idempotent'])
        self.assertEqual(
            CoworkingBooking.objects.filter(user=self.user, date=booking_date, status='booked').count(),
            1,
        )
        self.assertEqual(PointsAccount.objects.get(user=self.user).balance, 6)


class CoworkingReportViewSetTests(APITestCase):
    def setUp(self):
        self.url = reverse('coworking-report')
        self.admin_slack_id = 'UPOINTSADMIN'
        self.other_slack_id = 'UNOTADMIN'
        self.user_1 = User.objects.create_user(email='report1@example.com', slack_id='UREPORT1')
        self.user_2 = User.objects.create_user(email='report2@example.com', slack_id='UREPORT2')
        self.user_3 = User.objects.create_user(email='report3@example.com', slack_id='UREPORT3')
        PointsAdmin.objects.create(
            slack_user_id=self.admin_slack_id,
            role='admin',
            is_active=True,
        )

    def _create_booking(self, user, booking_date, status='booked'):
        return CoworkingBooking.objects.create(
            user=user,
            date=booking_date,
            status=status,
            points_cost=4,
        )

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_report_counts_active_bookings_and_includes_rollups(self, mock_permission):
        self._create_booking(self.user_1, date(2026, 1, 1))
        self._create_booking(self.user_2, date(2026, 1, 1))
        self._create_booking(self.user_1, date(2026, 1, 2), status='cancelled')
        self._create_booking(self.user_1, date(2026, 1, 5))
        self._create_booking(self.user_3, date(2026, 1, 10))
        self._create_booking(self.user_2, date(2026, 2, 1))

        response = self.client.get(
            self.url,
            {
                'slack_user_id': self.admin_slack_id,
                'start_date': '2026-01-01',
                'end_date': '2026-02-03',
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['range']['source'], 'active_coworking_bookings')
        self.assertEqual(response.data['totals']['booked_user_days'], 5)
        self.assertEqual(response.data['totals']['unique_users'], 3)
        self.assertEqual(response.data['totals']['active_days'], 4)
        self.assertEqual(response.data['totals']['range_days'], 34)
        self.assertEqual(response.data['totals']['average_per_day'], 0.15)
        self.assertEqual(
            response.data['totals']['busiest_days'],
            [{'date': '2026-01-01', 'booked_users': 2}],
        )

        daily_by_date = {row['date']: row['booked_users'] for row in response.data['daily']}
        self.assertEqual(daily_by_date['2026-01-01'], 2)
        self.assertEqual(daily_by_date['2026-01-02'], 0)
        self.assertEqual(daily_by_date['2026-01-03'], 0)
        self.assertEqual(daily_by_date['2026-02-01'], 1)
        self.assertEqual(len(response.data['daily']), 34)

        weekly_total = sum(row['booked_user_days'] for row in response.data['weekly'])
        monthly_by_month = {row['month']: row['booked_user_days'] for row in response.data['monthly']}
        self.assertEqual(weekly_total, 5)
        self.assertEqual(monthly_by_month['2026-01'], 4)
        self.assertEqual(monthly_by_month['2026-02'], 1)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_report_requires_points_admin(self, mock_permission):
        response = self.client.get(
            self.url,
            {
                'slack_user_id': self.other_slack_id,
                'start_date': '2026-01-01',
                'end_date': '2026-01-31',
            },
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('Points Admins', response.data['error'])

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_report_rejects_invalid_ranges(self, mock_permission):
        invalid_date_response = self.client.get(
            self.url,
            {
                'slack_user_id': self.admin_slack_id,
                'start_date': '2026-99-01',
                'end_date': '2026-01-31',
            },
        )
        reversed_range_response = self.client.get(
            self.url,
            {
                'slack_user_id': self.admin_slack_id,
                'start_date': '2026-02-01',
                'end_date': '2026-01-31',
            },
        )
        too_long_response = self.client.get(
            self.url,
            {
                'slack_user_id': self.admin_slack_id,
                'start_date': '2026-01-01',
                'end_date': '2027-01-02',
            },
        )

        self.assertEqual(invalid_date_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(reversed_range_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(too_long_response.status_code, status.HTTP_400_BAD_REQUEST)


class FirstChannelPostAwardViewTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='intro@example.com',
            slack_id='UINTRO',
        )
        self.url = reverse('first_post_award')

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_first_post_award_creates_marker_and_ledger_entry(self, mock_permission):
        response = self.client.post(
            self.url,
            {
                'slack_user_id': 'UINTRO',
                'channel_id': 'CSTART',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['awarded'], True)
        self.assertEqual(response.data['new_balance'], 2)
        self.assertEqual(ChannelFirstPost.objects.filter(slack_user_id='UINTRO', channel_id='CSTART').count(), 1)

        ledger = Ledger.objects.get(idempotency_key='first_post_award:UINTRO:CSTART')
        self.assertEqual(ledger.delta, 2)
        self.assertEqual(ledger.description, 'Completed quest: First Contact')
        self.assertEqual(ledger.created_by_slack_id, 'SYSTEM')

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_first_post_award_is_idempotent_on_repeat_request(self, mock_permission):
        first_response = self.client.post(
            self.url,
            {
                'slack_user_id': 'UINTRO',
                'channel_id': 'CSTART',
            },
            format='json',
        )
        second_response = self.client.post(
            self.url,
            {
                'slack_user_id': 'UINTRO',
                'channel_id': 'CSTART',
            },
            format='json',
        )

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertEqual(first_response.data['awarded'], True)
        self.assertEqual(second_response.data['awarded'], False)
        self.assertEqual(ChannelFirstPost.objects.filter(slack_user_id='UINTRO', channel_id='CSTART').count(), 1)
        self.assertEqual(Ledger.objects.filter(idempotency_key='first_post_award:UINTRO:CSTART').count(), 1)

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    @patch('roo.views.PointsService.award', side_effect=RuntimeError('boom'))
    def test_first_post_award_rolls_back_marker_when_points_award_fails(self, mock_award, mock_permission):
        response = self.client.post(
            self.url,
            {
                'slack_user_id': 'UINTRO',
                'channel_id': 'CSTART',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(ChannelFirstPost.objects.filter(slack_user_id='UINTRO', channel_id='CSTART').count(), 0)
        self.assertEqual(Ledger.objects.filter(idempotency_key='first_post_award:UINTRO:CSTART').count(), 0)


class FirstChannelPostAwardConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = User.objects.create_user(
            email='intro-concurrency@example.com',
            slack_id='UINTRO',
        )
        self.url = reverse('first_post_award')

    @patch('core.permissions.HasAPIKey.has_permission', return_value=True)
    def test_first_post_award_is_idempotent_under_concurrency(self, mock_permission):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker():
            close_old_connections()
            client = APIClient()
            try:
                barrier.wait(timeout=5)
                response = client.post(
                    self.url,
                    {
                        'slack_user_id': 'UINTRO',
                        'channel_id': 'CSTART',
                    },
                    format='json',
                )
                results.append(response.data)
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(ChannelFirstPost.objects.filter(slack_user_id='UINTRO', channel_id='CSTART').count(), 1)
        self.assertEqual(Ledger.objects.filter(idempotency_key='first_post_award:UINTRO:CSTART').count(), 1)
        self.assertEqual(sum(1 for result in results if result.get('awarded') is True), 1)
        self.assertEqual(sum(1 for result in results if result.get('awarded') is False), 1)
