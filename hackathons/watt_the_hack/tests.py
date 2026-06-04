from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient


User = get_user_model()


class WattTheHackSimulationApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email="watt-sim@example.com")
        self.client.force_authenticate(user=self.user)

    def test_simulation_routes_require_authentication(self):
        client = APIClient()

        response = client.get("/api/v1/hackathons/watt-the-hack/sim/scenarios/")

        self.assertIn(response.status_code, {401, 403})

    def test_scenarios_returns_unlocked_public_scenarios(self):
        response = self.client.get("/api/v1/hackathons/watt-the-hack/sim/scenarios/")

        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.data), 0)
        self.assertIn("id", response.data[0])
        self.assertIn("title", response.data[0])

    def test_init_step_and_run_match_browser_contract(self):
        scenarios_response = self.client.get("/api/v1/hackathons/watt-the-hack/sim/scenarios/")
        scenario_id = scenarios_response.data[0]["id"]

        init_response = self.client.post(
            "/api/v1/hackathons/watt-the-hack/sim/init/",
            {"scenario_id": scenario_id},
            format="json",
        )
        self.assertEqual(init_response.status_code, 200)
        self.assertIn("state", init_response.data)
        self.assertIn("steps", init_response.data)
        self.assertIn("scenario", init_response.data)

        step_response = self.client.post(
            "/api/v1/hackathons/watt-the-hack/sim/step/",
            {
                "state": init_response.data["state"],
                "controller": {"kind": "simple", "params": {}},
            },
            format="json",
        )
        self.assertEqual(step_response.status_code, 200)
        self.assertIn("state", step_response.data)
        self.assertIn("outputs", step_response.data)
        self.assertIn("controller_error", step_response.data)

        run_response = self.client.post(
            "/api/v1/hackathons/watt-the-hack/sim/run/",
            {
                "state": init_response.data["state"],
                "controller": {"kind": "simple", "params": {}},
                "steps": 1,
            },
            format="json",
        )
        self.assertEqual(run_response.status_code, 200)
        self.assertIn("final_state", run_response.data)
        self.assertIn("states", run_response.data)
        self.assertIn("outputs", run_response.data)
        self.assertIn("metrics", run_response.data)

    def test_locked_scenario_rejects_init(self):
        response = self.client.post(
            "/api/v1/hackathons/watt-the-hack/sim/init/",
            {"scenario_id": "t1_welcome"},
            format="json",
        )

        self.assertEqual(response.status_code, 403)

