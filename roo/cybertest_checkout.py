"""Consent, exact debit and pre-dispatch refunds for Cybertest audits."""
import hmac
from datetime import timedelta
from uuid import UUID
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Sum, Q
from django.utils import timezone
from .cybertest_models import CybertestCheckout as Receipt
from .cybertest_policy import CheckoutError, binding, binding_digest, price_points, token_digest, failure_settlement
from .services import PointsService
from .cybertest_delivery import phase_price, purchase_contract


def configured_mode():
    mode=getattr(settings,'CYBERTEST_BILLING_MODE','disabled')
    if getattr(settings,'CYBERTEST_ENTRY_STAGING_ENABLED',False) and mode=='live':
        raise CheckoutError('BILLING_UNAVAILABLE','Staging cannot use real points.',503)
    if mode not in ('test','live') or (mode=='live' and not getattr(settings,'CYBERTEST_LIVE_BILLING_ENABLED',False)):
        raise CheckoutError('BILLING_UNAVAILABLE','Cybertest billing is not enabled.',503)
    return mode


def _current_mode(row):
    if (configured_mode()=='test') != row.test_mode:
        raise CheckoutError('QUOTE_MODE_CHANGED','Billing mode changed. Request a new quote.')
    if row.phase_contract and not getattr(settings, 'CYBERTEST_PHASED_SCANS_ENABLED', False):
        raise CheckoutError('BILLING_UNAVAILABLE', 'Phased scans are not enabled.', 503)


def _uuid(value):
    try:return UUID(str(value))
    except (ValueError,TypeError,AttributeError) as exc:
        raise CheckoutError('INVALID_REQUEST','A valid request UUID is required.',400) from exc


def _get(checkout_id, *, lock=False):
    query=Receipt.objects.select_for_update() if lock else Receipt.objects
    try:row=query.get(pk=_uuid(checkout_id))
    except Receipt.DoesNotExist as exc:
        raise CheckoutError('CHECKOUT_NOT_FOUND','The checkout is unavailable.',404) from exc
    if getattr(settings,'CYBERTEST_ENTRY_STAGING_ENABLED',False) and row.test_mode is not True:
        raise CheckoutError('QUOTE_MODE_CHANGED','Staging accepts test receipts only.')
    return row


def _handoff(row, token):
    if not hmac.compare_digest(row.handoff_hash,token_digest(token)):
        raise CheckoutError('CHECKOUT_NOT_FOUND','The checkout is unavailable.',404)


def _payer(row,user):
    expected = row.binding.get('mlai_user_id')
    if expected is not None and expected != str(user.pk):
        raise CheckoutError('WRONG_ACCOUNT','Return to the MLAI account that selected this audit.',403)
    if not user.is_active or (row.user_id is not None and row.user_id!=user.pk):
        raise CheckoutError('WRONG_ACCOUNT','This checkout belongs to another account.',403)


def _unexpired(row):
    if row.expires_at<=timezone.now():
        raise CheckoutError('QUOTE_EXPIRED','The price confirmation expired. Request a new quote.')


def receipt_status(row, *, include_plan=False):
    from .cybertest_plan import public_purchase_scope
    return {'checkout_id':str(row.id),'status':row.status,'points':row.points,
        'test_mode':row.test_mode,'pricing_snapshot':row.pricing_snapshot,'settlement':row.settlement,'binding':row.binding,'binding_sha256':row.binding_sha256,
        'expires_at':row.expires_at.isoformat(),'audit_id':str(row.audit_id) if row.audit_id else None,
        'phase_contract':purchase_contract(row.phase_contract) if row.phase_contract else {},
        'purchase_scope':public_purchase_scope(row.phase_contract),
        **({'execution_plan':row.phase_contract.get('executionPlan')} if include_plan and row.phase_contract else {}),
        'parent_checkout_id':str(row.parent_checkout_id) if row.parent_checkout_id else None,
        'total_points':phase_price(row.phase_contract)['totalPoints'] if row.phase_contract else row.points}


def status(checkout_id):
    return receipt_status(_get(checkout_id),include_plan=True)


def pending_refunds(after=None):
    # Read-only recovery survives disabling new billing and mode changes.
    rows=Receipt.objects.filter(status__in=['charged','started'],audit_id__isnull=False,settlement__isnull=True).order_by('id')
    if after:rows=rows.filter(id__gt=_uuid(after))
    page=list(rows[:101])
    return {'receipts':[receipt_status(row) for row in page[:100]],
        'next_cursor':str(page[99].id) if len(page)>100 else None}


def balance(user, *, test_mode):
    if test_mode:
        initial=getattr(settings,'CYBERTEST_TEST_BALANCE_POINTS',100)
        if isinstance(initial,bool) or not isinstance(initial,int) or initial<0:
            raise CheckoutError('BILLING_UNAVAILABLE','Test balance is not configured.',503)
        rows=Receipt.objects.filter(user=user,test_mode=True,status__in=['charged','started'])
        used=sum(row.settlement['charge_microroo'] if row.settlement else row.points*1_000_000 for row in rows)
        return max(0,initial*1_000_000-used)//1_000_000
    total=PointsService.get_balance(user)['digital_service_balance_microroo']
    return max(0,total-PointsService._reserved_microroo(user))//1_000_000


def _pending_basic(selection, *, test_mode):
    rows=Receipt.objects.filter(test_mode=test_mode,settlement__isnull=True,
        phase_contract__phase='basic',phase_contract__executionPlan__isnull=False)
    for name,value in selection.items():
        if name!='purchase':rows=rows.filter(**{f'binding__{name}':value})
    return rows.filter(Q(status__in=['quoted','authorized'],expires_at__gt=timezone.now())|
                       Q(status__in=['charged','started']))


@transaction.atomic
def create_quote(checkout_id, token, intent, *, execution_plan=None):
    test_mode=configured_mode()=='test'
    normalized=binding(intent);digest=binding_digest(normalized);secret_hash=token_digest(token)
    stored_plan=None
    if execution_plan is not None:
        from .cybertest_plan import execution_plan as normalize_plan
        stored_plan=normalize_plan(execution_plan,normalized.get('purchase'))
        selected=stored_plan['scope'] if stored_plan['scope']=='all' else sorted(set(stored_plan['scope']))
        if selected!=normalized['scope']:
            raise CheckoutError('SCOPE_CHANGED','Execution plan scope does not match the quote.')
    # A lost response can be replayed after this offer closed and a replacement
    # was purchased. Return its identity/status without reopening entitlement.
    existing=Receipt.objects.filter(pk=_uuid(checkout_id)).first()
    if existing is not None:
        if not hmac.compare_digest(existing.binding_sha256,digest) or not hmac.compare_digest(existing.handoff_hash,secret_hash) or existing.test_mode!=test_mode:
            raise CheckoutError('CHECKOUT_CONFLICT','This checkout ID is already bound to another request.')
        if existing.phase_contract.get('executionPlan')!=stored_plan:
            raise CheckoutError('CHECKOUT_CONFLICT','The stored execution plan cannot be replaced.')
        return receipt_status(existing,include_plan=True)
    contract = normalized.get('purchase') or {}
    parent = None
    if contract:
        if not getattr(settings, 'CYBERTEST_PHASED_SCANS_ENABLED', False):
            raise CheckoutError('BILLING_UNAVAILABLE', 'Phased scans are not enabled.', 503)
        if contract['phase'] == 'upgrade':
            from .cybertest_phase_checkout import lock_upgrade_parent
            parent = lock_upgrade_parent(normalized, test_mode=test_mode, checkout_id=checkout_id)
            if stored_plan is not None and (
                    stored_plan['parentAuditId'] != str(parent.audit_id)
                    or stored_plan['baseEvidenceSha256'] != parent.settlement.get('delivery', {}).get('evidenceSha256')
                    or stored_plan['baseReportSha256'] != (parent.delivery_evidence or {}).get('report', {}).get('sha256')):
                raise CheckoutError('UPGRADE_BASE_CHANGED', 'The upgrade must reuse this delivered basic report.')
        elif stored_plan is not None:
            # Serialize first quotes across web instances, not just within one
            # session. No receipt lock is taken while holding the payer lock:
            # charge() uses receipt -> payer, so reversing that order deadlocks.
            payer=get_user_model().objects.select_for_update().filter(pk=normalized.get('mlai_user_id'),is_active=True).first()
            if payer is None:
                raise CheckoutError('WRONG_ACCOUNT','An active MLAI payer is required.',403)
            if _pending_basic(normalized,test_mode=test_mode).exclude(pk=_uuid(checkout_id)).exists():
                raise CheckoutError('BASIC_PURCHASE_PENDING','Recover the existing basic purchase before creating another.')
    row,_=Receipt.objects.get_or_create(pk=_uuid(checkout_id),defaults={
        'binding':normalized,'binding_sha256':digest,'handoff_hash':secret_hash,
        'points':phase_price(contract)['duePoints'] if contract else price_points(getattr(settings,'CYBERTEST_AUDIT_POINTS',10)),
        'phase_contract':{**contract,**({'executionPlan':stored_plan} if stored_plan else {})},'parent_checkout':parent,
        'pricing_snapshot':{'usd_per_aud':str(getattr(settings,'CYBERTEST_USD_PER_AUD','0.697')), 'aud_per_point':str(getattr(settings,'CYBERTEST_AUD_PER_POINT','1.2798'))},
        'test_mode':test_mode,'expires_at':timezone.now()+timedelta(minutes=30)})
    if not hmac.compare_digest(row.binding_sha256,digest) or not hmac.compare_digest(row.handoff_hash,secret_hash) or row.test_mode!=test_mode:
        raise CheckoutError('CHECKOUT_CONFLICT','This checkout ID is already bound to another request.')
    if row.phase_contract.get('executionPlan')!=stored_plan:
        raise CheckoutError('CHECKOUT_CONFLICT','The stored execution plan cannot be replaced.')
    return receipt_status(row,include_plan=True)


def preview(checkout_id, token, user):
    row=_get(checkout_id);_handoff(row,token);_current_mode(row);_payer(row,user)
    if row.status in ('quoted','authorized'):_unexpired(row)
    return {**receipt_status(row),'available_points':balance(user,test_mode=row.test_mode)}


@transaction.atomic
def confirm(checkout_id, token, user, *, expected_points):
    row=_get(checkout_id,lock=True);_current_mode(row);_handoff(row,token);_payer(row,user)
    if price_points(expected_points)!=row.points:
        raise CheckoutError('PRICE_CHANGED','Confirm the displayed quote before continuing.')
    if row.status in ('charged','started'):return receipt_status(row)
    if row.status not in ('quoted','authorized'):
        raise CheckoutError('CHECKOUT_CLOSED','This checkout can no longer start an audit.')
    _unexpired(row)
    if balance(user,test_mode=row.test_mode)<row.points:
        raise CheckoutError('INSUFFICIENT_ROO_POINTS','There are not enough Roo points.',402)
    row.user=user;row.status='authorized';row.save(update_fields=['user','status','updated_at'])
    return receipt_status(row)


def _audit(row,audit_id):
    audit_id=_uuid(audit_id)
    if row.audit_id!=audit_id:
        raise CheckoutError('AUDIT_MISMATCH','The checkout is bound to another audit.')
    return audit_id


@transaction.atomic
def charge(checkout_id, audit_id, intent):
    row=_get(checkout_id,lock=True);_current_mode(row)
    if not hmac.compare_digest(row.binding_sha256,binding_digest(intent)):
        raise CheckoutError('SCOPE_CHANGED','The audit scope changed. Confirm a new quote.')
    audit_id=_uuid(audit_id)
    if row.status in ('charged','started'):
        _audit(row,audit_id);return receipt_status(row)
    if row.status!='authorized' or row.user_id is None:
        raise CheckoutError('CONSENT_REQUIRED','Confirm the Roo quote before starting.')
    _unexpired(row)
    # Serialize different test receipts for one payer, just like real points.
    user=get_user_model().objects.select_for_update().get(pk=row.user_id)
    _payer(row,user)
    if row.test_mode:
        if balance(user,test_mode=True)<row.points:
            raise CheckoutError('INSUFFICIENT_ROO_POINTS','There are not enough Roo points.',402)
    else:
        row.spend_ledger,_=PointsService.spend(user=user,delta=row.points,source='TOOLS',
            description='Cybertest security audit',created_by_slack_id=user.slack_id or '',
            idempotency_key=f'cybertest:charge:{row.id}',reference_type='CYBERTEST_AUDIT',reference_id=str(audit_id))
    row.audit_id=audit_id;row.status='charged'
    row.save(update_fields=['audit_id','status','spend_ledger','updated_at'])
    return receipt_status(row)


@transaction.atomic
def start_dispatch(checkout_id, audit_id):
    row=_get(checkout_id,lock=True);_current_mode(row);_audit(row,audit_id)
    if row.status=='started':return {**receipt_status(row),'dispatch_allowed':False}
    if row.status!='charged':raise CheckoutError('NOT_CHARGED','The audit has no active charge.')
    user=get_user_model().objects.select_for_update().filter(pk=row.user_id).first()
    if user is None:raise CheckoutError('WRONG_ACCOUNT','The payer account is unavailable.',403)
    _payer(row,user)
    row.status='started';row.dispatched_at=timezone.now()
    row.save(update_fields=['status','dispatched_at','updated_at'])
    return {**receipt_status(row),'dispatch_allowed':True}


@transaction.atomic
def refund_start_failure(checkout_id, audit_id, failure_code):
    # Pausing new billing must not prevent compensation of an existing charge.
    row=_get(checkout_id,lock=True);_audit(row,audit_id)
    if row.status=='refunded':return receipt_status(row)
    if row.status!='charged' or row.dispatched_at is not None:
        raise CheckoutError('REFUND_REVIEW_REQUIRED','Paid work may have started; this charge needs review.')
    if failure_code not in ('PREPARATION_FAILED','CANCELLED_BEFORE_START','QUEUE_START_FAILED'):
        raise CheckoutError('INVALID_FAILURE','A known startup failure is required.',400)
    if not row.test_mode:
        if row.user_id is None:raise CheckoutError('REFUND_REVIEW_REQUIRED','The payer account is unavailable.')
        row.refund_ledger,_=PointsService.refund(user=row.user,delta=row.points,source='TOOLS',
            description='Cybertest audit failed before paid work started',created_by_slack_id=row.user.slack_id or '',
            idempotency_key=f'cybertest:refund:{row.id}',original_spend_key=f'cybertest:charge:{row.id}',
            reference_type='CYBERTEST_AUDIT',reference_id=str(row.audit_id))
    row.status='refunded';row.failure_code=failure_code
    row.save(update_fields=['status','failure_code','refund_ledger','updated_at'])
    return receipt_status(row)

@transaction.atomic
def settle_failed_review(checkout_id, audit_id, failure_code, costs):
    """Settle one failed final review; exact net model cost, never a second bill."""
    row = _get(checkout_id, lock=True)
    if row.phase_contract:
        raise CheckoutError('INVALID_SETTLEMENT', 'Phased scans require delivery-based settlement.')
    _audit(row, audit_id)
    if failure_code != 'EVIDENCE_REVIEW_FAILED':
        raise CheckoutError('INVALID_FAILURE', 'A failed final review is required.', 400)
    if row.status != 'started' or row.dispatched_at is None:
        raise CheckoutError('SETTLEMENT_REVIEW_REQUIRED', 'No dispatched audit matches this settlement.')
    if not isinstance(costs, dict) or str(costs.get('auditId')) != str(row.audit_id):
        raise CheckoutError('AUDIT_MISMATCH', 'Cost usage belongs to another audit.')
    result = failure_settlement(row.points, row.pricing_snapshot, costs, existing=row.settlement)
    if row.settlement is not None:
        return receipt_status(row)
    if not row.test_mode and result['refund_microroo']:
        if row.user_id is None:
            raise CheckoutError('SETTLEMENT_REVIEW_REQUIRED', 'The payer account is unavailable.')
        # The shared digital refund only reverses a whole original spend. Reverse
        # and re-charge the retained microroo inside this same atomic transaction;
        # any error rolls both back. Do not change the shared refund invariant.
        row.refund_ledger, _ = PointsService.refund(
            user=row.user, delta=row.points, source='TOOLS',
            description='Cybertest failed review: reverse original quote for model-only settlement',
            created_by_slack_id=row.user.slack_id or '',
            idempotency_key=f'cybertest:failed-review:refund:{row.id}',
            original_spend_key=f'cybertest:charge:{row.id}',
            reference_type='CYBERTEST_AUDIT', reference_id=str(row.audit_id))
        if result['charge_microroo']:
            retained, _ = PointsService.spend_microroo(
                user=row.user, delta_microroo=result['charge_microroo'], source='TOOLS',
                description='Cybertest failed review: actual model usage only',
                created_by_slack_id=row.user.slack_id or '',
                idempotency_key=f'cybertest:failed-review:retained:{row.id}',
                reference_type='CYBERTEST_AUDIT', reference_id=str(row.audit_id))
            result = {**result, 'retained_ledger_id':str(retained.pk)}
    row.settlement=result; row.settled_at=timezone.now(); row.failure_code=failure_code
    row.save(update_fields=['settlement','settled_at','failure_code','refund_ledger','updated_at'])
    return receipt_status(row)


def settle_delivery(checkout_id, audit_id, evidence, costs):
    from .cybertest_phase_checkout import settle_delivery as settle
    return settle(checkout_id, audit_id, evidence, costs)


@transaction.atomic
def recover_quote(intent, token, *, parent_checkout_id=None):
    """Recover this payer's purchase; never authorize, debit or restart it."""
    normalized=binding(intent)
    if not normalized.get('mlai_user_id') or 'purchase' in normalized:
        raise CheckoutError('INVALID_QUOTE','An authenticated MLAI selection is required.',400)
    if not getattr(settings,'CYBERTEST_PHASED_SCANS_ENABLED',False):
        raise CheckoutError('BILLING_UNAVAILABLE','Phased scans are not enabled.',503)
    test_mode=configured_mode()=='test'
    secret_hash=token_digest(token)
    if parent_checkout_id is None:
        rows=_pending_basic(normalized,test_mode=test_mode)
    else:
        # A delivered upgrade stays recoverable so relogin cannot offer a second
        # upgrade. Fully refunded attempts allow a new explicit purchase.
        rows=Receipt.objects.filter(test_mode=test_mode,parent_checkout_id=_uuid(parent_checkout_id),
            phase_contract__phase='upgrade',phase_contract__executionPlan__isnull=False)
        for name,value in normalized.items():
            rows=rows.filter(**{f'binding__{name}':value})
        rows=rows.filter(Q(status__in=['quoted','authorized'],expires_at__gt=timezone.now())|
                         Q(status__in=['charged','started']))
    row=rows.select_for_update().order_by('-created_at','-id').first()
    if row is None:return None
    # An old, unconfirmed link is replaced, not duplicated. Confirmed receipts
    # retain their consent; recovery does not extend their expiry or revive work.
    if row.status=='quoted':
        row.handoff_hash=secret_hash
        row.save(update_fields=['handoff_hash','updated_at'])
    return receipt_status(row,include_plan=True)
