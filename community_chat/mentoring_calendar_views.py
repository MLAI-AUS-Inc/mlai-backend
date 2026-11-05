"""Authenticated personal-calendar setup and strictly bound OAuth callback."""
from urllib.parse import urlencode, urlparse

from django.conf import settings
from django.db import transaction
from django.http import HttpResponseRedirect
from django.utils.decorators import method_decorator
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from mentoring.calendars import service
from mentoring.calendars.errors import CalendarError
from mentoring.service import MentoringService
from .mentoring_views import MentoringBaseView, _body, mentor_booking_enabled


def _provider_error(exc):
    messages={
        'calendar_not_configured':'Google Calendar connections are not configured yet.',
        'reconnect_required':'Reconnect your Google Calendar to continue.',
        'incomplete_busy_check':'Your calendar could not be checked. Please try again.',
        'permission':'A calendar permission has changed. Reconnect or choose another calendar.',
        'insufficient_scopes':'Grant the requested calendar permissions to continue.',
    }
    return Response({'code':exc.code,'detail':messages.get(exc.code,'The calendar could not be checked. Please try again.')},status=409)


@method_decorator(transaction.non_atomic_requests, name='dispatch')
class MentorCalendarBaseView(MentoringBaseView):
    """Preserve existing account identity, feature gate, throttles and no-store."""

    def handle_exception(self,exc):
        if isinstance(exc,CalendarError):
            return _provider_error(exc)
        return super().handle_exception(exc)


class MentorCalendarConnectionsView(MentorCalendarBaseView):
    def get(self,request):
        return Response(service.connections(request.user))


class MentorCalendarAuthorizeView(MentorCalendarBaseView):
    def post(self,request):
        data=_body(request)
        return Response(service.begin_authorization(request.user,data.get('return_path','/mentoring/settings'),
            account_session=getattr(request,'community_chat_account_session',None)))


class MentorCalendarListView(MentorCalendarBaseView):
    def get(self,request,connection_id):
        return Response(service.refresh_calendars(request.user,connection_id))


class MentorCalendarSelectionsView(MentorCalendarBaseView):
    def put(self,request):
        return Response(service.select_calendars(request.user,_body(request)))


class MentorCalendarDisconnectView(MentorCalendarBaseView):
    def post(self,request,connection_id):
        return Response(service.disconnect(request.user,connection_id))


class MentorReadinessView(MentorCalendarBaseView):
    def get(self,request):
        return Response(service.readiness(service._profile(request.user)))


class MentorSettingsView(MentorCalendarBaseView):
    """Convenience contract; legacy profile fields retain their canonical service."""
    def get(self,request):
        calendars=service.connections(request.user)
        return Response({'profile':MentoringService.get_profile(request.user),'calendar':calendars,'readiness':calendars['readiness']})

    def patch(self,request):
        MentoringService.update_profile(request.user,_body(request))
        return self.get(request)


@method_decorator(transaction.non_atomic_requests, name='dispatch')
class MentorCalendarCallbackView(APIView):
    """Google returns outside app auth; one-use state carries the original actor."""
    authentication_classes=()
    permission_classes=(AllowAny,)
    throttle_classes=()

    def get(self,request):
        if not mentor_booking_enabled():
            return Response({'code':'mentor_booking_disabled','detail':'Mentoring is not available.'},status=404)
        raw=request.query_params.get('state','')
        if not isinstance(raw,str) or not raw or len(raw)>4096:
            return Response({'code':'invalid_oauth_state','detail':'Start the connection again from Mentoring settings.'},status=400)
        try:
            service.finish_authorization(raw,request.query_params.get('code',''),denied=bool(request.query_params.get('error')))
            result={'calendar':'connected'}
        except CalendarError as exc:
            result={'calendar':'error','code':exc.code}
        origin=str(getattr(settings,'MENTOR_CALENDAR_CHAT_ORIGIN','https://chat.mlai.au')).rstrip('/')
        parsed=urlparse(origin)
        local=bool(getattr(settings,'IS_LOCAL_ENV',False))
        if (parsed.scheme!='https' and not(local and parsed.scheme=='http' and parsed.hostname in ('localhost','127.0.0.1'))) or not parsed.netloc or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return Response({'code':'invalid_return_origin','detail':'Return to Mentoring settings.'},status=400)
        response=HttpResponseRedirect(origin+'/mentoring/settings?'+urlencode(result))
        response['Cache-Control']='private, no-store'
        response['Referrer-Policy']='no-referrer'
        return response
