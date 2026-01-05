import time
import logging
import requests as http_requests
from django.conf import settings

from integrations.models import UserIntegration

logger = logging.getLogger(__name__)


class ScanError(Exception):
    """Exception raised when a scan fails."""
    pass


def scan_github_project(slack_user_id: str, integration: UserIntegration = None, progress_callback=None) -> dict:
    """
    Trigger a repository scan via Content Factory.

    Args:
        slack_user_id: The Slack user ID to scan for.
        integration: Optional pre-fetched UserIntegration instance.
        progress_callback: Optional function(msg: str) to report progress.

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

    cf_data = None
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
                         raise ScanError(f"Scan failed immediately: {data.get('error')}")
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
                            raise ScanError(f"Remote scan job failed: {error_detail}")
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
        # Support both nested 'config' key (legacy) and top-level keys (current)
        cf_config = cf_data.get('config', {})
        
        config.github_repo = integration.github_repo
        config.github_token_encrypted = integration.github_access_token 
        
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
        
        # Save additional metadata if present
        if 'article_path_pattern' in cf_data:
            config.article_path_pattern = cf_data.get('article_path_pattern')
        if 'registry_path' in cf_data:
            config.registry_path = cf_data.get('registry_path')
            
        config.save()
        logger.info(f"Updated OrganizationContentConfig for {org_domain}")

    except Exception as e:
        logger.error(f"Failed to save scan artifacts to OrganizationContentConfig: {e}")

    logger.info(f"Scan triggered successfully for {slack_user_id}, repo: {integration.github_repo}, SHA: {current_sha}")

    return {
        "status": "scan_completed",
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
    All progress updates are sent as threaded replies to the initial message.
    """
    import threading
    from integrations.services.slack import SlackService

    def _run_scan():
        # Notify start and capture thread_ts
        success, thread_ts = SlackService.send_dm(
            slack_user_id, 
            "🔍 I'm starting a deeper scan of your repository to understand the project structure..."
        )
        
        if not success:
            logger.error(f"Failed to send initial scan DM for {slack_user_id}")
            return
        
        def _progress_listener(msg):
            # Helper to send concise progress updates as threaded replies
            SlackService.send_dm(slack_user_id, msg, thread_ts=thread_ts)
        
        try:
            # Pass the listener to report progress
            result = scan_github_project(slack_user_id, progress_callback=_progress_listener)
            
            repo_name = result.get('github_repo', 'your repo')
            logger.info(f"Background scan completed: {result}")
            
            # Notify success (threaded)
            SlackService.send_dm(
                slack_user_id, 
                f"✅ Scan complete for `{repo_name}`! I've analyzed your codebase and I'm ready to help. You can now ask me to create blog pages or other content.",
                thread_ts=thread_ts
            )
        except ScanError as e:
            logger.error(f"Background scan failed for {slack_user_id}: {e}")
            SlackService.send_dm(slack_user_id, f"❌ Scan failed: {str(e)}", thread_ts=thread_ts)
        except Exception as e:
            logger.exception(f"Unexpected error in background scan for {slack_user_id}: {e}")
            SlackService.send_dm(slack_user_id, "❌ An unexpected error occurred while scanning your repository.", thread_ts=thread_ts)

    thread = threading.Thread(target=_run_scan, daemon=True)
    thread.start()
    logger.info(f"Triggered background scan for {slack_user_id}")

