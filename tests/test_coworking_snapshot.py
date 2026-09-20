"""Booking-list endpoint: real database, permission and URL contract tests."""
from datetime import date
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import resolve
from rest_framework.test import APIClient

from core.models import User
from roo.models import CoworkingBooking, PointsAdmin


@override_settings(ROO_API_KEY='snapshot-test-roo-key', INTERNAL_API_KEY='snapshot-other-key',
                   MLAI_API_KEY='snapshot-generic-key', POINTS_BOOTSTRAP_ADMIN_SLACK_IDS=[])
class BookingSnapshotTests(TestCase):
    url = '/api/v1/points/coworking/bookings-for-date/'
    day = date(2026, 9, 21)

    def setUp(self):
        self.client = APIClient()
        self.client.credentials(HTTP_X_API_KEY='snapshot-test-roo-key')
        self.admin = PointsAdmin.objects.create(slack_user_id='UADMIN', role='admin', is_active=True)
        self.alice = User.objects.create(email='alice@example.test', first_name='Alice', last_name='Smith')
        self.ben = User.objects.create(email='ben@example.test', first_name='Ben', last_name='Jones')

    def booking(self, user, **kwargs):
        return CoworkingBooking.objects.create(user=user, date=kwargs.pop('date', self.day),
                                               points_cost=8, **kwargs)

    def get(self, **params):
        return self.client.get(self.url, {'slack_user_id': 'UADMIN', 'date': '2026-09-21', **params})

    def test_active_selected_date_only_and_empty_result(self):
        self.booking(self.alice)
        self.booking(self.ben, status='cancelled')
        self.booking(self.ben, date=date(2026, 9, 22))
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'date': '2026-09-21', 'count': 1,
                         'people': [{'user_id': str(self.alice.pk), 'name': 'Alice Smith'}]})
        self.assertEqual(self.get(date='2026-09-23').json(),
                         {'date': '2026-09-23', 'count': 0, 'people': []})
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    def test_cancelled_then_rebooked_counts_once(self):
        self.booking(self.alice, status='cancelled')
        self.booking(self.alice)
        self.assertEqual(self.get().json()['count'], 1)

    def test_ordering_equal_names_and_missing_names(self):
        other = User.objects.create(email='other@example.test', first_name='Alice', last_name='Smith')
        blank = User.objects.create(email='private@example.test')
        for user in [self.ben, blank, other, self.alice]:
            self.booking(user)
        people = self.get().json()['people']
        self.assertEqual([p['name'] for p in people],
                         ['Alice Smith', 'Alice Smith', 'Ben Jones', f'Member {blank.pk}'])
        self.assertEqual([p['user_id'] for p in people[:2]], sorted([str(self.alice.pk), str(other.pk)]))
        self.assertNotIn('private@example.test', str(people))

    def test_only_full_admin_roles(self):
        for role, expected in [('admin', 200), ('committee', 200), ('portfolio_lead', 200), ('partner', 403)]:
            with self.subTest(role=role):
                self.admin.role = role
                self.admin.save(update_fields=['role'])
                self.assertEqual(self.get().status_code, expected)

    def test_deactivated_admin_denied_without_booking_lookup(self):
        self.assertEqual(self.get().status_code, 200)
        self.admin.is_active = False
        self.admin.save(update_fields=['is_active'])
        with patch('roo.views.build_booking_snapshot') as lookup:
            self.assertEqual(self.get().status_code, 403)
            lookup.assert_not_called()

    def test_unknown_actor_denied(self):
        self.assertEqual(self.get(slack_user_id='UUNKNOWN').status_code, 403)

    def test_service_credential_is_required_even_for_admin(self):
        for key in ['', 'wrong', 'snapshot-other-key', 'snapshot-generic-key']:
            with self.subTest(key=key):
                self.client.credentials(HTTP_X_API_KEY=key)
                self.assertIn(self.get().status_code, (401, 403))

    def test_missing_actor_and_bad_dates(self):
        self.assertEqual(self.get(slack_user_id='').status_code, 400)
        for day in ['', '20260921', '2026-02-30', '2026-09-21 extra']:
            with self.subTest(day=day):
                self.assertEqual(self.get(date=day).status_code, 400)
        self.assertEqual(self.client.get(self.url, {'slack_user_id':'UADMIN'}).status_code, 400)
        self.assertEqual(self.client.get(self.url, {'date':'2026-09-21'}).status_code, 400)

    def test_projection_is_read_only_and_includes_office_manager(self):
        self.booking(self.alice, booking_source='office_manager')
        before = list(CoworkingBooking.objects.values())
        self.assertEqual(self.get().json()['count'], 1)
        self.assertEqual(list(CoworkingBooking.objects.values()), before)

    def test_report_route_remains_separate(self):
        self.assertEqual(resolve('/api/v1/points/coworking/report/').func.actions['get'], 'report')
        self.assertEqual(resolve(self.url).func.actions['get'], 'bookings_for_date')
        with patch('roo.views.CoworkingService.build_report', return_value={'legacy': True}) as report:
            response = self.client.get('/api/v1/points/coworking/report/',
                {'slack_user_id':'UADMIN', 'start_date':'2026-09-21', 'end_date':'2026-09-21'})
            self.assertEqual(response.json(), {'legacy': True})
            report.assert_called_once_with(self.day, self.day)
