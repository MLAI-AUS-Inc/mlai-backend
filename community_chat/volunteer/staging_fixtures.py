"""Additive examples for the isolated password-staging database only."""
import re
import uuid
from datetime import date
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from mlai.staging_integrations import is_isolated_staging
from roo.staging_fixtures import EMAILS, require_fixture_database, require_fixture
from .access import community_id
HOST='chat-staging.mlai.au'
EVENT_ID='staging-volunteer-event-v1'
NAMES='_start-here boost-my-startup cowork-and-chill-melbourne events general healthhack i-need-advice-or-help jobs-and-founder-matching lost-and-found-items openclaw random-and-memes robots-and-hardware tech_volunteers victor-ai-founder-games volunteer token-leaderboard promo-and-offers'.split()
def fixture_id(name):return uuid.uuid5(uuid.NAMESPACE_DNS,HOST+'/feature-fixture/v1/'+name)
def channel_id(name):return 'a613b1cf-5492-53e6-931d-94592926df78' if name=='general' else str(uuid.uuid5(uuid.NAMESPACE_DNS,HOST+'/public-channel/v1/'+name))
def validate_receipt(receipt):
    if not isinstance(receipt,dict) or receipt.get('version')!=1 or receipt.get('community_host')!=HOST or not re.fullmatch('[a-f0-9]{64}',str(receipt.get('manifest_sha256',''))):raise ValueError('Unexpected staging channel receipt.')
    rows=receipt.get('channels',[])
    if len(rows)!=17 or {r.get('name') for r in rows}!=set(NAMES):raise ValueError('Incomplete staging catalogue.')
    for r in rows:
        if r.get('id')!=channel_id(r['name']) or not re.fullmatch('[a-f0-9]{64}',str(r.get('starter_event_id',''))):raise ValueError('Unexpected staging channel identity.')
    return {r['name']:r for r in rows}
def synthetic_public_event(event_id):
    if not is_isolated_staging() or event_id!=EVENT_ID:return None
    return dict(id=EVENT_ID,name='Synthetic staging volunteer session',description='A local test invitation. No external event booking or attendance.',url=f'https://{HOST}/channels/{channel_id("volunteer")}',start_at='2030-01-15T07:00:00Z',end_at='2030-01-15T08:00:00Z')

@transaction.atomic
def seed_features(receipt,*,apply=False):
    require_fixture_database()
    if community_id()!=HOST:raise ValueError('Unexpected fixture community.')
    channels=validate_receipt(receipt)
    users=list(get_user_model().objects.select_for_update().filter(email__in=EMAILS).order_by('email'))
    if {u.email for u in users}!=set(EMAILS):raise ValueError('All eight existing synthetic accounts are required.')
    for user in users:require_fixture(user)
    from .models import VolunteerProject,VolunteerOpportunity
    from founder_tools.models import VibeRaisingCompany,VibeRaisingProfile
    from founder_tools.services import ensure_company_organization
    from startup_updates.models import MonthlyUpdateDraft
    from startup_updates.update_identity import resolve_update
    from startup_updates.revisions import capture_snapshot,save_revision
    created=preserved=0
    def source(name):return dict(channel_id=channels[name]['id'],thread_root_id=channels[name]['starter_event_id'])
    guide=users[0]
    project_id=fixture_id('volunteer-project')
    planned=[(VolunteerProject,project_id,dict(community=HOST,title='Synthetic staging hardware helper project',purpose='Explore the Volunteer flow safely',description='Synthetic test brief; discuss it in the staging conversation.',guide=guide,source=source('tech_volunteers'),published=True)),
        (VolunteerOpportunity,fixture_id('project-opportunity'),dict(community=HOST,kind='project',project_id=project_id,action_key='staging_project',title='Synthetic: help with a staging prototype',purpose='Try the project conversation',description='Synthetic data only. No real assignment or reward.',guide=guide,reviewer=guide,source=source('tech_volunteers'),reward_microroo=0,reward_max_microroo=0)),
        (VolunteerOpportunity,fixture_id('event-opportunity'),dict(community=HOST,kind='event',event_id=EVENT_ID,action_key='staging_event',title='Synthetic: volunteer session',purpose='Try a local event invitation',description='Synthetic data only. No external booking or attendance.',guide=guide,reviewer=guide,source=source('volunteer'),reward_microroo=0,reward_max_microroo=0))]
    for model,pk,defaults in planned:
        old=model.objects.filter(pk=pk).first()
        if old and (old.community!=HOST or old.source!=defaults['source']):raise ValueError('Conflicting Volunteer fixture.')
    for user in users:
        for number in range(2 if user.email==EMAILS[0] else 1):
            pk=fixture_id(f'{user.email}/company/{number}')
            old=VibeRaisingCompany.objects.filter(pk=pk).first()
            if old and old.profile.user_id!=user.pk:raise ValueError('Conflicting company owner.')
    if not apply:return dict(dry_run=True,accounts=len(users),companies=9,volunteer_records=3)
    for model,pk,defaults in planned:
        _,new=model.objects.get_or_create(pk=pk,defaults=defaults);created+=int(new);preserved+=int(not new)
    for user in users:
        profile,_=VibeRaisingProfile.objects.get_or_create(user=user,defaults={'role':'founder'})
        if profile.role!='founder':raise ValueError('Conflicting founder profile.')
        for number in range(2 if user.email==EMAILS[0] else 1):
            company,new=VibeRaisingCompany.objects.get_or_create(pk=fixture_id(f'{user.email}/company/{number}'),defaults=dict(profile=profile,name=f'Synthetic {user.email.split("@")[0]} startup {number+1}',default_audience_visibility=['just_me']))
            created+=int(new);preserved+=int(not new)
            organization=ensure_company_organization(company)
            if not profile.active_company_id:profile.active_company=company;profile.save(update_fields=['active_company'])
            key=fixture_id(f'{user.email}/draft/{number}')
            if not MonthlyUpdateDraft.objects.filter(organization=organization,creation_key=key).exists():
                draft,_=resolve_update(organization,month=date(2026,10,1),creation_key=key)
                save_revision(draft,{'summary':'Synthetic private staging draft. Edit this example with your test account.','_audience_visibility':['just_me']},snapshot=capture_snapshot(organization,draft.month),expected_revision=None,audience='private',validation={'groundedness_status':'founder_asserted'})
                created+=1
            else:preserved+=1
    return dict(dry_run=False,created=created,preserved=preserved,accounts=len(users))
