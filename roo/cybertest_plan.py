"""Bounded source-free execution metadata stored with the purchase snapshot."""
import copy
import json
import math
import re
from .cybertest_delivery import purchase_contract
from .cybertest_policy import CheckoutError


def public_purchase_scope(snapshot):
    """Return customer-reviewable scope, never internal model or runner settings."""
    if not snapshot:
        return None
    from .cybertest_delivery import phase_price
    contract = purchase_contract(snapshot)
    price = phase_price(contract)
    groups = {}
    for check in contract['checks']:
        key = (check['kind'], check['required'])
        groups[key] = groups.get(key, 0) + 1
    plan = snapshot.get('executionPlan') or {}
    return {'phase': contract['phase'], 'tier': contract['tier'],
            'total_points': price['totalPoints'], 'due_points': price['duePoints'],
            'commit_sha': contract['commitSha'],
            'checks': [{'kind': kind, 'required': required, 'count': count}
                       for (kind, required), count in sorted(groups.items())],
            'runtime_targets': [target['name'] for target in plan.get('runtimeTargets', [])],
            'source_checks': [{'kind': check['kind'], 'files': list(check['files'])}
                              for check in plan.get('sourceChecks', [])]}


def execution_plan(value, contract):
    def require(condition):
        if not condition:
            raise CheckoutError('INVALID_EXECUTION_PLAN', 'A bounded source-free execution plan is required.')

    def number(v, maximum, *, integer=False, zero=False):
        return (not isinstance(v, bool) and isinstance(v, int if integer else (int, float))
                and (0 <= v if zero else 0 < v) and v <= maximum and math.isfinite(v))

    common = {
        'algorithm', 'contract', 'commitSha', 'sourceManifestSha256', 'scope', 'route', 'model',
        'pricing', 'execution', 'workload', 'checks'}
    require(isinstance(value, dict))
    upgrade = value.get('algorithm') == 'incremental-upgrade-v1'
    require(set(value) == common | ({'parentAuditId', 'baseEvidenceSha256', 'baseReportSha256',
                                   'policy', 'runtimeTargets', 'sourceChecks', 'coverage'} if upgrade
                                  else {'contextSha256', 'batchRanges'}))
    normalized = purchase_contract(contract)
    require(normalized['phase'] == ('upgrade' if upgrade else 'basic')
            and value['algorithm'] == ('incremental-upgrade-v1' if upgrade else 'basic-source-v1'))
    require(purchase_contract(value['contract']) == normalized)
    require(value['commitSha'] == normalized['commitSha'] and value['sourceManifestSha256'] == normalized['sourceManifestSha256'])
    require(isinstance(value['checks'], list) and all(isinstance(c, dict) and isinstance(c.get('id'), str) for c in value['checks'])
            and sorted(value['checks'], key=lambda c: c['id']) == normalized['checks'])
    require(value['route'] in ('frontend', 'supabase', 'backend'))
    require(isinstance(value['model'], str) and re.fullmatch(r'[A-Za-z0-9._:/-]{1,100}', value['model']))
    if not upgrade:
        require(isinstance(value['contextSha256'], str) and re.fullmatch(r'[a-f0-9]{64}', value['contextSha256']))
    scope = value['scope']
    require(scope == 'all' or isinstance(scope, list) and 1 <= len(scope) <= 100 and all(
        isinstance(s, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', s) for s in scope))
    pricing = value['pricing']
    require(isinstance(pricing, dict) and set(pricing) == {'input', 'cachedInput', 'output'}
            and all(number(v, 100000, zero=True) for v in pricing.values()))
    if upgrade:
        _upgrade_plan(value, normalized, require, number)
        require(len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()) <= 500000)
        return copy.deepcopy(value)
    limits = value['execution']
    require(isinstance(limits, dict) and set(limits) == {
        'maxSourceBytes', 'maxFiles', 'maxSourceBatches', 'maxDurationMs', 'maxModelUsd', 'reportReserveUsd'})
    for key, ceiling in [('maxSourceBytes', 64000000), ('maxFiles', 10000),
                         ('maxSourceBatches', 9998), ('maxDurationMs', 2147483647)]:
        require(number(limits[key], ceiling, integer=True))
    require(number(limits['maxModelUsd'], 100000) and number(limits['reportReserveUsd'], limits['maxModelUsd'])
            and limits['reportReserveUsd'] < limits['maxModelUsd'])
    work = value['workload']
    require(isinstance(work, dict) and set(work) == {
        'files', 'sourceBytes', 'sourceBatches', 'inputTokenUpperBound', 'outputTokenLimit', 'sourceReserveUsd'})
    for key, ceiling in [('files', limits['maxFiles']), ('sourceBytes', limits['maxSourceBytes']),
                         ('sourceBatches', limits['maxSourceBatches']), ('inputTokenUpperBound', 2**53-1),
                         ('outputTokenLimit', 2**53-1)]:
        require(number(work[key], ceiling, integer=True))
    require(number(work['sourceReserveUsd'], limits['maxModelUsd']-limits['reportReserveUsd'], zero=True))
    batches = value['batchRanges']
    require(isinstance(batches, list) and len(batches) == work['sourceBatches'])
    require(sum(c['kind'] == 'source_batch' for c in normalized['checks']) == len(batches))
    for batch in batches:
        require(isinstance(batch, list) and 1 <= len(batch) <= limits['maxFiles'])
        for part in batch:
            require(isinstance(part, dict) and set(part) == {'file', 'start', 'end'})
            file = part['file']
            require(isinstance(file, str) and 1 <= len(file) <= 1024 and not file.startswith('/')
                    and not any(c in file for c in ('\\', '\x00', ':')) and '..' not in file.split('/'))
            require(number(part['start'], 16000000, integer=True, zero=True)
                    and number(part['end'], 16000000, integer=True, zero=True)
                    and 0 <= part['end']-part['start'] <= 48000)
    require(len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()) <= 500000)
    return copy.deepcopy(value)


def _upgrade_plan(value, contract, require, number):
    """Validate the additional work without accepting source or runtime secrets."""
    require(value['route'] in ('supabase', 'backend'))
    require(isinstance(value['parentAuditId'], str) and re.fullmatch(
        r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', value['parentAuditId']))
    for key in ('baseEvidenceSha256', 'baseReportSha256'):
        require(isinstance(value[key], str) and re.fullmatch(r'[a-f0-9]{64}', value[key]))
    caps = {'maxSourceBytes': 64000000, 'maxFiles': 10000, 'maxRuntimeTargets': 100,
            'maxSourceChecks': 9998, 'maxCheckChars': 512000, 'maxWorkTokens': 2**53-1,
            'maxDurationMs': 2147483647, 'maxParallelRuntime': 2}

    def limits(row):
        require(isinstance(row, dict) and set(row) == set(caps) | {'maxModelUsd', 'reportReserveUsd'})
        for key, ceiling in caps.items():
            require(number(row[key], ceiling, integer=True, zero=key in ('maxRuntimeTargets', 'maxSourceChecks')))
        require(number(row['maxModelUsd'], 100000) and number(row['reportReserveUsd'], row['maxModelUsd'])
                and row['reportReserveUsd'] < row['maxModelUsd'])

    policy = value['policy']
    require(isinstance(policy, dict) and set(policy) == {
        'version', 'runtimeTargetTokenAllowance', 'runtimeTargetReserveUsd', 'standard', 'large'})
    require(type(policy['version']) is int and policy['version'] == 1
            and number(policy['runtimeTargetTokenAllowance'], 10000000, integer=True)
            and number(policy['runtimeTargetReserveUsd'], 100000))
    for tier in ('standard', 'large'):
        limits(policy[tier])
    limits(value['execution'])
    require(value['execution'] == policy[contract['tier']])
    row = value['execution']
    work = value['workload']
    require(isinstance(work, dict) and set(work) == {'files', 'sourceBytes', 'runtimeTargets',
            'sourceChecks', 'largestSourceCheckChars', 'estimatedTokenAllowance', 'requestReserveUsd'})
    for key, cap, zero in [('files', row['maxFiles'], False), ('sourceBytes', row['maxSourceBytes'], True),
                           ('runtimeTargets', row['maxRuntimeTargets'], True), ('sourceChecks', row['maxSourceChecks'], True),
                           ('largestSourceCheckChars', row['maxCheckChars'], True), ('estimatedTokenAllowance', row['maxWorkTokens'], False)]:
        require(number(work[key], cap, integer=True, zero=zero))
    require(number(work['requestReserveUsd'], row['maxModelUsd']-row['reportReserveUsd'], zero=True))
    runtime, source = value['runtimeTargets'], value['sourceChecks']
    require(isinstance(runtime, list) and len(runtime) == work['runtimeTargets']
            and isinstance(source, list) and len(source) == work['sourceChecks'] and len(runtime)+len(source) > 0)
    require(not runtime or value['route'] == 'supabase')
    declared = {c['id']: c for c in contract['checks'] if c['required']}
    observed, names = set(), set()
    for target in runtime:
        require(isinstance(target, dict) and set(target) == {'checkId', 'name'})
        name, check_id = target['name'], target['checkId']
        require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', name) and name not in names)
        require(isinstance(check_id, str) and check_id not in observed
                and declared.get(check_id, {}).get('kind') == 'runtime_target')
        names.add(name)
        observed.add(check_id)
    pairs, policies = set(), 0
    for check in source:
        require(isinstance(check, dict) and set(check) == {'checkId', 'kind', 'files'})
        check_id, kind, files = check['checkId'], check['kind'], check['files']
        require(isinstance(check_id, str) and check_id not in observed and kind in ('database_policy', 'cross_module')
                and declared.get(check_id, {}).get('kind') == kind)
        require(isinstance(files, list) and 1 <= len(files) <= row['maxFiles'])
        for file in files:
            require(isinstance(file, str) and 1 <= len(file) <= 1024 and not file.startswith('/')
                    and not any(c in file for c in ('\\', '\x00', ':')) and '..' not in file.split('/'))
        require(len(set(files)) == len(files))
        if kind == 'cross_module':
            pair = tuple(sorted(files))
            require(len(files) == 2 and pair not in pairs)
            pairs.add(pair)
        else:
            policies += 1
            require(policies == 1)
        observed.add(check_id)
    require(observed == set(declared))
    coverage = value['coverage']
    require(isinstance(coverage, list) and len(coverage) <= 10000
            and all(isinstance(line, str) and len(line) <= 4096 for line in coverage))
