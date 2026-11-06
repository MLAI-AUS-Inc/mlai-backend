"""Staff composer presets use current permission and never select a From domain."""

from django.test import TestCase, override_settings
from django.utils import timezone

from community_chat.onboarding_models import CommunityMemberConsent, CommunityMemberProfile
from crm import blast_email, blasts
from crm.deliverability.streams import sync_streams
from crm.models import Blast, Contact, MailDomainInfo, SendingStream

from .api_fixtures import PREFIX, CrmApiMixin
from .test_campaign_waves_database import WAVE_SETTINGS


@override_settings(**WAVE_SETTINGS)
class CampaignUiContractTests(CrmApiMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        sync_streams()
        self.member.email_verified_at = self.now; self.member.save()
        self.profile = CommunityMemberProfile.objects.create(user=self.member, status='approved', marketing_opt_in=True)
        CommunityMemberConsent.objects.create(user=self.member, purpose='marketing_email', granted=True,
            policy_version='synthetic', source='test')
        self.contact = Contact.objects.create(user=self.member, email=self.member.email, source='chat_account',
            marketing_consent='granted', marketing_consent_at=self.now, last_active_at=self.now)
        Contact.objects.create(email='manual@example.test', source='manual', marketing_consent='granted',
            marketing_consent_at=self.now)
        MailDomainInfo.objects.create(domain='example.test', has_mx=True, mx=['mx.example.test'], checked_at=self.now)

    def create(self, **extra):
        response = self.client.post(PREFIX + 'blasts/', {'purpose': 'marketing', 'channels': ['email'],
            'audience_preset': 'opted_in', 'email': {'subject': 'Our community news', 'body_markdown': 'Hello friends'}, **extra}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        return Blast.objects.get(pk=response.json()['id']), response.json()

    def test_opted_in_preset_checks_current_source_consent_without_mutating_contacts(self):
        blast, result = self.create()
        self.assertEqual(result['audience']['kind'], 'preset')
        self.assertEqual(result['audience_preset'], {'kind': 'opted_in'})
        self.assertEqual(list(blasts.resolve_audience(blast, now=self.now).querysets['email']), [self.contact])
        self.profile.marketing_opt_in = False; self.profile.save()
        self.assertEqual(blasts.resolve_audience(blast, now=self.now).querysets['email'].count(), 0)
        self.assertEqual(blast_email._drop_reason(blast, self.contact, False), 'consent_withdrawn')
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.marketing_consent, 'granted')

    def test_latest_consent_withdrawal_and_changed_account_email_are_excluded(self):
        blast, _ = self.create()
        CommunityMemberConsent.objects.create(user=self.member, purpose='marketing_email', granted=False,
            policy_version='synthetic', source='test')
        self.assertFalse(blasts.resolve_audience(blast, now=self.now).querysets['email'].exists())
        CommunityMemberConsent.objects.create(user=self.member, purpose='marketing_email', granted=True,
            policy_version='synthetic', source='test')
        self.member.email = 'changed@example.test'; self.member.save()
        self.assertFalse(blasts.resolve_audience(blast, now=self.now).querysets['email'].exists())

    def test_city_preset_uses_reviewed_location_ids_and_infers_marketing(self):
        blast, result = self.create(audience_preset='city', city_id='au-vic-melbourne', purpose='event_update')
        self.assertEqual(blast.purpose, 'marketing')
        self.assertEqual(blast.segment_definition['include']['rule'],
            {'type': 'location_affinity', 'op': 'has_any', 'city_ids': ['au-vic-melbourne']})
        self.assertEqual(result['audience']['label'], 'People connected to Melbourne')
        response = self.client.post(PREFIX + 'blasts/', {'purpose': 'marketing', 'audience_preset': 'city',
            'city_id': 'unreviewed-city'}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_partial_edits_keep_preset_and_clearing_it_cannot_broaden_the_audience(self):
        blast, _ = self.create()
        self.client.patch(PREFIX + f'blasts/{blast.pk}/', {'email': {'preview_text': 'Our next chapter'}}, format='json')
        blast.refresh_from_db()
        self.assertEqual(blast.stats['audience_preset']['kind'], 'opted_in')
        self.client.patch(PREFIX + f'blasts/{blast.pk}/', {'audience_preset': None}, format='json')
        blast.refresh_from_db()
        with self.assertRaises(blasts.BlastError) as caught:
            blasts.resolve_audience(blast, now=self.now)
        self.assertEqual(caught.exception.code, 'audience_required')

    def test_access_capabilities_fail_closed_and_list_includes_pause_copy(self):
        response = self.client.get(PREFIX + 'access/')
        self.assertEqual(response.json()['campaign_features'], {'waves': True, 'preflight': False})
        self.assertFalse(self.member_client.get(PREFIX + 'access/').json()['campaign_features']['waves'])
        blast, _ = self.create()
        blast.status = 'queued'; blast.pause_state = 'staff'; blast.paused_by = self.admin; blast.save()
        row = self.client.get(PREFIX + 'blasts/').json()['results'][0]
        self.assertEqual(row['pause']['state'], 'staff')
        self.assertIn('Paused by', row['status_message']['text'])

    def test_plan_preview_masks_addresses_and_does_not_persist_personal_details(self):
        blast, _ = self.create()
        response = self.client.post(PREFIX + f'blasts/{blast.pk}/plan/', {}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['people'], 1)
        preview = response.json()['people_preview'][0]
        self.assertEqual(preview['id'], str(self.contact.pk))
        self.assertNotEqual(preview['address'], self.contact.email)
        blast.refresh_from_db()
        self.assertNotIn('people_preview', blast.plan)

    def test_a_stale_editor_cannot_restore_a_queued_blast_to_draft(self):
        from community_chat.crm.services import save_blast_draft
        from community_chat.crm.errors import CrmError
        blast, _ = self.create()
        Blast.objects.filter(pk=blast.pk).update(status='queued')
        with self.assertRaises(CrmError) as caught:
            save_blast_draft(blast, {'email': {'subject': 'A late edit'}}, actor=self.admin, now=self.now)
        self.assertEqual(caught.exception.code, 'blast_not_draft')
        blast.refresh_from_db()
        self.assertEqual((blast.status, blast.subject), ('queued', 'Our community news'))
