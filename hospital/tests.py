from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Team


User = get_user_model()


class HospitalTeamCompatibilityTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="hospital-user@example.com",
            first_name="Hospital",
            last_name="User",
        )
        self.client.force_authenticate(self.user)

    def test_create_team_makes_derived_has_team_true(self):
        response = self.client.post(
            "/api/v1/hackathons/hospital/teams/",
            {"team_name": "Care Builders"},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["created"])
        self.assertTrue(Team.objects.filter(members=self.user).exists())

        auth_response = self.client.get("/api/v1/auth/me/")
        self.assertEqual(auth_response.status_code, 200)
        self.assertTrue(auth_response.data["has_team"])
        self.assertEqual(auth_response.data["team"]["members"][0]["role"], "participant")
