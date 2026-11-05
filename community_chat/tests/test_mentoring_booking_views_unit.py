"""Instant-booking HTTP authority, compatibility and recovery contracts."""
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat import mentoring_booking_views as views
from mentoring.calendars.errors import CalendarError
from mentoring.service import MentoringError


@override_settings(MENTOR_BOOKING_ENABLED=True)
class InstantBookingViewTests(SimpleTestCase):
    def setUp(self):
        self.user = SimpleNamespace(pk=7, is_authenticated=True, is_active=True)
        self.factory = APIRequestFactory()

    def call(self, view, method='get', payload=None, authenticated=True, **kwargs):
        request = getattr(self.factory, method)('/', payload or {}, format='json')
        if authenticated:
            force_authenticate(request, user=self.user)
        return view.as_view(throttle_classes=[])(request, **kwargs)

    def test_booking_ignores_payload_owner_and_waits_for_durable_confirmation(self):
        attempt = SimpleNamespace(status='provisioning')
        payload = {'quote':'reviewed', 'brief':{'stage':'seed'}, 'client_request_id':str(uuid.uuid4()), 'founder_id':999}
        with patch.object(views.instant_booking, 'book', return_value=attempt) as book, patch.object(
            views.instant_booking, 'serialize_attempt', return_value={'status':'provisioning'}
        ):
            response = self.call(views.MentorInstantBookingView, 'post', payload)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertIs(book.call_args.kwargs['founder'], self.user)
        self.assertNotIn('founder_id', book.call_args.kwargs)

    def test_confirmed_retry_returns_confirmed_result(self):
        with patch.object(views.instant_booking, 'book', return_value=SimpleNamespace(status='confirmed')), patch.object(
            views.instant_booking, 'serialize_attempt', return_value={'status':'confirmed'}
        ):
            response = self.call(views.MentorInstantBookingView, 'post', {'quote':'q'})
        self.assertEqual(response.status_code, 201)

    def test_quote_identity_is_authenticated_not_payload(self):
        with patch.object(views.instant_booking, 'make_quote', return_value={'quote':'q'}) as quote:
            self.call(views.MentorBookingQuoteView, 'post', {'mentor_id':3,'starts_at':'time','expected_price':60,'founder_id':999})
        self.assertIs(quote.call_args.kwargs['founder'], self.user)
        self.assertEqual(quote.call_args.kwargs['expected_price'], 60)

    @override_settings(MENTOR_INSTANT_BOOKING_ENABLED=False)
    def test_recovery_remains_accessible_when_new_admission_is_disabled(self):
        with patch.object(views.instant_booking, 'list_attempts', return_value=[]) as recover:
            response = self.call(views.MentorBookingAttemptListView)
        self.assertEqual(response.data, {'attempts':[]})
        recover.assert_called_once_with(self.user)

    def test_cancel_uses_participant_identity(self):
        attempt_id = uuid.uuid4()
        with patch.object(views.instant_booking, 'cancel_attempt', return_value=object()) as cancel, patch.object(
            views.instant_booking, 'serialize_attempt', return_value={'status':'cancelled'}
        ):
            response = self.call(views.MentorBookingAttemptCancelView, 'post', {'user_id':999}, attempt_id=attempt_id)
        cancel.assert_called_once_with(attempt_id, self.user)
        self.assertEqual(response.data['status'], 'cancelled')

    def test_provider_errors_never_return_provider_details(self):
        with patch.object(views.instant_booking, 'make_quote', side_effect=CalendarError('private-upstream-data')):
            response = self.call(views.MentorBookingQuoteView, 'post', {})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data['code'], 'calendar_unavailable')
        self.assertNotIn('private-upstream-data', str(response.data))

    def test_expired_quote_is_business_conflict(self):
        with patch.object(views.instant_booking, 'make_quote', side_effect=MentoringError('quote_expired','Review again.',409)):
            response = self.call(views.MentorBookingQuoteView, 'post', {})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'quote_expired')

    def test_anonymous_cannot_submit_or_recover(self):
        for view in (views.MentorInstantBookingView, views.MentorBookingAttemptListView):
            self.assertIn(self.call(view, authenticated=False).status_code, (401,403))

    @override_settings(MENTOR_BOOKING_ENABLED=False)
    def test_parent_flag_precedes_auth_and_side_effects(self):
        with patch.object(views.instant_booking, 'book') as book:
            response = self.call(views.MentorInstantBookingView, 'post', authenticated=False)
        self.assertEqual(response.status_code, 404)
        book.assert_not_called()

    def test_new_routes_are_registered_and_provider_views_are_non_atomic(self):
        for path, cls in (
            ('mentor/bookings/',views.MentorInstantBookingView),
            ('mentor/bookings/quote/',views.MentorBookingQuoteView),
            ('mentor/booking-attempts/',views.MentorBookingAttemptListView),
        ):
            callback = resolve('/api/v1/community-chat/' + path).func
            self.assertIs(callback.view_class, cls)
            self.assertIn('default', callback._non_atomic_requests)
