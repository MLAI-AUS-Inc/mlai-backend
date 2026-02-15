from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Hackathon
from esafety.models import Team as EsafetyTeam
from hospital.models import Team as HospitalTeam, Submission


User = get_user_model()


class MedHackOnboardingFlowTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email='participant@example.com',
            password='password',
            first_name='Pat',
            last_name='User',
            role='participant',
        )
        self.client.force_authenticate(user=self.user)

    def test_update_profile_assigns_hospital_team_when_app_is_hospital(self):
        response = self.client.patch(
            '/api/v1/auth/update-profile/',
            {'team': 'Code Blue', 'app': 'hospital'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()

        self.assertTrue(self.user.has_team)
        self.assertEqual(self.user.hospital_teams.count(), 1)
        self.assertEqual(self.user.hospital_teams.first().team_name, 'Code Blue')
        self.assertEqual(self.user.esafety_teams.count(), 0)

        self.assertEqual(response.data['hospital_team']['team_name'], 'Code Blue')
        self.assertEqual(response.data['team']['team_name'], 'Code Blue')

    def test_update_profile_defaults_to_esafety_for_legacy_team_field(self):
        response = self.client.patch(
            '/api/v1/auth/update-profile/',
            {'team': 'Safety Squad'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()

        self.assertTrue(self.user.has_team)
        self.assertEqual(self.user.esafety_teams.count(), 1)
        self.assertEqual(self.user.esafety_teams.first().team_name, 'Safety Squad')
        self.assertEqual(self.user.hospital_teams.count(), 0)

    def test_shared_team_names_endpoint_supports_hospital_context(self):
        HospitalTeam.objects.create(team_name='Trauma Team')
        HospitalTeam.objects.create(team_name='ICU Avengers')
        EsafetyTeam.objects.create(team_name='Ignore Me')

        response = self.client.get('/api/v1/teams/?app=hospital')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['app'], 'hospital')
        self.assertCountEqual(response.data['team_names'], ['Trauma Team', 'ICU Avengers'])

    def test_hospital_join_team_endpoint_switches_existing_team(self):
        team_one = HospitalTeam.objects.create(team_name='Initial Team')
        team_two = HospitalTeam.objects.create(team_name='Target Team')
        team_one.members.add(self.user)

        response = self.client.post(
            '/api/v1/hackathons/hospital/teams/join/',
            {'team_id': team_two.team_id},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()

        self.assertFalse(team_one.members.filter(id=self.user.id).exists())
        self.assertTrue(team_two.members.filter(id=self.user.id).exists())
        self.assertEqual(response.data['team_name'], 'Target Team')

    def test_hospital_join_team_accepts_code(self):
        team = HospitalTeam.objects.create(team_name='Code Team')

        response = self.client.post(
            '/api/v1/hackathons/hospital/teams/join/',
            {'code': f'TEAM{team.team_id}'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['team_id'], team.team_id)
        self.assertEqual(response.data['code'], f'TEAM{team.team_id}')
        self.assertTrue(team.members.filter(id=self.user.id).exists())

    def test_hospital_teams_post_creates_and_joins(self):
        response = self.client.post(
            '/api/v1/hackathons/hospital/teams/',
            {'team_name': 'New Team'},
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data['created'])
        self.assertEqual(response.data['team']['team_name'], 'New Team')
        self.assertEqual(self.user.hospital_teams.count(), 1)

    def test_hospital_teams_get_supports_member_filter(self):
        other_user = User.objects.create_user(email='other@example.com', password='password')
        my_team = HospitalTeam.objects.create(team_name='Mine')
        their_team = HospitalTeam.objects.create(team_name='Theirs')
        my_team.members.add(self.user)
        their_team.members.add(other_user)

        response = self.client.get(f'/api/v1/hackathons/hospital/teams/?member_id={self.user.id}')

        self.assertEqual(response.status_code, 200)
        returned_ids = [team['team_id'] for team in response.data]
        self.assertIn(my_team.team_id, returned_ids)
        self.assertNotIn(their_team.team_id, returned_ids)

    def test_hospital_submissions_endpoint_get_lists_user_submissions(self):
        team = HospitalTeam.objects.create(team_name='Submission Team')
        team.members.add(self.user)
        Submission.objects.create(
            user=self.user,
            team=team,
            participant_name='Pat User',
            score=12.5,
            accuracy=0.82,
        )

        response = self.client.get('/api/v1/hackathons/hospital/submissions/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['team']['team_name'], 'Submission Team')

    def test_hospital_leaderboard_endpoint_returns_ranked_results(self):
        team_a = HospitalTeam.objects.create(team_name='A Team')
        team_b = HospitalTeam.objects.create(team_name='B Team')
        team_a.members.add(self.user)
        other_user = User.objects.create_user(email='ranker@example.com', password='password')
        team_b.members.add(other_user)

        Submission.objects.create(
            user=self.user,
            team=team_a,
            participant_name='Pat User',
            score=20.0,
            accuracy=0.9,
        )
        Submission.objects.create(
            user=other_user,
            team=team_b,
            participant_name='Other User',
            score=10.0,
            accuracy=0.8,
        )

        response = self.client.get('/api/v1/hackathons/hospital/leaderboard/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]['rank'], 1)
        self.assertGreater(response.data[0]['score'], response.data[1]['score'])

    def test_hackathon_listing_includes_hospital_slug(self):
        response = self.client.get('/api/v1/hackathons/')

        self.assertEqual(response.status_code, 200)
        slugs = [item['slug'] for item in response.data]
        self.assertIn('hospital', slugs)
        self.assertTrue(Hackathon.objects.filter(slug='hospital').exists())
