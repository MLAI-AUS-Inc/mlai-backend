from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.user_compat import user_has_team
from hospital.models import Announcement, HospitalCompetitionRound, Submission, Team
from hospital.world_views import WORLD_CACHE_KEY


User = get_user_model()


class HospitalRoundArchiveTests(TestCase):
    def setUp(self):
        cache.clear()
        self.active_round = HospitalCompetitionRound.get_active()
        self.initial_round_count = HospitalCompetitionRound.objects.count()
        self.user = User.objects.create_user(
            email='legacy-healthhack@example.com',
            password='password',
            first_name='Legacy',
            last_name='Participant',
        )
        self.team = Team.objects.create(team_id=1, team_name='Legacy Team')
        self.team.members.add(self.user)
        self.submission = Submission.objects.create(
            user=self.user,
            team=self.team,
            participant_name='Legacy Participant',
            score=42,
            accuracy=0.8,
        )
        self.announcement = Announcement.objects.create(
            title='Legacy announcement',
            body='This belongs to the previous event.',
            author=self.user,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _archive(self, **overrides):
        options = {
            'execute': True,
            'new_slug': 'healthhack-test',
            'new_name': 'HealthHack Test',
            'expected_teams': 1,
            'expected_submissions': 1,
            'expected_announcements': 1,
            'archived_by_email': self.user.email,
        }
        options.update(overrides)
        stdout = StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command('archive_hospital_round', stdout=stdout, **options)
        return stdout.getvalue()

    def test_dry_run_reports_counts_without_changing_rounds(self):
        stdout = StringIO()

        call_command(
            'archive_hospital_round',
            new_slug='healthhack-test',
            new_name='HealthHack Test',
            stdout=stdout,
        )

        self.active_round.refresh_from_db()
        self.assertEqual(
            self.active_round.status,
            HospitalCompetitionRound.STATUS_ACTIVE,
        )
        self.assertFalse(
            HospitalCompetitionRound.objects.filter(slug='healthhack-test').exists()
        )
        self.assertIn('Teams to archive: 1', stdout.getvalue())
        self.assertIn('Submissions to archive: 1', stdout.getvalue())
        self.assertIn('Announcements to archive: 1', stdout.getvalue())
        self.assertIn('Dry run only', stdout.getvalue())

    def test_execute_archives_history_and_opens_an_empty_round(self):
        cache.set(WORLD_CACHE_KEY, {'stale': True}, timeout=60)

        stdout = self._archive()

        self.active_round.refresh_from_db()
        self.team.refresh_from_db()
        self.submission.refresh_from_db()
        self.announcement.refresh_from_db()
        new_round = HospitalCompetitionRound.get_active()

        self.assertEqual(
            self.active_round.status,
            HospitalCompetitionRound.STATUS_ARCHIVED,
        )
        self.assertIsNotNone(self.active_round.archived_at)
        self.assertEqual(self.active_round.archived_by, self.user)
        self.assertEqual(new_round.slug, 'healthhack-test')
        self.assertEqual(self.team.round, self.active_round)
        self.assertEqual(self.submission.round, self.active_round)
        self.assertEqual(self.announcement.round, self.active_round)
        self.assertTrue(self.team.members.filter(pk=self.user.pk).exists())
        self.assertIsNone(cache.get(WORLD_CACHE_KEY))
        self.assertIn("opened 'healthhack-test'", stdout)

        teams_response = self.client.get('/api/v1/hackathons/hospital/teams/')
        submissions_response = self.client.get(
            '/api/v1/hackathons/hospital/submissions/'
        )
        direct_submission_response = self.client.get(
            f'/api/v1/hackathons/hospital/get_submission/{self.submission.pk}/'
        )
        recent_submissions_response = self.client.get(
            '/api/v1/hackathons/hospital/get_recent_submissions/'
        )
        join_archived_team_response = self.client.post(
            '/api/v1/hackathons/hospital/teams/join/',
            {'team_id': self.team.team_id},
            format='json',
        )
        current_user_response = self.client.get('/api/v1/auth/me/')
        world_response = self.client.get('/api/v1/hackathons/hospital/world/')
        announcements_response = self.client.get(
            '/api/v1/hackathons/hospital/announcements/'
        )

        leaderboard_user = User.objects.create_user(
            email='hi@mlai.au',
            password='password',
        )
        leaderboard_client = APIClient()
        leaderboard_client.force_authenticate(user=leaderboard_user)
        leaderboard_response = leaderboard_client.get(
            '/api/v1/hackathons/hospital/leaderboard/'
        )

        self.assertEqual(teams_response.status_code, 200)
        self.assertEqual(teams_response.data, [])
        self.assertEqual(submissions_response.status_code, 200)
        self.assertEqual(submissions_response.data, [])
        self.assertEqual(direct_submission_response.status_code, 404)
        self.assertEqual(recent_submissions_response.status_code, 400)
        self.assertEqual(join_archived_team_response.status_code, 404)
        self.assertEqual(current_user_response.status_code, 200)
        self.assertFalse(current_user_response.data['has_team'])
        self.assertIsNone(current_user_response.data['hospital_team'])
        self.assertFalse(user_has_team(self.user))
        self.assertEqual(world_response.status_code, 200)
        self.assertEqual(world_response.data['entities'], [])
        self.assertEqual(announcements_response.status_code, 200)
        self.assertEqual(announcements_response.data, [])
        self.assertEqual(leaderboard_response.status_code, 200)
        self.assertEqual(leaderboard_response.data, [])

        replacement_response = self.client.post(
            '/api/v1/hackathons/hospital/teams/',
            {'team_name': 'Legacy Team'},
            format='json',
        )
        self.assertEqual(replacement_response.status_code, 201)
        replacement = Team.objects.get(round=new_round)
        self.assertEqual(replacement.team_id, self.team.team_id)
        self.assertEqual(replacement.round, new_round)
        self.assertTrue(self.team.members.filter(pk=self.user.pk).exists())

    def test_execute_refuses_stale_expected_counts(self):
        with self.assertRaisesMessage(CommandError, 'Expected 2 teams but found 1'):
            self._archive(expected_teams=2)

        self.active_round.refresh_from_db()
        self.assertEqual(
            self.active_round.status,
            HospitalCompetitionRound.STATUS_ACTIVE,
        )
        self.assertFalse(
            HospitalCompetitionRound.objects.filter(slug='healthhack-test').exists()
        )

    def test_execute_requires_all_expected_counts(self):
        with self.assertRaisesMessage(
            CommandError,
            '--expected-teams, --expected-submissions, and '
            '--expected-announcements are required',
        ):
            call_command(
                'archive_hospital_round',
                execute=True,
                new_slug='healthhack-test',
                expected_teams=1,
                stdout=StringIO(),
            )

    def test_execute_refuses_stale_expected_announcement_count(self):
        with self.assertRaisesMessage(
            CommandError,
            'Expected 2 announcements but found 1',
        ):
            self._archive(expected_announcements=2)

        self.active_round.refresh_from_db()
        self.assertEqual(
            self.active_round.status,
            HospitalCompetitionRound.STATUS_ACTIVE,
        )

    def test_rerunning_for_the_same_active_round_is_idempotent(self):
        self._archive()
        stdout = StringIO()

        call_command(
            'archive_hospital_round',
            execute=True,
            new_slug='healthhack-test',
            new_name='HealthHack Test',
            stdout=stdout,
        )

        self.assertEqual(
            HospitalCompetitionRound.objects.count(),
            self.initial_round_count + 1,
        )
        self.assertIn('already active; nothing to do', stdout.getvalue())

    def test_only_one_round_can_be_active(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            HospitalCompetitionRound.objects.create(
                slug='another-active-round',
                name='Another active round',
            )

    def test_submission_cannot_reference_a_team_from_another_round(self):
        archived_round = HospitalCompetitionRound.objects.create(
            slug='empty-archived-round',
            name='Empty archived round',
            status=HospitalCompetitionRound.STATUS_ARCHIVED,
        )

        with self.assertRaisesMessage(ValueError, 'must match its team round'):
            Submission.objects.create(
                round=archived_round,
                user=self.user,
                team=self.team,
                participant_name='Mismatched Participant',
                score=1,
                accuracy=0.1,
            )


class HospitalRoundSlackArchiveTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='slack-viewer@example.com',
            password='password',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.active_round = HospitalCompetitionRound.get_active()
        self.active_round.opened_at = timezone.now()
        self.active_round.save(update_fields=['opened_at'])

    def test_channel_feed_excludes_messages_before_active_round(self):
        cutoff = f'{self.active_round.opened_at.timestamp():.6f}'
        old_timestamp = f'{self.active_round.opened_at.timestamp() - 60:.6f}'
        current_timestamp = f'{self.active_round.opened_at.timestamp() + 60:.6f}'
        history = {
            'messages': [
                {'ts': old_timestamp, 'text': 'Old event message', 'user': 'UOLD'},
                {'ts': current_timestamp, 'text': 'Current event message', 'user': 'UNEW'},
            ],
            'next_cursor': None,
        }

        with (
            patch(
                'integrations.services.slack.SlackService.get_channel_id_by_name',
                return_value='CHEALTHHACK',
            ),
            patch(
                'integrations.services.slack.SlackService.get_channel_history',
                return_value=history,
            ) as get_history,
            patch(
                'integrations.services.slack.SlackService.get_user_profile',
                return_value={'name': 'Participant'},
            ),
        ):
            response = self.client.get(
                '/api/v1/hackathons/hospital/channel/?limit=20'
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [message['text'] for message in response.data['messages']],
            ['Current event message'],
        )
        get_history.assert_called_once_with(
            'CHEALTHHACK',
            limit=20,
            cursor=None,
            oldest=cutoff,
        )

    def test_old_round_threads_are_not_exposed(self):
        old_timestamp = f'{self.active_round.opened_at.timestamp() - 60:.6f}'

        response = self.client.get(
            f'/api/v1/hackathons/hospital/channel/thread/{old_timestamp}/'
        )

        self.assertEqual(response.status_code, 404)
