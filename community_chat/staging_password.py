"""Independent pilot authentication, confined to the staging URL surface."""
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from rest_framework.exceptions import PermissionDenied, NotFound, AuthenticationFailed
from rest_framework.response import Response
from django.utils import timezone
from . import views
from .authentication import CommunityChatAccountAuthentication, CommunityChatBootstrapAuthentication
from .account_cookies import access_cookie_name, refresh_cookie_name, clear_account_session_cookies
from .account_sessions import _hash_token, _valid_session, revoke_account_session, InvalidAccountSession
from .models import CommunityChatAccountSession

BROWSER='https://chat-staging.mlai.au'
NATIVE='mlaichat://callback'
CLIENTS={'mlai-chat-web':BROWSER,'mlai-chat-ios':NATIVE,'mlai-chat-android':NATIVE}

def require_password_pilot():
    if (settings.SETTINGS_MODULE!='mlai.settings_staging_chat' or settings.APP_ENV!='chat-staging'
        or settings.ROOT_URLCONF!='mlai.urls_staging_chat'
        or settings.COMMUNITY_CHAT_FRONTEND_URL!=BROWSER
        or settings.COMMUNITY_CHAT_RELAY_URL!='wss://chat-staging.mlai.au'
        or settings.COMMUNITY_CHAT_API_AUDIENCE!='https://api-staging.mlai.au'
        or settings.COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED
        or settings.COMMUNITY_CHAT_SIGNUP_ENABLED
        or not settings.COMMUNITY_CHAT_HOST_ONLY_COOKIES
        or settings.COMMUNITY_CHAT_ACCESS_COOKIE_NAME!='__Host-mlai_staging_access'
        or settings.COMMUNITY_CHAT_REFRESH_COOKIE_NAME!='__Host-mlai_staging_refresh'):
        raise ImproperlyConfigured('Independent pilot requires the isolated staging profile and exclusive sign-in mode.')
    if not getattr(settings,'COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED',False):
        raise NotFound('Staging password sign-in is disabled.')

def origin(request):
    return str(request.headers.get('Origin') or '').strip().rstrip('/')

class StagingPasswordView(views.PasswordAuthView):
    def post(self,request):
        if not getattr(settings,'COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED',False):
            return Response({'error':'password_auth_disabled'},status=404)
        require_password_pilot()
        client=request.data.get('client_id')
        if client not in CLIENTS or origin(request)!=CLIENTS[client]:
            raise PermissionDenied('Staging client and origin are not approved.')
        if any(request.COOKIES.get(name) for name in (access_cookie_name(),refresh_cookie_name())) and client!='mlai-chat-web':
            raise PermissionDenied('Native credentials cannot use browser cookies.')
        return super().post(request)

def refresh_context(request):
    """Bind refresh/logout transport to its stored client and exact origin."""
    explicit=str(request.data.get('refresh_token') or '').strip()
    current=origin(request)
    browser=current==BROWSER
    if current not in {BROWSER,NATIVE} or (browser and explicit) or (not browser and not explicit):
        raise PermissionDenied('Staging refresh client transport is invalid.')
    if not browser and any(request.COOKIES.get(name) for name in (access_cookie_name(),refresh_cookie_name())):
        raise PermissionDenied('Native credentials cannot use browser cookies.')
    raw=explicit or request.COOKIES.get(refresh_cookie_name(),'')
    session=CommunityChatAccountSession.objects.select_related('user').filter(refresh_token_hash=_hash_token(raw)).first()
    if not _valid_session(session,timezone.now()):
        raise AuthenticationFailed('Staging session has expired.')
    if CLIENTS.get(session.client_id)!=current or session.origin!=current:
        raise PermissionDenied('Staging session belongs to another client origin.')
    return raw,current,browser

class StagingPasswordRefreshView(views.AccountSessionRefreshView):
    def post(self,request):
        require_password_pilot();refresh_context(request)
        return views.AccountSessionRefreshView.post(self,request)

class StagingLogoutView(views.AccountSessionLogoutView):
    def post(self,request):
        if not getattr(settings,'COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED',False):
            return super().post(request)
        require_password_pilot();raw,current,browser=refresh_context(request)
        try:revoke_account_session(raw,required_origin=current)
        except InvalidAccountSession:return Response({'error':'invalid_session'},status=401)
        response=Response({'status':'signed_out'})
        return clear_account_session_cookies(response) if browser else response

def require_credential_transport(request):
    if not getattr(settings,'COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED',False):return
    require_password_pilot()
    client=getattr(request,'community_chat_client_id',None)
    if CLIENTS.get(client)!=origin(request):
        raise AuthenticationFailed('Staging credential origin is invalid.')
    if client=='mlai-chat-web' and request.headers.get('Authorization','').startswith('Bearer mlai_session_access_'):
        raise AuthenticationFailed('Browser account credentials require cookies.')
    if client!='mlai-chat-web' and any(request.COOKIES.get(name) for name in (access_cookie_name(),refresh_cookie_name())):
        raise AuthenticationFailed('Native credentials cannot use browser cookies.')

class StagingAccountAuthentication(CommunityChatAccountAuthentication):
    def authenticate(self,request):
        if request.headers.get('Authorization') and not request.headers.get('Authorization','').startswith('Bearer mlai_session_access_'):
            raise AuthenticationFailed('Hosted staging requires its own account session.')
        return super().authenticate(request)

class StagingBootstrapAuthentication(CommunityChatBootstrapAuthentication):
    def authenticate(self,request):
        return super().authenticate(request)
