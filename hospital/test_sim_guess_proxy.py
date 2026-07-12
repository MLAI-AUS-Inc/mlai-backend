from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient


URL = '/api/v1/hackathons/hospital/sim-guess/check/'
HEALTH_HACK_KEY = 'health-hack-test-key'
ROO_URL = 'http://10.126.0.5'
CLIENT_ID = 'aaaaaaaa-1111-4111-8111-111111111111'


def roo_reply(**overrides):
    payload = {
        'result': 'correct_first',
        'outcome': 'pending_claim',
        'prize_kind': 'free_ticket',
        'winner_taken': False,
        'case_id': 1,
        'diagnosis': 'Adrenal Crisis',
    }
    payload.update(overrides)
    return payload


@override_settings(
    HEALTH_HACK_API_KEY=HEALTH_HACK_KEY,
    ROO_SERVICE_URL=ROO_URL,
    ROO_SIM_PATIENT_KEY='roo-secret',
)
class SimGuessCheckProxyTests(SimpleTestCase):
    def setUp(self):
        self.client = APIClient()

    def post(self, data=None, key=HEALTH_HACK_KEY):
        headers = {}
        if key is not None:
            headers['HTTP_AUTHORIZATION'] = f'Bearer {key}'
        return self.client.post(
            URL,
            data or {'guess': 'adrenal crisis', 'client_id': CLIENT_ID},
            format='json',
            **headers,
        )

    def test_requires_health_hack_key(self):
        self.assertEqual(self.post(key=None).status_code, 403)
        self.assertEqual(self.post(key='wrong').status_code, 403)

    @patch('hospital.sim_contest_views.requests.post')
    def test_forwards_guess_to_private_roo(self, post):
        upstream = Mock(ok=True, status_code=200)
        upstream.json.return_value = roo_reply()
        post.return_value = upstream

        response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, roo_reply())
        post.assert_called_once_with(
            f'{ROO_URL}/api/diagnosis-check',
            headers={
                'content-type': 'application/json',
                'authorization': 'Bearer roo-secret',
            },
            json={'guess': 'adrenal crisis', 'client_id': CLIENT_ID},
            timeout=(3, 10),
        )

    def test_validates_before_calling_roo(self):
        self.assertEqual(self.post({'guess': '', 'client_id': CLIENT_ID}).status_code, 400)
        self.assertEqual(self.post({'guess': 'x', 'client_id': 'short'}).status_code, 400)

    @override_settings(ROO_SERVICE_URL='')
    def test_missing_roo_service_is_503(self):
        self.assertEqual(self.post().status_code, 503)

    @patch('hospital.sim_contest_views.requests.post', side_effect=requests.Timeout)
    def test_timeout_is_504(self, post):
        self.assertEqual(self.post().status_code, 504)

    @patch('hospital.sim_contest_views.requests.post')
    def test_rejects_upstream_error_and_malformed_reply(self, post):
        upstream = Mock(ok=False, status_code=503)
        post.return_value = upstream
        self.assertEqual(self.post().status_code, 502)

        upstream.ok = True
        upstream.json.return_value = roo_reply(diagnosis=123)
        self.assertEqual(self.post().status_code, 502)
