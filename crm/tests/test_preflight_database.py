"""Preflight binding, fixes and seed recovery against synthetic data/providers."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import re

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from crm import blasts
from crm.deliverability import guardian, volume, waves
from crm.deliverability.preflight import render, seeds, service
from crm.deliverability.streams import sync_streams
from crm.models import Blast, BlastDelivery, Contact, DeliverabilityIncident, EmailEvent, MailDomainInfo, ReputationCheck, SeedMailbox, SeedTest, SendingStream, StreamDay

from .api_fixtures import PREFIX, CrmApiMixin, make_event
from .test_blasts_database import FakeResponse, ok
from .test_campaign_waves_database import WAVE_SETTINGS, WaveProvider
from .test_preflight_content_unit import TEMPLATE


class PreflightProvider(WaveProvider):
    def __init__(self):
        super().__init__()
        self.template = TEMPLATE
        self.score = .5
        self.settings = {'privacy.unsubscribe_header': True, 'app.root_url': 'https://lists.mlai.au',
            'app.message_rate': 2, 'bounce.enabled': True,
            'bounce.actions': {'hard': {'action': 'blocklist'}, 'complaint': {'action': 'blocklist'}, 'soft': {'action': 'none'}},
            'smtp': [{'enabled': True, 'from_addresses': ['news.mlai.au'], 'tls_type': 'STARTTLS', 'tls_skip_verify': False}]}
        self.quality_calls = 0

    def __call__(self, method, url, **kwargs):
        if url == 'http://rspamd:11333/checkv2':
            self.quality_calls += 1
            return FakeResponse(200, {'score': self.score, 'symbols': {}})
        return super().__call__(method, url, **kwargs)

    def route(self, method, path, params, body):
        if (method, path) == ('GET', '/api/templates/3'):
            return ok({'id': 3, 'body': self.template, 'type': 'campaign'})
        if (method, path) == ('GET', '/api/settings'):
            return ok(self.settings)
        if method == 'GET' and re.fullmatch(r'/api/campaigns/\d+/preview', path):
            return SimpleNamespace(status_code=200, text='<html><p>MLAI community news for our friends.</p></html>')
        return super().route(method, path, params, body)


@override_settings(**{**WAVE_SETTINGS, 'CRM_PREFLIGHT_ENABLED': True})
class PreflightDatabaseTests(CrmApiMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.fake = PreflightProvider()
        self.enterContext(patch('integrations.http_client.request', self.fake))
        self.enterContext(patch('crm.sending_domains.is_verified', return_value=True))
        self.enterContext(patch('crm.deliverability.preflight.links.inspect_links', return_value=[]))
        sync_streams()
        self.news = SendingStream.objects.get(key='news')
        self.news.mailbox.enabled = True; self.news.mailbox.save()
        cache.set('crm:deliverability:healthy', True, 3600)
        MailDomainInfo.objects.create(domain='example.test', provider='google', mx=['mx.example.test'], has_mx=True, checked_at=self.now)
        Contact.objects.bulk_create([Contact(email=f'person-{n}@example.test', source='manual',
            marketing_consent='granted', marketing_consent_at=self.now) for n in range(50)])
        self.draft = Blast.objects.create(created_by=self.admin, stream=self.news, mailbox=self.news.mailbox,
            purpose='marketing', channels=['email'], subject='Your MLAI community news', preview_text='People and ideas from MLAI.',
            body_markdown='community ' * 60, sign_off_name='Sam', segment_definition={'rules': [{'type': 'source', 'values': ['manual']}]})

    def check(self):
        self.draft.refresh_from_db()
        return service.run_preflight(self.draft, now=self.now)

    def queue(self, digest):
        return blasts.queue_blast(self.draft.pk, actor=self.admin, confirm={'email': 50}, idempotency_key=str(self.draft.pk),
            preflight_hash=digest, now=self.now)

    def enroll(self):
        return [SeedMailbox.objects.create(provider=provider, address=f'seed-{provider}@example.test',
            imap_host='imap.example.test', auth='app_password', secret_env='CRM_SEED_' + provider.upper() + '_PASSWORD', enabled=True)
            for provider in ('gmail', 'outlook', 'yahoo')]

    def arrival(self, mailbox, probe, *, now):
        return {'provider': mailbox.provider, 'folder': 'inbox', 'spf': 'pass', 'dkim': 'pass', 'dmarc': 'pass',
            'list_unsubscribe': True, 'list_unsubscribe_post': True, 'one_click_signed': True, 'feedback_id_signed': True,
            'dkim_signed_headers': ['list-unsubscribe', 'list-unsubscribe-post', 'feedback-id'], 'arrived_at': now.isoformat()}

    def sending_time(self):
        clock = self.now.astimezone(volume.MELBOURNE)
        for _ in range(8 * 24):
            if volume.window(self.news, clock):
                return clock
            clock = clock.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        self.fail('No configured sending window')

    def test_fresh_complete_checks_bind_send_and_do_not_mutate_contacts(self):
        before = list(Contact.objects.order_by('pk').values('pk', 'email_status', 'marketing_consent', 'attributes'))
        result = self.check()
        self.assertTrue(result['can_send'], result)
        self.assertEqual(result['seed_test']['status'], 'timeout')
        self.assertEqual(before, list(Contact.objects.order_by('pk').values('pk', 'email_status', 'marketing_consent', 'attributes')))
        with self.assertRaises(blasts.BlastError) as caught:
            self.queue(None)
        self.assertEqual(caught.exception.code, 'preflight_required')
        self.assertFalse(BlastDelivery.objects.exists())
        self.queue(result['content_hash'])
        self.assertEqual(BlastDelivery.objects.count(), 50)
        self.assertEqual(self.fake.tests, [])

    def test_message_and_template_changes_make_prior_checks_stale(self):
        result = self.check()
        self.fake.template += '\nUpdated standard footer'
        with self.assertRaises(blasts.BlastError) as caught:
            self.queue(result['content_hash'])
        self.assertEqual(caught.exception.code, 'preflight_required')
        self.fake.template = TEMPLATE
        Blast.objects.filter(pk=self.draft.pk).update(body_markdown='A changed message')
        self.draft.refresh_from_db()
        self.assertEqual(service.current_result(self.draft)['status'], 'stale')
        with self.assertRaises(blasts.BlastError):
            self.queue(result['content_hash'])
        self.assertFalse(BlastDelivery.objects.exists())

    def test_expired_checks_and_changed_quality_policy_require_a_new_check(self):
        result = self.check()
        record = {**result, 'checked_at': (self.now - timedelta(hours=25)).isoformat()}
        Blast.objects.filter(pk=self.draft.pk).update(preflight=record)
        self.draft.refresh_from_db()
        self.assertFalse(service.current_result(self.draft)['can_send'])
        with self.assertRaises(blasts.BlastError):
            self.queue(result['content_hash'])
        Blast.objects.filter(pk=self.draft.pk).update(preflight=result)
        with override_settings(CRM_SPAM_BLOCK_SCORE=4.0), self.assertRaises(blasts.BlastError):
            self.queue(result['content_hash'])

    def test_config_drift_is_checked_again_at_send(self):
        result = self.check()
        self.fake.settings['privacy.unsubscribe_header'] = False
        with self.assertRaises(blasts.BlastError) as caught:
            self.queue(result['content_hash'])
        self.assertEqual(caught.exception.code, 'preflight_required')
        self.assertFalse(BlastDelivery.objects.exists())

    def test_spam_score_block_prevents_seed_submission_and_queueing(self):
        self.enroll()
        self.fake.score = 6.0
        result = self.check()
        self.assertIn('spam_score', result['blocking'])
        self.assertFalse(SeedTest.objects.exists())
        self.assertFalse(self.fake.tests)
        with self.assertRaises(blasts.BlastError):
            self.queue(result['content_hash'])

    def test_audience_checks_cover_size_exclusions_delay_event_and_duplicate(self):
        from crm.deliverability import planner
        prepared = planner.prepare(self.draft, self.now)
        digest = render.content_hash(self.draft, TEMPLATE)
        checks = lambda: {row['key']: row for row in service.audience_checks(self.draft, prepared, digest, self.now)}
        self.assertEqual(checks()['audience_size']['status'], 'pass')
        prepared.summary['people'] = 0
        self.assertEqual(checks()['audience_size']['status'], 'block')
        prepared.summary['people'] = 50001
        self.assertEqual(checks()['audience_size']['status'], 'block')
        prepared.summary['people'] = 50
        prepared.excluded['dormant'] = 11
        prepared.summary['estimate'] = {'days': 4, 'finish_at': (self.now + timedelta(days=4)).isoformat(),
            'slots': [{'at': (self.now + timedelta(hours=1)).isoformat(), 'count': 25}]}
        event = make_event('preflight-event', start=self.now + timedelta(days=1))
        self.draft.event, self.draft.purpose = event, 'event_update'
        result = checks()
        self.assertEqual(result['long_send']['status'], 'warn')
        self.assertEqual(result['time_critical']['status'], 'warn')
        self.assertIn('25', result['time_critical']['detail'])
        self.assertIn('11', result['dormant_excluded']['detail'])
        previous = Blast.objects.create(purpose='marketing', queued_at=self.now - timedelta(days=1),
            preflight={'content_hash': digest}, status='sent')
        contact = Contact.objects.first()
        BlastDelivery.objects.create(blast=previous, contact=contact, address=contact.email, channel='email', status='sent')
        self.draft.purpose = 'marketing'
        result = checks()
        self.assertEqual(result['duplicate_content']['status'], 'warn')
        self.assertIn('1 people', result['recently_emailed']['detail'])

    def test_system_checks_cover_stream_dns_feedback_and_reputation(self):
        checks = lambda: {row['key']: row for row in service.system_checks(self.draft, TEMPLATE, self.now)}
        for state, status in (('warming', 'pass'), ('held', 'warn'), ('slowed', 'warn'), ('stopped', 'block')):
            self.news.state = state
            self.assertEqual(checks()['stream_state']['status'], status)
        self.news.state = 'warming'
        with patch('crm.sending_domains.is_verified', return_value=False):
            self.assertEqual(checks()['dns']['status'], 'block')
        self.assertEqual(checks()['feedback']['status'], 'warn')
        EmailEvent.objects.create(stream=self.news, source='resend', event_type='email.delivered',
            receipt='f' * 64, recipient='synthetic@example.test', occurred_at=self.now)
        self.assertEqual(checks()['feedback']['status'], 'pass')
        self.assertEqual(checks()['reputation']['status'], 'warn')
        for subject in (self.news.domain, 'lists.mlai.au', 'mlai.au'):
            ReputationCheck.objects.create(subject=subject, source='surbl', listed=False, checked_at=self.now)
        self.assertEqual(checks()['reputation']['status'], 'pass')
        ReputationCheck.objects.create(subject=self.news.domain, source='spamhaus_dbl', listed=True, checked_at=self.now)
        self.assertEqual(checks()['reputation']['status'], 'block')
        link = service._reputation({'bad.example'}, self.now, 'link_reputation', 'Link safety', 'message')
        self.assertEqual(link['status'], 'warn')
        ReputationCheck.objects.create(subject='bad.example', source='safe_browsing', listed=True, checked_at=self.now)
        self.assertEqual(service._reputation({'bad.example'}, self.now, 'link_reputation', 'Link safety', 'message')['status'], 'block')

    def test_postmaster_data_needs_a_recent_valid_rate_and_obeys_thresholds(self):
        row = StreamDay.objects.create(stream=self.news, day=self.now.date() - timedelta(days=1), budget=150, ramp_stage=0)
        for rate, status in ((0.0, 'pass'), (.001, 'warn'), (.0015, 'block'), ('invalid', 'warn'), (True, 'warn'), (1.1, 'warn')):
            row.postmaster = {'spam_rate': rate}; row.save()
            result = {item['key']: item for item in service.system_checks(self.draft, TEMPLATE, self.now)}
            self.assertEqual(result['postmaster']['status'], status)
        row.day = self.now.date() - timedelta(days=8); row.postmaster = {'spam_rate': .002}; row.save()
        result = {item['key']: item for item in service.system_checks(self.draft, TEMPLATE, self.now)}
        self.assertEqual(result['postmaster']['status'], 'warn')
        self.assertIsNone(result['postmaster']['owner_detail']['spam_rate'])

    def test_seed_observed_identity_failure_and_two_spam_providers_block(self):
        mailboxes = self.enroll()
        first = self.check()
        def bad_identity(mailbox, probe, *, now):
            return {**self.arrival(mailbox, probe, now=now), 'dmarc': 'fail'}
        with patch('crm.deliverability.preflight.seed_inbox.read_mailbox', side_effect=bad_identity):
            probe = seeds.poll(first['seed_test']['id'], now=self.now + timedelta(minutes=1))
        self.assertEqual(seeds.probe_result(probe)['grade'], 'block')
        service.refresh_seed_result(probe, now=self.now)
        self.draft.refresh_from_db()
        self.assertIn('seed_test', service.current_result(self.draft)['blocking'])
        with self.assertRaises(blasts.BlastError):
            self.queue(first['content_hash'])
        # A different content hash gets a new probe. Every observed result is
        # receiver evidence, never an inferred pass from a successful API call.
        self.draft.subject += ' again'; self.draft.save()
        next_check = self.check()
        def spam(mailbox, probe, *, now):
            return {**self.arrival(mailbox, probe, now=now), 'folder': 'spam' if mailbox in mailboxes[:2] else 'inbox'}
        with patch('crm.deliverability.preflight.seed_inbox.read_mailbox', side_effect=spam):
            probe = seeds.poll(next_check['seed_test']['id'], now=self.now + timedelta(minutes=2))
        self.assertEqual(seeds.probe_result(probe)['grade'], 'block')
        with self.assertRaises(blasts.BlastError):
            self.queue(next_check['content_hash'])

    def test_unsafe_links_never_reach_preview_or_quality_service(self):
        from crm.deliverability.preflight.links import Observation
        self.draft.body_markdown = '[Internal](https://127.0.0.1/private)'; self.draft.save()
        with patch('crm.deliverability.preflight.links.inspect_links', return_value=[
                Observation('https://127.0.0.1/private', 'https://127.0.0.1/private', None, 'ip_address_link')]):
            result = self.check()
        self.assertIn('link_ip', result['blocking'])
        self.assertEqual((len(self.fake.campaigns), self.fake.quality_calls), (0, 0))

    def test_fixes_preserve_draft_edits_and_only_use_verified_reachable_destinations(self):
        from crm.deliverability.preflight.links import Observation
        self.draft.body_markdown = '[Join](https://bit.ly/synthetic)'; self.draft.save()
        with patch('crm.deliverability.preflight.links.inspect', return_value=Observation(
                'https://bit.ly/synthetic', 'https://example.org/full-address', 200)):
            fixed = service.fix_draft(self.draft.pk, 'expand_short_links', actor=self.admin, now=self.now)
        self.assertEqual(fixed.body_markdown, '[Join](https://example.org/full-address)')
        self.assertEqual(fixed.preflight, {})
        self.draft.body_markdown = 'Read [here](http://example.org)'; self.draft.save()
        with patch('crm.deliverability.preflight.links.inspect', return_value=Observation(
                'https://example.org', 'http://example.org', 200)):
            fixed = service.fix_draft(self.draft.pk, 'upgrade_https', actor=self.admin, now=self.now)
        self.assertIn('http://example.org', fixed.body_markdown)

    def test_preview_and_prefix_fixes_are_explicit_and_audited(self):
        self.draft.subject = 'Re: Fwd: Community news'
        self.draft.body_markdown = 'Hello friends. Here is our news.'
        self.draft.save()
        fixed = service.fix_draft(self.draft.pk, 'remove_subject_prefix', actor=self.admin, now=self.now)
        self.assertEqual(fixed.subject, 'Community news')
        fixed = service.fix_draft(self.draft.pk, 'add_preview_text', actor=self.admin, now=self.now)
        self.assertEqual(fixed.preview_text, 'Hello friends.')

    def test_uncertain_seed_send_is_never_blindly_repeated(self):
        from crm import listmonk
        self.enroll()
        original = listmonk.test_campaign
        def sent_then_lost(*args, **kwargs):
            original(*args, **kwargs)
            raise listmonk.ListmonkError('timeout')
        with patch('crm.listmonk.test_campaign', side_effect=sent_then_lost):
            first = self.check()
        self.assertEqual(first['status'], 'running')
        self.assertTrue(first['can_send'])
        second = self.check()
        self.assertEqual(first['seed_test']['id'], second['seed_test']['id'])
        self.assertEqual(len(self.fake.tests), 1)
        probe = SeedTest.objects.get()
        self.assertEqual(probe.results['submission']['state'], 'uncertain')
        self.assertTrue(all(not self.fake.on_list(list_id) for list_id in self.fake.lists))

    def test_staff_can_queue_during_inbox_check_but_no_audience_wave_starts(self):
        from crm.deliverability.copy import status_message
        self.enroll()
        first = self.check()
        self.assertEqual(first['status'], 'running')
        self.queue(first['content_hash'])
        clock = self.sending_time()
        waves.dispatch(self.draft.pk, now=clock)
        self.draft.refresh_from_db()
        self.assertFalse(self.draft.waves.exists())
        self.assertIn('inbox test', status_message(self.draft)['text'])
        self.assertFalse(self.fake.starts)
        with patch('crm.deliverability.preflight.seed_inbox.read_mailbox', side_effect=self.arrival):
            probe = seeds.poll(first['seed_test']['id'], now=self.now + timedelta(minutes=1))
        service.refresh_seed_result(probe, now=self.now)
        waves.dispatch(self.draft.pk, now=clock)
        self.assertEqual(self.draft.waves.count(), 1)

    def test_seed_results_complete_current_checks_but_never_authorize_edited_content(self):
        self.enroll()
        first = self.check()
        with patch('crm.deliverability.preflight.seed_inbox.read_mailbox', side_effect=self.arrival):
            probe = seeds.poll(first['seed_test']['id'], now=self.now + timedelta(minutes=1))
        service.refresh_seed_result(probe, now=self.now)
        self.draft.refresh_from_db()
        self.assertTrue(service.current_result(self.draft)['can_send'])
        Blast.objects.filter(pk=self.draft.pk).update(preview_text='A different preview')
        self.draft.refresh_from_db()
        self.assertEqual(service.current_result(self.draft)['status'], 'stale')
        self.assertFalse(service.current_result(self.draft)['can_send'])

    def test_timeout_halves_only_the_canary_and_creates_one_amber_incident(self):
        result = self.check()
        self.queue(result['content_hash'])
        waves.dispatch(self.draft.pk, now=self.sending_time())
        wave = self.draft.waves.get()
        self.assertEqual((wave.size, wave.stats['canary_min_sample']), (25, 25))
        self.assertEqual(DeliverabilityIncident.objects.get(signal='seed_timeout').action, 'reduce_canary')
        seeds.timeout_incident(SeedTest.objects.get(), self.now)
        self.assertEqual(DeliverabilityIncident.objects.filter(signal='seed_timeout').count(), 1)
        wave.deliveries.filter(pk=wave.deliveries.first().pk).update(status='bounced', bounce_kind='hard')
        cache.delete('crm:deliverability:guard')
        guardian.run_deliverability_guard(now=self.sending_time())
        self.assertTrue(DeliverabilityIncident.objects.filter(wave=wave, severity='amber').exists())

    def test_seed_content_uses_its_immutable_snapshot_after_a_draft_edit(self):
        self.enroll()
        with patch('crm.deliverability.preflight.seeds.submit'):
            self.check()
        probe = SeedTest.objects.get()
        original = self.draft.body_markdown
        Blast.objects.filter(pk=self.draft.pk).update(body_markdown='A changed message')
        seeds.submit(probe.pk, now=self.now)
        self.assertEqual(len(self.fake.tests), 1)
        self.assertIn(original.strip(), self.fake.tests[0][1]['body'])
        self.assertNotIn('A changed message', self.fake.tests[0][1]['body'])

    def test_verified_admin_api_keeps_owner_diagnostics_off_staff_responses(self):
        with override_settings(CRM_DELIVERABILITY_OWNERS=()):
            response = self.client.post(PREFIX + f'blasts/{self.draft.pk}/preflight/', {}, format='json')
            self.assertEqual(response.status_code, 200, response.content)
            self.assertTrue(response.json()['can_send'])
            self.assertTrue(all('owner_detail' not in row for row in response.json()['items']))
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(self.member_client.post(PREFIX + f'blasts/{self.draft.pk}/preflight/', {}).status_code, 403)

    def test_seed_config_references_do_not_copy_secret_values_into_the_database(self):
        import json
        spec = {'provider': 'gmail', 'address': 'probe@example.test', 'imap_host': 'imap.example.test',
            'auth': 'app_password', 'secret_env': 'CRM_SEED_SYNTHETIC_PASSWORD', 'enabled': False}
        with override_settings(CRM_SEED_MAILBOXES=json.dumps([spec])), patch.dict('os.environ', {'CRM_SEED_SYNTHETIC_PASSWORD': 'private-synthetic-value'}):
            self.assertEqual(seeds.sync_mailboxes(), 1)
        row = SeedMailbox.objects.get()
        self.assertEqual(row.secret_env, 'CRM_SEED_SYNTHETIC_PASSWORD')
        self.assertNotIn('private-synthetic-value', str(vars(row)))
        self.assertFalse(self.fake.tests)
