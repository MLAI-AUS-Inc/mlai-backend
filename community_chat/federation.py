"""One-use, confidential-client identity handoff; no production Chat enrollment."""
import base64
import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.core.exceptions import ValidationError as ModelValidationError
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, NotFound, PermissionDenied, ValidationError
from rest_framework.exceptions import APIException

from .federation_models import ChatFederationGrant
from .models import CommunityChatAccountSession
from .onboarding import has_community_access

CLIENT_ID = 'mlai-chat-staging'


class FederationUnavailable(APIException):
    status_code = 503
    default_detail = 'MLAI identity handoff is unavailable.'


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def pkce_challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')


def require_issuer():
    if not settings.COMMUNITY_CHAT_FEDERATION_ISSUER_ENABLED:
        raise NotFound()
    if not settings.COMMUNITY_CHAT_HOST_ONLY_COOKIES:
        raise FederationUnavailable()


def require_client(request):
    require_issuer()
    secret = settings.COMMUNITY_CHAT_FEDERATION_CLIENT_SECRET
    if len(secret) < 32:
        raise FederationUnavailable()
    if not hmac.compare_digest(str(request.headers.get('Authorization') or ''), f'Bearer {secret}'):
        raise AuthenticationFailed('Invalid identity client.')


def eligible_tester(user):
    return bool(user and str(user.community_chat_profile_id) in settings.COMMUNITY_CHAT_FEDERATION_TESTER_IDS
                and has_community_access(user))


def require_tester(user):
    if not eligible_tester(user):
        raise PermissionDenied('This account is not approved for staging.')


def create_request(data):
    if data.get('client_id') != CLIENT_ID or data.get('callback_url') != settings.COMMUNITY_CHAT_FEDERATION_CALLBACK_URL:
        raise ValidationError('Invalid identity client or callback.')
    challenge, state = str(data.get('code_challenge') or ''), str(data.get('state') or '')
    if data.get('code_challenge_method') != 'S256' or not re.fullmatch(r'[A-Za-z0-9_-]{43}', challenge):
        raise ValidationError('A valid S256 challenge is required.')
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', state):
        raise ValidationError('A valid sign-in state is required.')
    record = ChatFederationGrant.objects.create(client_id=CLIENT_ID,
        callback_url=settings.COMMUNITY_CHAT_FEDERATION_CALLBACK_URL,
        state=state, code_challenge=challenge, expires_at=timezone.now()+timedelta(minutes=5))
    return {'request_id':str(record.id), 'authorization_url':settings.COMMUNITY_CHAT_FEDERATION_APPROVAL_URL+'?'+urlencode({'request_id':record.id})}


def authorize_request(request_id, session):
    require_issuer()
    now = timezone.now()
    with transaction.atomic():
        user = get_user_model().objects.select_for_update().get(pk=session.user_id)
        require_tester(user)
        locked = CommunityChatAccountSession.objects.select_for_update().filter(pk=session.pk, user=user).first()
        if (not locked or locked.revoked_at or locked.auth_version != user.auth_version or
            locked.access_expires_at <= now or locked.expires_at <= now or
            locked.access_token_hash != session.access_token_hash or locked.client_id != 'mlai-chat-web'):
            raise AuthenticationFailed('MLAI session is no longer valid.')
        try:
            record = ChatFederationGrant.objects.select_for_update().get(pk=request_id)
        except (ChatFederationGrant.DoesNotExist, ValueError, TypeError, ModelValidationError):
            raise ValidationError('Invalid identity request.')
        if record.expires_at <= now or record.code_hash or record.client_id != CLIENT_ID:
            raise ValidationError('Identity request is expired or already approved.')
        code = secrets.token_urlsafe(48)
        record.user = user
        record.code_hash = digest(code)
        record.code_expires_at = now + timedelta(seconds=60)
        record.save(update_fields=('user', 'code_hash', 'code_expires_at'))
    return {'callback_url':record.callback_url+'?'+urlencode({'code':code, 'state':record.state})}


def exchange_code(data):
    verifier, code = str(data.get('code_verifier') or ''), str(data.get('code') or '')
    if (data.get('client_id') != CLIENT_ID or data.get('callback_url') != settings.COMMUNITY_CHAT_FEDERATION_CALLBACK_URL or
        not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier) or not re.fullmatch(r'[A-Za-z0-9_-]{64}', code)):
        raise ValidationError('Invalid identity grant.')
    now = timezone.now()
    candidate = ChatFederationGrant.objects.filter(code_hash=digest(code)).first()
    if not candidate or not candidate.user_id:
        raise ValidationError('Invalid identity grant.')
    with transaction.atomic():
        user = get_user_model().objects.select_for_update().get(pk=candidate.user_id)
        record = ChatFederationGrant.objects.select_for_update().get(pk=candidate.pk)
        if (record.consumed_at or not record.code_expires_at or record.code_expires_at <= now or
            record.callback_url != data['callback_url'] or record.client_id != CLIENT_ID or
            not hmac.compare_digest(record.code_challenge, pkce_challenge(verifier))):
            raise ValidationError('Invalid identity grant.')
        require_tester(user)
        if ChatFederationGrant.objects.filter(pk=record.pk, consumed_at__isnull=True).update(consumed_at=now) != 1:
            raise ValidationError('Identity grant has been used.')
        return identity_payload(user)


def identity_payload(user):
    return {'issuer':settings.COMMUNITY_CHAT_FEDERATION_ISSUER,
            'subject':str(user.community_chat_profile_id),
            'display_name':user.full_name[:80], 'eligible':eligible_tester(user)}


def check_subject(subject):
    try:
        user = get_user_model().objects.filter(community_chat_profile_id=subject).first()
    except (ValueError, TypeError, ModelValidationError):
        user = None
    if not user or not ChatFederationGrant.objects.filter(user=user, client_id=CLIENT_ID, consumed_at__isnull=False).exists():
        raise PermissionDenied('Identity has not been granted to this client.')
    return identity_payload(user)
