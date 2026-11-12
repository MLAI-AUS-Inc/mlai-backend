"""Immutable Cybertest quote identity; no database or provider access."""
import hashlib
import json
import re
from uuid import UUID


class CheckoutError(ValueError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.status = code, status


def binding(payload):
    if not isinstance(payload, dict):
        raise CheckoutError('INVALID_QUOTE', 'An audit scope object is required.', 400)
    def positive_id(name):
        value = str(payload.get(name, ''))
        if not re.fullmatch(r'[1-9][0-9]{0,18}', value) or int(value) > 2**63-1:
            raise CheckoutError('INVALID_QUOTE', f'{name} must be a positive numeric ID.', 400)
        return value

    try:
        workspace = str(UUID(str(payload.get('workspace_id', ''))))
    except (ValueError, TypeError, AttributeError) as exc:
        raise CheckoutError('INVALID_QUOTE', 'A workspace UUID is required.', 400) from exc
    repository = payload.get('repository', '')
    if not isinstance(repository, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or len(repository) > 200:
        raise CheckoutError('INVALID_QUOTE', 'A repository full name is required.', 400)
    scope = payload.get('scope')
    if scope != 'all' and (not isinstance(scope, list) or not 1 <= len(scope) <= 100 or any(
        not isinstance(item, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', item) for item in scope
    )):
        raise CheckoutError('INVALID_QUOTE', 'Choose a bounded list of function names.', 400)
    identity = {}
    if 'mlai_user_id' in payload or 'mlai_company_id' in payload:
        user_id = payload.get('mlai_user_id')
        if not isinstance(user_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', user_id):
            raise CheckoutError('INVALID_QUOTE', 'A valid MLAI payer is required.', 400)
        try: company_id = str(UUID(str(payload.get('mlai_company_id'))))
        except (ValueError, TypeError, AttributeError) as exc:
            raise CheckoutError('INVALID_QUOTE', 'A valid MLAI company is required.', 400) from exc
        identity = {'mlai_user_id': user_id, 'mlai_company_id': company_id}
    purchase = {}
    if 'purchase' in payload:
        from .cybertest_delivery import purchase_contract
        if not identity:
            raise CheckoutError('INVALID_QUOTE', 'Phased purchases require an MLAI payer and company.', 400)
        purchase = {'purchase': purchase_contract(payload['purchase'])}
    return {**identity, **purchase, 'github_actor_id': positive_id('github_actor_id'),
            'repository_id': positive_id('repository_id'), 'workspace_id': workspace,
            'repository': repository, 'scope': 'all' if scope == 'all' else sorted(set(scope))}


def binding_digest(value):
    canonical = binding(value)
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def token_digest(token):
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', token):
        raise CheckoutError('INVALID_HANDOFF', 'The checkout link is invalid.', 400)
    return hashlib.sha256(token.encode()).hexdigest()


def price_points(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100_000:
        raise CheckoutError('BILLING_UNAVAILABLE', 'The audit price is not configured.', 503)
    return value

def failure_settlement(points, pricing, costs, *, existing=None):
    """Provider usage only; exclude infrastructure, markup and reservations."""
    from decimal import Decimal, InvalidOperation, ROUND_FLOOR
    price_points(points)
    if not isinstance(costs, dict) or costs.get('status') != 'complete' or costs.get('pendingCalls') != 0 or costs.get('unpricedCalls') != 0 or costs.get('storageFailed'):
        raise CheckoutError('SETTLEMENT_REVIEW_REQUIRED', 'Complete provider usage is required.')
    def decimal(value, positive=False):
        try:
            if isinstance(value, bool) or value is None: raise ValueError()
            result = Decimal(str(value))
            if not result.is_finite() or result < 0 or (positive and result == 0): raise ValueError()
            return result
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise CheckoutError('SETTLEMENT_REVIEW_REQUIRED', 'A valid cost and quote conversion are required.') from exc
    model = decimal(costs.get('modelCostUsd'))
    pricing = pricing if isinstance(pricing, dict) else {}
    fx, point = decimal(pricing.get('usd_per_aud'), True), decimal(pricing.get('aud_per_point'), True)
    charge = min(points * 1_000_000, int((model / fx / point * 1_000_000).to_integral_value(rounding=ROUND_FLOOR)))
    evidence = {'audit_id': str(costs.get('auditId', '')), 'model_cost_usd': str(model), 'usd_per_aud': str(fx), 'aud_per_point': str(point)}
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    result = {**evidence, 'basis': 'model_usage_only', 'usage_sha256': digest, 'charge_microroo': charge, 'refund_microroo': points * 1_000_000 - charge}
    if existing is not None:
        if not isinstance(existing, dict) or any(existing.get(k) != v for k, v in result.items()):
            raise CheckoutError('SETTLEMENT_CONFLICT', 'This audit already has a different settlement.')
        return existing
    return result
