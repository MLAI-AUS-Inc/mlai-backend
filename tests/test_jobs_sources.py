from unittest.mock import patch

from django.test import SimpleTestCase

from jobs.services.careerone import collect_careerone_jobs


class JobsSourceTests(SimpleTestCase):
    @patch("jobs.services.careerone.fetch_query_jobs")
    def test_careerone_collector_uses_retrying_fetcher(self, mock_fetch_query_jobs):
        mock_fetch_query_jobs.return_value = [
            {
                "job_url": "https://www.careerone.com.au/jobview/example",
                "title": "AI Engineer",
            }
        ]

        jobs = collect_careerone_jobs()

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "AI Engineer")
        self.assertTrue(mock_fetch_query_jobs.called)
