"""Postmaster Tools v2: user OAuth, explicit daily metrics and sparse-data handling."""

import math
import re
from datetime import date, timedelta
from urllib.parse import quote

from django.conf import settings
from django.db import transaction

from crm.models import SendingStream, StreamDay

from .. import observations, policy, scopes, volume
from .http import Unavailable, json_request

SCOPE = 'https://www.googleapis.com/auth/postmaster.traffic.readonly'
ORIGIN = 'https://gmailpostmastertools.googleapis.com/v2/domains/'


def token():
    """Refresh in memory only; neither tokens nor upstream errors enter audit/cache/logs."""
    values = [getattr(settings, key, '') for key in (
        'POSTMASTER_OAUTH_CLIENT_ID', 'POSTMASTER_OAUTH_CLIENT_SECRET', 'POSTMASTER_OAUTH_REFRESH_TOKEN')]
    if not all(values):
        return None
    body = json_request('POST', 'https://oauth2.googleapis.com/token', data={
        'client_id': values[0], 'client_secret': values[1], 'refresh_token': values[2], 'grant_type': 'refresh_token'})
    access = body.get('access_token')
    if not isinstance(access, str) or not access or '\n' in access or '\r' in access:
        raise Unavailable('oauth_unavailable')
    return access


def definitions():
    rows = [{'name': 'spam_rate', 'baseMetric': {'standardMetric': 'SPAM_RATE'}},
        {'name': 'tls_rate', 'baseMetric': {'standardMetric': 'TLS_ENCRYPTION_RATE'}, 'filter': 'traffic_direction = "outbound"'},
        {'name': 'delivery_error_rate', 'baseMetric': {'standardMetric': 'DELIVERY_ERROR_RATE'}},
        {'name': 'feedback_ids', 'baseMetric': {'standardMetric': 'FEEDBACK_LOOP_ID'}}]
    rows += [{'name': kind + '_rate', 'baseMetric': {'standardMetric': 'AUTH_SUCCESS_RATE'}, 'filter': f'auth_type = "{kind}"'}
        for kind in ('spf', 'dkim', 'dmarc')]
    return rows


def parse_stats(rows, names, dates):
    """Use the response's metric field; the resource name is opaque."""
    result = {day.isoformat(): {} for day in dates}
    for row in rows:
        if not isinstance(row, dict) or row.get('metric') not in names:
            continue
        try:
            reported = date(**{key: int(row['date'][key]) for key in ('year', 'month', 'day')}).isoformat()
        except (KeyError, ValueError, TypeError):
            continue
        if reported not in result:
            continue
        value = row.get('value') or {}
        if row['metric'] == 'feedback_ids':
            strings = value.get('stringList', {})
            strings = strings.get('values', []) if isinstance(strings, dict) else strings
            if isinstance(strings, list):
                result[reported]['feedback_ids'] = [item for item in strings[:50] if isinstance(item, str) and re.fullmatch(r'[a-zA-Z0-9:_./-]{1,200}', item)]
            continue
        numeric = next((value[key] for key in ('doubleValue', 'floatValue', 'intValue') if key in value), None)
        try:
            numeric = float(numeric) if not isinstance(numeric, bool) else float('nan')
        except (ValueError, TypeError, OverflowError):
            continue
        if math.isfinite(numeric) and 0 <= numeric <= 1:
            result[reported][row['metric']] = numeric
    return result


def query(domain, access, dates, metrics):
    if not re.fullmatch(r'[a-z0-9.-]{1,253}', domain):
        raise Unavailable('invalid_domain')
    body = {'metricDefinitions': metrics, 'timeQuery': {'dateList': {'dates': [
        {'year': day.year, 'month': day.month, 'day': day.day} for day in dates]}},
        'aggregationGranularity': 'DAILY', 'pageSize': 200}
    rows, tokens = [], set()
    for _ in range(policy.value('CRM_MONITOR_MAX_PAGES')):
        data = json_request('POST', ORIGIN + quote(domain, safe='') + '/domainStats:query',
            headers={'Authorization': 'Bearer ' + access}, json=body)
        if not isinstance(data.get('domainStats', []), list):
            raise Unavailable('invalid_response')
        rows += data.get('domainStats', [])
        cursor = data.get('nextPageToken')
        if not cursor:
            return parse_stats(rows, {row['name'] for row in metrics}, dates)
        if not isinstance(cursor, str) or cursor in tokens or len(cursor) > 4096:
            raise Unavailable('pagination_incomplete')
        tokens.add(cursor); body['pageToken'] = cursor
    raise Unavailable('pagination_incomplete')


def compliance(data, domain):
    """A root-domain compliance row does not prove a requested subdomain compliant."""
    selected = next((row for key in ('subdomainComplianceData', 'complianceData')
        if isinstance((row := data.get(key)), dict) and row.get('domainId') == domain), None)
    if not selected:
        return {'status': 'no_data', 'requirements': {}}
    rows = {row['requirement']: row['status']['status'] for row in selected.get('rowData') or []
        if isinstance(row, dict) and isinstance(row.get('requirement'), str) and isinstance(row.get('status'), dict)
        and row['status'].get('status') in ('COMPLIANT', 'NEEDS_WORK', 'STATE_UNSPECIFIED')}
    return {'status': 'needs_work' if 'NEEDS_WORK' in rows.values() else 'compliant' if rows and all(
        value == 'COMPLIANT' for value in rows.values()) else 'no_data', 'requirements': rows}


def check(stream, *, now):
    access = token()
    if not access:
        return observations.record(stream, 'postmaster', {'status': 'not_configured'}, now=now)
    today = now.astimezone(volume.MELBOURNE).date()
    dates = [today - timedelta(days=offset) for offset in (1, 2)]
    stats = query(stream.domain, access, dates, definitions())
    ids = sorted({value for row in stats.values() for value in row.get('feedback_ids', [])})[:50]
    if ids:
        metrics = [{'name': f'fbl_{index}', 'baseMetric': {'standardMetric': 'FEEDBACK_LOOP_SPAM_RATE'},
            'filter': 'feedback_loop_id = "' + identity + '"'} for index, identity in enumerate(ids)]
        fbl = query(stream.domain, access, dates, metrics)
        for day, row in fbl.items():
            stats[day]['feedback_loop_rates'] = {ids[int(key[4:])]: value for key, value in row.items() if key.startswith('fbl_')}
    comp = compliance(json_request('GET', ORIGIN + quote(stream.domain, safe='') + '/complianceStatus',
        headers={'Authorization': 'Bearer ' + access}), stream.domain)
    from ..guardian import Signal, detect
    for reported, values in stats.items():
        payload = {**values, 'domain': stream.domain, 'report_day': reported, 'checked_at': now.isoformat(),
            'status': 'available' if values else 'no_data', 'compliance': comp}
        with transaction.atomic():
            locked = SendingStream.objects.select_for_update().get(pk=stream.pk)
            day, _ = StreamDay.objects.get_or_create(stream=locked, day=date.fromisoformat(reported),
                defaults={'budget': volume.budget(locked), 'ramp_stage': locked.ramp_stage})
            # A sparse retry cannot erase previously reported metrics for that same day.
            day.postmaster = {**(day.postmaster or {}), **payload}
            day.save(update_fields=['postmaster'])
        rate = values.get('spam_rate')
        if rate is not None and rate >= policy.value('CRM_GUARD_POSTMASTER_AMBER', stream):
            red = rate >= policy.value('CRM_GUARD_POSTMASTER_RED', stream)
            detect(None, Signal('postmaster_spam_rate', 'red' if red else 'amber', rate,
                policy.value('CRM_GUARD_POSTMASTER_RED' if red else 'CRM_GUARD_POSTMASTER_AMBER', stream), 1,
                'stop_stream' if red else 'slow_stream', evidence_key=scopes.evidence_key([reported, rate])), stream=stream, now=now)
    if comp['status'] == 'needs_work':
        detect(None, Signal('postmaster_compliance', 'amber', len([v for v in comp['requirements'].values() if v == 'NEEDS_WORK']),
            1, len(comp['requirements']), 'alert_only', evidence_key=scopes.evidence_key(comp['requirements'].items())), stream=stream, now=now)
    return observations.record(stream, 'postmaster', {'status': 'available' if any(stats.values()) else 'no_data',
        'days': stats, 'compliance': comp}, now=now)


def root_check(stream, *, now):
    access = token()
    if not access:
        return
    dates = [now.astimezone(volume.MELBOURNE).date() - timedelta(days=offset) for offset in (1, 2)]
    data = query('mlai.au', access, dates, [definitions()[0]])
    from ..guardian import Signal, detect
    for day, row in data.items():
        rate = row.get('spam_rate')
        if rate is not None and rate >= policy.value('CRM_GUARD_ROOT_SPAM_RATE'):
            detect(None, Signal('root_spam_rate', 'red', rate, policy.value('CRM_GUARD_ROOT_SPAM_RATE'), 1,
                'alert_only', evidence_key=scopes.evidence_key([day, rate])), stream=stream, now=now)
    observations.record(stream, 'root_postmaster', {'status': 'available' if any(data.values()) else 'no_data', 'domain': 'mlai.au', 'days': data}, now=now)
