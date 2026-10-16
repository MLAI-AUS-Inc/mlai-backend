"""DRF contract tests for the mentoring endpoints with the service mocked. No DB."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from django.test import override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat import mentoring_views as views
from mentoring.service import MentoringError

SID = str(uuid.uuid4())


class MentoringViewTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(pk=1, slack_id='UF', is_authenticated=True)
        self.factory = APIRequestFactory()
        self.service = patch.object(views, 'MentoringService').start()
        self.serialize = patch.object(views, 'serialize_session', side_effect=lambda s, u: {'id': 'x'}).start()
        self.addCleanup(patch.stopall)

    def call(self, view, method='get', path='/x/', data=None, authenticated=True, **kwargs):
        request = getattr(self.factory, method)(path, data or {}, format='json')
        if authenticated:
            force_authenticate(request, user=self.user)
        return view.as_view(throttle_classes=[])(request, **kwargs)

    @override_settings(MENTOR_BOOKING_ENABLED=True)
    def test_list_mentors(self):
        self.service.list_mentors.return_value = [{'id': 1}]
        response = self.call(views.MentorListView)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'mentors': [{'id': 1}]})
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    @override_settings(MENTOR_BOOKING_ENABLED=False)
    def test_everything_is_404_while_flag_is_off(self):
        for view, kwargs in ((views.MentorListView, {}), (views.MentorSessionsView, {}),
                             (views.MyMentorProfileView, {}), (views.MentorAvailabilityView, {'mentor_id': 1})):
            response = self.call(view, **kwargs)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.data['code'], 'mentor_booking_disabled')
        self.service.list_mentors.assert_not_called()

    @override_settings(MENTOR_BOOKING_ENABLED=False)
    def test_flag_off_is_404_for_anonymous_users_and_skips_throttling(self):
        # Even with throttling enabled, the flag is checked first.
        for view, kwargs in ((views.MentorListView, {}), (views.MentorSessionsView, {})):
            request = self.factory.get('/x/')
            response = view.as_view()(request, **kwargs)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.data['code'], 'mentor_booking_disabled')
        request = self.factory.post('/x/', {}, format='json')
        self.assertEqual(views.MentorSessionActionView.as_view(action='accept')(request, session_id=SID).status_code, 404)

    @override_settings(MENTOR_BOOKING_ENABLED=True)
    def test_unauthenticated_is_rejected(self):
        response = self.call(views.MentorListView, authenticated=False)
        self.assertIn(response.status_code, (401, 403))

    @override_settings(MENTOR_BOOKING_ENABLED=True)
    def test_create_uses_session_identity_only_and_returns_201_or_200(self):
        session = SimpleNamespace()
        self.service.request_session.return_value = (session, True)
        body = {'mentor_id': 3, 'starts_at': '2026-11-05T10:00:00+11:00', 'brief': {'stage': 'a', 'struggle': 'b', 'outcome': 'c'},
                'client_request_id': SID, 'expected_price': 40, 'founder_id': 99, 'user_id': 98}
        response = self.call(views.MentorSessionsView, 'post', data=body)
        self.assertEqual(response.status_code, 201)
        kw = self.service.request_session.call_args.kwargs
        self.assertEqual(kw['founder'], self.user)
        self.assertNotIn('founder_id', kw)
        self.assertEqual(kw['expected_price'], 40)
        self.service.request_session.return_value = (session, False)
        self.assertEqual(self.call(views.MentorSessionsView, 'post', data=body).status_code, 200)

    @override_settings(MENTOR_BOOKING_ENABLED=True)
    def test_errors_use_code_and_detail_with_status(self):
        for code, status in (('insufficient_points', 409), ('slot_unavailable', 409), ('invalid_brief', 400)):
            self.service.request_session.side_effect = MentoringError(code, 'msg', status)
            response = self.call(views.MentorSessionsView, 'post', data={})
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.data, {'code': code, 'detail': 'msg'})

    @override_settings(MENTOR_BOOKING_ENABLED=True)
    def test_actions_call_matching_service_method(self):
        for view, name in ((views.MentorSessionAcceptView, 'accept'), (views.MentorSessionDeclineView, 'decline'),
                           (views.MentorSessionCancelView, 'cancel'), (views.MentorSessionCompleteView, 'complete')):
            response = self.call(view, 'post', session_id=SID)
            self.assertEqual(response.status_code, 200)
            getattr(self.service, name).assert_called_once_with(SID, self.user)

    @override_settings(MENTOR_BOOKING_ENABLED=True)
    def test_sessions_list_validates_role_and_status(self):
        self.assertEqual(self.call(views.MentorSessionsView, data={'role': 'admin'}).status_code, 400)
        self.assertEqual(self.call(views.MentorSessionsView, data={'status': 'bogus'}).status_code, 400)
        self.service.list_sessions.return_value = []
        response = self.call(views.MentorSessionsView, data={'role': 'mentor', 'status': 'requested'})
        self.assertEqual(response.status_code, 200)
        self.service.list_sessions.assert_called_once_with(self.user, 'mentor', 'requested')

    @override_settings(MENTOR_BOOKING_ENABLED=True)
    def test_profile_get_and_put(self):
        self.service.get_profile.return_value = {'id': 1}
        self.assertEqual(self.call(views.MyMentorProfileView).data, {'id': 1})
        self.service.update_profile.return_value = {'id': 1, 'price_roo': 50}
        response = self.call(views.MyMentorProfileView, 'put', data={'price_roo': 50})
        self.assertEqual(response.data['price_roo'], 50)
        self.service.update_profile.assert_called_once_with(self.user, {'price_roo': 50})

    @override_settings(MENTOR_BOOKING_ENABLED=True)
    def test_availability_parses_dates(self):
        self.service.availability.return_value = {'slots': []}
        self.call(views.MentorAvailabilityView, data={'from': '2026-11-03', 'to': '2026-11-10'}, mentor_id=3)
        args = self.service.availability.call_args.args
        self.assertEqual((args[0], str(args[1]), str(args[2])), (3, '2026-11-03', '2026-11-10'))
        self.assertEqual(self.call(views.MentorAvailabilityView, data={'from': 'tomorrow'}, mentor_id=3).status_code, 400)

    def test_write_views_use_the_write_throttle_scope(self):
        view = views.MentorSessionsView()
        view.request = SimpleNamespace(method='POST')
        self.assertEqual(view.community_chat_throttle_scope, 'community_chat_mentoring_write')
        view.request = SimpleNamespace(method='GET')
        self.assertEqual(view.community_chat_throttle_scope, 'community_chat_mentoring')


if __name__ == '__main__':
    unittest.main()
