"""Staging relying party: identity proof creates only local account authority."""
import hmac
import json
import secrets
import uuid
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from .account_sessions import issue_account_session
from .federation import CLIENT_ID, FederationUnavailable, digest, pkce_challenge
from .federation_models import ChatFederationIdentity, ChatFederationTransaction
from .serializers import CommunityChatDeviceLoginSerializer

TRANSACTION_COOKIE = '__Host-mlai_staging_transaction'


def require_staging():
    if not settings.COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED:
        raise NotFound()
    if not settings.COMMUNITY_CHAT_HOST_ONLY_COOKIES or len(settings.COMMUNITY_CHAT_FEDERATION_CLIENT_SECRET) < 32:
        raise FederationUnavailable()


def issuer_post(endpoint, payload):
    """Fixed issuer, bounded body, TLS verification, no redirects or credential logs."""
    url = settings.COMMUNITY_CHAT_FEDERATION_ISSUER.rstrip('/') + '/api/v1/community-chat/federation/' + endpoint + '/'
    try:
        with requests.post(url, json=payload, headers={
                'Authorization':'Bearer '+settings.COMMUNITY_CHAT_FEDERATION_CLIENT_SECRET},
                timeout=(3,8), allow_redirects=False, stream=True) as response:
            if response.status_code != 200 and not (endpoint == 'requests' and response.status_code == 201):
                raise FederationUnavailable()
            body = bytearray()
            for chunk in response.iter_content(4096):
                body.extend(chunk)
                if len(body) > 32768:
                    raise FederationUnavailable()
            data = json.loads(body)
            if not isinstance(data, dict):
                raise FederationUnavailable()
            return data
    except (requests.RequestException, ValueError, TypeError):
        raise FederationUnavailable() from None


def start_transaction(data, origin):
    require_staging()
    if origin != settings.COMMUNITY_CHAT_FRONTEND_URL:
        raise PermissionDenied('Staging browser origin is required.')
    if set(data) - {'public_key','installation_id','platform','name'}:
        raise ValidationError('Unsupported device fields.')
    serializer = CommunityChatDeviceLoginSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    device = serializer.validated_data
    if device['platform'] != 'web':
        raise ValidationError('This pilot supports the browser only.')
    state, verifier, cookie = (secrets.token_urlsafe(48) for _ in range(3))
    registered = issuer_post('requests', dict(client_id=CLIENT_ID,
        callback_url=settings.COMMUNITY_CHAT_FEDERATION_CALLBACK_URL,
        state=state, code_challenge=pkce_challenge(verifier), code_challenge_method='S256'))
    try:
        request_id = uuid.UUID(registered['request_id'])
        approval = urlsplit(registered['authorization_url'])
        expected = urlsplit(settings.COMMUNITY_CHAT_FEDERATION_APPROVAL_URL)
        if (approval.scheme,approval.netloc,approval.path) != (expected.scheme,expected.netloc,expected.path) or approval.fragment or parse_qs(approval.query) != {'request_id':[str(request_id)]}:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise FederationUnavailable() from None
    record = ChatFederationTransaction.objects.create(cookie_hash=digest(cookie), state_hash=digest(state),
        verifier=verifier, public_key=device['public_key'], installation_id=device['installation_id'],
        origin=origin, device_name=device.get('name',''), request_id=request_id,
        expires_at=timezone.now()+timedelta(minutes=5))
    return record, cookie, registered['authorization_url']


def valid_identity(data, *, subject=None):
    if (set(data) != {'issuer','subject','display_name','eligible'} or
        data.get('issuer') != settings.COMMUNITY_CHAT_FEDERATION_ISSUER or data.get('eligible') is not True or
        not isinstance(data.get('display_name'),str) or len(data['display_name']) > 80):
        raise PermissionDenied('Identity is not approved for staging.')
    try:
        parsed_subject = str(uuid.UUID(data['subject']))
    except (ValueError, TypeError, AttributeError):
        raise PermissionDenied('Invalid identity subject.') from None
    if parsed_subject != data['subject'] or subject is not None and not hmac.compare_digest(subject, parsed_subject):
        raise PermissionDenied('Identity subject does not match.')
    return data


def complete_transaction(cookie, state, code):
    require_staging()
    now = timezone.now()
    with transaction.atomic():
        record = ChatFederationTransaction.objects.select_for_update().filter(cookie_hash=digest(cookie)).first()
        if not record or record.consumed_at or record.expires_at <= now or not record.request_id or not hmac.compare_digest(record.state_hash,digest(state)):
            raise ValidationError('Staging sign-in is expired or invalid. Start again.')
        if ChatFederationTransaction.objects.filter(pk=record.pk, consumed_at__isnull=True).update(consumed_at=now, verifier='') != 1:
            raise ValidationError('Staging sign-in has already been used.')
    # Burn the local transaction before the remote exchange. Failure requires a
    # new sign-in, so ambiguous network results cannot issue duplicate sessions.
    identity = valid_identity(issuer_post('token', dict(client_id=CLIENT_ID,
        callback_url=settings.COMMUNITY_CHAT_FEDERATION_CALLBACK_URL,
        code=code, code_verifier=record.verifier)))
    with transaction.atomic():
        mapping = ChatFederationIdentity.objects.select_for_update().filter(
            issuer=identity['issuer'], subject=identity['subject']).first()
        if mapping:
            user = get_user_model().objects.select_for_update().get(pk=mapping.user_id)
            if not user.is_active:
                raise PermissionDenied('Staging access has been removed.')
        else:
            # The unique subject and unique synthetic email prevent two first
            # handoffs from creating different accounts for the same identity.
            email = digest(identity['issuer']+'|'+identity['subject'])+'@staging.invalid'
            user, _ = get_user_model().objects.get_or_create(email=email,
                defaults={'first_name':identity['display_name'], 'password':'!', 'is_staff':False, 'is_superuser':False})
            mapping, _ = ChatFederationIdentity.objects.get_or_create(
                issuer=identity['issuer'], subject=identity['subject'],
                defaults={'user':user, 'last_checked_at':now})
            if mapping.user_id != user.pk:
                raise PermissionDenied('Staging account mapping changed.')
        if user.is_staff or user.is_superuser or user.has_usable_password():
            raise PermissionDenied('Invalid staging account authority.')
        mapping.last_checked_at = now
        mapping.save(update_fields=('last_checked_at',))
        return issue_account_session(user, SimpleNamespace(
            public_key=record.public_key, installation_id=record.installation_id,
            client_id='mlai-chat-web', origin=record.origin, platform='web', device_name=record.device_name))


def recheck_session(session):
    require_staging()
    mapping = ChatFederationIdentity.objects.filter(user_id=session.user_id).first()
    if not mapping:
        raise PermissionDenied('Staging identity is unavailable.')
    valid_identity(issuer_post('eligibility',{'subject':mapping.subject}), subject=mapping.subject)
    mapping.last_checked_at = timezone.now()
    mapping.save(update_fields=('last_checked_at',))
