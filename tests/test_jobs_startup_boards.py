from django.test import SimpleTestCase

from jobs.services.sources import PHASE_1_SOURCES
from jobs.services.startup_boards import map_main_sequence_job, parse_yc_jobs_html


class JobsStartupBoardsTests(SimpleTestCase):
    def test_main_sequence_and_yc_are_enabled(self):
        sources = {source.name: source for source in PHASE_1_SOURCES}

        self.assertTrue(sources["Main Sequence Jobs"].enabled)
        self.assertTrue(sources["YC Jobs"].enabled)

    def test_map_main_sequence_job(self):
        job = map_main_sequence_job(
            {
                "id": 123,
                "title": "Machine Learning Engineer",
                "slug": "machine-learning-engineer",
                "company_name": "Example Deep Tech",
                "company_slug": "example-deep-tech",
                "company_logo": "https://example.com/logo.png",
                "company_website": "https://example.com",
                "description_html": "<p>Build applied AI systems.</p>",
                "is_remote": True,
                "remote_only": False,
                "remote_required_location": "Australia",
                "apply_url": "https://example.com/apply",
                "timeago": "2 days ago",
                "published_at": "2026-05-20T00:00:00Z",
            }
        )

        self.assertEqual(job["title"], "Machine Learning Engineer")
        self.assertEqual(job["company_name"], "Example Deep Tech")
        self.assertEqual(job["location"], "Australia")
        self.assertEqual(job["job_url"], "https://jobs.mseq.vc/job/123-machine-learning-engineer-example-deep-tech")

    def test_parse_yc_jobs_html(self):
        html = """
        <div data-page="{&quot;jobs&quot;:[{&quot;id&quot;:94615,&quot;title&quot;:&quot;Software Engineer - Backend&quot;,
        &quot;url&quot;:&quot;/companies/spate/jobs/nE9nLQz-software-engineer-backend&quot;,
        &quot;applyUrl&quot;:&quot;https://account.ycombinator.com/apply&quot;,
        &quot;location&quot;:&quot;Remote&quot;,&quot;type&quot;:&quot;Full-time&quot;,&quot;prettyRole&quot;:&quot;Engineering&quot;,
        &quot;salaryRange&quot;:&quot;$120K - $180K&quot;,&quot;visa&quot;:&quot;US citizenship/visa not required&quot;,
        &quot;minExperience&quot;:&quot;3+ years&quot;,&quot;companyLogoUrl&quot;:&quot;https://example.com/logo.png&quot;,
        &quot;companyName&quot;:&quot;SPATE&quot;,&quot;companyBatchName&quot;:&quot;S18&quot;,
        &quot;companyOneLiner&quot;:&quot;Trends prediction for marketers&quot;,
        &quot;createdAt&quot;:&quot;10 days&quot;,&quot;ctaText&quot;:&quot;Apply&quot;,
        &quot;ctaUrl&quot;:&quot;https://account.ycombinator.com/apply&quot;}]}"></div>
        """

        jobs = parse_yc_jobs_html(html, limit=5)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "Software Engineer - Backend")
        self.assertEqual(jobs[0]["company_name"], "SPATE")
        self.assertEqual(jobs[0]["location"], "Remote")
        self.assertEqual(
            jobs[0]["job_url"],
            "https://www.ycombinator.com/companies/spate/jobs/nE9nLQz-software-engineer-backend",
        )
