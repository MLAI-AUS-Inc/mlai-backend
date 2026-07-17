from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import Submission, Team

User = get_user_model()

LEADERBOARD_URL = "/api/v1/hackathons/hospital/leaderboard/"


class LeaderboardViewTests(TestCase):
    """Covers the two-phase best-per-team rewrite (2026-07-13 meltdown fix):
    the ranking scan must stay light, while the rendered rows still read
    clinical metrics from each team's actual best submission's feedback."""

    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            email="hi@mlai.au",
            first_name="Leader",
            last_name="Board",
        )
        self.player = User.objects.create_user(
            email="lb-player@example.com",
            first_name="Player",
            last_name="One",
        )
        self.team_a = Team.objects.create(team_id=1, team_name="Alpha")
        self.team_b = Team.objects.create(team_id=2, team_name="Beta")

    def _submit(self, team, score, clinical_metrics, submitted_at=None):
        sub = Submission.objects.create(
            user=self.player,
            team=team,
            participant_name="Player One",
            score=score,
            accuracy=0.9,
            feedback={
                "clinical_metrics": clinical_metrics,
                "row_details": [{"row": i, "ok": bool(i % 2)} for i in range(100)],
            },
        )
        if submitted_at is not None:
            Submission.objects.filter(pk=sub.pk).update(submitted_at=submitted_at)
            sub.refresh_from_db()
        return sub

    def test_requires_authentication_and_allows_participants(self):
        self._submit(self.team_a, 0.5, {"patients_saved": 1})
        self.assertIn(self.client.get(LEADERBOARD_URL).status_code, (401, 403))
        self.client.force_authenticate(self.player)
        response = self.client.get(LEADERBOARD_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["team_name"], "Alpha")

    def test_rows_use_each_teams_best_submission(self):
        self._submit(self.team_a, 0.4, {"patients_saved": 4, "false_alarms": 40})
        best_a = self._submit(
            self.team_a, 0.9, {"patients_saved": 9, "false_alarms": 90}
        )
        self._submit(self.team_b, 0.6, {"patients_saved": 6, "false_alarms": 60})

        self.client.force_authenticate(self.admin)
        response = self.client.get(LEADERBOARD_URL)
        self.assertEqual(response.status_code, 200)

        rows = response.data
        self.assertEqual([row["team_name"] for row in rows], ["Alpha", "Beta"])
        self.assertEqual(rows[0]["score"], 0.9)
        self.assertEqual(rows[0]["submitted_at"], best_a.submitted_at.isoformat())
        # Clinical metrics come from the BEST row's feedback, not any other.
        self.assertEqual(rows[0]["patients_saved"], 9)
        self.assertEqual(rows[0]["false_alarms"], 90)
        self.assertEqual(rows[1]["patients_saved"], 6)

    def test_tied_teams_rank_newest_first(self):
        now = timezone.now()
        self._submit(
            self.team_a,
            0.7,
            {"patients_saved": 1},
            submitted_at=now - timezone.timedelta(minutes=5),
        )
        self._submit(self.team_b, 0.7, {"patients_saved": 2}, submitted_at=now)

        self.client.force_authenticate(self.admin)
        rows = self.client.get(LEADERBOARD_URL).data
        # Stable sort over the '-score, -submitted_at' scan order: on a tie
        # across teams, the team with the newer best submission lists first.
        self.assertEqual([row["team_name"] for row in rows], ["Beta", "Alpha"])

    def test_equal_scores_prefer_the_newest_submission(self):
        now = timezone.now()
        self._submit(
            self.team_a,
            0.7,
            {"patients_saved": 1},
            submitted_at=now - timezone.timedelta(minutes=5),
        )
        self._submit(self.team_a, 0.7, {"patients_saved": 2}, submitted_at=now)

        self.client.force_authenticate(self.admin)
        rows = self.client.get(LEADERBOARD_URL).data
        self.assertEqual(len(rows), 1)
        # '-score, -submitted_at': on a tie the newer submission represents
        # the team (pre-rewrite behaviour, preserved).
        self.assertEqual(rows[0]["patients_saved"], 2)
