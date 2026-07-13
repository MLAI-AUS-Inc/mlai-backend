from django.test import TestCase
from rest_framework.test import APIClient

from .models import VictorApplication


URL = '/api/v1/victor-ai/applications/'


def lead_payload(**overrides):
    payload = {
        'client_ref': 'lead_abc123',
        'stage': 'lead',
        'first_name': 'Jordan',
        'last_name': 'Taylor',
        'email': 'jordan@example.com',
    }
    payload.update(overrides)
    return payload


def complete_payload(**overrides):
    payload = lead_payload(
        stage='complete',
        team_name='Team Sunrise',
        role='Founder',
        startup_stage='Prototype',
        location='Melbourne, AU',
        idea='An AI copilot for grant applications.',
        support='Intros to mentors.',
        consent=True,
    )
    payload.update(overrides)
    return payload


class VictorApplicationApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_lead_post_creates_record(self):
        response = self.client.post(URL, lead_payload(), format='json')
        self.assertEqual(response.status_code, 201)
        application = VictorApplication.objects.get(client_ref='lead_abc123')
        self.assertEqual(application.stage, VictorApplication.STAGE_LEAD)
        self.assertEqual(application.first_name, 'Jordan')
        self.assertEqual(application.last_name, 'Taylor')
        self.assertEqual(application.email, 'jordan@example.com')
        self.assertFalse(application.consent)

    def test_complete_post_upserts_same_record(self):
        self.client.post(URL, lead_payload(), format='json')
        response = self.client.post(URL, complete_payload(), format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(VictorApplication.objects.count(), 1)
        application = VictorApplication.objects.get(client_ref='lead_abc123')
        self.assertEqual(application.stage, VictorApplication.STAGE_COMPLETE)
        self.assertEqual(application.role, 'Founder')
        self.assertEqual(application.startup_stage, 'Prototype')
        self.assertEqual(application.idea, 'An AI copilot for grant applications.')
        self.assertTrue(application.consent)

    def test_complete_without_prior_lead_creates_record(self):
        response = self.client.post(URL, complete_payload(), format='json')
        self.assertEqual(response.status_code, 201)
        application = VictorApplication.objects.get(client_ref='lead_abc123')
        self.assertEqual(application.stage, VictorApplication.STAGE_COMPLETE)

    def test_complete_requires_consent(self):
        response = self.client.post(URL, complete_payload(consent=False), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('consent', response.json())
        self.assertEqual(VictorApplication.objects.count(), 0)

    def test_complete_requires_role_startup_stage_and_idea(self):
        response = self.client.post(URL, complete_payload(role='', startup_stage='', idea=''), format='json')
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertIn('role', body)
        self.assertIn('startup_stage', body)
        self.assertIn('idea', body)
        self.assertEqual(VictorApplication.objects.count(), 0)

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
        response = self.client.post(URL, lead_payload(first_name='Jordan-Updated'), format='json')
        self.assertEqual(response.status_code, 200)
        application = VictorApplication.objects.get(client_ref='lead_abc123')
        self.assertEqual(application.stage, VictorApplication.STAGE_COMPLETE)
        self.assertEqual(application.first_name, 'Jordan-Updated')
        self.assertEqual(application.idea, 'An AI copilot for grant applications.')
        self.assertTrue(application.consent)

    def test_get_not_allowed(self):
        self.client.post(URL, complete_payload(), format='json')
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 405)
