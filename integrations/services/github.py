import json
import logging
import time
import urllib.parse

import requests as http_requests
from django.conf import settings
from django.urls import reverse

from integrations.models import UserIntegration
from integrations.utils import normalize_domain
from core.article_system import merge_article_system, resolve_article_system
from core.models import GeneratedComponent, ComponentMapping

logger = logging.getLogger(__name__)


class ScanError(Exception):
    """Exception raised when a scan fails."""
    pass


class GitHubAuthScanError(ScanError):
    """Exception raised when a scan fails due to invalid GitHub credentials."""
    pass


class TokenRefreshError(Exception):
    """Exception raised when token refresh fails."""
    pass


AUTH_RECONNECT_TEXT = (
    "❌ Scan failed: GitHub token expired. Please reconnect your GitHub account to continue."
)


def build_github_auth_url(slack_user_id: str, domain: str = None, request=None) -> str:
    """
    Build the same GitHub auth URL returned by GET /api/v1/integrations/github/auth-url.
    """
    normalized_domain = normalize_domain(domain) if domain else ''

    if normalized_domain:
        from integrations.services.article_generation import build_github_oauth_url
        return build_github_oauth_url(normalized_domain, slack_user_id)

    connect_path = reverse('github_connect')
    if request is not None:
        base_connect_url = request.build_absolute_uri(connect_path)
    else:
        redirect_uri = getattr(settings, 'GITHUB_OAUTH_REDIRECT_URI', '')
        parsed = urllib.parse.urlparse(redirect_uri)
        if parsed.scheme and parsed.netloc:
            base_connect_url = urllib.parse.urlunparse(
                (parsed.scheme, parsed.netloc, connect_path, '', '', '')
            )
        else:
            base_connect_url = connect_path

    return f"{base_connect_url}?{urllib.parse.urlencode({'slack_user_id': slack_user_id})}"


def is_github_auth_scan_error_message(message: str) -> bool:
    if not message:
        return False

    normalized = message.lower()
    auth_markers = (
        "github token expired",
        "token expired",
        "access revoked",
        "bad credentials",
        "please reconnect your github account",
        "please re-authenticate",
        "reauthenticate",
        "re-authenticate",
        "token refresh failed",
        "no github token found",
        "no github credentials found",
    )
    return any(marker in normalized for marker in auth_markers)


def coerce_scan_error(message: str) -> ScanError:
    if is_github_auth_scan_error_message(message):
        return GitHubAuthScanError(message)
    return ScanError(message)


def build_github_reconnect_blocks(slack_user_id: str, domain: str = None) -> tuple[str, list]:
    auth_url = build_github_auth_url(slack_user_id, domain=domain)
    normalized_domain = normalize_domain(domain) if domain else ''
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": AUTH_RECONNECT_TEXT,
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Re-connect GitHub",
                        "emoji": True,
                    },
                    "url": auth_url,
                    "action_id": "connect_github",
                    "style": "danger",
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "I've Connected - Resume",
                        "emoji": True,
                    },
                    "action_id": "resume_scan",
                    "value": json.dumps({"domain": normalized_domain}),
                    "style": "primary",
                },
            ],
        },
    ]
    return AUTH_RECONNECT_TEXT, blocks


def refresh_github_token(slack_user_id: str) -> dict:
    """
    Refresh the GitHub access token using the stored refresh token.

    Args:
        slack_user_id: The Slack user ID to refresh token for.

    Returns:
        dict with keys: access_token, refresh_token, expires_at

    Raises:
        TokenRefreshError: If refresh fails or no refresh token available.
    """
    from datetime import timedelta
    from django.utils import timezone

    try:
        integration = UserIntegration.objects.get(slack_user_id=slack_user_id)
    except UserIntegration.DoesNotExist:
        raise TokenRefreshError("No integration found for this user.")

    if not integration.github_refresh_token:
        raise TokenRefreshError("No refresh token available. Please re-authenticate with GitHub.")

    # Call GitHub's token refresh endpoint
    token_resp = http_requests.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
            "client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": integration.github_refresh_token,
        },
        timeout=20,
    )

    if token_resp.status_code != 200:
        logger.error(f"GitHub token refresh failed with status {token_resp.status_code}: {token_resp.text}")
        raise TokenRefreshError(f"GitHub token refresh failed: {token_resp.status_code}")

    token_data = token_resp.json()

    if "error" in token_data:
        error_desc = token_data.get('error_description', token_data.get('error'))
        logger.error(f"GitHub token refresh error: {error_desc}")
        raise TokenRefreshError(f"GitHub token refresh failed: {error_desc}")

    new_access_token = token_data.get("access_token")
    new_refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in")

    if not new_access_token:
        raise TokenRefreshError("No access token in refresh response.")

    # Calculate new expiry time
    token_expires_at = None
    if expires_in:
        token_expires_at = timezone.now() + timedelta(seconds=expires_in)

    # Update the integration with new tokens
    integration.github_access_token = new_access_token
    if new_refresh_token:
        integration.github_refresh_token = new_refresh_token
    integration.github_token_expires_at = token_expires_at
    integration.save()

    logger.info(f"Successfully refreshed GitHub token for {slack_user_id}")

    return {
        "access_token": new_access_token,
        "refresh_token": new_refresh_token,
        "expires_at": token_expires_at,
    }


def is_token_expired(integration: UserIntegration) -> bool:
    """
    Check if the GitHub token is expired or about to expire (within 5 minutes).
    """
    from django.utils import timezone

    if not integration.github_token_expires_at:
        # If we don't have expiry info, assume it might be expired
        return True

    # Consider token expired if it expires within 5 minutes
    buffer_time = timezone.timedelta(minutes=5)
    return timezone.now() >= (integration.github_token_expires_at - buffer_time)


def ensure_valid_token(slack_user_id: str) -> str:
    """
    Ensure we have a valid GitHub token, refreshing if necessary.

    Returns the valid access token.

    Raises:
        TokenRefreshError: If token refresh fails.
        ScanError: If no integration exists.
    """
    try:
        integration = UserIntegration.objects.get(slack_user_id=slack_user_id)
    except UserIntegration.DoesNotExist:
        raise ScanError("No integration found for this user.")

    if not integration.github_access_token:
        raise ScanError("No GitHub token found. Please authenticate with GitHub first.")

    # Check if token needs refresh
    if is_token_expired(integration):
        if integration.github_refresh_token:
            logger.info(f"Token expired for {slack_user_id}, attempting refresh...")
            result = refresh_github_token(slack_user_id)
            return result["access_token"]
        else:
            raise TokenRefreshError("Token expired and no refresh token available. Please re-authenticate.")

    return integration.github_access_token


def scan_github_project(
    slack_user_id: str,
    integration: UserIntegration = None,
    progress_callback=None,
    slack_channel_id: str = None,
    slack_thread_ts: str = None,
    domain: str = None
) -> dict:
    """
    Trigger a repository scan via Content Factory.

    Args:
        slack_user_id: The Slack user ID to scan for.
        integration: Optional pre-fetched UserIntegration instance.
        progress_callback: Optional function(msg: str) to report progress.
        slack_channel_id: Optional Slack channel ID to maintain thread context.
        slack_thread_ts: Optional Slack thread timestamp to maintain thread context.
        domain: The company's website domain (required for company context scraping).

    Returns:
        dict: Response data from Content Factory.

    Raises:
        ScanError: If validation fails or the external call fails.
    """
    # Resolve credentials via domain-aware resolution (org-level preferred, domain-verified user-level)
    from core.models import Organization, OrganizationContentConfig, ContentFactoryJob
    from integrations.services.article_generation import get_github_credentials_for_domain, ArticleGenerationError

    resolved_domain = normalize_domain(domain)

    try:
        creds = get_github_credentials_for_domain(resolved_domain, slack_user_id)
        github_token = creds['token']
        github_repo = creds['repo']
        cred_source = creds['source']
        logger.info(f"Scan using {cred_source}-level credentials for {resolved_domain}: repo={github_repo}")
    except ArticleGenerationError as e:
        raise coerce_scan_error(str(e))

    # Look up UserIntegration separately for post-scan tracking (not for credentials)
    if integration is None:
        try:
            integration = UserIntegration.objects.get(slack_user_id=slack_user_id)
        except UserIntegration.DoesNotExist:
            integration = None  # OK — org-level creds are sufficient for the scan

    # Call Content Factory
    content_factory_url = getattr(settings, 'CONTENT_FACTORY_URL', 'http://localhost:8001')
    scan_endpoint = f"{content_factory_url.rstrip('/')}/api/pipeline/scan"

    api_key = getattr(settings, 'CONTENT_FACTORY_API_KEY', None)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-KEY"] = api_key
        logger.info(f"Using Content Factory API key: {api_key[:4]}***")
    else:
        logger.warning("No CONTENT_FACTORY_API_KEY found in settings! Scan request may fail.")

    # Early validation: Verify GitHub access before calling Content Factory
    import requests as http_requests_lib
    current_sha = None
    try:
        current_sha = get_latest_repo_sha(github_token, github_repo)
    except http_requests_lib.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            raise GitHubAuthScanError("GitHub token expired. Please reconnect your GitHub account.")
        elif e.response.status_code in [403, 404]:
            raise GitHubAuthScanError("GitHub access revoked or repository not found. Please reconnect your GitHub account.")
        else:
            logger.warning(f"Failed to fetch latest SHA for {github_repo}: {e}")
    except Exception as e:
        logger.warning(f"Failed to fetch latest SHA for {github_repo}: {e}")

    # Prepare existing artifacts if available and resolve domain
    config = None
    existing_artifacts = {}
    try:
        # Try to find config by domain first, then by repo
        if resolved_domain:
            try:
                org = Organization.objects.get(domain=resolved_domain)
                config = getattr(org, 'content_config', None)
            except Organization.DoesNotExist:
                pass
        if config is None:
            config = (
                OrganizationContentConfig.objects
                .select_related('organization')
                .filter(github_repo=github_repo)
                .first()
            )
        if config:
            if config.article_template:
                existing_artifacts['article_template'] = config.article_template
            if config.design_guide:
                existing_artifacts['design_guide'] = config.design_guide
            if config.resource_prompt:
                existing_artifacts['resource_prompt'] = config.resource_prompt
            if config.tech_stack:
                existing_artifacts['tech_stack'] = config.tech_stack
            if config.company_context:
                existing_artifacts['company_context'] = config.company_context
            if config.organization and config.organization.domain:
                resolved_domain = resolved_domain or normalize_domain(config.organization.domain)
            # Include existing generated components so CF doesn't regenerate them
            existing_comps = list(
                GeneratedComponent.objects.filter(organization=config.organization)
                .values('name', 'content', 'source')
            )
            if existing_comps:
                existing_artifacts['generated_components'] = existing_comps
    except Exception as e:
        logger.warning(f"Failed to fetch existing artifacts for payload: {e}")

    resolved_domain = normalize_domain(resolved_domain)
    if not resolved_domain:
        raise ScanError("Domain is required for repository scan. Please provide the company's website domain when queueing the scan.")

    cf_data = None
    try:
        payload = {
            "slack_user_id": slack_user_id,
            "github_repo": github_repo,
            "github_token": github_token,
            "domain": resolved_domain,
            "github_client_id": settings.GITHUB_OAUTH_CLIENT_ID,
            "github_client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
            "existing_artifacts": existing_artifacts,
        }

        # Add thread context if available
        if slack_channel_id:
            payload["slack_channel_id"] = slack_channel_id
        if slack_thread_ts:
            payload["slack_thread_ts"] = slack_thread_ts

        cf_response = http_requests.post(
            scan_endpoint,
            json=payload,
            headers=headers,
            # Fallback: Support legacy sync (long timeout) AND async (short response).
            # If the backend is synchronous, this thread will block for up to 60 mins.
            # If the backend is async (returns 202), it returns immediately.
            timeout=3600, 
        )
        
        # Handle both 202 Accepted (standard async) and 200 OK with job_id (potential new behavior)
        # If we get a job_id, we treat it as an async task that needs polling.
        start_polling = False
        cf_data = None
        
        if cf_response.status_code in [200, 202]:
            data = cf_response.json()
            # If it has a job_id and status is queued/processing, or just has job_id and we are in 202
            if 'job_id' in data:
                # Check if it's already done (unlikely for 202, possible for 200)
                if data.get('status') in ['completed', 'failed']:
                     # It's already done, just use the result
                     if data.get('status') == 'completed':
                         cf_data = data.get('result')
                     else:
                         raise coerce_scan_error(f"Scan failed immediately: {data.get('error')}")
                else:
                    # It's queued or processing, start polling
                    start_polling = True
            elif cf_response.status_code == 200:
                # Legacy synchronous response (no job_id, just data)
                cf_data = data

        else:
             cf_response.raise_for_status()

        if start_polling:
            data = cf_response.json()
            job_id = data.get('job_id')
            if not job_id:
                raise ScanError("Async response received but no job_id provided.")

            # Create a ContentFactoryJob so callback handlers can look up thread context
            try:
                ContentFactoryJob.objects.create(
                    job_id=job_id,
                    domain=resolved_domain,
                    slack_user_id=slack_user_id,
                    status='queued',
                    slack_channel_id=slack_channel_id or '',
                    slack_thread_ts=slack_thread_ts or '',
                    request_meta={'type': 'scan', 'github_repo': github_repo},
                )
                logger.info(f"Scan job created: {job_id} for {resolved_domain} (channel={slack_channel_id}, thread={slack_thread_ts})")
            except Exception as e:
                logger.warning(f"Could not create ContentFactoryJob for scan {job_id}: {e}")

            status_url = f"{content_factory_url.rstrip('/')}/api/pipeline/scan/{job_id}"
            
            # Start Polling Loop
            last_progress = ""
            max_retries = 720 # 720 * 5s = 60 minutes max
            
            for _ in range(max_retries):
                time.sleep(5) 
                try:
                    status_resp = http_requests.get(status_url, headers=headers, timeout=10)
                    if status_resp.status_code == 200:
                        status_data = status_resp.json()
                        state = status_data.get('status')
                        progress_msg = status_data.get('progress')
                        
                        # Report progress if changed
                        if progress_msg and progress_msg != last_progress:
                            last_progress = progress_msg
                            if progress_callback:
                                progress_callback(f"⏳ {progress_msg}")
                        
                        if state == 'completed':
                            cf_data = status_data.get('result')
                            break # Done!
                        elif state == 'failed':
                            error_detail = status_data.get('error', 'Unknown error')
                            raise coerce_scan_error(f"Remote scan job failed: {error_detail}")
                        # else: processing/queued, continue loop
                    else:
                        logger.warning(f"Status check returned {status_resp.status_code}")
                except http_requests.exceptions.RequestException as req_err:
                     logger.warning(f"Transient error checking status: {req_err}")
                     
            if not cf_data:
                raise ScanError("Scan timed out or did not complete successfully.")

    except http_requests.exceptions.RequestException as e:
        logger.error(f"Content Factory scan request failed: {e}")
        if hasattr(e, 'response') and e.response is not None:
             logger.error(f"Response status: {e.response.status_code}")
             logger.error(f"Response body: {e.response.text}")
        raise ScanError(f"Failed to trigger scan: {str(e)}")
    except ScanError:
        raise
    except Exception as e:
        raise ScanError(f"Unexpected error during scan: {e}")

    # Update project_scanned status and tracking info on UserIntegration (if available)
    from django.utils import timezone
    if integration:
        integration.project_scanned = True

        # Fetch the CURRENT latest SHA after scan completes (not the one from start)
        # This prevents false "has_updates" if commits were pushed during the scan
        try:
            final_sha = get_latest_repo_sha(github_token, github_repo)
            integration.last_scanned_sha = final_sha
            logger.info(f"Updated last_scanned_sha to final SHA: {final_sha}")
        except Exception as e:
            # Fallback to the SHA from scan start if re-fetch fails
            logger.warning(f"Failed to fetch final SHA, using start SHA: {e}")
            if current_sha:
                integration.last_scanned_sha = current_sha

        integration.last_scanned_at = timezone.now()
        integration.save()

    # Save scan artifacts to OrganizationContentConfig
    org_domain = resolved_domain or github_repo
    try:
        # Ensure Organization exists (idempotent, keyed by domain)
        org_name = resolved_domain or "Unknown"
        if integration and integration.github_user_name:
            org_name = integration.github_user_name

        org, _ = Organization.objects.get_or_create(
            domain=org_domain,
            defaults={"name": org_name}
        )

        # Ensure Config exists
        config, _ = OrganizationContentConfig.objects.get_or_create(organization=org)

        # Update fields from scan response
        # Support both nested 'config' key (legacy) and top-level keys (current)
        cf_config = cf_data.get('config', {})

        # Only set github_repo/token if not already set (preserve org-level auth)
        if not config.github_repo:
            config.github_repo = github_repo
        if not config.github_token_encrypted:
            config.github_token_encrypted = github_token

        # Save artifacts if present (check top-level first, then nested 'config')
        if 'article_template' in cf_data:
            config.article_template = cf_data['article_template']
        elif 'article_template' in cf_config:
            config.article_template = cf_config['article_template']

        if 'design_guide' in cf_data:
            config.design_guide = cf_data['design_guide']
        elif 'design_guide' in cf_config:
            config.design_guide = cf_config['design_guide']

        if 'resource_prompt' in cf_data:
            config.resource_prompt = cf_data['resource_prompt']
        elif 'resource_prompt' in cf_config:
            config.resource_prompt = cf_config['resource_prompt']

        if 'scan_summary' in cf_data:
            config.scan_summary = cf_data['scan_summary']
        elif 'scan_summary' in cf_config:
            config.scan_summary = cf_config['scan_summary']

        if 'tech_stack' in cf_data:
            config.tech_stack = cf_data['tech_stack']
        elif 'tech_stack' in cf_config:
            config.tech_stack = cf_config['tech_stack']
        if 'company_context' in cf_data:
            config.company_context = cf_data['company_context']
        elif 'company_context' in cf_config:
            config.company_context = cf_config['company_context']

        if 'pillar_strategy' in cf_data:
            config.pillar_strategy = cf_data['pillar_strategy']
        elif 'pillar_strategy' in cf_config:
            config.pillar_strategy = cf_config['pillar_strategy']

        if 'article_system' in cf_data:
            config.article_system = merge_article_system(resolve_article_system(config), cf_data['article_system'])
        elif 'article_system' in cf_config:
            config.article_system = merge_article_system(resolve_article_system(config), cf_config['article_system'])

        # Save additional metadata if present
        if 'article_path_pattern' in cf_data:
            config.article_path_pattern = cf_data.get('article_path_pattern')
        if 'registry_path' in cf_data:
            config.registry_path = cf_data.get('registry_path')

        config.save()
        logger.info(f"Updated OrganizationContentConfig for {org_domain}")

        # Save generated components and component mapping from scan response
        component_generation = cf_data.get('component_generation', {})
        generated_components = cf_data.get('generated_components', [])
        component_mapping_data = cf_data.get('component_mapping', {})

        if generated_components:
            components_saved = 0
            for comp_data in generated_components:
                comp_name = comp_data.get('name')
                if not comp_name:
                    continue
                GeneratedComponent.objects.update_or_create(
                    organization=org,
                    name=comp_name,
                    defaults={
                        'content': comp_data.get('content', ''),
                        'source': comp_data.get('source', 'generated'),
                        'original_path': comp_data.get('original_path'),
                        'similarity_score': comp_data.get('similarity_score', 0.0),
                        'matched_component': comp_data.get('matched_component'),
                        'adaptation_notes': comp_data.get('adaptation_notes', ''),
                    }
                )
                components_saved += 1
            logger.info(f"Saved {components_saved} generated components for {org_domain}")

        if component_generation or component_mapping_data:
            mapping_defaults = {
                'mapping_data': component_mapping_data,
            }
            if component_generation:
                mapping_defaults['generation_status'] = component_generation.get('status')
                mapping_defaults['design_guide_path'] = component_generation.get('design_guide_path')
                mapping_defaults['failed_components'] = component_generation.get('failed_components', [])
                generated = component_generation.get('components_generated', 0)
                adapted = component_generation.get('components_adapted', 0)
                mapping_defaults['generated_count'] = generated
                mapping_defaults['matched_count'] = adapted
                mapping_defaults['total_components'] = generated + adapted
                storage = component_generation.get('storage', {})
                if storage:
                    mapping_defaults['storage_local_path'] = storage.get('local_path')
                    mapping_defaults['storage_pr_url'] = storage.get('pr_url')
                    mapping_defaults['storage_branch_url'] = storage.get('branch_url')
            if current_sha:
                mapping_defaults['last_scan_commit'] = current_sha
            ComponentMapping.objects.update_or_create(
                organization=org,
                defaults=mapping_defaults
            )
            logger.info(f"Updated ComponentMapping for {org_domain}")

    except Exception as e:
        logger.error(f"Failed to save scan artifacts to OrganizationContentConfig: {e}")

    logger.info(f"Scan triggered successfully for {slack_user_id}, repo: {github_repo}, domain: {org_domain}, SHA: {current_sha}")

    return {
        "status": "scan_completed",
        "slack_user_id": slack_user_id,
        "github_repo": github_repo,
        "domain": org_domain,
        "scanned_sha": current_sha,
        "content_factory_response": cf_data,
    }


def get_latest_repo_sha(token: str, repo_name: str) -> str:
    """
    Fetch the latest commit SHA for the default branch (usually main/master).
    """
    if not token or not repo_name:
        raise ValueError("Token and repo_name required")
    
    # Check simple HEAD first or branch? 
    # Let's try fetching branches first to be safe, or just commits/HEAD
    # Actually, /repos/:owner/:repo/commits/HEAD works for default branch
    url = f"https://api.github.com/repos/{repo_name}/commits/HEAD"

    resp = http_requests.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
        },
        timeout=10
    )
    if resp.status_code == 404:
        # Fallback to master if HEAD fails (unlikely)
        pass
        
    resp.raise_for_status()
    data = resp.json()
    return data['sha']


def scaffold_articles_directory(
    domain: str,
    slack_user_id: str,
    github_token: str,
    github_repo: str,
    slack_channel_id: str = None,
    slack_thread_ts: str = None,
) -> dict:
    """
    Trigger articles directory scaffolding via Content Factory.

    CF handles all the heavy lifting (pillar strategy, tech stack, path patterns)
    using the domain to look up its own stored config. We just need to pass
    credentials and context.

    Returns:
        dict with job_id and status.

    Raises:
        ScanError: If the API call fails.
    """
    from core.models import ContentFactoryJob

    content_factory_url = getattr(settings, 'CONTENT_FACTORY_URL', 'http://localhost:8001')
    scaffold_endpoint = f"{content_factory_url.rstrip('/')}/api/pipeline/scaffold-articles"

    api_key = getattr(settings, 'CONTENT_FACTORY_API_KEY', None)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-KEY"] = api_key

    payload = {
        "domain": domain,
        "slack_user_id": slack_user_id,
        "github_repo": github_repo,
        "github_token": github_token,
    }

    if slack_channel_id:
        payload["slack_channel_id"] = slack_channel_id
    if slack_thread_ts:
        payload["slack_thread_ts"] = slack_thread_ts

    logger.info(f"Triggering article scaffolding for {domain}")

    try:
        response = http_requests.post(
            scaffold_endpoint,
            json=payload,
            headers=headers,
            timeout=120,
        )

        if response.status_code in [200, 202]:
            data = response.json()
            job_id = data.get('job_id')

            if job_id:
                ContentFactoryJob.objects.create(
                    job_id=job_id,
                    domain=domain,
                    slack_user_id=slack_user_id,
                    status='queued',
                    slack_channel_id=slack_channel_id or '',
                    slack_thread_ts=slack_thread_ts or '',
                    request_meta={'type': 'scaffold_articles'},
                )
                logger.info(f"Scaffold job created: {job_id} for {domain}")

            return {
                "job_id": job_id,
                "status": data.get('status', 'queued'),
            }
        elif response.status_code == 412:
            # Content Factory prerequisite check failed
            try:
                data = response.json()
                missing_step = data.get('missing_step', 'unknown')
                cf_message = data.get('message', 'Prerequisite step missing')
            except Exception:
                missing_step = 'unknown'
                cf_message = response.text
            raise ScanError(
                f"PREREQUISITE_MISSING: {cf_message} (missing: {missing_step})"
            )
        else:
            logger.error(f"Content Factory scaffold failed: {response.status_code} - {response.text}")
            raise ScanError(f"Scaffold request failed: {response.status_code}")

    except http_requests.exceptions.RequestException as e:
        logger.error(f"Failed to connect to Content Factory for scaffolding: {e}")
        raise ScanError(f"Failed to trigger scaffolding: {str(e)}")


def trigger_scan_async(slack_user_id: str, slack_channel_id: str = None, slack_thread_ts: str = None, domain: str = None):
    """
    Trigger a scan in a background thread.
    Logs errors instead of raising them (fire-and-forget).
    All progress updates are sent as threaded replies to the initial message.
    """
    import threading
    from integrations.services.slack import SlackService

    def _run_scan():
        # Notify start and capture thread_ts
        thread_ts = slack_thread_ts
        
        # If we have context, use it
        if slack_channel_id and thread_ts:
            SlackService.send_message(
                slack_channel_id,
                "🔍 I'm starting a deeper scan of your repository...",
                thread_ts=thread_ts
            )
        else:
            # Fallback to DM if no context provided (legacy behavior)
            success, thread_ts = SlackService.send_dm(
                slack_user_id, 
                "🔍 I'm starting a deeper scan of your repository to understand the project structure..."
            )
            if not success:
                logger.error(f"Failed to send initial scan DM for {slack_user_id}")
                return
        
        def _progress_listener(msg):
            # Helper to send concise progress updates as threaded replies
            if slack_channel_id:
                SlackService.send_message(slack_channel_id, msg, thread_ts=thread_ts)
            else:
                SlackService.send_dm(slack_user_id, msg, thread_ts=thread_ts)
        
        try:
            # Pass the listener to report progress
            result = scan_github_project(
                slack_user_id, 
                progress_callback=_progress_listener,
                domain=domain,
                slack_channel_id=slack_channel_id,
                slack_thread_ts=thread_ts
            )
            
            import json as _json

            scan_domain = result.get('domain', domain)
            logger.info(f"Background scan completed: {result}")

            # Build success message with component info + scaffold confirmation buttons
            cf_response = result.get('content_factory_response', {}) or {}
            generated_components = cf_response.get('generated_components', [])

            # Check if scaffolding is available
            has_pillars = False
            already_scaffolded = False
            try:
                from core.models import Organization, OrganizationContentConfig
                scaffold_org = Organization.objects.get(domain=scan_domain)
                scaffold_config = scaffold_org.content_config
                already_scaffolded = scaffold_config.articles_scaffolded
                pillar_strategy = (
                    cf_response.get('pillar_strategy') or
                    scaffold_config.pillar_strategy or
                    {}
                )
                has_pillars = bool(pillar_strategy.get('pillars'))
            except (Organization.DoesNotExist, OrganizationContentConfig.DoesNotExist):
                pass

            if generated_components:
                comp_names = [c.get('name', '?') for c in generated_components[:8]]
                comp_list = "\n".join(f"  • {name}" for name in comp_names)
                if len(generated_components) > 8:
                    comp_list += f"\n  • ...and {len(generated_components) - 8} more"

                # Build pillar summary from strategy
                pillar_line = ""
                if has_pillars:
                    pillars = pillar_strategy.get('pillars', [])
                    p_names = [p.get('name', '') for p in pillars if p.get('name')]
                    if p_names:
                        pillar_display = ", ".join(p_names[:6])
                        if len(p_names) > 6:
                            pillar_display += f", +{len(p_names) - 6} more"
                        pillar_line = f"\n\n*{len(p_names)} content pillars:* {pillar_display}"

                if has_pillars and not already_scaffolded:
                    text_body = (
                        f"✅ *Scan complete for {scan_domain}!*\n\n"
                        f"I've analysed your codebase and generated "
                        f"*{len(generated_components)} article components* "
                        f"matched to your website's design:\n"
                        f"{comp_list}{pillar_line}\n\n"
                        f"The next step is to create an articles directory in your repo. "
                        f"This will set up content pillar directories, article components, "
                        f"an index page, and a demo article — submitted as a PR for your review."
                    )
                    blocks = [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": text_body}
                        },
                        {
                            "type": "actions",
                            "block_id": f"scaffold_confirm_{scan_domain}",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Create Articles Directory"},
                                    "style": "primary",
                                    "action_id": "scaffold_confirm",
                                    "value": _json.dumps({
                                        "domain": scan_domain,
                                        "slack_user_id": slack_user_id,
                                        "channel_id": slack_channel_id or "",
                                        "thread_ts": thread_ts or "",
                                    })
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Skip for now"},
                                    "action_id": "scaffold_skip",
                                    "value": _json.dumps({"domain": scan_domain})
                                }
                            ]
                        }
                    ]
                    fallback_text = f"✅ Scan complete for {scan_domain}! Generated {len(generated_components)} components."
                else:
                    fallback_text = (
                        f"✅ *Scan complete for {scan_domain}!*\n\n"
                        f"I've analysed your codebase and generated "
                        f"*{len(generated_components)} article components* "
                        f"matched to your website's design:\n{comp_list}\n\n"
                        f"These components will be used to create articles that look native to your site.\n\n"
                        f"Would you like me to write your first article? Just say:\n"
                        f"  `@Roo write me an article about [topic]`"
                    )
                    blocks = None
            else:
                fallback_text = (
                    f"✅ *Scan complete for {scan_domain}!*\n\n"
                    f"I've analysed your codebase and I'm ready to help. "
                    f"You can now ask me to create blog pages or other content.\n\n"
                    f"To get started, say:\n"
                    f"  `@Roo write me an article about [topic]`"
                )
                blocks = None

            if slack_channel_id:
                SlackService.send_message(slack_channel_id, fallback_text, blocks=blocks, thread_ts=thread_ts)
            else:
                SlackService.send_dm(slack_user_id, fallback_text, blocks=blocks, thread_ts=thread_ts)

        except GitHubAuthScanError as e:
            logger.error(f"Background scan auth failed for {slack_user_id}: {e}")
            fallback_text, blocks = build_github_reconnect_blocks(slack_user_id, domain=domain)
            if slack_channel_id:
                SlackService.send_message(slack_channel_id, fallback_text, blocks=blocks, thread_ts=thread_ts)
            else:
                SlackService.send_dm(slack_user_id, fallback_text, blocks=blocks, thread_ts=thread_ts)
        except ScanError as e:
            logger.error(f"Background scan failed for {slack_user_id}: {e}")
            if slack_channel_id:
                SlackService.send_message(slack_channel_id, f"❌ Scan failed: {str(e)}", thread_ts=thread_ts)
            else:
                SlackService.send_dm(slack_user_id, f"❌ Scan failed: {str(e)}", thread_ts=thread_ts)
        except Exception as e:
            logger.exception(f"Unexpected error in background scan for {slack_user_id}: {e}")
            if slack_channel_id:
                SlackService.send_message(slack_channel_id, "❌ An unexpected error occurred while scanning your repository.", thread_ts=thread_ts)
            else:
                SlackService.send_dm(slack_user_id, "❌ An unexpected error occurred while scanning your repository.", thread_ts=thread_ts)

    thread = threading.Thread(target=_run_scan, daemon=True)
    thread.start()
    logger.info(f"Triggered background scan for {slack_user_id}")
