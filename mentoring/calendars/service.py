"""Owner-scoped personal-calendar OAuth, connection selection and booking adapter."""
import base64
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from integrations.fields import _configured_keyring
from mentoring.calendar_models import MentorCalendarConnection
from mentoring.models import MentorProfile
from mentoring.service import MentoringError

from . import google
from .errors import CalendarError

STATE_TTL = 600
STATE_SALT = 'mentoring-personal-google-calendar-v1'


def _setting(name):
    return str(getattr(settings,name,'') or '').strip()


def configured(*, maintenance=False):
    """Personal-calendar grants require explicit deployment config and encryption."""
    if not maintenance and not getattr(settings,'MENTOR_CALENDAR_GOOGLE_ENABLED',False):
        return False
    names=('MENTOR_CALENDAR_GOOGLE_CLIENT_ID','MENTOR_CALENDAR_GOOGLE_CLIENT_SECRET','MENTOR_CALENDAR_GOOGLE_REDIRECT_URI')
    if not all(_setting(name) for name in names):
        return False
    parsed=urlparse(_setting('MENTOR_CALENDAR_GOOGLE_REDIRECT_URI'))
    local=bool(getattr(settings,'IS_LOCAL_ENV',False))
    if parsed.scheme != 'https' and not (local and parsed.scheme=='http' and parsed.hostname in ('localhost','127.0.0.1')):
        return False
    if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if not local and 'redis' not in settings.CACHES.get('default',{}).get('BACKEND','').lower():
        # OAuth nonce consumption and token-refresh locks must be shared across
        # web processes. Do not degrade to process-local replay protection.
        return False
    try:
        _configured_keyring()
        from mentoring.google import _staging_blocked
        return not _staging_blocked()
    except ImproperlyConfigured:
        return False


def _require_configured(*, maintenance=False):
    if not configured(maintenance=maintenance):
        raise CalendarError('calendar_not_configured')


def _profile(user):
    profile=MentorProfile.objects.filter(user=user,is_active=True).first()
    if profile is None:
        raise MentoringError('mentor_inactive','An approved mentor profile is required.',403)
    return profile


def begin_authorization(user, return_path='/mentoring/settings', *, account_session=None):
    """Create a short-lived single-use authorisation bound to the signed-in owner."""
    _require_configured()
    _profile(user)
    if return_path != '/mentoring/settings':
        raise MentoringError('invalid_return_path','Return to Mentoring settings.',400)
    if account_session is None or account_session.user_id!=user.pk:
        raise CalendarError('invalid_account_session')
    nonce=secrets.token_urlsafe(32)
    verifier=secrets.token_urlsafe(64)
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    state=signing.dumps({'uid':user.pk,'nonce':nonce,'return_path':return_path},salt=STATE_SALT)
    key='mentoring:calendar:oauth:'+hashlib.sha256(nonce.encode()).hexdigest()
    cache.set(key,{'user_id':user.pk,'return_path':return_path,'verifier':verifier,
                   'account_session_id':str(account_session.pk),'origin':account_session.origin,
                   'intent':'mentoring_calendar'},STATE_TTL)
    query={'client_id':_setting('MENTOR_CALENDAR_GOOGLE_CLIENT_ID'),
           'redirect_uri':_setting('MENTOR_CALENDAR_GOOGLE_REDIRECT_URI'),
           'response_type':'code','access_type':'offline','prompt':'consent select_account',
           'include_granted_scopes':'false','scope':' '.join(google.SCOPES),'state':state,
           'code_challenge':challenge,'code_challenge_method':'S256'}
    return {'authorization_url':'https://accounts.google.com/o/oauth2/v2/auth?'+urlencode(query)}


def _consume_state(raw):
    try:
        data=signing.loads(raw,salt=STATE_SALT,max_age=STATE_TTL)
        nonce=data['nonce']
        if not isinstance(nonce,str) or len(nonce)<32:
            raise ValueError
        key='mentoring:calendar:oauth:'+hashlib.sha256(nonce.encode()).hexdigest()
        expected=cache.get(key)
        if not expected or expected['user_id']!=data['uid'] or data['return_path']!='/mentoring/settings':
            raise ValueError
        if not cache.add(key+':used',True,STATE_TTL):
            raise ValueError
        cache.delete(key)
        return expected
    except (signing.BadSignature,TypeError,ValueError,KeyError):
        raise CalendarError('invalid_oauth_state') from None


def _expires(data):
    value=data.get('expires_in',3600)
    if isinstance(value,bool):
        raise CalendarError('invalid_response')
    try:
        seconds=int(value)
    except (TypeError,ValueError,OverflowError):
        raise CalendarError('invalid_response') from None
    if seconds<60 or seconds>86400:
        raise CalendarError('invalid_response')
    return timezone.now()+timedelta(seconds=seconds)


def finish_authorization(raw_state, code, *, denied=False):
    """Verify Google identity and preserve tokens only for that same identity."""
    _require_configured()
    state=_consume_state(raw_state)
    if denied:
        raise CalendarError('authorization_denied')
    if not isinstance(code,str) or not code or len(code)>4096:
        raise CalendarError('invalid_oauth_code')
    user=get_user_model().objects.filter(pk=state['user_id'],is_active=True).first()
    if user is None:
        raise CalendarError('invalid_oauth_state')
    _profile(user)
    from community_chat.onboarding import has_community_access
    if not has_community_access(user):
        raise CalendarError('invalid_account_session')
    from community_chat.models import CommunityChatAccountSession
    from community_chat.account_sessions import _valid_session
    session=CommunityChatAccountSession.objects.select_related('user').filter(
        pk=state.get('account_session_id'),user=user).first()
    if session is None or not _valid_session(session,timezone.now()) or session.origin!=state.get('origin') or state.get('intent')!='mentoring_calendar':
        raise CalendarError('invalid_account_session')
    token=google.send('POST',google.TOKEN_URL,data={
        'client_id':_setting('MENTOR_CALENDAR_GOOGLE_CLIENT_ID'),
        'client_secret':_setting('MENTOR_CALENDAR_GOOGLE_CLIENT_SECRET'),
        'redirect_uri':_setting('MENTOR_CALENDAR_GOOGLE_REDIRECT_URI'),
        'code':code,'grant_type':'authorization_code','code_verifier':state['verifier']})
    access=token.get('access_token')
    scopes=str(token.get('scope') or '').split()
    if not isinstance(access,str) or not access or not google.CALENDAR_SCOPES.issubset(scopes):
        raise CalendarError('insufficient_scopes')
    identity=google.send('GET',google.USERINFO_URL,headers={'Authorization':'Bearer '+access})
    subject,email=identity.get('sub'),identity.get('email')
    if not isinstance(subject,str) or not subject or len(subject)>255 or not isinstance(email,str) or len(email)>254 or '@' not in email or identity.get('email_verified') is not True:
        raise CalendarError('unverified_identity')
    expiry=_expires(token)
    calendars=google.GoogleCalendarClient(access).list_calendars()
    with transaction.atomic():
        # Serialises reconnects for this MLAI user and exact Google subject.
        get_user_model().objects.select_for_update().get(pk=user.pk)
        connection=MentorCalendarConnection.objects.select_for_update().filter(user=user,provider='google',provider_subject=subject).first()
        if connection is None:
            connection=MentorCalendarConnection(user=user,provider='google',provider_subject=subject)
        refresh=token.get('refresh_token') or connection.refresh_token
        if not isinstance(refresh,str) or not refresh:
            raise CalendarError('offline_access_required')
        connection.email=email
        connection.refresh_token=refresh
        connection.access_token=access
        connection.access_token_expires_at=expiry
        connection.scopes=scopes
        connection.status='connected'
        connection.calendar_metadata=calendars
        connection.checked_at=None
        connection.last_error_code=''
        connection.revision+=1
        connection.save()
        MentorProfile.objects.filter(user=user).update(booking_revision=F('booking_revision')+1)
    return connection


def _access_token(connection):
    if connection.status!='connected':
        raise CalendarError('reconnect_required')
    if connection.access_token and connection.access_token_expires_at and connection.access_token_expires_at>timezone.now()+timedelta(seconds=60):
        return connection.access_token
    key=f'mentoring:calendar:refresh:{connection.pk}'
    lock=secrets.token_hex(16)
    if not cache.add(key,lock,30):
        raise CalendarError('refresh_in_progress')
    try:
        fresh=MentorCalendarConnection.objects.get(pk=connection.pk)
        if fresh.status!='connected' or fresh.revision!=connection.revision:
            raise CalendarError('reconnect_required')
        if fresh.access_token and fresh.access_token_expires_at and fresh.access_token_expires_at>timezone.now()+timedelta(seconds=60):
            connection.access_token=fresh.access_token
            connection.access_token_expires_at=fresh.access_token_expires_at
            return fresh.access_token
        try:
            token=google.send('POST',google.TOKEN_URL,data={
                'client_id':_setting('MENTOR_CALENDAR_GOOGLE_CLIENT_ID'),
                'client_secret':_setting('MENTOR_CALENDAR_GOOGLE_CLIENT_SECRET'),
                'refresh_token':fresh.refresh_token,'grant_type':'refresh_token'})
        except CalendarError as exc:
            if exc.code in ('invalid_request','reconnect_required','permission'):
                MentorCalendarConnection.objects.filter(pk=fresh.pk,revision=fresh.revision).update(status='reconnect_required',last_error_code='reconnect_required')
            raise
        scope=token.get('scope')
        if scope is not None and not google.CALENDAR_SCOPES.issubset(str(scope).split()):
            MentorCalendarConnection.objects.filter(pk=fresh.pk,revision=fresh.revision).update(status='reconnect_required',last_error_code='insufficient_scopes')
            raise CalendarError('insufficient_scopes')
        access=token.get('access_token')
        if not isinstance(access,str) or not access:
            raise CalendarError('invalid_response')
        updates={'access_token':access,'access_token_expires_at':_expires(token)}
        if token.get('refresh_token'):
            updates['refresh_token']=token['refresh_token']
        changed=MentorCalendarConnection.objects.filter(pk=fresh.pk,revision=fresh.revision,status='connected').update(**updates)
        if not changed:
            raise CalendarError('reconnect_required')
        connection.access_token=access
        connection.access_token_expires_at=updates['access_token_expires_at']
        return access
    finally:
        # Leave the short lease to expire. get-then-delete is not an atomic
        # compare-delete and could release a successor's lock after expiry.
        pass


def _client(connection, *, maintenance=False):
    _require_configured(maintenance=maintenance)
    if not google.CALENDAR_SCOPES.issubset(connection.scopes):
        raise CalendarError('insufficient_scopes')
    return google.GoogleCalendarClient(lambda:_access_token(connection))


def public_connection(connection):
    """Owner-only connection representation, with no token or Google subject."""
    return {key:getattr(connection,key) for key in ('id','provider','email','status','conflict_calendar_ids','booking_calendar_id','revision','last_error_code')} | {
        'calendars':connection.calendar_metadata,
        'checked_at':connection.checked_at.isoformat() if connection.checked_at else None}


def refresh_calendars(user, connection_id):
    connection=_owned(user,connection_id)
    calendars=_client(connection).list_calendars()
    MentorCalendarConnection.objects.filter(pk=connection.pk,revision=connection.revision).update(calendar_metadata=calendars)
    connection.calendar_metadata=calendars
    return public_connection(connection)


def _owned(user, connection_id):
    if type(connection_id) is not int:
        raise MentoringError('invalid_connection','Choose a calendar connection.',400)
    connection=MentorCalendarConnection.objects.filter(pk=connection_id,user=user).first()
    if connection is None:
        raise MentoringError('calendar_connection_not_found','Calendar connection not found.',404)
    return connection


def select_calendars(user, payload):
    profile=_profile(user)
    connection=_owned(user,payload.get('connection_id'))
    ids=payload.get('conflict_calendar_ids')
    destination=payload.get('booking_calendar_id')
    if not isinstance(ids,list) or not ids or len(ids)>50 or any(not isinstance(i,str) or not i or len(i)>1024 for i in ids) or not isinstance(destination,str) or not destination or len(destination)>1024:
        raise MentoringError('invalid_selection','Choose conflict calendars and a booking destination.',400)
    calendars=_client(connection).list_calendars()
    by_id={c['id']:c for c in calendars}
    ids=list(dict.fromkeys([*ids,destination]))
    if len(ids)>50 or any(i not in by_id or not by_id[i]['can_check_conflicts'] for i in ids):
        raise MentoringError('invalid_selection','A selected calendar cannot be checked.',400)
    if destination not in by_id or not by_id[destination]['can_write']:
        raise MentoringError('calendar_read_only','Choose a calendar you own for bookings.',400)
    if profile.location_type=='video' and not by_id[destination]['can_create_meet']:
        raise MentoringError('conference_unavailable','This calendar cannot create Google Meet links.',400)
    now=timezone.now()
    _client(connection).fetch_busy(ids,now,now+timedelta(days=1))
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        fresh=MentorCalendarConnection.objects.select_for_update().get(pk=connection.pk,user=user)
        if fresh.revision!=connection.revision or fresh.status!='connected':
            raise CalendarError('settings_changed')
        MentorCalendarConnection.objects.filter(user=user).exclude(pk=fresh.pk).update(booking_calendar_id='')
        fresh.conflict_calendar_ids=ids
        fresh.booking_calendar_id=destination
        fresh.calendar_metadata=calendars
        fresh.checked_at=now
        fresh.last_error_code=''
        fresh.revision+=1
        fresh.save()
        # An explicit configuration change invalidates every prior booking quote.
        MentorProfile.objects.filter(pk=profile.pk).update(booking_revision=F('booking_revision')+1)
    return connections(user)


def readiness(profile):
    reasons=[]
    if not configured():
        reasons.append({'code':'calendar_not_configured','detail':'Google Calendar connection is not configured yet.'})
    if not profile.is_active:
        reasons.append({'code':'mentor_inactive','detail':'Your mentor profile needs approval.'})
    if not profile.availability_rules.exists():
        reasons.append({'code':'availability_required','detail':'Add at least one weekly availability window.'})
    selected=list(MentorCalendarConnection.objects.filter(user_id=profile.user_id).exclude(booking_calendar_id=''))
    if len(selected)!=1:
        reasons.append({'code':'calendar_required','detail':'Choose one calendar to receive bookings.'})
    for connection in MentorCalendarConnection.objects.filter(user_id=profile.user_id):
        if not connection.conflict_calendar_ids and not connection.booking_calendar_id:
            continue
        if connection.status!='connected' or connection.last_error_code or not connection.checked_at or not google.CALENDAR_SCOPES.issubset(connection.scopes):
            reasons.append({'code':'calendar_reconnect_required','detail':'Reconnect or check your selected calendar.'})
        metadata={item.get('id'):item for item in connection.calendar_metadata if isinstance(item,dict)}
        if any(i not in metadata or not metadata[i].get('can_check_conflicts') for i in connection.conflict_calendar_ids):
            reasons.append({'code':'calendar_reconnect_required','detail':'Check permissions on your selected calendars.'})
        destination=metadata.get(connection.booking_calendar_id,{})
        if connection.booking_calendar_id and (not destination.get('can_write') or (profile.location_type=='video' and not destination.get('can_create_meet'))):
            reasons.append({'code':'calendar_permission_required','detail':'Choose a writable calendar with the required meeting capability.'})
        if connection.booking_calendar_id and connection.booking_calendar_id not in connection.conflict_calendar_ids:
            reasons.append({'code':'invalid_selection','detail':'The booking calendar must also be checked for conflicts.'})
    return {'ready':not reasons,'reasons':reasons}


def connections(user):
    profile=_profile(user)
    return {'configured':configured(),'connections':[public_connection(c) for c in MentorCalendarConnection.objects.filter(user=user).order_by('pk')], 'readiness':readiness(profile)}


def disconnect(user, connection_id):
    connection=_owned(user,connection_id)
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        connection=MentorCalendarConnection.objects.select_for_update().get(pk=connection.pk,user=user)
        connection.status='disconnected'
        connection.conflict_calendar_ids=[]
        connection.booking_calendar_id=''
        connection.checked_at=None
        connection.refresh_token=''
        connection.access_token=''
        connection.access_token_expires_at=None
        connection.last_error_code=''
        connection.revision+=1
        connection.save()
        MentorProfile.objects.filter(user=user).update(accepting_bookings=False,booking_revision=F('booking_revision')+1)
    return connections(user)


def calendar_target(profile, *, verify=False):
    check=readiness(profile)
    if not check['ready']:
        raise CalendarError(check['reasons'][0]['code'])
    if verify:
        selected=list(MentorCalendarConnection.objects.filter(user_id=profile.user_id).exclude(conflict_calendar_ids=[]))
        for selected_connection in selected:
            metadata=_client(selected_connection).list_calendars()
            by_id={item['id']:item for item in metadata}
            if any(i not in by_id or not by_id[i]['can_check_conflicts'] for i in selected_connection.conflict_calendar_ids):
                raise CalendarError('permission')
            destination=selected_connection.booking_calendar_id
            if destination and (destination not in by_id or not by_id[destination]['can_write']):
                raise CalendarError('permission')
            if destination and profile.location_type=='video' and not by_id[destination]['can_create_meet']:
                raise CalendarError('conference_unavailable')
    c=MentorCalendarConnection.objects.get(user_id=profile.user_id,status='connected',booking_calendar_id__gt='')
    return {'connection_id':c.pk,'calendar_id':c.booking_calendar_id,'revision':c.revision}


def fetch_busy(profile, start, end):
    """Read every selected calendar live; partial errors never imply free time."""
    _require_configured()
    rows=list(MentorCalendarConnection.objects.filter(user_id=profile.user_id).exclude(conflict_calendar_ids=[]))
    if not rows:
        raise CalendarError('calendar_required')
    intervals=[]
    for c in rows:
        try:
            intervals.extend(_client(c).fetch_busy(c.conflict_calendar_ids,start,end))
        except CalendarError as exc:
            MentorCalendarConnection.objects.filter(pk=c.pk,revision=c.revision).update(last_error_code=exc.code)
            raise
        MentorCalendarConnection.objects.filter(pk=c.pk,revision=c.revision).update(checked_at=timezone.now(),last_error_code='')
    return sorted(set(intervals))


def _target(session,target=None):
    target=target or getattr(session,'calendar_snapshot',None)
    if not isinstance(target,dict):
        raise CalendarError('calendar_required')
    c=MentorCalendarConnection.objects.filter(pk=target.get('connection_id'),user_id=session.mentor.user_id,provider='google').first()
    if c is None or c.status!='connected':
        raise CalendarError('reconnect_required')
    identifier=target.get('calendar_id')
    if not isinstance(identifier,str) or not identifier:
        raise CalendarError('calendar_required')
    return c,identifier


def create_booking(session,target=None):
    c,identifier=_target(session,target)
    return _client(c).create_booking(identifier,session)


def lookup_booking(session,target=None):
    c,identifier=_target(session,target)
    client=_client(c,maintenance=True)
    event=client.get_booking(identifier,session.pk)
    return None if event is None else client.booking_result(event,session)


def cancel_booking(session,target=None):
    c,identifier=_target(session,target)
    return _client(c,maintenance=True).cancel_booking(identifier,session.pk)


def invite_attendees(session,target=None):
    c,identifier=_target(session,target)
    _client(c,maintenance=True).invite_attendees(identifier,session)


def check_after_create(session,start,end,target=None):
    """Revalidate other calendars and exclude only this destination's exact event."""
    c,identifier=_target(session,target)
    _client(c).check_destination_conflicts(identifier,start,end,session.pk)
    rows=MentorCalendarConnection.objects.filter(user_id=session.mentor.user_id).exclude(conflict_calendar_ids=[])
    for row in rows:
        ids=[i for i in row.conflict_calendar_ids if not (row.pk==c.pk and i==identifier)]
        if ids and _client(row).fetch_busy(ids,start,end):
            raise CalendarError('calendar_conflict')


def verify_booking(session,target=None):
    """Fresh post-create checks over the snapshotted full protected interval."""
    before=getattr(session,'buffer_before_minutes',15)
    after=getattr(session,'buffer_after_minutes',15)
    start=getattr(session,'protected_starts_at',None) or session.starts_at-timedelta(minutes=before)
    end=getattr(session,'protected_ends_at',None) or session.ends_at+timedelta(minutes=after)
    check_after_create(session,start,end,target)
