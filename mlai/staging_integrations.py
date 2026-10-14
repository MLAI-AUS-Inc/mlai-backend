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
        elif module in {'community_chat.apple_iap_views','community_chat.slack_views','community_chat.slack_action_views'}:capability='apple_iap' if 'apple' in module else 'slack'
        if capability:
            try:require_staging_capability(capability)
            except StagingIntegrationUnavailable as exc:return JsonResponse(exc.detail,status=503)
    def process_exception(self,request,exception):
        if isinstance(exception,StagingIntegrationUnavailable):return JsonResponse(exception.detail,status=503)
        if is_isolated_staging():
            import requests,httpx
            if isinstance(exception,(requests.RequestException,httpx.HTTPError,ConnectionError,TimeoutError)):
                return JsonResponse({'code':'staging_integration_unavailable','capability':'external_network'},status=503)
