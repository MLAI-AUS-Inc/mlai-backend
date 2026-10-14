"""Explicit synthetic outcome adapter, real purchase/consent/ledger records."""
import uuid
from django.db import transaction
from django.contrib.auth import get_user_model
from django.utils import timezone
from roo.staging_fixtures import require_fixture_database,require_fixture
from roo.models import PointsPurchase
from roo.services import PointsPurchaseService,PointsService

@transaction.atomic
def create_staging_purchase(user,pack_id,request_id,consent):
    require_fixture_database();user=get_user_model().objects.select_for_update().get(pk=user.pk);require_fixture(user)
    key='staging:'+str(user.pk)+':'+str(uuid.UUID(str(request_id)))
    if consent is not True:raise ValueError('Explicit terms/privacy acceptance required.')
    existing=PointsPurchase.objects.filter(checkout_request_id=key).first()
    if existing:
        if existing.user_id!=user.pk or existing.pack_id!=pack_id or existing.metadata.get('adapter')!='staging_local':raise ValueError('Purchase request identity conflict.')
        return existing
    purchase=PointsPurchaseService.create_purchase_for_user(user,pack_id,purchase_from={'source':'staging_local','surface':'chat'})
    purchase.checkout_request_id=key
    purchase.metadata={'adapter':'staging_local','synthetic':True}
    purchase.terms_version_accepted=PointsPurchaseService.DEFAULT_TERMS_VERSION;purchase.privacy_version_accepted=PointsPurchaseService.DEFAULT_PRIVACY_VERSION
    purchase.terms_accepted_at=timezone.now();purchase.privacy_accepted_at=timezone.now()
    purchase.save();return purchase

@transaction.atomic
def resolve_staging_purchase(user,purchase_id,outcome):
    require_fixture_database();user=get_user_model().objects.select_for_update().get(pk=user.pk);require_fixture(user)
    if outcome not in {'paid','cancelled','failed'}:raise ValueError('Unknown synthetic outcome.')
    purchase=PointsPurchase.objects.select_for_update().filter(pk=purchase_id,user=user).first()
    if not purchase or purchase.metadata.get('adapter')!='staging_local':raise ValueError('Owned synthetic purchase not found.')
    if purchase.status!='pending':
        if purchase.status!=outcome:raise ValueError('Purchase already has a different terminal outcome.')
        return purchase
    if purchase.expires_at<=timezone.now():raise ValueError('Purchase expired.')
    if outcome=='paid':
        ledger,_=PointsService.credit_purchased_topup(user=user,delta=purchase.points_amount,description='Synthetic local top-up; no payment charged',idempotency_key='staging-purchase:'+str(purchase.pk),reference_type='POINTS_PURCHASE',reference_id=str(purchase.pk),created_by_slack_id='STAGING_LOCAL')
        purchase.ledger_entry=ledger;purchase.paid_at=timezone.now()
    purchase.status=outcome;purchase.save();return purchase
