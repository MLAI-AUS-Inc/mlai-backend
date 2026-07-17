from io import StringIO

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from core.user_compat import user_has_team
from hospital.models import HospitalCompetitionRound, Submission, Team
from hospital.world_views import WORLD_CACHE_KEY


User = get_user_model()


class HospitalRoundArchiveTests(TestCase):
    def setUp(self):
        cache.clear()
        self.active_round = HospitalCompetitionRound.get_active()
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
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _archive(self, **overrides):
        options = {
            'execute': True,
            'new_slug': 'healthhack-test',
            'new_name': 'HealthHack Test',
            'expected_teams': 1,
            'expected_submissions': 1,
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
        self.assertIn('Dry run only', stdout.getvalue())

    def test_execute_archives_history_and_opens_an_empty_round(self):
        cache.set(WORLD_CACHE_KEY, {'stale': True}, timeout=60)

        stdout = self._archive()

        self.active_round.refresh_from_db()
        self.team.refresh_from_db()
        self.submission.refresh_from_db()
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

    def test_execute_requires_both_expected_counts(self):
        with self.assertRaisesMessage(
            CommandError,
            '--expected-teams and --expected-submissions are required',
        ):
            call_command(
                'archive_hospital_round',
                execute=True,
                new_slug='healthhack-test',
                expected_teams=1,
                stdout=StringIO(),
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

        self.assertEqual(HospitalCompetitionRound.objects.count(), 2)
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
