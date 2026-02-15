from django.contrib.auth import get_user_model
from django.test import TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import JsonResponse
from rest_framework.test import APIClient
from unittest.mock import patch
import base64

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

    def _make_png_upload(self, name='team.png'):
        # 1x1 PNG
        raw = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO8B9V8AAAAASUVORK5CYII='
        )
        return SimpleUploadedFile(name, raw, content_type='image/png')

    def test_update_profile_basic_details(self):
        response = self.client.patch(
            '/api/v1/auth/update-profile/',
            {
                'first_name': 'Alex',
                'last_name': 'Morgan',
                'phone': '0412345678',
                'about': 'Building triage tools',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Alex')
        self.assertEqual(self.user.last_name, 'Morgan')
        self.assertEqual(self.user.phone, '0412345678')
        self.assertEqual(self.user.about, 'Building triage tools')

    def test_update_profile_personas_accepts_healer(self):
        response = self.client.patch(
            '/api/v1/auth/update-profile/',
            {'personas': ['hacker', 'healer']},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.personas, ['hacker', 'healer'])

    def test_update_profile_personas_rejects_invalid(self):
        response = self.client.patch(
            '/api/v1/auth/update-profile/',
            {'personas': ['hacker', 'wizard']},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('allowed_personas', response.data)

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
        self.assertCountEqual(response.data['team_names'], ['Trauma Team', 'ICU Avengers'])

    def test_shared_team_names_endpoint_returns_all_by_default(self):
        HospitalTeam.objects.create(team_name='Trauma Team')
        EsafetyTeam.objects.create(team_name='Safety Team')

        response = self.client.get('/api/v1/teams/')

        self.assertEqual(response.status_code, 200)
        self.assertIn('Trauma Team', response.data['team_names'])
        self.assertIn('Safety Team', response.data['team_names'])

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

    def test_hospital_join_team_accepts_team_name_as_code(self):
        team = HospitalTeam.objects.create(team_name='Name Join Team')

        response = self.client.post(
            '/api/v1/hackathons/hospital/teams/join/',
            {'code': 'Name Join Team'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['team_id'], team.team_id)
        self.assertTrue(team.members.filter(id=self.user.id).exists())

    def test_hospital_join_team_enforces_max_six_members(self):
        team = HospitalTeam.objects.create(team_name='Full Team')
        for i in range(6):
            teammate = User.objects.create_user(email=f'full{i}@example.com', password='password')
            team.members.add(teammate)

        response = self.client.post(
            '/api/v1/hackathons/hospital/teams/join/',
            {'team_id': team.team_id},
            format='json',
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['max_members'], 6)
        self.assertFalse(team.members.filter(id=self.user.id).exists())

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
        self.assertEqual(response.data[0]['name'], 'Mine')
        self.assertEqual(response.data[0]['members'][0]['email'], self.user.email)

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

    def test_hospital_submissions_post_requires_min_two_members(self):
        team = HospitalTeam.objects.create(team_name='Solo Team')
        team.members.add(self.user)

        response = self.client.post('/api/v1/hackathons/hospital/submissions/', {}, format='multipart')

        self.assertEqual(response.status_code, 400)
        self.assertIn('Team size must be between', response.json().get('detail', ''))

    @patch('hospital.views.submit_predictions')
    def test_hospital_submissions_post_returns_contract_shape(self, mock_submit):
        mock_submit.return_value = JsonResponse(
            {'score': 0.95, 'submitted_at': '2026-02-15T12:00:00Z', 'accuracy': 0.8},
            status=200,
        )

        response = self.client.post('/api/v1/hackathons/hospital/submissions/', {}, format='multipart')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'score': 0.95, 'submitted_at': '2026-02-15T12:00:00Z'})

    @patch('hospital.views.submit_predictions')
    def test_hospital_submissions_post_error_returns_detail(self, mock_submit):
        mock_submit.return_value = JsonResponse({'error': 'CSV invalid'}, status=400)

        response = self.client.post('/api/v1/hackathons/hospital/submissions/', {}, format='multipart')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'detail': 'CSV invalid'})

    @patch('core.firebase_utils.upload_file_to_storage', return_value='https://cdn.example.com/team-avatar.png')
    def test_update_profile_can_upload_hospital_team_avatar(self, mock_upload):
        self.client.patch(
            '/api/v1/auth/update-profile/',
            {'team': 'Avatar Team', 'app': 'hospital'},
            format='json',
        )

        avatar = self._make_png_upload()
        response = self.client.patch(
            '/api/v1/auth/update-profile/',
            {'app': 'hospital', 'team_avatar': avatar},
            format='multipart',
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        hospital_team = self.user.hospital_teams.first()
        self.assertIsNotNone(hospital_team)
        self.assertEqual(hospital_team.avatar_url, 'https://cdn.example.com/team-avatar.png')
        self.assertEqual(response.data['hospital_team']['avatar_url'], 'https://cdn.example.com/team-avatar.png')
        mock_upload.assert_called()

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
        self.assertGreater(response.data[0]['score'], response.data[1]['score'])
        self.assertIn('team_id', response.data[0])
        self.assertIn('team_name', response.data[0])
        self.assertIn('submitted_at', response.data[0])

    def test_hackathon_listing_includes_hospital_slug(self):
        response = self.client.get('/api/v1/hackathons/')

        self.assertEqual(response.status_code, 200)
        slugs = [item['slug'] for item in response.data]
        self.assertIn('hospital', slugs)
        self.assertTrue(Hackathon.objects.filter(slug='hospital').exists())

    def test_hospital_hackathon_detail_matches_expected_name(self):
        response = self.client.get('/api/v1/hackathons/hospital/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['slug'], 'hospital')
        self.assertEqual(response.data['name'], 'Medhack: Frontiers')
