from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from jobs.services.workforce import fetch_external_company_name


class WorkforceSourceTests(SimpleTestCase):
    @patch("jobs.services.workforce.requests.get")
    def test_external_company_fetch_rejects_private_address(self, mock_get):
        self.assertIsNone(fetch_external_company_name("http://127.0.0.1/internal"))
        mock_get.assert_not_called()

    @patch("jobs.services.workforce.requests.get")
    def test_external_company_fetch_does_not_follow_redirect_to_private_address(self, mock_get):
        redirect = Mock(
            status_code=302,
            headers={"Location": "http://169.254.169.254/latest/meta-data/"},
        )
        mock_get.return_value = redirect

        self.assertIsNone(fetch_external_company_name("https://93.184.216.34/job"))

        mock_get.assert_called_once()
        redirect.close.assert_called_once()

    @patch("jobs.services.workforce.requests.get")
    def test_external_company_fetch_reads_bounded_public_response(self, mock_get):
        response = Mock(
            status_code=200,
            headers={"Content-Length": "141"},
            encoding="utf-8",
        )
        response.iter_content.return_value = [
            b'<script type="application/ld+json">'
            b'{"@type":"JobPosting","hiringOrganization":{"name":"Example Labs"}}'
            b"</script>"
        ]
        mock_get.return_value = response

        self.assertEqual(
            fetch_external_company_name("https://93.184.216.34/job"),
            "Example Labs",
        )
        response.raise_for_status.assert_called_once()
        response.close.assert_called_once()

    @patch("jobs.services.workforce.requests.get")
    def test_external_company_fetch_rejects_oversized_response(self, mock_get):
        response = Mock(
            status_code=200,
            headers={"Content-Length": str(512 * 1024 + 1)},
            encoding="utf-8",
        )
        mock_get.return_value = response

        self.assertIsNone(fetch_external_company_name("https://93.184.216.34/job"))
        response.iter_content.assert_not_called()
        response.close.assert_called_once()
