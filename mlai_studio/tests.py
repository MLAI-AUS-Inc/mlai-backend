from django.test import TestCase
from rest_framework.test import APIClient

from .models import StudioApplication


URL = '/api/v1/mlai-studio/applications/'


def lead_payload(**overrides):
    payload = {
        'client_ref': 'lead_abc123',
        'stage': 'lead',
        'full_name': 'Jordan Taylor',
        'email': 'jordan@example.com',
        'phone': '+61 400 000 000',
    }
    payload.update(overrides)
    return payload


def complete_payload(**overrides):
    payload = lead_payload(
        stage='complete',
        location='Melbourne, AU',
        legal_work='Yes',
        visa='',
        linkedin='https://linkedin.com/in/jordan',
        github='https://github.com/jordan',
        portfolio='',
        skills=['Frontend', 'LLM / agents'],
        skills_other=[],
        ai_tools=['Claude', 'Cursor'],
        ai_tools_other=[],
        interests=['AI products'],
        interests_other=['Energy'],
        availability='10-20 hrs',
        availability_other='',
        start_date='Right now',
        start_date_other='',
        rate='$90/hr',
        projects='Shipped an AI thing.',
        anything_else='',
        consent=True,
    )
    payload.update(overrides)
    return payload


class StudioApplicationApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_lead_post_creates_record(self):
        response = self.client.post(URL, lead_payload(), format='json')
        self.assertEqual(response.status_code, 201)
        application = StudioApplication.objects.get(client_ref='lead_abc123')
        self.assertEqual(application.stage, StudioApplication.STAGE_LEAD)
        self.assertEqual(application.full_name, 'Jordan Taylor')
        self.assertEqual(application.email, 'jordan@example.com')
        self.assertFalse(application.consent)

    def test_complete_post_upserts_same_record(self):
        self.client.post(URL, lead_payload(), format='json')
        response = self.client.post(URL, complete_payload(), format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StudioApplication.objects.count(), 1)
        application = StudioApplication.objects.get(client_ref='lead_abc123')
        self.assertEqual(application.stage, StudioApplication.STAGE_COMPLETE)
        self.assertEqual(application.skills, ['Frontend', 'LLM / agents'])
        self.assertEqual(application.interests_other, ['Energy'])
        self.assertEqual(application.projects, 'Shipped an AI thing.')
        self.assertTrue(application.consent)

    def test_complete_without_prior_lead_creates_record(self):
        response = self.client.post(URL, complete_payload(), format='json')
        self.assertEqual(response.status_code, 201)
        application = StudioApplication.objects.get(client_ref='lead_abc123')
        self.assertEqual(application.stage, StudioApplication.STAGE_COMPLETE)

    def test_complete_requires_consent(self):
        response = self.client.post(URL, complete_payload(consent=False), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('consent', response.json())
        self.assertEqual(StudioApplication.objects.count(), 0)

    def test_email_required(self):
        response = self.client.post(URL, lead_payload(email=''), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('email', response.json())

    def test_invalid_email_rejected(self):
        response = self.client.post(URL, lead_payload(email='not-an-email'), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('email', response.json())

    def test_client_ref_required(self):
        payload = lead_payload()
        payload.pop('client_ref')
        response = self.client.post(URL, payload, format='json')
        self.assertEqual(response.status_code, 400)

    def test_invalid_stage_rejected(self):
        response = self.client.post(URL, lead_payload(stage='bogus'), format='json')
        self.assertEqual(response.status_code, 400)

    def test_stage_never_downgrades_and_partial_update_preserves_fields(self):
        self.client.post(URL, complete_payload(), format='json')
        response = self.client.post(URL, lead_payload(phone='+61 411 111 111'), format='json')
        self.assertEqual(response.status_code, 200)
        application = StudioApplication.objects.get(client_ref='lead_abc123')
        self.assertEqual(application.stage, StudioApplication.STAGE_COMPLETE)
        self.assertEqual(application.phone, '+61 411 111 111')
        self.assertEqual(application.projects, 'Shipped an AI thing.')
        self.assertTrue(application.consent)

    def test_get_not_allowed(self):
        self.client.post(URL, complete_payload(), format='json')
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 405)
