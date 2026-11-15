"""Owner-scoped Xero cash evidence for Studio clients whose work comes from Linear."""
from datetime import datetime, timedelta, timezone
import json
import time

from .studio_report_source import (
    SourceIncomplete, all_invoices, cents, digest, finance_cache_path, finance_cooldown,
    finance_retry_at, instant, refresh_finance, store_finance,
)


def contract_cash(scope, request, get, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = instant(request['cutoff'])
    owner = request['owner']
    response = {'version': 1, 'mode': 'contract_cash', 'owner': owner, 'team': request['team'],
                'organization': scope['linear_organization'], 'project_ids': sorted(scope['project_ids']),
                'contact_id': scope['xero_contact_id'], 'cutoff': request['cutoff'], 'nonce': request['nonce'],
                'checked_at': now.isoformat(), 'missing': [], 'status': 'ready'}
    if cutoff > now:
        return {**response, 'status': 'waiting_data', 'missing': ['closing_refresh_required']}
    path = finance_cache_path(scope, request).with_suffix('.contract.json')
    cache = json.loads(path.read_text()) if path.exists() else None
    usable = (cache is not None and instant(cache['cutoff']) <= cutoff
              and instant(cache['checked_at']) <= cutoff and now - instant(cache['checked_at']) <= timedelta(days=7))
    retry = finance_cooldown(scope, now)
    if not retry and not (usable and now - instant(cache['checked_at']) <= timedelta(minutes=15)):
        start = time.monotonic()
        try:
            contact = scope['xero_contact_id']
            invoices = all_invoices(get, contact)
            previous = cache['invoices'] if cache and instant(cache['cutoff']) <= cutoff else []
            seed = {'evidence': {}, 'clients': {owner: {'invoices': previous, 'payments': []}}}
            snapshot = refresh_finance(seed, owner, invoices, get, request['cutoff'], contact_id=contact)
            if digest(all_invoices(get, contact)) != digest(invoices):
                raise SourceIncomplete('finance_changed_during_refresh')
            raw = {v['InvoiceID']: v for v in invoices}
            data = snapshot['clients'][owner]
            for inv in data['invoices']:
                source = raw[inv['id']]
                inv['net_cents'] = cents(source['SubTotal'])
                inv['tax_cents'] = cents(source['TotalTax'])
                if inv['net_cents'] + inv['tax_cents'] != inv['amount_cents']:
                    raise SourceIncomplete('invoice_total_mismatch')
            if now - cutoff <= timedelta(minutes=1):
                store_finance(path, snapshot, owner, now.isoformat(), request['cutoff'], len(invoices))
            return {**response, 'ledger': data, 'finance_checked_at': now.isoformat()}
        except Exception as exc:
            retry = finance_retry_at(exc, now + timedelta(seconds=time.monotonic() - start))
            if retry is None:
                return {**response, 'status': 'waiting_data', 'missing': ['finance_sync_failed']}
            finance_cooldown(scope, now, until=retry)
    if retry:
        response.update(status='partial' if usable else 'waiting_data', missing=['finance_rate_limited'],
                        retry_at=retry.isoformat())
    if usable:
        response.update(ledger={key: cache[key] for key in ('invoices', 'payments')},
                        finance_checked_at=cache['checked_at'])
    return response
