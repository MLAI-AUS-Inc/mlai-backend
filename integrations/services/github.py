"""
GitHub integration services.
"""
import logging
import requests as http_requests
from django.conf import settings

from integrations.models import UserIntegration

logger = logging.getLogger(__name__)


class ScanError(Exception):
    """Exception raised when a scan fails."""
    pass


def scan_github_project(slack_user_id: str, integration: UserIntegration = None) -> dict:
    """
    Trigger a repository scan via Content Factory.

    Args:
        slack_user_id: The Slack user ID to scan for.
        integration: Optional pre-fetched UserIntegration instance.

    Returns:
        dict: Response data from Content Factory.

    Raises:
        ScanError: If validation fails or the external call fails.
    """
    # Fetch integration if not provided
    if integration is None:
        try:
            integration = UserIntegration.objects.get(slack_user_id=slack_user_id)
        except UserIntegration.DoesNotExist:
            raise ScanError("No integration found for this user. Please connect GitHub first.")

    # Validate token
    if not integration.github_access_token:
        raise ScanError("No GitHub token found. Please authenticate with GitHub first.")

    # Validate repo
    if not integration.github_repo:
        raise ScanError("No GitHub repository configured for this user.")

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

    # Get latest commit SHA BEFORE scan to ensure we track what we scanned
    current_sha = None
    try:
        current_sha = get_latest_repo_sha(integration.github_access_token, integration.github_repo)
    except Exception as e:
        logger.warning(f"Failed to fetch latest SHA for {integration.github_repo}: {e}")

    # Prepare existing artifacts if available
    existing_artifacts = {}
    try:
        from core.models import OrganizationContentConfig
        # Try to find config by repo name (stored as github_repo in Config)
        config = OrganizationContentConfig.objects.filter(github_repo=integration.github_repo).first()
        if config:
            if config.article_template: existing_artifacts['article_template'] = config.article_template
            if config.design_guide: existing_artifacts['design_guide'] = config.design_guide
            if config.resource_prompt: existing_artifacts['resource_prompt'] = config.resource_prompt
            if config.tech_stack: existing_artifacts['tech_stack'] = config.tech_stack
    except Exception as e:
        logger.warning(f"Failed to fetch existing artifacts for payload: {e}")

    try:
        cf_response = http_requests.post(
            scan_endpoint,
            json={
                "slack_user_id": slack_user_id,
                "github_repo": integration.github_repo,
                "github_token": integration.github_access_token,
                "github_client_id": settings.GITHUB_OAUTH_CLIENT_ID,
                "github_client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
                "existing_artifacts": existing_artifacts,
            },
            headers=headers,
            timeout=1200,
        )
        cf_response.raise_for_status()
        cf_data = cf_response.json()
    except http_requests.exceptions.RequestException as e:
        logger.error(f"Content Factory scan request failed: {e}")
        if hasattr(e, 'response') and e.response is not None:
             logger.error(f"Response status: {e.response.status_code}")
             logger.error(f"Response body: {e.response.text}")
        raise ScanError(f"Failed to trigger scan: {str(e)}")

    # Update project_scanned status and tracking info
    from django.utils import timezone
    integration.project_scanned = True
    if current_sha:
        integration.last_scanned_sha = current_sha
        integration.last_scanned_at = timezone.now()
    integration.save()

    # Save scan artifacts to OrganizationContentConfig
    try:
        from core.models import Organization, OrganizationContentConfig
        
        # Ensure Organization exists (idempotent, keyed by domain/repo name)
        # We use repo name as domain for now since we don't have a better unique ID
        org_name = integration.github_user_name or "Unknown User"
        org_domain = integration.github_repo
        
        org, _ = Organization.objects.get_or_create(
            domain=org_domain,
            defaults={"name": org_name}
        )

        # Ensure Config exists
        config, _ = OrganizationContentConfig.objects.get_or_create(organization=org)

        # Update fields from scan response
        cf_config = cf_data.get('config', {})
        
        config.github_repo = integration.github_repo
        config.github_token_encrypted = integration.github_access_token 
        
        # Save artifacts if present
        if 'article_template' in cf_config:
            config.article_template = cf_config['article_template']
        
        if 'design_guide' in cf_config:
            config.design_guide = cf_config['design_guide']
            
        if 'resource_prompt' in cf_config:
            config.resource_prompt = cf_config['resource_prompt']
            
        if 'scan_summary' in cf_data:
            config.scan_summary = cf_data['scan_summary']
        elif 'scan_summary' in cf_config:
             config.scan_summary = cf_config['scan_summary']
             
        if 'tech_stack' in cf_config:
            config.tech_stack = cf_config['tech_stack']
            
        config.save()
        logger.info(f"Updated OrganizationContentConfig for {org_domain}")

    except Exception as e:
        logger.error(f"Failed to save scan artifacts to OrganizationContentConfig: {e}")

    logger.info(f"Scan triggered successfully for {slack_user_id}, repo: {integration.github_repo}, SHA: {current_sha}")

    return {
        "status": "scan_triggered",
        "slack_user_id": slack_user_id,
        "github_repo": integration.github_repo,
        "scanned_sha": current_sha,
        "content_factory_response": cf_data,
    }


def get_latest_repo_sha(token: str, repo_name: str) -> str:
    """
    Fetch the latest commit SHA for the default branch (usually main/master).
    """
    if not token or not repo_name:
        raise ValueError("Token and repo_name required")

    url = f"https://api.github.com/repos/{repo_name}/commits/main" # default to main, fallback to master if needed
    
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


def trigger_scan_async(slack_user_id: str):
    """
    Trigger a scan in a background thread.
    Logs errors instead of raising them (fire-and-forget).
    """
    import threading
    from integrations.services.slack import SlackService

    def _run_scan():
        # Notify start
        SlackService.send_dm(slack_user_id, "🔍 GitHub connected! I'm starting a scan of your repository to understand the project structure...")
        
        try:
            result = scan_github_project(slack_user_id)
            repo_name = result.get('github_repo', 'your repo')
            logger.info(f"Background scan completed: {result}")
            
            # Notify success
            SlackService.send_dm(
                slack_user_id, 
                f"✅ Scan complete for `{repo_name}`! I've analyzed your codebase and I'm ready to help. You can now ask me to create blog pages or other content."
            )
        except ScanError as e:
            logger.error(f"Background scan failed for {slack_user_id}: {e}")
            SlackService.send_dm(slack_user_id, f"❌ Scan failed: {str(e)}")
        except Exception as e:
            logger.exception(f"Unexpected error in background scan for {slack_user_id}: {e}")
            SlackService.send_dm(slack_user_id, "❌ An unexpected error occurred while scanning your repository.")

    thread = threading.Thread(target=_run_scan, daemon=True)
    thread.start()
    logger.info(f"Triggered background scan for {slack_user_id}")
