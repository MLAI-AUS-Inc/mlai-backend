"""
Security tests for MedHack endpoints.
"""
from django.test import TestCase, override_settings
from django.conf import settings
from rest_framework.test import APIClient
from hospital.models import MedHackCase


@override_settings(
    ROO_API_KEY='test-security-key',
    MEDHACK_ADMIN_IDS=['U12345678', 'system']
)
class MedHackSecurityTests(TestCase):
    """Test rate limiting and input validation for MedHack endpoints."""

    def setUp(self):
        self.client = APIClient()
        self.api_key = 'test-security-key'
        self.headers = {'HTTP_X_API_KEY': self.api_key}

        # Create an active case for testing
        self.case = MedHackCase.objects.create(
            case_id=1,
            is_active=True,
            started_by_slack_id='system'
        )

    def test_guess_length_validation(self):
        """Test that overly long guesses are rejected."""
        long_guess = "A" * 501  # Exceeds MAX_GUESS_LENGTH of 500

        response = self.client.post(
            '/api/v1/medhack/guesses/pending/',
            {
                'case_id': 1,
                'slack_user_id': 'U12345678',
                'guess': long_guess
            },
            **self.headers
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('exceeds maximum length', response.json()['detail'])

    def test_guess_empty_validation(self):
        """Test that empty guesses are rejected."""
        response = self.client.post(
            '/api/v1/medhack/guesses/pending/',
            {
                'case_id': 1,
                'slack_user_id': 'U12345678',
                'guess': '   '  # Just whitespace
            },
            **self.headers
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('cannot be empty', response.json()['detail'])

    def test_guess_xss_sanitization(self):
        """Test that HTML in guesses is escaped."""
        xss_guess = '<script>alert("xss")</script>'

        response = self.client.post(
            '/api/v1/medhack/guesses/pending/',
            {
                'case_id': 1,
                'slack_user_id': 'U12345678',
                'guess': xss_guess
            },
            **self.headers
        )

        self.assertEqual(response.status_code, 201)
        # Verify HTML was escaped
        sanitized = response.json()['pending_guess']
        self.assertNotIn('<script>', sanitized)
        self.assertIn('&lt;script&gt;', sanitized)

    def test_slack_id_format_validation(self):
        """Test that invalid Slack IDs are rejected."""
        invalid_ids = [
            'invalid',
            '123456',
            'A12345678',  # Wrong prefix
            'U123',  # Too short
        ]

        for invalid_id in invalid_ids:
            response = self.client.post(
                '/api/v1/medhack/guesses/pending/',
                {
                    'case_id': 1,
                    'slack_user_id': invalid_id,
                    'guess': 'Test diagnosis'
                },
                **self.headers
            )

            self.assertEqual(response.status_code, 400, f"Failed for ID: {invalid_id}")
            self.assertIn('Invalid Slack ID', response.json()['detail'])

    def test_valid_slack_id_formats(self):
        """Test that valid Slack IDs are accepted."""
        valid_ids = [
            'U12345678',
            'W12345678',
            'U123456789',
            'UABCDEFGH',
            'system',  # Special case
        ]

        for valid_id in valid_ids:
            response = self.client.post(
                '/api/v1/medhack/guesses/pending/',
                {
                    'case_id': 1,
                    'slack_user_id': valid_id,
                    'guess': 'Test diagnosis'
                },
                **self.headers
            )

            self.assertEqual(response.status_code, 201, f"Failed for valid ID: {valid_id}")

    def test_admin_slack_id_validation_on_start(self):
        """Test that admin Slack IDs are validated when starting cases."""
        response = self.client.post(
            '/api/v1/medhack/cases/start/',
            {
                'case_id': 2,
                'admin_slack_id': 'invalid_id'
            },
            **self.headers
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid Slack ID', response.json()['detail'])

    def test_rate_limiting_configured(self):
        """Test that rate limiting is configured."""
        from hospital.medhack_views import MedHackRateThrottle

        throttle = MedHackRateThrottle()
        self.assertEqual(throttle.rate, '100/hour')

    def test_submit_guess_sanitization(self):
        """Test that submitted guesses are sanitized."""
        response = self.client.post(
            '/api/v1/medhack/guesses/submit/',
            {
                'case_id': 1,
                'slack_user_id': 'U12345678',
                'guess': '<b>Pneumonia</b>',
                'correct': True
            },
            **self.headers
        )

        self.assertEqual(response.status_code, 200)
        sanitized = response.json()['guess']
        self.assertNotIn('<b>', sanitized)
        self.assertIn('&lt;b&gt;', sanitized)

    def test_winner_slack_id_validation(self):
        """Test that winner Slack IDs are validated."""
        response = self.client.post(
            f'/api/v1/medhack/cases/{self.case.case_id}/winners/',
            {
                'slack_user_id': 'bad_id',
                'is_first_solver': True
            },
            **self.headers
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid Slack ID', response.json()['detail'])
