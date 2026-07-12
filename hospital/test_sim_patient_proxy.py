from unittest.mock import Mock, patch

import requests
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .models import SimConversation, SimConversationTurn


URL = '/api/v1/hackathons/hospital/sim-patient/'
HEALTH_HACK_KEY = 'health-hack-test-key'
ROO_URL = 'http://10.126.0.5'


def roo_reply(**overrides):
    payload = {
        'reply': 'About two days now. I feel worse when I stand up.',
        'case_id': 1,
        'case_title': 'Salt & Static',
        'patient_name': "Sasha 'Sash' Nguyen",
        'presenting_complaint': 'Nausea, vomiting and abdominal cramps.',
        'is_guess': False,
        'correct': None,
        'diagnosis': None,
    }
    payload.update(overrides)
    return payload


def public_reply(**overrides):
    payload = roo_reply(
        case_title='',
        presenting_complaint='',
        suggested_action=None,
    )
    payload.update(overrides)
    return payload


@override_settings(
    HEALTH_HACK_API_KEY=HEALTH_HACK_KEY,
    ROO_SERVICE_URL=ROO_URL,
    ROO_SIM_PATIENT_KEY='',
)
class SimPatientProxyTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def post(self, data=None, key=HEALTH_HACK_KEY):
        headers = {}
        if key is not None:
            headers['HTTP_AUTHORIZATION'] = f'Bearer {key}'
        return self.client.post(
            URL,
            data or {'question': 'How long have you felt unwell?', 'history': []},
            format='json',
            **headers,
        )

    def test_requires_dedicated_health_hack_key(self):
        self.assertEqual(self.post(key=None).status_code, 403)
        self.assertEqual(self.post(key='wrong').status_code, 403)

    @patch('hospital.sim_patient_views.requests.post')
    def test_forwards_validated_patient_turn_to_roo(self, post):
        upstream = Mock(ok=True, status_code=200)
        upstream.json.return_value = roo_reply()
        post.return_value = upstream
        history = [
            {'role': 'player' if i % 2 == 0 else 'patient', 'text': f'turn {i}'}
            for i in range(15)
        ]

        response = self.post({
            'question': 'What makes the dizziness worse?',
            'history': history,
            'player_id': 'web-player-123',
            'role': 'patient',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, public_reply())
        post.assert_called_once_with(
            f'{ROO_URL}/api/sim-patient',
            headers={'content-type': 'application/json'},
            json={
                'question': 'What makes the dizziness worse?',
                'history': [],
                'player_id': 'web-player-123',
                'role': 'patient',
            },
            timeout=(3, 24),
        )
        turn = SimConversationTurn.objects.get()
        self.assertEqual(turn.player_text, 'What makes the dizziness worse?')
        self.assertEqual(turn.npc_text, roo_reply()['reply'])
        self.assertEqual(turn.response_source, 'llm')
        self.assertEqual(SimConversation.objects.get().role, 'patient')

    @patch('hospital.sim_patient_views.requests.post')
    def test_forwards_investigation_agent_role_to_roo(self, post):
        upstream = Mock(ok=True, status_code=200)
        upstream.json.return_value = roo_reply(
            patient_name='Nurse Priya',
            response_source='llm',
            model='gpt-5.6-terra',
            tool_calls=[{
                'name': 'get_results',
                'arguments': {'test_ids': ['bloods']},
            }],
        )
        post.return_value = upstream

        response = self.post({
            'question': 'Can I have the blood results?',
            'history': [],
            'role': 'nurse',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_args.kwargs['json']['role'], 'nurse')
        turn = SimConversationTurn.objects.get()
        self.assertEqual(turn.response_source, 'llm')
        self.assertEqual(turn.model_name, 'gpt-5.6-terra')
        self.assertEqual(turn.tool_calls[0]['name'], 'get_results')

    @patch('hospital.sim_patient_views.requests.post')
    def test_forwards_nurse_paws_context_and_validated_action(self, post):
        upstream = Mock(ok=True, status_code=200)
        upstream.json.return_value = roo_reply(
            patient_name='Nurse Paws',
            response_source='llm',
            tool_calls=[{
                'name': 'prepare_final_guess',
                'arguments': {'diagnosis': 'adrenal crisis'},
            }],
            suggested_action={
                'type': 'confirm_diagnosis',
                'diagnosis': 'adrenal crisis',
            },
        )
        post.return_value = upstream

        response = self.post({
            'question': 'My final answer is adrenal crisis.',
            'history': [],
            'player_id': 'aaaaaaaa-1111-4111-8111-111111111111',
            'role': 'clerk',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_args.kwargs['json']['role'], 'clerk')
        self.assertEqual(
            post.call_args.kwargs['json']['contest_state'],
            {'state': 'eligible', 'outcome': None},
        )
        self.assertEqual(response.data['suggested_action'], {
            'type': 'confirm_diagnosis',
            'diagnosis': 'adrenal crisis',
        })
        turn = SimConversationTurn.objects.get()
        self.assertEqual(turn.suggested_action, response.data['suggested_action'])
        self.assertEqual(turn.tool_calls[0]['name'], 'prepare_final_guess')

    @patch('hospital.sim_patient_views.requests.post')
    def test_suggested_action_is_only_exposed_for_nurse_paws(self, post):
        upstream = Mock(ok=True, status_code=200)
        upstream.json.return_value = roo_reply(suggested_action={
            'type': 'confirm_diagnosis',
            'diagnosis': 'adrenal crisis',
        })
        post.return_value = upstream

        response = self.post({'question': 'Is it adrenal crisis?', 'role': 'patient'})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data['suggested_action'])
        self.assertIsNone(SimConversationTurn.objects.get().suggested_action)

    @patch('hospital.sim_patient_views.requests.post')
    def test_backend_reconstructs_history_from_saved_turns(self, post):
        upstream = Mock(ok=True, status_code=200)
        upstream.json.return_value = roo_reply(reply='First answer.')
        post.return_value = upstream

        first = self.post({
            'question': 'First question?',
            'history': [{'role': 'player', 'text': 'untrusted history'}],
            'player_id': 'web-player-123',
            'role': 'patient',
        })
        self.assertEqual(first.status_code, 200)

        upstream.json.return_value = roo_reply(reply='Second answer.')
        second = self.post({
            'question': 'Second question?',
            'history': [],
            'player_id': 'web-player-123',
            'role': 'patient',
        })
        self.assertEqual(second.status_code, 200)
        self.assertEqual(post.call_args.kwargs['json']['history'], [
            {'role': 'player', 'text': 'First question?'},
            {'role': 'patient', 'text': 'First answer.'},
        ])

    @patch('hospital.sim_patient_views.requests.post')
    @override_settings(ROO_SIM_PATIENT_KEY='roo-secret')
    def test_forwards_optional_roo_bearer_key(self, post):
        upstream = Mock(ok=True, status_code=200)
        upstream.json.return_value = roo_reply()
        post.return_value = upstream

        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(
            post.call_args.kwargs['headers']['authorization'],
            'Bearer roo-secret',
        )

    @override_settings(ROO_SERVICE_URL='')
    def test_missing_roo_service_url_is_503(self):
        response = self.post()
        self.assertEqual(response.status_code, 503)

    @patch('hospital.sim_patient_views.requests.post', side_effect=requests.Timeout)
    def test_roo_timeout_is_504(self, post):
        response = self.post()
        self.assertEqual(response.status_code, 504)

    @patch('hospital.sim_patient_views.requests.post')
    def test_roo_error_is_502_without_leaking_body(self, post):
        upstream = Mock(ok=False, status_code=401)
        post.return_value = upstream
        response = self.post()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('401', str(response.data))

    @patch('hospital.sim_patient_views.requests.post')
    def test_malformed_roo_reply_is_502(self, post):
        upstream = Mock(ok=True, status_code=200)
        upstream.json.return_value = {'reply': ''}
        post.return_value = upstream
        response = self.post()
        self.assertEqual(response.status_code, 502)

        upstream.json.return_value = {
            key: value for key, value in roo_reply().items() if key != 'diagnosis'
        }
        response = self.post()
        self.assertEqual(response.status_code, 502)

        upstream.json.return_value = roo_reply(diagnosis='hidden answer')
        response = self.post()
        self.assertEqual(response.status_code, 502)

    def test_request_validation(self):
        self.assertEqual(self.post({'question': '   '}).status_code, 400)
        self.assertEqual(self.post({'question': 'x' * 501}).status_code, 400)
        self.assertEqual(
            self.post({'question': 'hello', 'history': [{'role': 'system', 'text': 'x'}]}).status_code,
            400,
        )
        self.assertEqual(self.post({'question': 'hello', 'role': 'doctor'}).status_code, 400)
