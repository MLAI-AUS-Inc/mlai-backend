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
        role='CEO',
        startup_stage='Prototype / MVP',
        industry_sector='Software & Enterprise',
        location='Melbourne, AU',
        team_size=2,
        team_members=[
            {'first_name': 'Alex', 'last_name': 'Chen', 'email': 'alex@example.com', 'role': 'CTO'},
        ],
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
        self.assertEqual(application.role, 'CEO')
        self.assertEqual(application.startup_stage, 'Prototype / MVP')
        self.assertEqual(application.team_size, 2)
        self.assertEqual(
            application.team_members,
            [{'first_name': 'Alex', 'last_name': 'Chen', 'email': 'alex@example.com', 'role': 'CTO'}],
        )
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

    def test_complete_requires_core_fields(self):
        response = self.client.post(
            URL,
            complete_payload(team_name='', role='', startup_stage='', industry_sector='', idea=''),
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertIn('team_name', body)
        self.assertIn('role', body)
        self.assertIn('startup_stage', body)
        self.assertIn('industry_sector', body)
        self.assertIn('idea', body)
        self.assertEqual(VictorApplication.objects.count(), 0)

    def test_complete_requires_team_size(self):
        payload = complete_payload()
        payload.pop('team_size')
        response = self.client.post(URL, payload, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('team_size', response.json())

    def test_team_members_must_match_team_size(self):
        response = self.client.post(URL, complete_payload(team_size=3), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('team_members', response.json())

    def test_solo_founder_needs_no_members(self):
        response = self.client.post(
            URL, complete_payload(team_size=1, team_members=[]), format='json'
        )
        self.assertEqual(response.status_code, 201)
        application = VictorApplication.objects.get(client_ref='lead_abc123')
        self.assertEqual(application.team_size, 1)
        self.assertEqual(application.team_members, [])

    def test_team_member_email_validated(self):
        members = [{'first_name': 'Alex', 'last_name': 'Chen', 'email': 'not-an-email', 'role': 'CTO'}]
        response = self.client.post(URL, complete_payload(team_members=members), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('team_members', response.json())

    def test_team_member_role_required(self):
        members = [{'first_name': 'Alex', 'last_name': 'Chen', 'email': 'alex@example.com'}]
        response = self.client.post(URL, complete_payload(team_members=members), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('team_members', response.json())

    def test_idea_capped_at_240_chars(self):
        response = self.client.post(URL, complete_payload(idea='x' * 241), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('idea', response.json())

    def test_paying_stage_requires_revenue(self):
        response = self.client.post(
            URL, complete_payload(startup_stage='We have paying users'), format='json'
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('revenue_last_3_months', response.json())

    def test_paying_stage_with_revenue_accepted(self):
        revenue = {'2026-05': 1200, '2026-06': 3400.5, '2026-07': 500}
        response = self.client.post(
            URL,
            complete_payload(
                startup_stage='We have paying users', revenue_last_3_months=revenue
            ),
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        application = VictorApplication.objects.get(client_ref='lead_abc123')
        self.assertEqual(application.revenue_last_3_months['2026-06'], 3400.5)

    def test_revenue_rejects_bad_month_keys_and_negatives(self):
        bad_key = {'May': 100, '2026-06': 200, '2026-07': 300}
        response = self.client.post(
            URL,
            complete_payload(startup_stage='Seed', revenue_last_3_months=bad_key),
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('revenue_last_3_months', response.json())

        negative = {'2026-05': -5, '2026-06': 200, '2026-07': 300}
        response = self.client.post(
            URL,
            complete_payload(startup_stage='Seed', revenue_last_3_months=negative),
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('revenue_last_3_months', response.json())

    def test_non_paying_stage_needs_no_revenue(self):
        response = self.client.post(
            URL, complete_payload(startup_stage='Idea stage'), format='json'
        )
        self.assertEqual(response.status_code, 201)

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
