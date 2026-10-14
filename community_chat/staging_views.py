"""Explicit browser-only staging surface; no production integration workflows."""
import secrets
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import HttpResponseRedirect
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import federation_relying as relying, adapter, views
from .account_cookies import refresh_cookie_name, set_account_session_cookies, clear_account_session_cookies
from .account_sessions import InvalidAccountSession, rotate_account_session, _hash_token, _valid_session
from .authentication import CommunityChatAccountAuthentication
from .federation import FederationUnavailable
from .models import CommunityChatAccountSession, CommunityChatDevice, CommunityChatInviteAudit, DeviceBindingStatus
from .serializers import public_chat_profile
from .throttles import enforce_dimension_limit, client_ip
from .staging_password import StagingAccountAuthentication, StagingBootstrapAuthentication, StagingPasswordRefreshView
from .permission_views import ChatPermissionsView


class StagingView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_authenticate_header(self, request):
        return "Bearer"

    def initial(self, request, *args, **kwargs):
        relying.require_staging()
        super().initial(request, *args, **kwargs)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request,response,*args,**kwargs)
        response['Cache-Control']='no-store'
        response['Referrer-Policy']='no-referrer'
        return response


class StagingStartView(StagingView):
    def post(self, request):
        enforce_dimension_limit(action='staging-sign-in',dimension='ip',value=client_ip(request),limit=60,window_seconds=60)
        _, cookie, url = relying.start_transaction(request.data,str(request.headers.get('Origin') or '').rstrip('/'))
        response=Response({'authorization_url':url},status=201)
        response.set_cookie(relying.TRANSACTION_COOKIE,cookie,max_age=300,secure=True,httponly=True,samesite='Lax',path='/')
        return response


class StagingCallbackView(StagingView):
    def get(self,request):
        credentials=relying.complete_transaction(request.COOKIES.get(relying.TRANSACTION_COOKIE,''),
            request.query_params.get('state',''),request.query_params.get('code',''))
        response=set_account_session_cookies(HttpResponseRedirect(settings.COMMUNITY_CHAT_FRONTEND_URL+'/auth/staging/complete'),credentials)
        response.delete_cookie(relying.TRANSACTION_COOKIE,path='/',samesite='Lax')
        return response


class StagingRefreshView(StagingView):
    def initial(self,request,*args,**kwargs):
        if getattr(settings,'COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED',False):
            APIView.initial(self,request,*args,**kwargs)
        else:super().initial(request,*args,**kwargs)

    def post(self,request):
        if getattr(settings,'COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED',False):
            return StagingPasswordRefreshView.post(self,request)
        origin=str(request.headers.get('Origin') or '').rstrip('/')
        if origin != settings.COMMUNITY_CHAT_FRONTEND_URL or request.data.get('refresh_token'):
            raise PermissionDenied('Staging browser refresh is required.')
        raw=request.COOKIES.get(refresh_cookie_name(),'')
        session=CommunityChatAccountSession.objects.select_related('user').filter(refresh_token_hash=_hash_token(raw)).first()
        if not _valid_session(session,timezone.now()) or session.origin != origin:
            raise AuthenticationFailed('Staging session has expired.')
        try:
            relying.recheck_session(session)
        except PermissionDenied:
            CommunityChatAccountSession.objects.filter(pk=session.pk).update(revoked_at=timezone.now())
            return Response({'error':'invalid_session'},status=401)
        try:
            credentials=rotate_account_session(raw,required_origin=origin)
        except InvalidAccountSession:
            # Another tab may have already rotated this token while eligibility
            # was checked. Reject the stale request without revoking its winner.
            return Response({'error':'invalid_session'},status=401)
        response=Response({'status':'refreshed','session':views._account_session_payload(credentials)})
        return set_account_session_cookies(response,credentials)


class StagingAccountView(views.AccountView):
    http_method_names=['get','patch','head','options']
    authentication_classes=[StagingAccountAuthentication]

class StagingProfileBatchView(views.PublicProfileBatchView):
    authentication_classes=[StagingAccountAuthentication]

class StagingPermissionsView(ChatPermissionsView):
    authentication_classes=[StagingAccountAuthentication]


class StagingChallengeView(views.ChallengeView):
    authentication_classes=[StagingBootstrapAuthentication,StagingAccountAuthentication]


class StagingInviteView(views.InviteView):
    authentication_classes=[StagingBootstrapAuthentication,StagingAccountAuthentication]


class StagingSessionView(views.SessionView):
    authentication_classes=[StagingBootstrapAuthentication,StagingAccountAuthentication]


class StagingConfirmView(APIView):
    authentication_classes=[StagingBootstrapAuthentication,StagingAccountAuthentication]
    permission_classes=[IsAuthenticated]

    def post(self,request):
        key=views._public_key(request.data.get('public_key'))
        views._require_token_key(request,key)
        views._request_origin(request)
        try:
            with transaction.atomic():
                user=get_user_model().objects.select_for_update().get(pk=request.user.pk)
                views._require_current_chat_credential_locked(request,user)
                device=CommunityChatDevice.objects.select_for_update().filter(user=user,public_key=key,
                    status__in=[DeviceBindingStatus.PENDING,DeviceBindingStatus.VERIFIED],revoked_at__isnull=True).first()
                if not device:
                    return Response({'error':'device_not_found'},status=404)
                membership=adapter.get_relay_membership(key)
                if not membership.is_member or membership.role != 'member':
                    return Response({'error':'membership_not_found'},status=409)
                now=timezone.now()
                device.status=DeviceBindingStatus.VERIFIED
                device.verified_at=device.verified_at or now
                device.last_verified_membership_at=now
                device.save(update_fields=['status','verified_at','last_verified_membership_at','updated_at'])
                CommunityChatInviteAudit.objects.filter(device=device,confirmed_at__isnull=True).update(confirmed_at=now)
                token=getattr(request,'community_chat_bootstrap_token',None)
                if token:
                    type(token).objects.filter(pk=token.pk,revoked_at__isnull=True).update(revoked_at=now)
        except adapter.MembershipAdapterUnavailable:
            raise FederationUnavailable() from None
        return Response({'status':'verified','device':views._device_payload(device),'public_profile':public_chat_profile(user)})


class StagingDeviceView(APIView):
    authentication_classes=[StagingBootstrapAuthentication,StagingAccountAuthentication]
    permission_classes=[IsAuthenticated]

    def delete(self,request,public_key):
        key=views._public_key(public_key)
        views._require_token_key(request,key)
        views._request_origin(request)
        try:
            with transaction.atomic():
                user=get_user_model().objects.select_for_update().get(pk=request.user.pk)
                views._require_current_chat_credential_locked(request,user)
                device=CommunityChatDevice.objects.select_for_update().filter(user=user,public_key=key,revoked_at__isnull=True).first()
                if not device:
                    return Response({'error':'device_not_found'},status=404)
                for audit in CommunityChatInviteAudit.objects.filter(device=device,confirmed_at__isnull=True):
                    adapter.revoke_member_invite(audit.adapter_invite_id)
                adapter.revoke_relay_membership(key)
                now=timezone.now()
                device.status=DeviceBindingStatus.REVOKED
                device.revoked_at=now
                device.revoked_by=user
                device.save(update_fields=['status','revoked_at','revoked_by','updated_at'])
                CommunityChatAccountSession.objects.filter(user=user,public_key=key,revoked_at__isnull=True).update(revoked_at=now)
                from .models import CommunityChatBootstrapToken
                CommunityChatBootstrapToken.objects.filter(user=user,public_key=key,revoked_at__isnull=True).update(revoked_at=now)
        except adapter.MembershipAdapterUnavailable:
            raise FederationUnavailable() from None
        return Response({'status':'revoked','device':views._device_payload(device)})
