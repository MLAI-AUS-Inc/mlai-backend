from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Hackathon
from .models import (
    GenericHackathonAnnouncement,
    GenericHackathonResource,
    GenericHackathonSubmission,
    GenericHackathonTeam,
)


User = get_user_model()


class GenericHackathonApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.hackathon, _ = Hackathon.objects.update_or_create(
            slug='watt-the-hack',
            defaults={
                'name': 'Watt The Hack',
                'description': 'Energy hackathon',
                'start_date': '2026-06-01',
                'end_date': '2026-12-31',
            },
        )
        self.user = User.objects.create_user(
            email='watt-user@example.com',
            first_name='Watt',
            last_name='User',
        )
        self.client.force_authenticate(user=self.user)

    def test_create_team_joins_user_and_sets_code(self):
        response = self.client.post(
            '/api/v1/hackathons/watt-the-hack/app/teams/',
            {'team_name': 'Grid Builders'},
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data['created'])
        self.assertEqual(response.data['team']['team_name'], 'Grid Builders')
        self.assertEqual(response.data['team']['code'], 'TEAM1')
        self.assertTrue(GenericHackathonTeam.objects.get(team_name='Grid Builders').members.filter(id=self.user.id).exists())

    def test_join_while_on_a_team_is_blocked(self):
        old_team = GenericHackathonTeam.objects.create(hackathon=self.hackathon, team_name='Old Team')
        new_team = GenericHackathonTeam.objects.create(hackathon=self.hackathon, team_name='New Team')
        old_team.members.add(self.user)

        response = self.client.post(
            '/api/v1/hackathons/watt-the-hack/app/teams/join/',
            {'code': new_team.code},
            format='json',
        )

        # Phase 3: joining is a request, and you must leave your current team first.
        self.assertEqual(response.status_code, 409)
        self.assertTrue(old_team.members.filter(id=self.user.id).exists())
        self.assertFalse(new_team.members.filter(id=self.user.id).exists())

    def test_join_team_creates_pending_request(self):
        team = GenericHackathonTeam.objects.create(hackathon=self.hackathon, team_name='Name Team')

        response = self.client.post(
            '/api/v1/hackathons/watt-the-hack/app/teams/join/',
            {'code': 'Name Team'},
            format='json',
        )

        # Phase 3: a teamless user's join is a pending request, not instant membership.
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data['pending'])
        self.assertEqual(response.data['team_name'], 'Name Team')
        self.assertFalse(team.members.filter(id=self.user.id).exists())

    def test_join_team_enforces_max_six_members(self):
        team = GenericHackathonTeam.objects.create(hackathon=self.hackathon, team_name='Full Team')
        for i in range(6):
            teammate = User.objects.create_user(email=f'full{i}@example.com')
            team.members.add(teammate)

        response = self.client.post(
            '/api/v1/hackathons/watt-the-hack/app/teams/join/',
            {'code': team.code},
            format='json',
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['max_members'], 6)
        self.assertFalse(team.members.filter(id=self.user.id).exists())

    def test_submission_requires_team(self):
        response = self.client.post(
            '/api/v1/hackathons/watt-the-hack/app/submissions/',
            {'title': 'Smart Grid', 'summary': 'A useful project'},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('team', response.data['error'].lower())

    def test_create_project_submission_and_list_team_submissions(self):
        team = GenericHackathonTeam.objects.create(hackathon=self.hackathon, team_name='Submitters')
        team.members.add(self.user)

        create_response = self.client.post(
            '/api/v1/hackathons/watt-the-hack/app/submissions/',
            {
                'title': 'Smart Grid',
                'summary': 'A useful project',
                'repository_url': 'https://github.com/example/smart-grid',
                'demo_url': 'https://example.com/demo',
            },
            format='json',
        )
        list_response = self.client.get('/api/v1/hackathons/watt-the-hack/app/submissions/')

        self.assertEqual(create_response.status_code, 201)
        self.assertEqual(create_response.data['title'], 'Smart Grid')
        self.assertEqual(create_response.data['team']['team_name'], 'Submitters')
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.data), 1)
        self.assertEqual(list_response.data[0]['title'], 'Smart Grid')

    def test_submission_rejects_unsupported_attachment_type(self):
        team = GenericHackathonTeam.objects.create(hackathon=self.hackathon, team_name='File Team')
        team.members.add(self.user)
        upload = SimpleUploadedFile('malware.exe', b'nope', content_type='application/x-msdownload')

        response = self.client.post(
            '/api/v1/hackathons/watt-the-hack/app/submissions/',
            {'title': 'Attached', 'summary': 'Has a file', 'attachment': upload},
            format='multipart',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('Attachment type', response.data['error'])

    @patch('generic_hackathons.views._upload_attachment', return_value='https://files.example/submission.pdf')
    def test_submission_accepts_supported_attachment(self, mock_upload):
        team = GenericHackathonTeam.objects.create(hackathon=self.hackathon, team_name='Upload Team')
        team.members.add(self.user)
        upload = SimpleUploadedFile('deck.pdf', b'%PDF', content_type='application/pdf')

        response = self.client.post(
            '/api/v1/hackathons/watt-the-hack/app/submissions/',
            {'title': 'Attached', 'summary': 'Has a file', 'attachment': upload},
            format='multipart',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['attachment_url'], 'https://files.example/submission.pdf')
        self.assertEqual(response.data['attachment_name'], 'deck.pdf')
        mock_upload.assert_called_once()

    def test_resources_and_announcements_are_authenticated_lists(self):
        GenericHackathonAnnouncement.objects.create(
            hackathon=self.hackathon,
            title='Kickoff',
            body='Welcome',
            author=self.user,
        )
        GenericHackathonResource.objects.create(
            hackathon=self.hackathon,
            title='Guide',
            summary='Read this',
            order=1,
        )

        announcement_response = self.client.get('/api/v1/hackathons/watt-the-hack/app/announcements/')
        resource_response = self.client.get('/api/v1/hackathons/watt-the-hack/app/resources/')

        self.assertEqual(announcement_response.status_code, 200)
        self.assertEqual(announcement_response.data[0]['title'], 'Kickoff')
        self.assertEqual(resource_response.status_code, 200)
        self.assertEqual(resource_response.data[0]['title'], 'Guide')

    def test_current_user_has_team_includes_generic_hackathon_membership(self):
        team = GenericHackathonTeam.objects.create(hackathon=self.hackathon, team_name='Compat Team')
        team.members.add(self.user)

        response = self.client.get('/api/v1/auth/me/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['id'], self.user.id)
        self.assertTrue(response.data['has_team'])
