"""Pure versioned scan-delivery policy; the authenticated service supplies evidence.

Do not use browser-supplied completion flags. Transactional callers must verify
the receipt, persisted audit, immutable plan and original payer before settlement.
"""
import hashlib
import json
import re
from .cybertest_policy import CheckoutError, failure_settlement

SHA = re.compile(r'[a-f0-9]{64}')
UUID = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}')
KINDS = {'source_batch', 'credential_patterns', 'runtime_target', 'cross_module', 'database_policy', 'evidence_review'}
STATES = {'completed', 'incomplete', 'skipped', 'error'}


def _fail(message):
    raise CheckoutError('INVALID_DELIVERY', message)


def _valid(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def phase_price(contract):
    if not isinstance(contract, dict) or type(contract.get('version')) is not int or contract['version'] != 1:
        _fail('Unsupported scan purchase contract.')
    phase, tier = contract.get('phase'), contract.get('tier')
    if phase == 'basic' and tier == 'basic' and contract.get('parentCheckoutId') is None:
        return {'totalPoints': 24, 'duePoints': 24}
    if phase == 'upgrade' and tier in ('standard', 'large') and _valid(UUID, contract.get('parentCheckoutId')):
        total = 48 if tier == 'standard' else 124
        return {'totalPoints': total, 'duePoints': total-24}
    _fail('A valid phase and linked basic purchase are required.')


def _normalize_plan(contract):
    price = phase_price(contract)
    checks = contract.get('checks')
    if not _valid(SHA, contract.get('planId')) or not isinstance(checks, list) or not 1 <= len(checks) <= 10000:
        _fail('A bounded check plan is required.')
    ids, normalized = set(), []
    for check in checks:
        if (not isinstance(check, dict) or not _valid(re.compile(r'[a-zA-Z0-9:._/-]{1,160}'), check.get('id'))
                or not check['id'].startswith(contract['phase']+':')
                or not isinstance(check.get('kind'), str) or check['kind'] not in KINDS
                or type(check.get('required')) is not bool or check['id'] in ids):
            _fail('The check plan is invalid or contains repeated IDs.')
        if contract['phase'] == 'upgrade' and check['kind'] in ('source_batch', 'credential_patterns'):
            _fail('An upgrade must deliver additional checks, not repeat basic work.')
        ids.add(check['id'])
        normalized.append({'id': check['id'], 'kind': check['kind'], 'required': check['required']})
    if contract['phase'] == 'basic' and (
            not any(c['kind'] == 'source_batch' for c in normalized)
            or not any(c['kind'] == 'credential_patterns' for c in normalized)
            or any(c['kind'] not in ('source_batch', 'credential_patterns', 'evidence_review')
                   or c['kind'] != 'evidence_review' and not c['required'] for c in normalized)):
        _fail('Basic source and credential checks must all be present and required.')
    if not any(c['required'] and c['kind'] != 'evidence_review' for c in normalized):
        _fail('At least one substantive required check is needed.')
    return {'version': 1, 'phase': contract['phase'], 'tier': contract['tier'],
            'parentCheckoutId': contract.get('parentCheckoutId'), 'planId': contract['planId'],
            'checks': sorted(normalized, key=lambda c: c['id']), **price}


def purchase_contract(contract):
    """Canonical consent snapshot; never accept a branch name or client price."""
    result = _normalize_plan(contract)
    commit = contract.get('commitSha')
    if not _valid(re.compile(r'(?:[a-f0-9]{40}|[a-f0-9]{64})'), commit):
        _fail('An immutable source commit is required.')
    if not _valid(SHA, contract.get('sourceManifestSha256')):
        _fail('An immutable source manifest is required.')
    return {**result, 'commitSha': commit, 'sourceManifestSha256': contract['sourceManifestSha256']}


def assess_delivery(contract, evidence, expected_audit_id):
    plan = _normalize_plan(contract)
    if (not _valid(UUID, expected_audit_id) or not isinstance(evidence, dict)
            or evidence.get('auditId') != expected_audit_id or evidence.get('planId') != plan['planId']):
        _fail('Delivery does not match the audit and purchased plan.')
    checks = evidence.get('checks')
    if not isinstance(checks, list) or len(checks) > len(plan['checks']):
        _fail('Invalid check evidence.')
    allowed, observed = {c['id'] for c in plan['checks']}, {}
    for check in checks:
        if (not isinstance(check, dict) or not isinstance(check.get('id'), str)
                or check['id'] not in allowed or check['id'] in observed
                or not isinstance(check.get('status'), str) or check['status'] not in STATES):
            _fail('Check evidence is unknown, duplicated or invalid.')
        observed[check['id']] = check['status']
    report = evidence.get('report')
    if (not isinstance(report, dict) or type(report.get('persisted')) is not bool
            or report['persisted'] and not _valid(SHA, report.get('sha256'))):
        _fail('Saved report evidence is required.')
    completed = [c for c in plan['checks'] if observed.get(c['id']) == 'completed']
    required = [c for c in plan['checks'] if c['required']]
    required_completed = sum(c['required'] for c in completed)
    substantive_completed = sum(c['kind'] != 'evidence_review' for c in completed)
    incomplete = [c['id'] for c in plan['checks'] if observed.get(c['id']) != 'completed']
    status = 'none' if not report['persisted'] or not substantive_completed else 'partial' if incomplete else 'full'
    normalized = {'auditId': expected_audit_id, 'plan': plan,
                  'checks': [{'id': c['id'], 'status': observed.get(c['id'], 'not_started')} for c in plan['checks']],
                  'report': {'persisted': report['persisted'], 'sha256': report.get('sha256') if report['persisted'] else None}}
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return {'status': status, 'minimumMet': bool(report['persisted'] and substantive_completed and required_completed == len(required)),
            'plannedChecks': len(plan['checks']), 'completedChecks': len(completed),
            'requiredChecks': len(required), 'requiredCompleted': required_completed,
            'incompleteCheckIds': incomplete, 'evidenceSha256': digest}


def delivery_settlement(contract, evidence, expected_audit_id, pricing, costs, *, existing=None):
    delivery = assess_delivery(contract, evidence, expected_audit_id)
    points = phase_price(contract)['duePoints']
    if delivery['minimumMet']:
        amounts = {'basis': 'phase_delivered', 'charge_microroo': points*1_000_000, 'refund_microroo': 0}
    elif contract['phase'] == 'upgrade':
        # The original basic receipt remains untouched. Provider costs are the
        # operator's responsibility if the purchased upgrade was not delivered.
        amounts = {'basis': 'upgrade_not_delivered', 'charge_microroo': 0, 'refund_microroo': points*1_000_000}
    else:
        if not isinstance(costs, dict) or costs.get('auditId') != expected_audit_id:
            raise CheckoutError('SETTLEMENT_REVIEW_REQUIRED', 'Actual usage for this audit is required.')
        amounts = failure_settlement(points, pricing, costs)
    result = {**amounts, 'version': 1, 'phase': contract['phase'], 'audit_id': expected_audit_id,
              'plan_id': contract['planId'], 'delivery': delivery}
    if existing is not None:
        if not isinstance(existing, dict) or any(existing.get(k) != v for k, v in result.items()):
            raise CheckoutError('SETTLEMENT_CONFLICT', 'This scan already has a different settlement.')
        return existing
    return result
