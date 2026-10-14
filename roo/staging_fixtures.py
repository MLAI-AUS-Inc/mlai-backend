"""Idempotent synthetic records using the real ledger, never resetting balances."""
from django.conf import settings
from django.db import connection,transaction
from django.contrib.auth import get_user_model
from django.utils import timezone
from community_chat.staging_password import require_password_pilot
from community_chat.models import CommunityMemberProfile
from roo.models import Ledger,PointsAccount,MeetingRoom,RewardsCatalog
from roo.services import PointsService
ALIASES=('ashwin',*(f'volunteer{i:02d}' for i in range(1,8)))
EMAILS=tuple(f'{alias}@staging.mlai.au' for alias in ALIASES)
ROOMS=('small-meeting-room','big-meeting-room')

def require_fixture_database():
    require_password_pilot()
    expected=getattr(settings,'STAGING_FIXTURE_DATABASE','staging_account_api')
    if settings.DATABASES['default']['NAME']!=expected:raise ValueError('Unexpected fixture database.')
    with connection.cursor() as cursor:
        cursor.execute('select current_database()')
        if cursor.fetchone()[0]!=expected:raise ValueError('Unexpected connected fixture database.')

def require_fixture(user):
    profile=CommunityMemberProfile.objects.filter(user=user).first()
    if not user.is_active or user.is_staff or user.is_superuser or not user.email.endswith('@staging.mlai.au') or not profile or 'synthetic_staging_pilot' not in profile.review_reasons:
        raise ValueError('Only ordinary marked synthetic staging fixtures may be seeded.')

@transaction.atomic
def seed_user(user):
    require_fixture_database()
    user=get_user_model().objects.select_for_update().get(pk=user.pk);require_fixture(user)
    key=f'staging-fixture-v1:{user.pk}'
    ledger,_=PointsService.award(user=user,delta=12,source='MANUAL',description='Synthetic staging contribution fixture',created_by_slack_id='STAGING_LOCAL',idempotency_key=key+':general',reference_type='STAGING_FIXTURE',reference_id=str(user.pk))
    account=PointsAccount.objects.select_for_update().get(user=user)
    digital=Ledger.objects.filter(idempotency_key=key+':digital').first()
    if digital is None:
        Ledger.objects.create(user=user,kind='ADJUST',source='MANUAL',delta=0,delta_microroo=0,digital_delta_microroo=24_000_000,description='Synthetic staging digital-only fixture',created_by_slack_id='STAGING_LOCAL',idempotency_key=key+':digital',reference_type='STAGING_FIXTURE',reference_id=str(user.pk))
        account.digital_balance_microroo+=24_000_000;account.save(update_fields=['digital_balance_microroo'])
    elif digital.user_id!=user.pk or digital.digital_delta_microroo!=24_000_000:raise ValueError('Conflicting digital fixture ledger.')
    from community_chat.volunteer.models import VolunteerRecognition
    from community_chat.volunteer.access import community_id
    VolunteerRecognition.objects.get_or_create(community=community_id(),user=user,outcome_key=key,defaults=dict(action_key='staging_fixture',source={'kind':'synthetic_staging'},policy_snapshot={'synthetic':True},occurred_at=timezone.now(),note='Synthetic staging contribution example',status='approved',reward_microroo=12_000_000,ledger=ledger))

@transaction.atomic
def seed_catalogue():
    require_fixture_database()
    for slug in ROOMS:
        # These existing migration-created rooms are in the synthetic-only DB.
        room,_=MeetingRoom.objects.get_or_create(slug=slug,defaults={'name':'Synthetic '+slug.replace('-',' ').title()})
        if not room.name.startswith('Synthetic '):room.name='Synthetic '+room.name;room.save(update_fields=['name'])
    for code,fulfillment in [('STAGING_AUTO','auto'),('STAGING_MANUAL','manual')]:
        RewardsCatalog.objects.get_or_create(code=code,defaults={'name':'Synthetic staging reward','description':'No physical fulfillment; isolated test fixture','cost_points':1,'fulfillment':fulfillment,'stock_remaining':100})
