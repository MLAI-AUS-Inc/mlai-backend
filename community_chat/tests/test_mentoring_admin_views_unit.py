"""Permission and contract tests for the staff-only mentoring admin endpoints. No DB."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from django.test import override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat import mentoring_views as views
from mentoring.service import MentoringError

ADMIN_VIEWS = (
    (views.MentoringAdminMembersView, 'get', {}),
    (views.MentoringAdminInvitesView, 'get', {}),
    (views.MentoringAdminInvitesView, 'post', {}),
    (views.MentoringAdminInviteRevokeView, 'post', {'invite_id': 1}),
    (views.MentoringAdminApplicationsView, 'get', {}),
    (views.MentoringAdminApplicationView, 'get', {'profile_id': 1}),
    (views.MentoringAdminApplicationApproveView, 'post', {'profile_id': 1}),
    (views.MentoringAdminApplicationRejectView, 'post', {'profile_id': 1}),
    (views.MentoringAdminMentorsView, 'get', {}),
    (views.MentoringAdminMentorPauseView, 'post', {'profile_id': 1}),
    (views.MentoringAdminMentorUnpauseView, 'post', {'profile_id': 1}),
    (views.MentoringAdminSessionsView, 'get', {}),
)


def make_user(**kw):
    base = dict(pk=1, is_authenticated=True, is_active=True, is_staff=False, is_superuser=False)
    base.update(kw)
    return SimpleNamespace(**base)


class AdminViewTests(unittest.TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.service = patch.object(views, 'MentoringAdminService').start()
        self.role = patch.object(views, 'account_chat_role', return_value='member').start()
        self.addCleanup(patch.stopall)

    def call(self, view, method='get', user=None, data=None, **kwargs):
        request = getattr(self.factory, method)('/x/', data or {}, format='json')
        if user is not None:
            force_authenticate(request, user=user)
        return view.as_view(throttle_classes=[])(request, **kwargs)

    @override_settings(MENTOR_BOOKING_ENABLED=True)
    def test_plain_members_get_403_on_every_admin_endpoint_and_no_service_call(self):
        for view, method, kwargs in ADMIN_VIEWS:
            response = self.call(view, method, user=make_user(), **kwargs)
            self.assertEqual(response.status_code, 403, view.__name__)
            self.assertEqual(response.data['code'], 'mentoring_admin_required')
        self.assertEqual(self.service.method_calls, [])

    @override_settings(MENTOR_BOOKING_ENABLED=True)
    def test_anonymous_is_rejected(self):
        for view, method, kwargs in ADMIN_VIEWS:
            self.assertIn(self.call(view, method, user=None, **kwargs).status_code, (401, 403))
        self.assertEqual(self.service.method_calls, [])

    @override_settings(MENTOR_BOOKING_ENABLED=False)
    def test_flag_off_is_404_even_for_staff(self):
        for view, method, kwargs in ADMIN_VIEWS:
            response = self.call(view, method, user=make_user(is_staff=True), **kwargs)
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.call(views.MentoringAdminAccessView, user=make_user(is_staff=True)).status_code, 404)

    @override_settings(MENTOR_BOOKING_ENABLED=True)
    def test_chat_admins_owners_and_django_staff_pass(self):
        self.service.search_members.return_value = []
        for role in ('owner', 'admin'):
            self.role.return_value = role
            self.assertEqual(self.call(views.MentoringAdminMembersView, user=make_user()).status_code, 200)
        self.role.return_value = 'moderator'
        self.assertEqual(self.call(views.MentoringAdminMembersView, user=make_user()).status_code, 403)
        self.role.return_value = 'member'
        for flag in ('is_staff', 'is_superuser'):
            self.assertEqual(self.call(views.MentoringAdminMembersView, user=make_user(**{flag: True})).status_code, 200)
        self.assertEqual(
            self.call(views.MentoringAdminMembersView, user=make_user(is_staff=True, is_active=False)).status_code, 403)

    @override_settings(MENTOR_BOOKING_ENABLED=True)
    def test_access_endpoint_answers_every_member(self):
        response = self.call(views.MentoringAdminAccessView, user=make_user())
        self.assertEqual((response.status_code, response.data), (200, {'is_admin': False}))
        response = self.call(views.MentoringAdminAccessView, user=make_user(is_staff=True))
        self.assertEqual(response.data, {'is_admin': True})
        self.role.return_value = 'admin'
        self.assertEqual(self.call(views.MentoringAdminAccessView, user=make_user()).data, {'is_admin': True})

    @override_settings(MENTOR_BOOKING_ENABLED=True)
    def test_staff_calls_pass_the_session_user_and_ids_from_the_url(self):
        staff = make_user(is_staff=True)
        self.service.create_invite.return_value = {'id': 1, 'link': 'L'}
        response = self.call(views.MentoringAdminInvitesView, 'post', user=staff, data={'user_id': 5, 'created_by': 99})
        self.assertEqual(response.status_code, 201)
        self.service.create_invite.assert_called_once_with(staff, 5)
        self.service.revoke_invite.return_value = {}
        self.call(views.MentoringAdminInviteRevokeView, 'post', user=staff, invite_id=7)
        self.service.revoke_invite.assert_called_once_with(staff, 7)
        self.service.approve_application.return_value = {}
        self.call(views.MentoringAdminApplicationApproveView, 'post', user=staff, profile_id=4)
        self.service.approve_application.assert_called_once_with(staff, 4)
        self.service.reject_application.return_value = {}
        self.call(views.MentoringAdminApplicationRejectView, 'post', user=staff, profile_id=4)
        self.service.reject_application.assert_called_once_with(staff, 4)
        self.service.set_paused.return_value = {}
        self.call(views.MentoringAdminMentorPauseView, 'post', user=staff, profile_id=2)
        self.call(views.MentoringAdminMentorUnpauseView, 'post', user=staff, profile_id=2)
        self.assertEqual([c.args[1:] for c in self.service.set_paused.call_args_list], [(2, True), (2, False)])

    @override_settings(MENTOR_BOOKING_ENABLED=True)
    def test_list_filters_come_from_the_query_string(self):
        staff = make_user(is_staff=True)
        self.service.list_sessions.return_value = []
        request = self.factory.get('/x/', {'status': 'completed', 'problem': 'meeting_failed'})
        force_authenticate(request, user=staff)
        self.assertEqual(views.MentoringAdminSessionsView.as_view(throttle_classes=[])(request).status_code, 200)
        self.service.list_sessions.assert_called_once_with('completed', 'meeting_failed')

    @override_settings(MENTOR_BOOKING_ENABLED=True)
    def test_service_errors_become_code_and_detail(self):
        self.service.list_mentors.side_effect = MentoringError('mentor_not_found', 'msg', 404)
        response = self.call(views.MentoringAdminMentorsView, user=make_user(is_staff=True))
        self.assertEqual((response.status_code, response.data), (404, {'code': 'mentor_not_found', 'detail': 'msg'}))

    @override_settings(MENTOR_BOOKING_ENABLED=True)
    def test_responses_are_not_cached(self):
        self.service.list_mentors.return_value = []
        response = self.call(views.MentoringAdminMentorsView, user=make_user(is_staff=True))
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    def test_write_endpoints_use_the_write_throttle_scope(self):
        request = self.factory.post('/x/')
        view = views.MentoringAdminInvitesView()
        view.request = request
        self.assertEqual(view.community_chat_throttle_scope, 'community_chat_mentoring_write')
        view.request = self.factory.get('/x/')
        self.assertEqual(view.community_chat_throttle_scope, 'community_chat_mentoring')


if __name__ == '__main__':
    unittest.main()
