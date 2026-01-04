import logging
import requests as http_requests
from django.conf import settings
from integrations.models import UserIntegration
from core.models import OrganizationContentConfig

logger = logging.getLogger(__name__)

class ArticleGenerationError(Exception):
    """Exception raised when article generation fails."""
    pass

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
    # 1. Fetch UserIntegration for GitHub credentials
    try:
        integration = UserIntegration.objects.get(slack_user_id=slack_user_id)
    except UserIntegration.DoesNotExist:
        raise ArticleGenerationError("No integration found for this user. Please connect GitHub first.")

    if not integration.github_access_token:
        raise ArticleGenerationError("No GitHub token found. Please authenticate with GitHub first.")
    
    if not integration.github_repo:
        raise ArticleGenerationError("No GitHub repository configured for this user.")

    # 2. Fetch OrganizationContentConfig for artifacts
    # We match config by github_repo
    config = OrganizationContentConfig.objects.filter(github_repo=integration.github_repo).first()
    
    existing_artifacts = {}
    if config:
        if config.article_template: existing_artifacts['article_template'] = config.article_template
        if config.design_guide: existing_artifacts['design_guide'] = config.design_guide
        if config.resource_prompt: existing_artifacts['resource_prompt'] = config.resource_prompt
        if config.tech_stack: existing_artifacts['tech_stack'] = config.tech_stack
        if config.brand_name: existing_artifacts['brand_name'] = config.brand_name
        if config.article_path_pattern: existing_artifacts['article_path_pattern'] = config.article_path_pattern
        if config.registry_path: existing_artifacts['registry_path'] = config.registry_path

    # extract specific fields
    domain = article_request.get('domain')
    topic = article_request.get('topic')
    target_keyword = article_request.get('target_keyword')
    context = article_request.get('context')

    if not domain or not topic:
         raise ArticleGenerationError("Domain and Topic are required.")

    # 3. Prepare Payload (Strict Interface)
    payload = {
        "slack_user_id": slack_user_id,
        "github_repo": integration.github_repo,
        "github_token": integration.github_access_token,
        # Flattened fields as per user request example? 
        # The user request showed:
        # {
        #   "slack_user_id": "...",
        #   "domain": "...",
        #   "topic": "...",
        #   ...
        # }
        # AND check behavior: "Call content-factory... using retrieved integration details"
        # I'll construct the payload that CONTENT FACTORY expects.
        # Assuming Content Factory expects the structure I planned before BUT including specific fields.
        
        # Let's map the user request specific fields to what CF likely needs.
        # If CF endpoint is /api/pipeline/generate, I should adhere to its schema.
        # User request to ME (mlai-backend) has specific fields.
        # Logic says: "Call content-factory's /api/pipeline/generate endpoint"
        # I will send a superset payload to CF to be safe, or structure it nicely.
        
        # Payload for Content Factory:
        "domain": domain,
        "topic": topic,
        "target_keyword": target_keyword,
        "context": context,
        
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

    try:
        response = http_requests.post(
            generate_endpoint,
            json=payload,
            headers=headers,
            timeout=30 
        )
        
        if response.status_code in [200, 202]:
            data = response.json()
            job_id = data.get('job_id')
            if not job_id:
                logger.warning("Content Factory returned success but no job_id")
                # If CF returns completed result immediately
                return {"job_id": "unknown", "status": "completed", "message": "Generation completed immediately (unexpected)"}
            
            return {
                "job_id": job_id,
                "status": "queued",
                "message": "Generation started"
            }
        else:
            logger.error(f"Content Factory generate failed: {response.text}")
            raise ArticleGenerationError(f"Content Factory returned {response.status_code}: {response.text}")

    except http_requests.exceptions.RequestException as e:
        logger.error(f"Failed to connect to Content Factory: {e}")
        raise ArticleGenerationError(f"Failed to trigger generation: {str(e)}")


def check_generation_status(job_id: str) -> dict:
    """
    Check status of a generation job.
    
    Returns:
        dict: { "job_id": "...", "status": "...", "progress": int, "current_step": "...", "error": ... }
    """

    content_factory_url = getattr(settings, 'CONTENT_FACTORY_URL', 'http://209.38.83.23:80')
    status_endpoint = f"{content_factory_url.rstrip('/')}/api/v1/content/jobs/{job_id}"
    
    api_key = getattr(settings, 'CONTENT_FACTORY_API_KEY', None)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-KEY"] = api_key

    try:
        response = http_requests.get(
            status_endpoint,
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 404:
            raise ArticleGenerationError(f"Job not found: {job_id}")
        else:
            logger.error(f"Content Factory status check failed: {response.text}")
            raise ArticleGenerationError(f"Status check returned {response.status_code}")
            
    except http_requests.exceptions.RequestException as e:
        logger.error(f"Failed to connect to Content Factory: {e}")
        raise ArticleGenerationError(f"Failed to check status: {str(e)}")


def publish_article(job_id: str, slack_user_id: str) -> dict:
    """
    Trigger publication (PR creation) for a job.
    
    Args:
        job_id: The Content Factory job ID.
        slack_user_id: The Slack user ID (used to lookup GitHub credentials).
    
    Returns:
        dict: { "status": "published", "preview_url": "...", "pr_url": "...", "branch_name": "..." }
    """
    # 1. Fetch UserIntegration for GitHub credentials
    try:
        integration = UserIntegration.objects.get(slack_user_id=slack_user_id)
    except UserIntegration.DoesNotExist:
        raise ArticleGenerationError("No integration found for this user. Please connect GitHub first.")

    if not integration.github_access_token:
        raise ArticleGenerationError("No GitHub token found. Please authenticate with GitHub first.")
    
    if not integration.github_repo:
        raise ArticleGenerationError("No GitHub repository configured for this user.")

    # 2. Prepare payload with GitHub credentials
    payload = {
        "github_token": integration.github_access_token,
        "github_repo": integration.github_repo,
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

