"""Calendar endpoint authority and safe response tests without database access."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from django.test import override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat import mentoring_calendar_views as views
from mentoring.calendars.errors import CalendarError


class CalendarViewsTests(unittest.TestCase):
    def setUp(self):
        override=override_settings(MENTOR_BOOKING_ENABLED=True)
        override.enable()
        self.addCleanup(override.disable)
        self.factory=APIRequestFactory()
        self.user=SimpleNamespace(pk=17,is_authenticated=True,is_active=True)

    def request(self,method,path,data=None):
        req=getattr(self.factory,method)(path,data or {},format='json')
        force_authenticate(req,user=self.user)
        return req

    def test_connection_read_is_owner_scoped_and_no_store(self):
        with patch.object(views.service,'connections',return_value={'configured':False,'connections':[]}) as call:
            result=views.MentorCalendarConnectionsView.as_view()(self.request('get','/'))
        call.assert_called_once_with(self.user)
        self.assertEqual(result['Cache-Control'],'private, no-store')
        self.assertFalse(result.data['configured'])

    def test_calendar_ids_do_not_choose_another_owner(self):
        with patch.object(views.service,'refresh_calendars',return_value={'id':2}) as call:
            result=views.MentorCalendarListView.as_view()(self.request('get','/'),connection_id=2)
        call.assert_called_once_with(self.user,2)
        self.assertEqual(result.status_code,200)

    def test_selections_identity_comes_from_request(self):
        payload={'connection_id':2,'booking_calendar_id':'mine','conflict_calendar_ids':['mine'],'user_id':999}
        with patch.object(views.service,'select_calendars',return_value={'connections':[]}) as call:
            views.MentorCalendarSelectionsView.as_view()(self.request('put','/',payload))
        self.assertIs(call.call_args.args[0],self.user)

    def test_provider_error_returns_safe_code_not_exception_body(self):
        with patch.object(views.service,'connections',side_effect=CalendarError('permission')):
            result=views.MentorCalendarConnectionsView.as_view()(self.request('get','/'))
        self.assertEqual(result.status_code,409)
        self.assertEqual(result.data['code'],'permission')

    @override_settings(MENTOR_BOOKING_ENABLED=False)
    def test_disabled_flag_precedes_auth_and_provider(self):
        with patch.object(views.service,'connections') as call:
            result=views.MentorCalendarConnectionsView.as_view()(self.factory.get('/'))
        self.assertEqual(result.status_code,404)
        call.assert_not_called()

    @override_settings(MENTOR_CALENDAR_CHAT_ORIGIN='https://chat.mlai.au')
    def test_callback_returns_no_tokens_or_codes_in_redirect(self):
        with patch.object(views.service,'finish_authorization',return_value=None):
            result=views.MentorCalendarCallbackView.as_view()(self.factory.get('/',{'state':'signed','code':'private-code'}))
        self.assertEqual(result.status_code,302)
        self.assertEqual(result['Location'],'https://chat.mlai.au/mentoring/settings?calendar=connected')
        self.assertEqual(result['Referrer-Policy'],'no-referrer')
        self.assertNotIn('private-code',result['Location'])

    @override_settings(MENTOR_CALENDAR_CHAT_ORIGIN='https://chat.mlai.au/evil')
    def test_callback_rejects_non_origin_return_configuration(self):
        with patch.object(views.service,'finish_authorization',return_value=None):
            result=views.MentorCalendarCallbackView.as_view()(self.factory.get('/',{'state':'signed','code':'code'}))
        self.assertEqual(result.status_code,400)

    def test_callback_missing_state_does_not_call_provider(self):
        with patch.object(views.service,'finish_authorization') as call:
            result=views.MentorCalendarCallbackView.as_view()(self.factory.get('/'))
        self.assertEqual(result.status_code,400)
        call.assert_not_called()
