from bs4 import BeautifulSoup
from django.test import SimpleTestCase

from jobs.services.public_pages import (
    parse_ai_jobs_au_jobs,
    parse_topstartups_jobs,
)
from jobs.services.sources import PHASE_1_SOURCES


class JobsPublicPagesParserTests(SimpleTestCase):
    def test_available_static_sources_are_enabled(self):
        enabled = {source.name for source in PHASE_1_SOURCES if source.enabled}
        self.assertIn("TopStartups.io", enabled)
        self.assertIn("ai-jobs.com.au", enabled)
        self.assertNotIn("Matchstiq", enabled)
        self.assertNotIn("CareerOne", enabled)
        self.assertTrue(all(source.enabled for source in PHASE_1_SOURCES))

    def test_parse_topstartups_jobs(self):
        soup = BeautifulSoup(
            """
            <main>
              <a href="https://example.ai">Example AI</a>
              <a href="https://jobs.ashbyhq.com/example/1">Machine Learning Engineer</a>
              <p>Remote (Australia)</p>
              <p>Experience: 3-4 years</p>
              <p>Posted: 1 day ago</p>
              <p>11-50 employees</p>
              <a href="https://jobs.ashbyhq.com/example/1">Apply</a>
            </main>
            """,
            "html.parser",
        )

        jobs = parse_topstartups_jobs(soup, "https://topstartups.io/jobs/", "TopStartups.io", "startup_board", 0.78, 5)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "Machine Learning Engineer")
        self.assertEqual(jobs[0]["company_name"], "Example AI")
        self.assertEqual(jobs[0]["location"], "Remote (Australia)")

    def test_parse_ai_jobs_au_jobs(self):
        soup = BeautifulSoup(
            """
            <main>
              <h3>Freelance Agent Evaluation Engineer</h3>
              <p>Mindrift•Remote, AU</p>
              <p>Part-time</p>
              <p>workable.com</p>
              <p>Project-based AI opportunities.</p>
              <p>yesterday</p>
            </main>
            """,
            "html.parser",
        )

        jobs = parse_ai_jobs_au_jobs(soup, "https://ai-jobs.com.au/", "ai-jobs.com.au", "ai_board", 0.86, 5)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["company_name"], "Mindrift")
        self.assertEqual(jobs[0]["location"], "Remote, AU")
