"""Explicit environment provider boundary; networking enforces the backstop."""
from django.conf import settings
from django.http import JsonResponse
from rest_framework.exceptions import APIException
from django.utils.deprecation import MiddlewareMixin


def is_isolated_staging():
    return (settings.SETTINGS_MODULE,settings.APP_ENV) in {('mlai.settings_staging_chat','chat-staging'),('mlai.settings_staging_local','staging-local')}

class StagingIntegrationUnavailable(APIException):
    status_code=503
    default_code='staging_integration_unavailable'
    def __init__(self,capability):
        super().__init__({'code':'staging_integration_unavailable','capability':capability})

def require_staging_capability(name):
    if is_isolated_staging() and getattr(settings,'STAGING_CAPABILITIES',{}).get(name,'disabled')!='local':
        # Sandbox requires a separate reviewed networking/key rollout. Merely
        # assigning a mode cannot bypass the installed network policy.
        raise StagingIntegrationUnavailable(name)

def exception_handler(exc,context):
    from rest_framework.views import exception_handler as default
    if is_isolated_staging():
        import requests,httpx
        if isinstance(exc,(requests.RequestException,httpx.HTTPError,ConnectionError,TimeoutError)):
            exc=StagingIntegrationUnavailable('external_network')
    return default(exc,context)

class IntegrationMiddleware(MiddlewareMixin):
    """Environment auth/provider entry points; ordinary new routes pass through."""
    def process_view(self,request,view,args,kwargs):
        if not is_isolated_staging():return None
        cls=getattr(view,'view_class',None) or getattr(view,'cls',None)
        name=getattr(cls,'__name__',getattr(view,'__name__',''))
        module=getattr(cls,'__module__',getattr(view,'__module__',''))
        capability=None
        if name in {'SendMagicLinkView','CreateUserView','MagicLinkVerifyView','MyTokenObtainPairView','CookieTokenRefreshView','PasswordResetRequestView','PasswordResetConfirmView','GetOrCreateSlackUserView','EmailCodeRequestView','EmailCodeVerifyView','DeviceAuthStartView','DeviceAuthAuthorizeView','DeviceAuthExchangeView','MemberOnboardingView'}:
            capability='account_creation'
        elif 'Webhook' in name or 'NotificationView' in name or 'Callback' in name and module!='community_chat.staging_views':capability='provider_callback'
        elif module=='community_chat.apple_iap_views' and not (name=='AppleIapView' and request.method in {'GET','HEAD','OPTIONS'}):capability='apple_iap'
        elif module in {'community_chat.slack_views','community_chat.slack_action_views'}:capability='slack'
        if capability:
            try:require_staging_capability(capability)
            except StagingIntegrationUnavailable as exc:return JsonResponse(exc.detail,status=503)
    def process_exception(self,request,exception):
        if isinstance(exception,StagingIntegrationUnavailable):return JsonResponse(exception.detail,status=503)
        if is_isolated_staging():
            import requests,httpx
            if isinstance(exception,(requests.RequestException,httpx.HTTPError,ConnectionError,TimeoutError)):
                return JsonResponse({'code':'staging_integration_unavailable','capability':'external_network'},status=503)


def pin_isolated_destinations(namespace, *, local=False):
    """No first-party reporter/callback defaults escape via a non-URL suffix."""
    from urllib.parse import urlsplit,urlunsplit
    api='http://127.0.0.1:8000' if local else 'https://api-staging.mlai.au'
    frontend='http://localhost:5173' if local else 'https://chat-staging.mlai.au'
    relay='ws://127.0.0.1:3000' if local else 'wss://chat-staging.mlai.au'
    for name,value in list(namespace.items()):
        if name.isupper() and isinstance(value,str) and value.startswith(('https://','http://','wss://','ws://')):
            old=urlsplit(value)
            if old.hostname and (old.hostname=='mlai.au' or old.hostname.endswith('.mlai.au')):
                target=urlsplit(relay if old.scheme in {'ws','wss'} else api if old.hostname.startswith(('api.','api-staging.')) else frontend)
                namespace[name]=urlunsplit((target.scheme,target.netloc,old.path,old.query,''))
    namespace.update(TOKEN_USAGE_API_BASE=api+'/api/v1/community-chat/usage',COMMUNITY_CHAT_RELAY_URL=relay,TOKENMAXER_FEDERATION_ENABLED=False,TOKENMAXER_PUBLIC_API_BASE='',APPLE_IAP_ENABLED=False)
