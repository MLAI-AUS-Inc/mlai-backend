"""Explicit website consent fixtures for tests of downstream service behavior.

Boundary denial/revocation tests use the ordinary client. These fixtures supply
valid consent so existing config/callback tests still exercise their own logic.
"""
from rest_framework.test import APIClient
from content_factory.website_connections import contract_for
from content_factory.website_models import WebsiteConnection


def certify_live_target_fixture(target):
    """Persist exact synthetic live evidence without replacing authority guards."""
    from django.utils import timezone
    from content_factory.website_models import WebsiteConnectionOperation
    website = target.connection
    target.contract = {**target.contract, "contract_digest": "d" * 64, "live_marker": {"value": "e" * 64}}
    target.save(update_fields=["contract"])
    WebsiteConnectionOperation.objects.create(connection=website, generation=website.generation,
        action="deployment-verify", state="completed", idempotency_key="synthetic-live-" + str(target.pk),
        payload={"source_sha": website.verified_sha, "target_id": target.target_key},
        receipt={"status": "passed", "source_sha": website.verified_sha,
            "connection_generation": website.generation, "target_id": target.target_key,
            "contract_digest": "d" * 64, "artifact_digest": "e" * 64,
            "public_url": "https://site.example.test/articles", "checked_at": timezone.now().isoformat()})


def bind_config_fixture(config, *, repo=None):
    website = WebsiteConnection.objects.create(organization=config.organization,
        github_repo=repo or config.github_repo or 'example/site', repository_id=12345,
        installation_id=config.github_installation_id or '123', branch='main')
    config.github_repo = website.github_repo
    config.website_connection = website
    config.save(update_fields=['github_repo', 'website_connection'])
    return contract_for(website)


class WebsiteBoundAPIClient(APIClient):
    def bind_fixture(self, config, *, repo='owner/mlai-au'):
        self.website_fixture = WebsiteConnection.objects.create(organization=config.organization,
            github_repo=repo, repository_id=12345, installation_id='123', branch='main')
        config.github_repo = repo
        config.website_connection = self.website_fixture
        config.save(update_fields=['github_repo', 'website_connection'])

    def _bound_payload(self, data):
        data = dict(data or {})
        website = getattr(self, 'website_fixture', None)
        if website and data.get('domain', website.organization.domain) == website.organization.domain:
            data = {**contract_for(website), **data}
        return data

    def put(self, path, data=None, *args, **kwargs):
        return super().put(path, self._bound_payload(data), *args, **kwargs)

    def post(self, path, data=None, *args, **kwargs):
        return super().post(path, self._bound_payload(data), *args, **kwargs)


class CallbackFixtureClient(WebsiteBoundAPIClient):
    """Attach synthetic consent to manually built downstream callback fixtures."""
    def post(self, path, data=None, *args, **kwargs):
        if '/runs/' in path:
            data = {**(data or {}), 'run_id': path.split('/runs/', 1)[1].split('/', 1)[0]}
        return super().post(path, data, *args, **kwargs)

    def _bound_payload(self, data):
        from content_factory.models import OrganizationContentConfig, ContentFactoryJob
        from organizations.models import Organization
        from workflow_runs.models import ContentFactoryRun
        data = dict(data or {})
        run_id = data.get('run_id') or data.get('job_id')
        run = ContentFactoryRun.objects.filter(run_id=run_id).first() if run_id else None
        job = ContentFactoryJob.objects.filter(job_id=run_id).first() if run_id else None
        domain = data.get('domain') or getattr(run, 'domain', '') or getattr(job, 'domain', '') or 'mlai.au'
        org, _ = Organization.objects.get_or_create(domain=domain, defaults={'name': 'Synthetic callback fixture'})
        config, _ = OrganizationContentConfig.objects.get_or_create(organization=org)
        if not config.website_connection_id:
            self.bind_fixture(config, repo=data.get('github_repo') or config.github_repo or getattr(run, 'github_repo', '') or 'MLAI-AUS-Inc/mlai-au')
        else:
            self.website_fixture = config.website_connection
        binding = contract_for(self.website_fixture)
        if run and not (run.run_request or {}).get('website_connection_id'):
            run.organization = org
            run.run_request = {**(run.run_request or {}), **binding}
            run.save(update_fields=['organization', 'run_request'])
        return {**binding, **data}
