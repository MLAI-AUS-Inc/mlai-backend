"""Preflight orchestration, current-content binding and explicit draft fixes."""

import hashlib
import html as html_tools
import json
import math
import re
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from crm import blast_email, listmonk, sending_domains
from crm.blasts import BlastError
from crm.models import Blast, Contact, EmailEvent, ReputationCheck, SeedTest

from .. import guardian, planner, policy, volume
from ..monitors.config_check import check_listmonk
from . import content, links, render, seeds

VERSION = '2026-11-05.1'


def policy_hash():
    prefixes = ('CRM_SUBJECT_', 'CRM_BODY_', 'CRM_HTML_', 'CRM_LINKS_', 'CRM_SPAM_', 'CRM_FOOTER_',
        'CRM_ALLOWED_ACRONYMS', 'CRM_SEED_SPAM_', 'CRM_PREFLIGHT_POSTMASTER_', 'CRM_POSTMASTER_MAX_AGE_DAYS', 'CRM_RECONFIRMATION_')
    values = {key: policy.value(key) for key in policy.DEFAULTS if key.startswith(prefixes)}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _reputation(subjects, now, key, title, group, urls=None):
    latest = {}
    for check in ReputationCheck.objects.filter(subject__in=subjects,
            checked_at__gte=now - timedelta(hours=policy.value('CRM_REPUTATION_MAX_AGE_HOURS'))).order_by('-checked_at'):
        if check.source == 'safe_browsing' and urls is not None:
            from ..monitors.reputation import urls_digest
            wanted = [url for url in urls if links.parsed_url(url)[1] == check.subject]
            if check.detail.get('urls_hash') != urls_digest(wanted):
                continue
        latest.setdefault((check.subject, check.source), check)
    listed = sorted({check.subject for check in latest.values() if check.listed is True})
    checked = {check.subject for check in latest.values() if check.listed is not None}
    unknown = sorted(set(subjects) - checked)
    return content.item(key, 'block' if listed else 'warn' if unknown else 'pass', title,
        'A web address needs the email team to check it.' if listed else 'Some safety checks are not available yet.' if unknown else 'Ready.',
        group=group, owner_detail={'listed': listed, 'unknown': unknown})


def audience_checks(blast, prepared, digest, now):
    summary = prepared.summary
    people = summary['people']
    contacts = Contact.objects.filter(pk__in=[row.contact.pk for row in prepared.recipients]).annotate(contact_id=F('pk'))
    deferred = people - planner.eligible_rows(contacts, blast, now).count()
    days = summary['estimate'].get('days')
    rows = [content.item('audience_size', 'block' if not people or people > policy.value('CRM_MAX_BLAST_RECIPIENTS') else 'pass',
        'People who can receive it', f'{people:,} people.', group='audience'),
        content.item('dormant_excluded', 'pass', 'People waiting for a new invitation',
            f'{prepared.excluded.get("dormant", 0):,} people were left out because they have not been active recently.', group='audience'),
        content.item('recently_emailed', 'pass', 'People recently emailed',
            f'{deferred:,} people will wait until enough time has passed since their last email.', group='audience'),
        content.item('long_send', 'warn' if days is None or days > policy.value('CRM_LONG_SEND_DAYS') else 'pass',
            'Expected timing', 'Waiting for the email system before estimating.' if days is None else f'This is expected to take about {days} sending days.', group='audience')]
    finish = blast_email.parse_iso(summary['estimate'].get('finish_at'))
    critical = volume.time_critical(blast, now)
    late = critical and (finish is None or finish > blast.event.start_at)
    before = sum(row['count'] for row in summary['estimate'].get('slots', [])
        if blast_email.parse_iso(row['at']) < blast.event.start_at) if critical and finish else None
    rows.append(content.item('time_critical', 'warn' if late else 'pass', 'Before the event',
        (f'About {before:,} people can be reached before the event. Consider a text message for the rest.' if before is not None
         else 'We cannot yet estimate how many will receive this before the event.') if late else 'Ready.', group='audience'))
    duplicate = Blast.objects.exclude(pk=blast.pk).filter(preflight__content_hash=digest,
        queued_at__gte=now - timedelta(days=policy.value('CRM_DUPLICATE_CONTENT_DAYS')),
        deliveries__contact_id__in=contacts.values('pk'), deliveries__channel='email',
        deliveries__status__in=('submitting', 'sent', 'delivered', 'read', 'undelivered', 'bounced', 'unknown')).exists()
    rows.append(content.item('duplicate_content', 'warn' if duplicate else 'pass', 'Already shared',
        'Some people recently received this same message.' if duplicate else 'No recent copy found for these people.', group='audience'))
    return rows


def system_checks(blast, template, now, *, refresh=False):
    stream = blast.stream
    enabled = (settings.CRM_SENDING_ENABLED and blast.mailbox.enabled and policy.value('CRM_WAVES_ENABLED')
        and policy.value('CRM_GUARDIAN_ENABLED') and guardian.admission_ready())
    rows = [content.item('stream_state', 'block' if stream.state == 'stopped' or not enabled else 'warn' if stream.state in ('held', 'slowed') else 'pass',
        'Sending system', 'The email team needs to finish a system check.' if stream.state == 'stopped' or not enabled else
        'This email will wait or send more slowly while we check the email service.' if stream.state in ('held', 'slowed') else 'Ready.', group='system',
        owner_detail={'state': stream.state, 'enabled': bool(enabled)})]
    verified = sending_domains.is_verified(stream.domain)
    rows.append(content.item('dns', 'pass' if verified else 'block', 'Sender identity',
        'Ready.' if verified else 'The email team needs to check the sending address.', group='system'))
    config = check_listmonk(stream, now=now, template=template, refresh=refresh)
    rows.append(content.item('listmonk', 'pass' if config['status'] == 'pass' else 'block', 'Email service',
        'Ready.' if config['status'] == 'pass' else 'The email service needs a check before sending.', group='system', owner_detail=config))
    feedback = EmailEvent.objects.filter(stream=stream, source='resend',
        received_at__gte=now - timedelta(hours=policy.value('CRM_PREFLIGHT_FEEDBACK_HOURS'))).exists()
    tested = EmailEvent.objects.filter(stream=stream, source='resend', recipient__iendswith='@resend.dev',
        received_at__gte=now - timedelta(days=policy.value('CRM_WEBHOOK_TEST_MAX_AGE_DAYS'))).exists()
    rows.append(content.item('feedback', 'pass' if feedback or tested else 'warn', 'Delivery updates',
        'Ready.' if feedback or tested else 'The email team has not received a recent delivery update.', group='system'))
    rows.append(_reputation({stream.domain, 'lists.mlai.au', 'mlai.au'}, now, 'reputation', 'Sending address safety', 'system'))
    latest = stream.days.exclude(postmaster={}).order_by('-day').first()
    data = latest.postmaster if latest else {}
    rate = data.get('spam_rate')
    if (type(rate) not in (int, float) or not math.isfinite(rate) or not 0 <= rate <= 1
            or not latest or latest.day > now.date()
            or latest.day < now.date() - timedelta(days=policy.value('CRM_POSTMASTER_MAX_AGE_DAYS'))):
        rate = None
    status = 'warn' if rate is None else 'block' if rate >= policy.value('CRM_PREFLIGHT_POSTMASTER_BLOCK') else 'warn' if rate >= policy.value('CRM_PREFLIGHT_POSTMASTER_WARN') else 'pass'
    rows.append(content.item('postmaster', status, 'Inbox feedback',
        'The email team has no recent inbox feedback yet.' if rate is None else 'Inbox feedback needs a look.' if status != 'pass' else 'Ready.',
        group='system', owner_detail={'spam_rate': rate, 'day': latest.day.isoformat() if latest else None}))
    return rows


def _seed_item(result):
    waiting = result.get('status') in ('sending', 'waiting', 'not_requested', 'not_started')
    status = 'warn' if waiting else result.get('grade', 'warn')
    detail = 'Checking how this email arrives in the test inboxes.' if waiting else (
        'The email team needs to fix how this email arrives.' if status == 'block' else
        'Some inbox checks need a look. The first batch will be smaller if a check timed out.' if status == 'warn' else 'The test inboxes received it.')
    return content.item('seed_test', status, 'Inbox check', detail, group='inbox', owner_detail={'reason': result.get('reason')})


def _finish(result):
    waiting = (result.get('seed_test') or {}).get('status') in ('sending', 'waiting', 'not_requested', 'not_started')
    result['blocking'] = [row['key'] for row in result['items'] if row['status'] == 'block']
    # Queueing and actual wave admission are separate. Staff may press Send
    # during a probe; admission waits for it without sending to the audience.
    result['status'] = 'running' if waiting and not result['blocking'] else 'complete'
    result['can_send'] = not result['blocking']
    return result


def run_preflight(blast, *, now=None, include_seed=True):
    if not policy.value('CRM_PREFLIGHT_ENABLED'):
        raise BlastError('preflight_disabled', 404)
    now = now or timezone.now()
    template, rendered = '', None
    try:
        template = render.template_body()
    except listmonk.ListmonkError:
        pass
    digest = render.content_hash(blast, template)
    prepared = planner.prepare(blast, now)
    observations = links.inspect_links(blast.body_markdown)
    unsafe = any(row.error in ('ip_address_link', 'nonpublic_link', 'invalid_link') for row in observations)
    unsafe |= len({url for url, _ in links.link_targets(blast.body_markdown)}) > policy.value('CRM_LINKS_BLOCK')
    try:
        if blast.subject.strip() and not unsafe:
            rendered = render.preview(blast, digest)
    except (listmonk.ListmonkError, ValueError):
        pass
    spam = render.spam_score(blast, rendered, digest, now) if not unsafe else {'status': 'unavailable', 'reason': 'unsafe_links'}
    items = content.message_checks(blast, html=rendered, template=template, observations=observations, spam=spam)
    subjects = set()
    for url, _ in links.link_targets(blast.body_markdown):
        try:
            subjects.add(links.parsed_url(url)[1])
        except ValueError:
            continue
    urls = set()
    for url, _ in links.link_targets(blast.body_markdown):
        try:
            links.parsed_url(url)
            urls.add(url)
        except ValueError:
            pass
    for observation in observations:
        try:
            subjects.add(links.parsed_url(observation.final_url)[1])
            urls.add(observation.final_url)
        except ValueError:
            pass
    if policy.value('CRM_MONITORS_ENABLED'):
        from ..monitors.reputation import check_subjects
        from ..monitors.http import budget
        try:
            with budget(policy.value('CRM_MONITOR_PASS_SECONDS')):
                check_subjects(subjects, urls=urls, now=now)
        except Exception:
            # A failed reputation lookup remains unknown, never a false pass.
            pass
    items.append(_reputation(subjects, now, 'link_reputation', 'Link safety', 'message', urls=urls))
    items += audience_checks(blast, prepared, digest, now) + system_checks(blast, template, now)
    seed = {'status': 'not_started', 'results': []}
    if not any(row['status'] == 'block' for row in items) and include_seed:
        try:
            probe = seeds.get_or_start(blast, digest, now=now, template=template)
        except listmonk.ListmonkError:
            probe = SeedTest.objects.filter(blast=blast, content_hash=digest).order_by('-created_at').first()
        if probe:
            seed = seeds.probe_result(probe)
            seeds.timeout_incident(probe, now)
    elif not include_seed:
        seed['status'] = 'not_requested'
    items.append(_seed_item(seed))
    if seed.get('timed_out'):
        used, hourly = volume.usage(blast.stream, now)
        prepared.summary['estimate'] = volume.estimate(blast.stream, prepared.summary['people'], now,
            used_today=used, used_hour=hourly, critical=volume.time_critical(blast, now), canary_factor=policy.value('CRM_SEED_CANARY_FACTOR'))
    result = _finish({'content_hash': digest, 'checked_at': now.isoformat(), 'version': VERSION,
        'policy_hash': policy_hash(), 'items': items, 'seed_test': seed})
    with transaction.atomic():
        current = Blast.objects.select_for_update(of=('self',)).select_related('event', 'mailbox', 'stream').get(pk=blast.pk)
        if render.content_hash(current, template) != digest:
            raise BlastError('draft_changed', 409)
        current.preflight = result
        current.plan = prepared.summary
        current.save(update_fields=['preflight', 'plan', 'updated_at'])
    return result


def refresh_seed_result(probe, *, now):
    with transaction.atomic():
        blast = Blast.objects.select_for_update().get(pk=probe.blast_id)
        result = dict(blast.preflight or {})
        if result.get('content_hash') != probe.content_hash or (result.get('seed_test') or {}).get('id') != str(probe.pk):
            return
        result['seed_test'] = seeds.probe_result(probe)
        result['items'] = [row for row in result.get('items') or [] if row['key'] != 'seed_test'] + [_seed_item(result['seed_test'])]
        blast.preflight = _finish(result)
        blast.save(update_fields=['preflight', 'updated_at'])


def current_result(blast):
    result = dict(blast.preflight or {})
    checked, now = blast_email.parse_iso(result.get('checked_at')), timezone.now()
    expired = blast.status == Blast.Status.DRAFT and (not checked or checked > now
        or checked <= now - timedelta(hours=policy.value('CRM_PREFLIGHT_MAX_AGE_HOURS')))
    try:
        current = render.content_hash(blast, render.template_body())
    except listmonk.ListmonkError:
        current = None
    if (not result or expired or current != result.get('content_hash') or result.get('version') != VERSION
            or result.get('policy_hash') != policy_hash()):
        return {'status': 'stale', 'content_hash': current, 'items': [], 'seed_test': {'status': 'not_started', 'results': []},
            'can_send': False, 'blocking': ['preflight_required']}
    seed_id = (result.get('seed_test') or {}).get('id')
    if seed_id:
        probe = SeedTest.objects.filter(pk=seed_id, blast=blast, content_hash=current).first()
        if probe:
            result['seed_test'] = seeds.probe_result(probe)
            result['items'] = [row for row in result.get('items') or [] if row['key'] != 'seed_test'] + [_seed_item(result['seed_test'])]
    return _finish(result)


def queue_context(blast, *, now):
    """Read provider configuration before entering queue_blast's transaction."""
    try:
        template = render.template_body(refresh=True)
        checks = system_checks(blast, template, now, refresh=True)
    except listmonk.ListmonkError:
        raise BlastError('preflight_required', 409) from None
    if any(row['status'] == 'block' for row in checks):
        raise BlastError('preflight_required', 409)
    return template


def require(blast, supplied_hash, *, now, template):
    result = blast.preflight or {}
    seed = seed_result(blast, result)
    checked = blast_email.parse_iso(result.get('checked_at'))
    digest = render.content_hash(blast, template)
    if (not supplied_hash or supplied_hash != digest or result.get('content_hash') != digest
            or result.get('version') != VERSION or result.get('policy_hash') != policy_hash()
            or not checked or checked > now or checked <= now - timedelta(hours=policy.value('CRM_PREFLIGHT_MAX_AGE_HOURS'))
            or not result.get('can_send') or result.get('blocking') or seed.get('grade') == 'block'
            or (result.get('status') != 'complete' and not (result.get('status') == 'running'
                and (result.get('seed_test') or {}).get('status') in ('sending', 'waiting')))):
        raise BlastError('preflight_required', 409)


def seed_result(blast, result):
    """Read the probe's committed evidence even if its preflight refresh is pending."""
    seed = result.get('seed_test') or {}
    if seed.get('id'):
        probe = SeedTest.objects.filter(pk=seed['id'], blast=blast, content_hash=result.get('content_hash')).first()
        if probe:
            return seeds.probe_result(probe)
    return seed


def admission(blast):
    """A queued campaign may span days; edits to its content or template still stop it."""
    result = blast.preflight or {}
    try:
        valid = (result.get('can_send') and result.get('content_hash') == render.content_hash(blast, render.template_body())
            and result.get('version') == VERSION and result.get('policy_hash') == policy_hash())
    except listmonk.ListmonkError:
        valid = False
    seed = seed_result(blast, result)
    if not valid or seed.get('grade') == 'block':
        return 'preflight_required'
    return 'inbox_test_pending' if seed.get('status') not in ('complete', 'timeout', 'error') else None


def fix_draft(blast_id, kind, *, actor, now=None):
    from community_chat.admin_access import require_mlai_admin
    from crm.models import CrmAuditEvent
    require_mlai_admin(actor)
    if kind not in ('expand_short_links', 'upgrade_https', 'add_preview_text', 'remove_subject_prefix'):
        raise BlastError('invalid_preflight_fix')
    now = now or timezone.now()
    draft = Blast.objects.get(pk=blast_id)
    if draft.status != 'draft':
        raise BlastError('blast_not_draft', 409)
    original = (draft.subject, draft.body_markdown, draft.preview_text)
    if kind == 'add_preview_text':
        draft.preview_text = re.split(r'(?<=[.!?])\s+', content.plain_text(draft.body_markdown), maxsplit=1)[0][:200]
    elif kind == 'remove_subject_prefix':
        while content.PREFIX.match(draft.subject):
            draft.subject = content.PREFIX.sub('', draft.subject, count=1)
    else:
        for url, _ in links.link_targets(draft.body_markdown):
            try:
                parsed, host = links.parsed_url(url)
            except ValueError:
                continue
            if kind == 'expand_short_links' and not (host in links.SHORTENERS or any(host.endswith('.' + value) for value in links.SHORTENERS)):
                continue
            if kind == 'upgrade_https' and parsed.scheme != 'http':
                continue
            candidate = parsed._replace(scheme='https').geturl() if kind == 'upgrade_https' else url
            result = links.inspect(candidate)
            if result.error or result.status is None or result.status >= 400:
                continue
            replacement = result.final_url
            if kind == 'upgrade_https' and urlsplit(replacement).scheme != 'https':
                continue
            draft.body_markdown = draft.body_markdown.replace(url, replacement).replace(html_tools.escape(url, quote=True), html_tools.escape(replacement, quote=True))
    with transaction.atomic():
        current = Blast.objects.select_for_update().get(pk=blast_id)
        if current.status != 'draft' or (current.subject, current.body_markdown, current.preview_text) != original:
            raise BlastError('draft_changed', 409)
        current.subject, current.body_markdown, current.preview_text = draft.subject, draft.body_markdown, draft.preview_text
        current.preflight = {}
        current.save(update_fields=['subject', 'body_markdown', 'preview_text', 'preflight', 'updated_at'])
        CrmAuditEvent.objects.create(actor=actor, action='edit', target_type='blast', target_id=str(blast_id),
            detail={'operation': 'preflight_fix', 'kind': kind})
    return current
