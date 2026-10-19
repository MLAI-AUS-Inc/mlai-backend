"""Isolated Chat pilot, using a separate DB/cache and no worker processes."""
from .settings import *
from .settings import _env_is_true

if APP_ENV!='chat-staging':raise ImproperlyConfigured('Hosted staging requires APP_ENV=chat-staging.')
ROOT_URLCONF='mlai.urls_staging_chat'
COMMUNITY_CHAT_FEDERATION_ISSUER_ENABLED=False
COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED=_env_is_true('COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED',False)
COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED=_env_is_true('COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED',False)
if COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED and (APP_ENV!='chat-staging' or COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED):
    raise ImproperlyConfigured('Password pilot requires chat-staging and exclusive sign-in mode.')
COMMUNITY_CHAT_HOST_ONLY_COOKIES=True
COMMUNITY_CHAT_CLEAR_LEGACY_COOKIES=False
COMMUNITY_CHAT_ACCESS_COOKIE_NAME='__Host-mlai_staging_access'
COMMUNITY_CHAT_REFRESH_COOKIE_NAME='__Host-mlai_staging_refresh'
COMMUNITY_CHAT_FRONTEND_URL='https://chat-staging.mlai.au'
COMMUNITY_CHAT_ALLOWED_ORIGINS=[COMMUNITY_CHAT_FRONTEND_URL]+(['mlaichat://callback'] if COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED else [])
COMMUNITY_CHAT_RELAY_URL='wss://chat-staging.mlai.au'
COMMUNITY_CHAT_API_AUDIENCE='https://api-staging.mlai.au'
COMMUNITY_CHAT_SESSION_ACCESS_TTL_SECONDS=300
COMMUNITY_CHAT_SIGNUP_ENABLED=False
COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED=COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED
COMMUNITY_CHAT_EMAIL_CODE_AUTH_ENABLED=False
COMMUNITY_CHAT_DEVICE_AUTH_ENABLED=False
CORS_ALLOWED_ORIGINS=[COMMUNITY_CHAT_FRONTEND_URL]
CSRF_TRUSTED_ORIGINS=[COMMUNITY_CHAT_FRONTEND_URL]
SESSION_COOKIE_DOMAIN=None
SESSION_COOKIE_SECURE=True
COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=True
COMMUNITY_CHAT_AI_PROVIDERS=[]
CUSTOMERIO_API_KEY=''
CUSTOMERIO_SITE_ID=''
SLACK_BOT_TOKEN=''
STRIPE_SECRET_KEY=''

# Private container readiness must not redirect to an unavailable HTTPS socket.
SECURE_REDIRECT_EXEMPT=['^healthz/live$']

# Hold the shared user/session boundary through ordinary domain mutations.
DATABASES['default']['ATOMIC_REQUESTS']=True

# Source may use ordinary domain routes; no live credentials/defaults survive.
STAGING_CAPABILITIES={}
MIDDLEWARE=['mlai.staging_integrations.IntegrationMiddleware',*MIDDLEWARE]
REST_FRAMEWORK={**REST_FRAMEWORK,'EXCEPTION_HANDLER':'mlai.staging_integrations.exception_handler'}
for _name in list(globals()):
    if _name.isupper() and any(word in _name for word in ('API_KEY','BOT_TOKEN','WEBHOOK_SECRET','OAUTH_CLIENT_SECRET','SENTRY_DSN','OTEL_EXPORTER','SERVICE_ACCOUNT')):
        globals()[_name]=''
DEFAULT_FRONTEND_URL=COMMUNITY_CHAT_FRONTEND_URL
for _name in ('MEDHACK_URL','ESAFETY_URL','WATT_THE_HACK_URL','VIBE_RAISING_URL','CONTENT_FACTORY_FRONTEND_URL'):
    globals()[_name]=COMMUNITY_CHAT_FRONTEND_URL
MEDIA_ROOT='/app/staging-media'
EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'
MEETING_ROOM_BOOKING_ENABLED=True
# Synthetic mentoring uses the isolated staging database and normal account roles.
MENTOR_BOOKING_ENABLED=True
MENTORING_GOOGLE_ENABLED=False
MENTORING_GOOGLE_CLIENT_ID=''
MENTORING_GOOGLE_CLIENT_SECRET=''
MENTORING_GOOGLE_REFRESH_TOKEN=''
for _name in list(globals()):
    _value=globals()[_name]
    if _name.isupper() and _name.endswith(('URL','ORIGIN','BASE_URL')) and isinstance(_value,str) and 'mlai.au' in _value and 'staging.mlai.au' not in _value:
        from urllib.parse import urlsplit as _urlsplit,urlunsplit as _urlunsplit
        _parsed=_urlsplit(_value)
        if _parsed.hostname and _parsed.hostname.endswith('.mlai.au'):
            globals()[_name]=_urlunsplit((_parsed.scheme,'api-staging.mlai.au' if _parsed.hostname.startswith('api.') else 'chat-staging.mlai.au',_parsed.path,'',''))

from .staging_integrations import pin_isolated_destinations
pin_isolated_destinations(globals(),local=False)

# Ordinary local features use independent staging channel identities.
from uuid import uuid5 as _uuid5, NAMESPACE_DNS as _dns
COMMUNITY_CHAT_VOLUNTEER_ENABLED=True
COMMUNITY_CHAT_VOLUNTEER_COMMUNITY="chat-staging.mlai.au"
COMMUNITY_CHAT_VOLUNTEER_CHANNELS={name:str(_uuid5(_dns,"chat-staging.mlai.au/public-channel/v1/"+name)) for name in ("tech_volunteers","volunteer")}
