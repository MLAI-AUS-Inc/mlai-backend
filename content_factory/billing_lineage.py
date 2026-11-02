"""Resolve original article spends from persisted, tenant-scoped billing proof."""
from .billing import normalize_content_factory_domain

ARTICLE_CHARGE_PREFIX = 'content_factory:charge:'


def paid_article_request(request):
    """Identify a saved paid obligation without inferring one from today's price.

    A free first article ('entitled') is not paid: its ledger id names a
    zero-point entitlement row, which is released rather than refunded.
    """
    if request.get('roo_points_billing_status') == 'entitled':
        return False
    return bool(request.get('roo_points_cost') or request.get('roo_points_ledger_id')
        or request.get('roo_points_billing_status') in {'charged', 'reused'})


def settlement_owed_request(request):
    """A stop without delivery owes settlement: a refund when paid, a release when free.

    The refund sweeps queue only the original free run. A restart or revision
    on the same grant settles through its own stop or failure callback, and
    releases only as the last run of the lineage without a delivery.
    """
    from .first_article_entitlement import original_entitled_request
    return paid_article_request(request) or original_entitled_request(request)


def _ledger_id(value):
    text = str(value or '')
    return int(text) if not isinstance(value, bool) and text.isdecimal() and len(text) <= 19 and 0 < int(text) < 2**63 else None


def _matches_charge(ledger, request, *, prefix, key):
    from integrations.services.article_generation import _ledger_reference_id
    if not ledger or not ledger.user_id or ledger.idempotency_key != prefix + key:
        return False
    if ledger.kind != 'SPEND' or ledger.source != 'CONTENT_FACTORY':
        return False
    if ledger.reference_type != 'CONTENT_FACTORY' or ledger.reference_id != _ledger_reference_id(key):
        return False
    amount = -int(ledger.delta_microroo or 0)
    if amount <= 0 or amount % 1_000_000:
        return False
    stamp = request.get('roo_points_ledger_id')
    payer = request.get('roo_points_billing_user_id')
    return (stamp in (None, '') or _ledger_id(stamp) == ledger.pk) and (
        payer in (None, '') or str(payer) == str(ledger.user_id))


def original_spend_for_job(job):
    """Use the job's saved charge identity, never its requesting actor, as payer."""
    from roo.models import Ledger
    request = dict(getattr(job, 'request_meta', {}) or {})
    key = str(getattr(job, 'client_request_id', '') or request.get('client_request_id') or '')
    if not key:
        return None
    prefix = ('content_factory:topic_generation:charge:'
        if request.get('roo_points_action') == 'content_island_topic_generation' else ARTICLE_CHARGE_PREFIX)
    saved_id = getattr(job, 'billing_ledger_id', None) or request.get('roo_points_ledger_id')
    if saved_id not in (None, ''):
        parsed = _ledger_id(saved_id)
        if parsed is None:
            return None
        ledger = Ledger.objects.filter(pk=parsed, kind='SPEND', source='CONTENT_FACTORY').select_related('user').first()
    else:
        ledger = Ledger.objects.filter(idempotency_key=prefix + key, kind='SPEND', source='CONTENT_FACTORY').select_related('user').first()
    return ledger if _matches_charge(ledger, request, prefix=prefix, key=key) else None


def _same_company_charge(ledger, *, organization_id, domain, key):
    from .dispatch_models import ContentFactoryDispatchOutbox
    from .models import ContentFactoryJob
    row = ContentFactoryDispatchOutbox.objects.filter(client_request_id=key).first()
    if row:
        saved = row.payload.get('request') or {}
        return row.organization_id == organization_id and str(row.payload.get('billing_user_id')) == str(ledger.user_id) and (
            _ledger_id(saved.get('roo_points_ledger_id')) == ledger.pk
            and normalize_content_factory_domain(saved.get('domain')) == domain)
    # Older authenticated/Roo starts materialize their billing on the job.
    from .job_view import job_runs, read_job_flag
    return read_job_flag("same_company_charge",
        from_job=lambda: ContentFactoryJob.objects.filter(domain__iexact=domain, client_request_id=key,
            billing_ledger_id=ledger.pk).exists(),
        from_run=lambda: job_runs().filter(domain__iexact=domain, client_request_id=key,
            billing_ledger_id=ledger.pk).exists())


def original_article_spend_for_run(run):
    """Follow a reused payment only through the original company's saved lineage."""
    from roo.models import Ledger
    from workflow_runs.models import ContentFactoryRun
    request = dict(run.run_request or {})
    domain = normalize_content_factory_domain(run.domain)
    organization_id = run.organization_id
    if not organization_id or not domain:
        return None
    saved_id = request.get('roo_points_ledger_id')
    key = str(request.get('client_request_id') or '')
    if saved_id not in (None, ''):
        parsed = _ledger_id(saved_id)
        if parsed is None:
            return None
        ledger = Ledger.objects.filter(pk=parsed, kind='SPEND', source='CONTENT_FACTORY').select_related('user').first()
    elif key:
        ledger = Ledger.objects.filter(idempotency_key=ARTICLE_CHARGE_PREFIX + key,
            kind='SPEND', source='CONTENT_FACTORY').select_related('user').first()
    else:
        return None
    if not ledger or not str(ledger.idempotency_key or '').startswith(ARTICLE_CHARGE_PREFIX):
        return None
    original_key = ledger.idempotency_key.removeprefix(ARTICLE_CHARGE_PREFIX)
    if not _matches_charge(ledger, request, prefix=ARTICLE_CHARGE_PREFIX, key=original_key):
        return None
    if not _same_company_charge(ledger, organization_id=organization_id, domain=domain, key=original_key):
        return None
    pending, seen = [run], set()
    for _ in range(16):
        if not pending:
            break
        source = pending.pop(0)
        if source.run_id in seen:
            continue
        seen.add(source.run_id)
        saved = dict(source.run_request or {})
        if str(saved.get('client_request_id') or '') == original_key and _matches_charge(
                ledger, saved, prefix=ARTICLE_CHARGE_PREFIX, key=original_key):
            return ledger
        for field in ('original_billing_source_run_id', 'billing_source_run_id', 'source_run_id', 'revision_source_run_id'):
            source_id = str(saved.get(field) or '')
            if source_id and source_id not in seen:
                candidate = ContentFactoryRun.objects.filter(run_id=source_id,
                    organization_id=organization_id, domain__iexact=domain).first()
                if candidate:
                    pending.append(candidate)
    return None
