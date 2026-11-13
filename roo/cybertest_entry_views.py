"""Explicit local, staging and production entry with revalidated company access."""
import hashlib
import re
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle

from community_chat.account_sessions import _valid_session, _validate_device_owner
from community_chat.models import CommunityChatAccountSession
from community_chat.my_startup_views import MyStartupAccess
from community_chat.onboarding import require_community_access
from founder_tools.models import VibeRaisingCompany
from founder_tools.services import user_may_use_organization
from .cybertest_entry import EntryAuthority, EntryError
from .cybertest_models import CybertestCompanyBinding
from .cybertest_paths import base_path
from .cybertest_production_config import production_entry_settings
from .cybertest_views import PrivateView, ServiceView


def local_origin():
    """Validated service origin (legacy name retained for existing callers)."""
    if getattr(settings, 'CYBERTEST_ENTRY_PRODUCTION_ENABLED', False):
        return production_origin()
    value = getattr(settings, 'CYBERTEST_APP_ORIGIN', '').rstrip('/')
    try:
        base_path(getattr(settings, 'CYBERTEST_BASE_PATH', ''))
        parsed = urlsplit(value)
        parsed.port  # Reject malformed authorities before issuing a handoff.
    except ValueError as exc:
        raise EntryError('ENTRY_UNAVAILABLE', 'Cybertest test entry is not configured.', 503) from exc
    if getattr(settings, 'CYBERTEST_ENTRY_STAGING_ENABLED', False):
        backend = settings.CACHES.get('default', {}).get('BACKEND')
        if (settings.DEBUG or getattr(settings, 'CYBERTEST_ENTRY_LOCAL_ENABLED', False)
            or getattr(settings, 'CYBERTEST_BILLING_MODE', 'disabled') not in ('disabled', 'test')
            or not getattr(settings, 'CYBERTEST_PERSISTENT_BINDINGS_ENABLED', False)
            or backend not in ('django.core.cache.backends.redis.RedisCache', 'django_redis.cache.RedisCache')
            or parsed.scheme != 'https' or not parsed.hostname
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise EntryError('ENTRY_UNAVAILABLE', 'Cybertest staging entry is not configured.', 503)
        staging_companies()
        return value
    if (not settings.DEBUG or not getattr(settings, 'CYBERTEST_ENTRY_LOCAL_ENABLED', False)
        or getattr(settings, 'CYBERTEST_BILLING_MODE', 'disabled') == 'live'
        or parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1')
        or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise EntryError('ENTRY_UNAVAILABLE', 'Cybertest local entry is not enabled.', 503)
    return value


def production_companies():
    values = getattr(settings, 'CYBERTEST_PRODUCTION_COMPANY_IDS', [])
    try:
        minimum = 0 if general_access() else 1
        if not isinstance(values, (list, tuple)) or not minimum <= len(values) <= 100:
            raise ValueError()
        return {str(UUID(str(value))) for value in values}
    except (ValueError, TypeError, AttributeError) as exc:
        raise EntryError('ENTRY_UNAVAILABLE', 'Configure the enabled audit companies.', 503) from exc


def general_access():
    """Only explicit production activation enables automatic company enrollment."""
    return (getattr(settings, 'CYBERTEST_ENTRY_PRODUCTION_ENABLED', False) is True
            and getattr(settings, 'CYBERTEST_PRODUCTION_ALL_COMPANIES', False) is True)


def production_origin():
    """Defend the effective runtime settings as well as startup environment."""
    flags = ('CYBERTEST_ENTRY_PRODUCTION_ENABLED', 'CYBERTEST_ENTRY_LOCAL_ENABLED',
             'CYBERTEST_ENTRY_STAGING_ENABLED', 'CYBERTEST_LIVE_BILLING_ENABLED',
             'CYBERTEST_PHASED_SCANS_ENABLED', 'CYBERTEST_PRODUCTION_ALL_COMPANIES', 'DEBUG')
    values = {name: str(getattr(settings, name, False)).lower() for name in flags}
    values.update({name: getattr(settings, name, '') for name in (
        'APP_ENV', 'CYBERTEST_APP_ORIGIN', 'CYBERTEST_BASE_PATH',
        'CYBERTEST_BILLING_MODE', 'CYBERTEST_BILLING_API_KEY')})
    values['CYBERTEST_PRODUCTION_COMPANY_IDS'] = ','.join(sorted(production_companies()))
    try:
        configured = production_entry_settings(values)
        backend = settings.CACHES.get('default', {}).get('BACKEND')
        if (not getattr(settings, 'CYBERTEST_PERSISTENT_BINDINGS_ENABLED', False)
                or backend not in ('django.core.cache.backends.redis.RedisCache', 'django_redis.cache.RedisCache')):
            raise ValueError()
        return configured['CYBERTEST_APP_ORIGIN']
    except ValueError as exc:
        raise EntryError('ENTRY_UNAVAILABLE', 'Cybertest production entry is not configured.', 503) from exc


def staging_companies():
    values = getattr(settings, 'CYBERTEST_STAGING_COMPANY_IDS', [])
    try:
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError()
        return {str(UUID(str(value))) for value in values}
    except (ValueError, TypeError, AttributeError) as exc:
        raise EntryError('ENTRY_UNAVAILABLE', 'Configure the staging test companies.', 503) from exc


def entry_metadata():
    if getattr(settings, 'CYBERTEST_ENTRY_PRODUCTION_ENABLED', False):
        return {'local_test': False, 'entry_mode': 'production'}
    staging = getattr(settings, 'CYBERTEST_ENTRY_STAGING_ENABLED', False)
    return {'local_test': not staging, 'entry_mode': 'staging' if staging else 'local'}


def workspace_for_company(company):
    if getattr(settings, 'CYBERTEST_PERSISTENT_BINDINGS_ENABLED', False):
        binding = CybertestCompanyBinding.objects.filter(company_id=company.pk).first()
        if binding is None and general_access():
            # Caller has already checked current company ownership. OneToOne
            # makes concurrent first visits converge. Never reactivate an
            # existing pending/revoked row or change its workspace/creator.
            binding, _ = CybertestCompanyBinding.objects.get_or_create(company=company, defaults={
                'workspace_id': uuid4(), 'created_by_id': company.profile.user_id,
                'is_active': True, 'verified_at': timezone.now(),
            })
        if binding is None or not binding.is_active or binding.verified_at is None:
            raise EntryError('COMPANY_UNMAPPED', 'This company has no active audit workspace.', 409)
        return str(binding.workspace_id)
    if not settings.DEBUG or not getattr(settings, 'CYBERTEST_ENTRY_LOCAL_ENABLED', False):
        raise EntryError('ENTRY_UNAVAILABLE', 'Company workspace mapping is not enabled.', 503)
    binding = getattr(settings, 'CYBERTEST_LOCAL_COMPANY_WORKSPACES', {}).get(str(company.pk))
    try:
        return str(UUID(str(binding)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise EntryError('COMPANY_UNMAPPED', 'This local company has no test workspace mapping.', 409) from exc


def authorize(session_id, company_id):
    local_origin()
    if getattr(settings, 'CYBERTEST_ENTRY_PRODUCTION_ENABLED', False) and not general_access() and company_id not in production_companies():
        raise EntryError('COMPANY_UNAVAILABLE', 'This company is not enabled for security scans.', 403)
    if getattr(settings, 'CYBERTEST_ENTRY_STAGING_ENABLED', False) and company_id not in staging_companies():
        raise EntryError('COMPANY_UNAVAILABLE', 'This company is not enabled for the test workspace.', 403)
    session = CommunityChatAccountSession.objects.select_related('user').filter(pk=session_id).first()
    if not _valid_session(session, timezone.now()):
        raise EntryError('ACCESS_REVOKED', 'Your MLAI session expired. Sign in again.', 401)
    _validate_device_owner(session)
    require_community_access(session.user)
    company = VibeRaisingCompany.objects.select_related('organization').filter(
        pk=company_id, profile__user=session.user).first()
    if not company or (company.organization_id and not user_may_use_organization(session.user, company.organization)):
        raise EntryError('COMPANY_UNAVAILABLE', 'This company is unavailable.', 403)
    workspace_id = workspace_for_company(company)
    result = dict(user_id=str(session.user_id), company_id=str(company.pk),
                  workspace_id=workspace_id, company_name=company.name)
    if general_access(): result['workspace_provisioning'] = True
    return result


def authority():
    local_origin()
    return EntryAuthority(cache, authorize)


class EntryErrors:
    def handle_exception(self, exc):
        if isinstance(exc, EntryError):
            return Response({'code': exc.code, 'message': str(exc)}, status=exc.status)
        return super().handle_exception(exc)


class EntryView(EntryErrors, MyStartupAccess, PrivateView):
    def get(self, request):
        origin = local_origin()
        session = request.community_chat_account_session
        identity = authorize(str(session.pk), str(self.company.pk))
        return Response({**identity, **entry_metadata(),
                         'start_url': origin + base_path(getattr(settings,'CYBERTEST_BASE_PATH','')) + '/auth/mlai/start?' + urlencode({'company_id': identity['company_id']})})


class AuthorizeView(EntryErrors, MyStartupAccess, PrivateView):
    def post(self, request):
        origin = local_origin()
        allowed = {'company_id', 'companyId', 'state', 'code_challenge'}
        if not isinstance(request.data, dict) or set(request.data) - allowed:
            raise EntryError('INVALID_ENTRY', 'Unsupported authorization fields.')
        session = request.community_chat_account_session
        code = authority().issue(str(session.pk), str(request.user.pk), str(self.company.pk),
                                 request.data.get('state'), request.data.get('code_challenge'))
        return Response({'callback_url': origin + base_path(getattr(settings,'CYBERTEST_BASE_PATH','')) + '/auth/mlai/callback?' + urlencode(
            {'code': code, 'state': request.data['state']}), **entry_metadata()})


class ExchangeView(EntryErrors, ServiceView):
    def post(self, request):
        if not isinstance(request.data, dict) or set(request.data) != {'code','state','verifier'}:
            raise EntryError('INVALID_ENTRY', 'Invalid exchange.')
        return Response(authority().exchange(**request.data))


class EntryInspectServiceThrottle(SimpleRateThrottle):
    """Bound authenticated introspection without consuming checkout capacity."""
    rate = '300/minute'
    scope = 'cybertest_entry_inspect_service'

    def get_cache_key(self, request, view):
        # ServicePermission runs first. There is one configured service identity.
        return self.cache_format % {'scope': self.scope, 'ident': 'service'}


class EntryInspectGrantThrottle(SimpleRateThrottle):
    """Separate sessions fairly; never persist raw grants in throttle keys."""
    # Page assets, two checkout tabs, heartbeats and refreshes share one grant.
    rate = '120/minute'
    scope = 'cybertest_entry_inspect_grant'

    def get_cache_key(self, request, view):
        grant = request.data.get('grant') if isinstance(request.data, dict) else None
        ident = (hashlib.sha256(grant.encode()).hexdigest()
                 if isinstance(grant, str) and re.fullmatch(r'[A-Za-z0-9_-]{43,128}', grant)
                 else 'invalid')
        return self.cache_format % {'scope': self.scope, 'ident': ident}


class InspectView(EntryErrors, ServiceView):
    throttle_classes = [EntryInspectServiceThrottle, EntryInspectGrantThrottle]

    def post(self, request):
        if not isinstance(request.data, dict) or set(request.data) != {'grant'}:
            raise EntryError('INVALID_ENTRY', 'Invalid session check.')
        return Response(authority().inspect(request.data['grant']))
