"""Explicit website consent fixtures for tests of downstream service behavior.

Boundary denial/revocation tests use the ordinary client. These fixtures supply
valid consent so existing config/callback tests still exercise their own logic.
"""
from rest_framework.test import APIClient
from content_factory.website_connections import contract_for
from content_factory.website_models import WebsiteConnection


def setup_branch_policy_get_fixture(url, **kwargs):
    """Serve only branch-policy provider reads; keep merge/protection logic real."""
    import json
    import re
    from urllib.parse import urlsplit
    from requests import Response
    parsed = urlsplit(url)
    response = Response()
    if parsed.scheme != "https" or parsed.netloc != "api.github.com":
        raise AssertionError(f"Unexpected branch-policy origin: {url}")
    if re.fullmatch(r"/repos/[^/]+/[^/]+/rules/branches/[^/]+", parsed.path):
        response.status_code, payload = 200, []
    elif re.fullmatch(r"/repos/[^/]+/[^/]+/branches/[^/]+/protection", parsed.path):
        response.status_code, payload = 403, {"message": "Resource not accessible by integration"}
    else:
        raise AssertionError(f"Unexpected branch-policy read: {url}")
    response._content = json.dumps(payload).encode()
    return response


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
            "public_url": f"https://{website.organization.domain}/articles", "checked_at": timezone.now().isoformat()})


def bind_config_fixture(config, *, repo=None):
    website = WebsiteConnection.objects.create(organization=config.organization,
        github_repo=repo or config.github_repo or 'example/site', repository_id=12345,
        installation_id=config.github_installation_id or '123', branch='main')
    config.github_repo = website.github_repo
    config.github_installation_id = website.installation_id
    config.website_connection = website
    config.save(update_fields=['github_repo', 'github_installation_id', 'website_connection'])
    return contract_for(website)


def bind_verified_target_fixture(config, *, user):
    """Seed the current records produced by connection/verification workflows."""
    from django.utils import timezone
    from content_factory.website_models import WebsiteConnectionTarget

    bind_config_fixture(config)
    website = config.website_connection
    website.authorized_by = user
    website.verified_sha = 'a' * 40
    website.last_verified_at = timezone.now()
    website.capabilities = {'publishingReady': True, 'generationReady': True}
    website.save(update_fields=['authorized_by', 'verified_sha', 'last_verified_at', 'capabilities'])
    contract = {'target_id': 'fixture-articles', 'kind': 'react_article_system',
                'publish_capability': 'direct', 'route_path': '/articles'}
    target = WebsiteConnectionTarget.objects.create(connection=website,
        generation=website.generation, target_key=contract['target_id'], adapter='react_article_system',
        source_sha=website.verified_sha, verified_at=timezone.now(), contract=contract,
        capabilities={'publishingReady': True})
    certify_live_target_fixture(target)
    config.publish_targets = [target.contract]
    config.default_publish_target_id = target.target_key
    config.connected_slack_user_id = user.slack_id or f'mlai_user:{user.pk}'
    config.save(update_fields=['publish_targets', 'default_publish_target_id', 'connected_slack_user_id'])
    return contract_for(website)


def install_github_provider_fixture(test_case, website):
    """Stub provider I/O for one exact repository, retaining permission parsing."""
    import json
    from unittest.mock import patch
    from urllib.parse import quote
    from requests import Response
    from django.test import override_settings
    from integrations.services.github_app import GitHubInstallationToken, INSTALLATION_LIVE

    provider_settings = override_settings(GITHUB_APP_ID='12', GITHUB_APP_PRIVATE_KEY='synthetic-provider-key')
    provider_settings.enable()
    test_case.addCleanup(provider_settings.disable)

    def response(payload, status=200):
        result = Response()
        result.status_code = status
        result._content = json.dumps(payload).encode()
        return result

    def get(url, **kwargs):
        grants = {'contents': 'write', 'pull_requests': 'write', 'metadata': 'read'}
        if url == 'https://api.github.com/app':
            return response({'id': 12, 'permissions': grants})
        if url == f'https://api.github.com/app/installations/{website.installation_id}':
            return response({'id': int(website.installation_id), 'permissions': grants, 'suspended_at': None})
        repo_url = f'https://api.github.com/repos/{website.github_repo}'
        if url == repo_url:
            return response({'id': website.repository_id, 'full_name': website.github_repo,
                             'default_branch': website.branch, 'permissions': {'push': True}})
        if url == repo_url + '/commits/' + quote(website.branch, safe=''):
            # Reading a current HEAD does not certify a native integration.
            return response({'sha': website.verified_sha or 'a' * 40})
        raise AssertionError(f'Unexpected provider read in fixture: {url}')

    def mint(*, installation_id, repository, permission_mode='write', **kwargs):
        test_case.assertEqual(str(installation_id), website.installation_id)
        test_case.assertEqual(repository, website.github_repo)
        return GitHubInstallationToken('synthetic-provider-token', None, str(installation_id), repository,
            permissions={'contents': permission_mode, 'pull_requests': permission_mode})

    for patcher in (
        patch('integrations.services.github_app._github_app_jwt', return_value='synthetic-provider-jwt'),
        patch('integrations.services.github_app.create_installation_access_token', side_effect=mint),
        patch('integrations.services.github_app.probe_installation_liveness', return_value=INSTALLATION_LIVE),
        patch('integrations.http_client.get', side_effect=get),
        patch('integrations.http_client.delete', return_value=response({}, 204)),
    ):
        patcher.start()
        test_case.addCleanup(patcher.stop)


class WebsiteBoundAPIClient(APIClient):
    def bind_fixture(self, config, *, repo='owner/mlai-au'):
        self.website_fixture = WebsiteConnection.objects.create(organization=config.organization,
            github_repo=repo, repository_id=12345, installation_id='123', branch='main')
        config.github_repo = repo
        config.github_installation_id = self.website_fixture.installation_id
        config.website_connection = self.website_fixture
        config.save(update_fields=['github_repo', 'github_installation_id', 'website_connection'])

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
            run.github_repo = run.github_repo or self.website_fixture.github_repo
            run.run_request = {**(run.run_request or {}), **binding}
            run.save(update_fields=['organization', 'github_repo', 'run_request'])
        if isinstance(data.get('run_request'), dict):
            data['run_request'] = {**data['run_request'], **binding}
        return {**binding, **data}
