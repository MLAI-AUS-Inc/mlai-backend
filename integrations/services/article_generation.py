import logging
import secrets
import urllib.parse
import requests as http_requests
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from integrations.models import UserIntegration
from core.models import OrganizationContentConfig, Organization
from integrations.utils import normalize_domain
from integrations.services.github import ensure_valid_token, TokenRefreshError

logger = logging.getLogger(__name__)


class ArticleGenerationError(Exception):
    """Exception raised when article generation fails."""
    pass


def build_github_oauth_url(domain: str, slack_user_id: str = '') -> str:
    """
    Build a GitHub App OAuth URL for domain-level authentication.

    Returns a URL the user can visit to connect GitHub for the given domain.
    """
    normalized_domain = normalize_domain(domain) if domain else ''
    rand_token = secrets.token_urlsafe(16)
    state = f"{normalized_domain}::{rand_token}::{slack_user_id}::org"

    app_slug = "mlai-tools"
    install_url = f"https://github.com/apps/{app_slug}/installations/new"
    params = {"state": state}
    return install_url + "?" + urllib.parse.urlencode(params)


def refresh_org_github_token(domain: str) -> dict:
    """
    Refresh the GitHub access token for an organization using its refresh token.

    Args:
        domain: The organization domain to refresh token for.

    Returns:
        dict with keys: access_token, refresh_token, expires_at

    Raises:
        TokenRefreshError: If refresh fails or no refresh token available.
    """
    # Normalize domain
    normalized_domain = normalize_domain(domain)

    try:
        org = Organization.objects.get(domain=normalized_domain)
        config = org.content_config
    except (Organization.DoesNotExist, OrganizationContentConfig.DoesNotExist):
        raise TokenRefreshError(f"No organization config found for domain: {domain}")

    if not config.github_refresh_token_encrypted:
        raise TokenRefreshError("No refresh token available for this organization. Please re-authenticate with GitHub.")

    # Call GitHub's token refresh endpoint
    token_resp = http_requests.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
            "client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": config.github_refresh_token_encrypted,
        },
        timeout=20,
    )

    if token_resp.status_code != 200:
        logger.error(f"GitHub token refresh failed for org {domain} with status {token_resp.status_code}: {token_resp.text}")
        raise TokenRefreshError(f"GitHub token refresh failed: {token_resp.status_code}")

    token_data = token_resp.json()

    if "error" in token_data:
        error_desc = token_data.get('error_description', token_data.get('error'))
        logger.error(f"GitHub token refresh error for org {domain}: {error_desc}")
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

    # Update the org config with new tokens
    config.github_token_encrypted = new_access_token
    if new_refresh_token:
        config.github_refresh_token_encrypted = new_refresh_token
    config.github_token_expires_at = token_expires_at
    config.save()

    logger.info(f"Successfully refreshed GitHub token for org {domain}")

    return {
        "access_token": new_access_token,
        "refresh_token": new_refresh_token,
        "expires_at": token_expires_at,
    }


def ensure_valid_org_token(domain: str) -> str:
    """
    Ensure we have a valid GitHub token for an organization, refreshing if necessary.

    Returns the valid access token.

    Raises:
        TokenRefreshError: If token refresh fails.
        ArticleGenerationError: If no config exists.
    """
    normalized_domain = normalize_domain(domain)

    try:
        org = Organization.objects.get(domain=normalized_domain)
        config = org.content_config
    except Organization.DoesNotExist:
        raise ArticleGenerationError(f"Organization not found: {domain}")
    except OrganizationContentConfig.DoesNotExist:
        raise ArticleGenerationError(f"No config found for organization: {domain}")

    if not config.github_token_encrypted:
        raise ArticleGenerationError(f"No GitHub token found for {domain}. Please connect GitHub first.")

    # Check if token needs refresh
    if config.github_token_expires_at:
        buffer_time = timedelta(minutes=5)
        if timezone.now() >= (config.github_token_expires_at - buffer_time):
            if config.github_refresh_token_encrypted:
                logger.info(f"Token expired for org {domain}, attempting refresh...")
                result = refresh_org_github_token(domain)
                return result["access_token"]
            else:
                raise TokenRefreshError("Token expired and no refresh token available. Please re-authenticate.")

    return config.github_token_encrypted


def get_github_credentials_for_domain(domain: str, slack_user_id: str = None) -> dict:
    """
    Get GitHub credentials for a domain, preferring org-level tokens.

    Args:
        domain: The organization domain.
        slack_user_id: Optional Slack user ID for fallback to user-level tokens.

    Returns:
        dict with keys: token, repo, source ('org' or 'user')

    Raises:
        ArticleGenerationError: If no valid credentials found.
    """
    normalized_domain = normalize_domain(domain) if domain else None

    # 1. Try org-level credentials first
    if normalized_domain:
        try:
            org = Organization.objects.get(domain=normalized_domain)
            config = getattr(org, 'content_config', None)
            if config and config.github_token_encrypted and config.github_repo:
                # Ensure token is valid (refresh if needed)
                fresh_token = ensure_valid_org_token(normalized_domain)
                logger.info(f"Using org-level GitHub credentials for {normalized_domain}")
                return {
                    'token': fresh_token,
                    'repo': config.github_repo,
                    'source': 'org',
                    'config': config,
                }
        except Organization.DoesNotExist:
            logger.debug(f"No organization found for domain {normalized_domain}")
        except (TokenRefreshError, ArticleGenerationError) as e:
            logger.warning(f"Org-level token issue for {normalized_domain}: {e}")
            # Fall through to user-level

    # 2. Fall back to user-level credentials (only if repo is relevant to requested domain)
    if slack_user_id:
        try:
            integration = UserIntegration.objects.get(slack_user_id=slack_user_id)
            if integration.github_access_token and integration.github_repo:
                # If a specific domain was requested, verify the user's repo is associated with it
                if normalized_domain:
                    repo_matches_domain = OrganizationContentConfig.objects.filter(
                        github_repo=integration.github_repo,
                        organization__domain=normalized_domain
                    ).exists()
                    if not repo_matches_domain:
                        logger.info(
                            f"User repo {integration.github_repo} is not associated with "
                            f"{normalized_domain}, skipping user-level fallback"
                        )
                        # Don't fall back — this repo isn't for the requested domain
                    else:
                        fresh_token = ensure_valid_token(slack_user_id)
                        logger.info(f"Using user-level GitHub credentials for {slack_user_id} (domain-verified)")
                        return {
                            'token': fresh_token,
                            'repo': integration.github_repo,
                            'source': 'user',
                            'integration': integration,
                        }
                else:
                    # No domain specified — allow user-level fallback (backward compat)
                    fresh_token = ensure_valid_token(slack_user_id)
                    logger.info(f"Using user-level GitHub credentials for {slack_user_id}")
                    return {
                        'token': fresh_token,
                        'repo': integration.github_repo,
                        'source': 'user',
                        'integration': integration,
                    }
        except UserIntegration.DoesNotExist:
            pass
        except TokenRefreshError as e:
            raise ArticleGenerationError(f"GitHub token refresh failed: {e}. Please re-authenticate.")

    oauth_url = build_github_oauth_url(domain, slack_user_id or '')
    raise ArticleGenerationError(
        f"No GitHub credentials found for domain '{domain}'. "
        f"Please connect GitHub: {oauth_url}"
    )


def trigger_article_generation(slack_user_id: str, article_request: dict) -> dict:
    """
    Trigger article generation via Content Factory.

    Args:
        slack_user_id: The Slack user ID requesting the article.
        article_request: Dictionary containing article parameters:
                         - domain (str)
                         - topic (str)
                         - target_keyword (str, optional)
                         - context (str, optional)

    Returns:
        dict: { "job_id": "...", "status": "queued", "message": "Generation started" }
    """
    domain = article_request.get('domain')

    # Get GitHub credentials (org-level preferred, domain-verified user-level fallback)
    creds = get_github_credentials_for_domain(domain, slack_user_id)
    fresh_token = creds['token']
    github_repo = creds['repo']
    logger.info(f"Using {creds['source']}-level GitHub credentials for article generation")

    # Fetch OrganizationContentConfig for artifacts
    # We match config by github_repo
    config = (
        OrganizationContentConfig.objects
        .select_related('organization')
        .filter(github_repo=github_repo)
        .first()
    )
    
    existing_artifacts = {}
    if config:
        if config.article_template: existing_artifacts['article_template'] = config.article_template
        if config.design_guide: existing_artifacts['design_guide'] = config.design_guide
        if config.resource_prompt: existing_artifacts['resource_prompt'] = config.resource_prompt
        if config.tech_stack: existing_artifacts['tech_stack'] = config.tech_stack
        if config.brand_name: existing_artifacts['brand_name'] = config.brand_name
        if config.article_path_pattern: existing_artifacts['article_path_pattern'] = config.article_path_pattern
        if config.registry_path: existing_artifacts['registry_path'] = config.registry_path
        if config.company_context: existing_artifacts['company_context'] = config.company_context

    # extract specific fields
    resolved_domain = normalize_domain(
        article_request.get('domain') or (
            config.organization.domain if config and getattr(config, "organization", None) else None
        )
    )
    topic = article_request.get('topic')
    target_keyword = article_request.get('target_keyword')
    context = article_request.get('context')

    if not resolved_domain:
        raise ArticleGenerationError("Domain is required.")

    # Check prerequisites: scan must have completed and articles must be scaffolded
    if not config or not config.scan_summary:
        raise ArticleGenerationError(
            f"PREREQUISITE_MISSING:scan:{resolved_domain}:"
            f"Repository must be scanned before writing articles. "
            f"Ask me to scan your codebase first."
        )

    if not config.articles_scaffolded:
        raise ArticleGenerationError(
            f"PREREQUISITE_MISSING:scaffold:{resolved_domain}:"
            f"Articles directory must be scaffolded before writing articles."
        )

    # Retrieve competitors and seed_keywords early for Auto-Write or Payload
    competitors = []
    seed_keywords = []
    if config and hasattr(config, 'organization'):
        competitors = config.organization.competitors or []
        seed_keywords = config.organization.seed_keywords or []

    # Auto-Write Mode: If topic is missing, we send empty topic/keyword
    # Content Factory will handle the research phase and callback with topic_selection
    if not topic:
        logger.info(f"Auto-Write Mode enabled for {resolved_domain}. Content Factory will perform research.")
        topic = ""
        target_keyword = ""

    # Auto-fill target_keyword from topic if missing
    if not target_keyword and topic:
        target_keyword = topic

    # Ensure CF receives strings, not nulls (CF requires string fields even in research mode)
    if topic is None:
        topic = ""
    if target_keyword is None:
        target_keyword = ""

    # 3. Prepare Payload (Strict Interface)
    # competitors is already set above

    payload = {
        "slack_user_id": slack_user_id,
        "github_repo": github_repo,
        "github_token": fresh_token,  # Use the refreshed token

        # Generated Content Parameters
        "domain": resolved_domain,
        "topic": topic, # Can be None/Empty for research mode
        "target_keyword": target_keyword,
        "context": context,
        "competitors": competitors,
        "seed_keywords": seed_keywords,

        # Backend injected data
        "existing_artifacts": existing_artifacts,
        "github_client_id": settings.GITHUB_OAUTH_CLIENT_ID,
        "github_client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
    }

    # 4. Call Content Factory
    content_factory_url = getattr(settings, 'CONTENT_FACTORY_URL', 'http://209.38.83.23:80')
    generate_endpoint = f"{content_factory_url.rstrip('/')}/api/pipeline/generate"
    
    api_key = getattr(settings, 'CONTENT_FACTORY_API_KEY', None)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-KEY"] = api_key

    # Debug logging
    masked_payload = payload.copy()
    if 'github_token' in masked_payload:
        masked_payload['github_token'] = '***'
    logger.info(f"Triggering article generation at {generate_endpoint} with payload: {masked_payload}")

    try:
        response = http_requests.post(
            generate_endpoint,
            json=payload,
            headers=headers,
            timeout=600  # allow CF to enqueue/return job id without premature timeout
        )
        
        if response.status_code in [200, 202]:
            data = response.json()
            job_id = data.get('job_id') or data.get('task_id')
            if not job_id:
                logger.warning("Content Factory returned success but no job_id")
                # If CF returns completed result immediately
                return {"job_id": "unknown", "status": "completed", "message": "Generation completed immediately (unexpected)"}
            # Create job record with request metadata for retry
            from core.models import ContentFactoryJob
            ContentFactoryJob.objects.create(
                job_id=job_id,
                domain=resolved_domain,
                slack_user_id=slack_user_id,
                status='queued',
                request_meta=article_request,  # Store original request
            )

            status_url = f"{content_factory_url.rstrip('/')}/api/pipeline/publish/status/{job_id}"
            return {
                "job_id": job_id,
                "status": "queued",
                "message": "Generation started",
                "job_status_url": status_url
            }
        elif response.status_code == 412:
            # Content Factory prerequisite check failed (fallback — our proactive check should catch this first)
            try:
                data = response.json()
                missing_step = data.get('missing_step', 'unknown')
                cf_message = data.get('message', 'Prerequisite step missing')
            except Exception:
                missing_step = 'unknown'
                cf_message = response.text
            raise ArticleGenerationError(
                f"PREREQUISITE_MISSING:{missing_step}:{resolved_domain}:{cf_message}"
            )
        else:
            logger.error(f"Content Factory generate failed: {response.text}")
            raise ArticleGenerationError(f"Content Factory returned {response.status_code}: {response.text}")

    except http_requests.exceptions.RequestException as e:
        logger.error(f"Failed to connect to Content Factory: {e}")
        raise ArticleGenerationError(f"Failed to trigger generation: {str(e)}")


def _handle_status_failure(job_id: str, result: dict):
    """
    Handle a failed job detected during status polling.
    Updates the local ContentFactoryJob and sends a Slack notification (once).
    """
    from core.models import ContentFactoryJob
    from integrations.services.slack import SlackService

    try:
        job = ContentFactoryJob.objects.get(job_id=job_id)
    except ContentFactoryJob.DoesNotExist:
        logger.warning(f"Status failure for unknown job {job_id}")
        return

    # Only notify once — skip if already in error state
    if job.status == 'error':
        return

    error_message = result.get('error') or result.get('error_message') or 'Unknown error'
    job.status = 'error'
    job.error_message = error_message
    job.save()
    logger.info(f"Updated job {job_id} to error: {error_message}")

    # Send Slack notification
    if job.slack_user_id:
        try:
            domain_display = f" for *{job.domain}*" if job.domain else ""
            slack_text = (
                f"The article generation pipeline encountered an error{domain_display}.\n\n"
                f"*Error:* {error_message}\n\n"
                f"You can try again by requesting a new article."
            )

            # If error suggests missing config/credentials, include OAuth URL
            if job.domain and ("no configuration" in error_message.lower() or "no github credentials" in error_message.lower()):
                oauth_url = build_github_oauth_url(job.domain, job.slack_user_id)
                slack_text += f"\n\n<{oauth_url}|Connect GitHub for {job.domain}>"

            SlackService.send_dm(job.slack_user_id, slack_text)
        except Exception as e:
            logger.warning(f"Failed to send failure notification for job {job_id}: {e}")


def check_generation_status(job_id: str) -> dict:
    """
    Check status of a generation job.

    Returns:
        dict: { "job_id": "...", "status": "...", "progress": int, "current_step": "...", "error": ... }
    """

    content_factory_url = getattr(settings, 'CONTENT_FACTORY_URL', 'http://209.38.83.23:80')
    status_endpoint_primary = f"{content_factory_url.rstrip('/')}/api/pipeline/publish/status/{job_id}"
    status_endpoint_legacy = f"{content_factory_url.rstrip('/')}/api/v1/content/jobs/{job_id}"

    api_key = getattr(settings, 'CONTENT_FACTORY_API_KEY', None)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-KEY"] = api_key

    try:
        response = http_requests.get(
            status_endpoint_primary,
            headers=headers,
            timeout=30
        )
        if response.status_code == 200:
            result = response.json()
            # Detect failure and update local state + notify user
            if result.get('status') in ('failed', 'error'):
                _handle_status_failure(job_id, result)
            return result
        # Fallback to legacy endpoint if primary fails (e.g., older CF deployment)
        response = http_requests.get(
            status_endpoint_legacy,
            headers=headers,
            timeout=30
        )
        if response.status_code == 200:
            result = response.json()
            if result.get('status') in ('failed', 'error'):
                _handle_status_failure(job_id, result)
            return result
        elif response.status_code == 404:
            raise ArticleGenerationError(f"Job not found: {job_id}")
        else:
            logger.error(f"Content Factory status check failed: {response.text}")
            raise ArticleGenerationError(f"Status check returned {response.status_code}")

    except http_requests.exceptions.RequestException as e:
        logger.error(f"Failed to connect to Content Factory: {e}")
        raise ArticleGenerationError(f"Failed to check status: {str(e)}")


def publish_article(job_id: str, slack_user_id: str, domain: str = None) -> dict:
    """
    Trigger publication (PR creation) for a job.

    Args:
        job_id: The Content Factory job ID.
        slack_user_id: The Slack user ID (used to lookup GitHub credentials).
        domain: Optional domain to use for org-level credentials.

    Returns:
        dict: { "status": "published", "preview_url": "...", "pr_url": "...", "branch_name": "..." }
    """
    # Get GitHub credentials (org-level preferred, domain-verified user-level fallback)
    creds = get_github_credentials_for_domain(domain, slack_user_id)
    github_token = creds['token']
    github_repo = creds['repo']
    logger.info(f"Using {creds['source']}-level GitHub credentials for publishing")

    # Prepare payload with GitHub credentials
    payload = {
        "github_token": github_token,
        "github_repo": github_repo,
    }

    # 3. Call Content Factory
    content_factory_url = getattr(settings, 'CONTENT_FACTORY_URL', 'http://209.38.83.23:80')
    publish_endpoint = f"{content_factory_url.rstrip('/')}/api/pipeline/publish/{job_id}"
    
    api_key = getattr(settings, 'CONTENT_FACTORY_API_KEY', None)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-KEY"] = api_key

    # Debug logging for troubleshooting
    masked_payload = payload.copy()
    if 'github_token' in masked_payload:
        masked_payload['github_token'] = '***'
    logger.info(f"Publishing article to: {publish_endpoint} with payload: {masked_payload}")

    try:
        response = http_requests.post(
            publish_endpoint,
            json=payload,
            headers=headers,
            timeout=120  # Publishing might take a moment (git ops)
        )
        
        if response.status_code == 200:
            return response.json()
        else:
            logger.error(f"Content Factory publish failed: {response.text}")
            raise ArticleGenerationError(f"Publish failed: {response.text}")
            
    except http_requests.exceptions.RequestException as e:
        logger.error(f"Failed to connect to Content Factory: {e}")
        raise ArticleGenerationError(f"Failed to publish: {str(e)}")


def confirm_topic(
    domain: str,
    confirmed_keyword: str,
    slack_user_id: str,
    custom_title: str = None,
    skip_alternatives: list = None
) -> dict:
    """
    Confirm topic selection and trigger Phase 2 generation.
    POST /api/pipeline/confirm-topic

    Args:
        domain: The organization domain.
        confirmed_keyword: The selected keyword (or alternative) to generate.
        slack_user_id: The Slack user confirming the topic.
        custom_title: Optional custom title override.
        skip_alternatives: List of keywords to send back as temporary rejections/cooldowns.
                          These are the alternatives that were shown but not selected.

    Returns:
        dict: { "job_id": "...", "status": "queued", ... }
    """
    # Get GitHub credentials (org-level preferred, domain-verified user-level fallback)
    creds = get_github_credentials_for_domain(domain, slack_user_id)
    fresh_token = creds['token']
    github_repo = creds['repo']
    logger.info(f"Using {creds['source']}-level GitHub credentials for topic confirmation")

    # Prepare payload with fresh token
    payload = {
        "domain": domain,
        "confirmed_keyword": confirmed_keyword,
        "slack_user_id": slack_user_id,
        "github_token": fresh_token,
        "github_repo": github_repo,
        "custom_title": custom_title,
    }

    # Include skip_alternatives if provided (temporary rejection/cooldown feedback)
    if skip_alternatives:
        payload["skip_alternatives"] = skip_alternatives

    # 3. Call Content Factory
    content_factory_url = getattr(settings, 'CONTENT_FACTORY_URL', 'http://209.38.83.23:80')
    confirm_endpoint = f"{content_factory_url.rstrip('/')}/api/pipeline/confirm-topic"
    
    api_key = getattr(settings, 'CONTENT_FACTORY_API_KEY', None)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-KEY"] = api_key

    # Debug logging
    masked_payload = payload.copy()
    if 'github_token' in masked_payload:
        masked_payload['github_token'] = '***'
    logger.info(f"Confirming topic at {confirm_endpoint} with payload: {masked_payload}")

    try:
        response = http_requests.post(
            confirm_endpoint,
            json=payload,
            headers=headers,
            timeout=30
        )
        
        if response.status_code in [200, 202]:
            return response.json()
        else:
            logger.error(f"Content Factory confirm topic failed: {response.text}")
            raise ArticleGenerationError(f"Topic confirmation failed: {response.text}")
            
    except http_requests.exceptions.RequestException as e:
        logger.error(f"Failed to connect to Content Factory: {e}")
        raise ArticleGenerationError(f"Failed to confirm topic: {str(e)}")
