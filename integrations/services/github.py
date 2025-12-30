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

    try:
        cf_response = http_requests.post(
            scan_endpoint,
            json={
                "slack_user_id": slack_user_id,
                "github_repo": integration.github_repo,
                "github_token": integration.github_access_token,
                "github_client_id": settings.GITHUB_OAUTH_CLIENT_ID,
                "github_client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
            },
            headers=headers,
            timeout=600,
        )
        cf_response.raise_for_status()
        cf_data = cf_response.json()
    except http_requests.exceptions.RequestException as e:
        logger.error(f"Content Factory scan request failed: {e}")
        if hasattr(e, 'response') and e.response is not None:
             logger.error(f"Response status: {e.response.status_code}")
             logger.error(f"Response body: {e.response.text}")
        raise ScanError(f"Failed to trigger scan: {str(e)}")

    # Update project_scanned status
    integration.project_scanned = True
    integration.save()

    logger.info(f"Scan triggered successfully for {slack_user_id}, repo: {integration.github_repo}")

    return {
        "status": "scan_triggered",
        "slack_user_id": slack_user_id,
        "github_repo": integration.github_repo,
        "content_factory_response": cf_data,
    }


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
