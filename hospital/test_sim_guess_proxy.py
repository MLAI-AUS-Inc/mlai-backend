import json
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
        'winner_taken': True,
        'case_id': 1,
        'diagnosis': 'Adrenal Crisis',
    }
    payload.update(overrides)
    return payload


def upstream_response(payload=None, *, status_code=200, raw=None):
    if raw is None:
        raw = json.dumps(payload if payload is not None else roo_reply()).encode('utf-8')
    response = Mock(
        ok=200 <= status_code < 300,
        status_code=status_code,
        headers={'content-length': str(len(raw))},
    )
    response.iter_content.return_value = [raw]
    response.close = Mock()
    return response


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
        post.return_value = upstream_response()

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
            stream=True,
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
        post.return_value = upstream_response(status_code=503)
        self.assertEqual(self.post().status_code, 502)

        post.return_value = upstream_response(roo_reply(diagnosis=123))
        self.assertEqual(self.post().status_code, 502)

        post.return_value = upstream_response(roo_reply(diagnosis="\ud800"))
        self.assertEqual(self.post().status_code, 502)

    @patch('hospital.sim_contest_views.requests.post')
    def test_bounds_stream_and_rejects_incoherent_or_extra_fields(self, post):
        post.side_effect = [
            upstream_response(raw=b'{' + (b'x' * 9000) + b'}'),
            upstream_response(roo_reply(winner_taken=False)),
            upstream_response({**roo_reply(), 'internal': 'hidden'}),
        ]

        self.assertEqual(self.post().status_code, 502)
        self.assertEqual(self.post().status_code, 502)
        self.assertEqual(self.post().status_code, 502)

    @patch('hospital.sim_contest_views.requests.post')
    def test_stream_failure_and_recursive_json_are_controlled_502s(self, post):
        broken = upstream_response()
        broken.iter_content.side_effect = requests.ConnectionError('stream reset')
        recursive = (b'[' * 1500) + b'0' + (b']' * 1500)
        post.side_effect = [broken, upstream_response(raw=recursive)]

        self.assertEqual(self.post().status_code, 502)
        self.assertEqual(self.post().status_code, 502)


    @patch('hospital.sim_contest_views.requests.post')
    def test_forwards_case_id_and_accepts_matching_echo(self, post):
        post.return_value = upstream_response(roo_reply(
            case_id=2, diagnosis='Acute Intermittent Porphyria',
        ))

        response = self.post({
            'guess': 'acute intermittent porphyria',
            'client_id': CLIENT_ID,
            'case_id': 2,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['case_id'], 2)
        self.assertEqual(post.call_args.kwargs['json'], {
            'guess': 'acute intermittent porphyria',
            'client_id': CLIENT_ID,
            'case_id': 2,
        })

    def test_rejects_non_open_case_before_calling_roo(self):
        with patch('hospital.sim_contest_views.requests.post') as post:
            response = self.post({
                'guess': 'anything', 'client_id': CLIENT_ID, 'case_id': 9,
            })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'case_not_open')
        post.assert_not_called()

    @patch('hospital.sim_contest_views.requests.post')
    def test_case_echo_mismatch_is_502(self, post):
        # An older roo that ignores case_id must never pass its default-case
        # verdict off as the case the player targeted.
        post.return_value = upstream_response(roo_reply(case_id=1))

        response = self.post({
            'guess': 'acute intermittent porphyria',
            'client_id': CLIENT_ID,
            'case_id': 2,
        })

        self.assertEqual(response.status_code, 502)
