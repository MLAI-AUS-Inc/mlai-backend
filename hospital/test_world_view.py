import json

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import Submission, Team

User = get_user_model()

WORLD_URL = "/api/v1/hackathons/hospital/world/"


class WorldStateViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="world-test@example.com",
            first_name="World",
            last_name="Tester",
        )
        self.team_a = Team.objects.create(team_id=1, team_name="Alpha")
        self.team_b = Team.objects.create(team_id=2, team_name="Beta")
        self.team_a.members.add(self.user)

    def _submit(self, team, score, accuracy=0.9, submitted_at=None):
        sub = Submission.objects.create(
            user=self.user,
            team=team,
            participant_name="World Tester",
            score=score,
            accuracy=accuracy,
            # Every real submission carries a multi-KB scoring blob. The
            # world payload must never depend on it (2026-07-13 meltdown:
            # materialising feedback for the whole table on every poll).
            feedback={
                "confusion_matrix": [[123] * 4] * 4,
                "per_class": {
                    str(i): {"precision": 0.5, "recall": 0.5} for i in range(4)
                },
                "row_details": [{"row": i, "ok": bool(i % 2)} for i in range(100)],
            },
        )
        if submitted_at is not None:
            Submission.objects.filter(pk=sub.pk).update(submitted_at=submitted_at)
            sub.refresh_from_db()
        return sub

    def test_anonymous_get_returns_world_state(self):
        self._submit(self.team_a, 0.5)
        response = self.client.get(WORLD_URL)
        self.assertEqual(response.status_code, 200)
        self.assertIn("updated_at", response.data)
        self.assertEqual(response.data["world"], {"radius": 30})
        self.assertTrue(response.data["entities"])

    def test_teams_ranked_by_best_score(self):
        self._submit(self.team_a, 0.4)
        self._submit(self.team_a, 0.9)  # best for Alpha
        self._submit(self.team_b, 0.6)

        response = self.client.get(WORLD_URL)
        cubes = [e for e in response.data["entities"] if e["kind"] == "cube"]
        self.assertEqual(len(cubes), 2)

        by_id = {e["id"]: e for e in cubes}
        alpha = by_id["team-1"]
        beta = by_id["team-2"]
        self.assertEqual(alpha["meta"]["rank"], 1)
        self.assertEqual(alpha["label"], "#1 Alpha")
        self.assertEqual(beta["meta"]["rank"], 2)
        # Sizes span 1..3 across the score range.
        self.assertEqual(alpha["size"], 3.0)
        self.assertEqual(beta["size"], 1.0)
        # Rank 1 sits near the north pole.
        self.assertEqual(alpha["lat"], 80.0)

    def test_team_without_submissions_still_appears(self):
        self._submit(self.team_a, 0.7)
        response = self.client.get(WORLD_URL)
        cubes = {e["id"]: e for e in response.data["entities"] if e["kind"] == "cube"}
        self.assertIn("team-2", cubes)
        self.assertIsNone(cubes["team-2"]["meta"]["score"])
        self.assertEqual(cubes["team-2"]["label"], "Beta")

    def test_recent_submissions_become_spheres(self):
        sub = self._submit(self.team_a, 0.7, accuracy=0.85)
        response = self.client.get(WORLD_URL)
        spheres = [e for e in response.data["entities"] if e["kind"] == "sphere"]
        self.assertEqual(len(spheres), 1)
        sphere = spheres[0]
        self.assertEqual(sphere["id"], "sub-%s" % sub.id)
        self.assertEqual(sphere["altitude"], 8)
        self.assertTrue(sphere["spin"])
        self.assertEqual(sphere["meta"]["team"], "Alpha")
        self.assertEqual(sphere["meta"]["accuracy"], 0.85)

    def test_old_submissions_do_not_become_spheres(self):
        sub = self._submit(self.team_a, 0.7)
        Submission.objects.filter(pk=sub.pk).update(
            submitted_at=timezone.now() - timezone.timedelta(minutes=20)
        )
        cache.clear()
        response = self.client.get(WORLD_URL)
        spheres = [e for e in response.data["entities"] if e["kind"] == "sphere"]
        self.assertEqual(spheres, [])
        # ...but the team cube (fed by best score, not the window) remains.
        cubes = [e for e in response.data["entities"] if e["kind"] == "cube"]
        self.assertTrue(any(e["id"] == "team-1" for e in cubes))

    def test_no_member_data_leaks(self):
        self._submit(self.team_a, 0.7)
        response = self.client.get(WORLD_URL)
        blob = json.dumps(response.data)
        self.assertNotIn("@", blob)
        self.assertNotIn("World Tester", blob)
        self.assertNotIn("world-test", blob)

    def test_payload_is_cached_briefly(self):
        self._submit(self.team_a, 0.7)
        first = self.client.get(WORLD_URL)
        self._submit(self.team_b, 0.95)  # would change ranks...
        second = self.client.get(WORLD_URL)
        # ...but within the cache window the payload is identical.
        self.assertEqual(first.data, second.data)
        cache.clear()
        third = self.client.get(WORLD_URL)
        self.assertNotEqual(first.data, third.data)

    def test_tie_break_prefers_the_earliest_best_submission(self):
        now = timezone.now()
        self._submit(self.team_b, 0.9, submitted_at=now)
        self._submit(
            self.team_a, 0.9, submitted_at=now - timezone.timedelta(minutes=5)
        )
        cache.clear()
        response = self.client.get(WORLD_URL)
        cubes = {e["id"]: e for e in response.data["entities"] if e["kind"] == "cube"}
        # Equal best scores: the team that got there first outranks.
        self.assertEqual(cubes["team-1"]["meta"]["rank"], 1)
        self.assertEqual(cubes["team-2"]["meta"]["rank"], 2)

    def test_build_query_count_is_constant_and_light(self):
        # REGRESSION (2026-07-13): the build previously materialised every
        # Submission row — feedback blob included — per cache miss. The
        # rewrite scans narrow values() rows in exactly three queries; any
        # per-row attribute access would reintroduce N+1 and fail this.
        for i in range(1, 7):
            self._submit(self.team_a, 0.1 * i)
            self._submit(self.team_b, 0.05 * i)
        cache.clear()
        with self.assertNumQueries(3):
            response = self.client.get(WORLD_URL)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["entities"])
