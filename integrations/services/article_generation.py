import logging
import secrets
import urllib.parse
from typing import Optional
import requests as http_requests
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from core.article_system import (
    article_system_ready,
    resolve_article_system_with_source,
)
from integrations.models import UserIntegration
from core.models import OrganizationContentConfig, Organization
from integrations.utils import normalize_domain
from integrations.services.github import ensure_valid_token, TokenRefreshError

logger = logging.getLogger(__name__)

DEFAULT_ARTICLE_DELIVERY_MODE = "publish_code"
VALID_ARTICLE_DELIVERY_MODES = {"publish_code", "content_only"}
FAILURE_RUN_STATUSES = {"failed", "error", "blocked", "blocked_verification", "denied"}
APPROVAL_PENDING_STATUSES = {"approval_required", "awaiting_approval"}


class ArticleGenerationError(Exception):
    """Exception raised when article generation fails."""
    pass


class ArticleSystemActionRequiredError(ArticleGenerationError):
    """Raised when article writing is blocked on article-system readiness."""

    def __init__(
        self,
        *,
        domain: str,
        article_system: dict,
        recommended_action: str,
        hint: str,
        message: str,
        resolution_source: str,
    ):
        super().__init__(message)
        self.domain = domain
        self.article_system = article_system
        self.recommended_action = recommended_action
        self.hint = hint
        self.resolution_source = resolution_source


def _raise_article_system_action_required(
    resolved_domain: str,
    article_system: dict,
    *,
    resolution_source: str,
) -> None:
    state = article_system.get('state', 'missing')
    detected_location = article_system.get('directory_path') or article_system.get('directory_name') or 'the detected article directory'
    if state == 'ambiguous':
        raise ArticleSystemActionRequiredError(
            domain=resolved_domain,
            article_system=article_system,
            recommended_action='confirm_article_system',
            hint='Confirm the detected article system, rescan the repo, or scaffold a Roo-managed structure.',
            message=(
                f"I found what looks like an article system at {detected_location}, "
                f"but the detection confidence is low. Confirm it before writing."
            ),
            resolution_source=resolution_source,
        )

    raise ArticleSystemActionRequiredError(
        domain=resolved_domain,
        article_system=article_system,
        recommended_action='scaffold',
        hint='Scaffold an article system first, or rescan if the repo already contains one.',
        message='This repository needs an article system before Roo can write a concrete article into it.',
        resolution_source=resolution_source,
    )


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


def get_default_article_delivery_mode() -> str:
    """
    Resolve the default Content Factory article delivery mode.
    """
    raw_mode = str(
        getattr(
            settings,
            "CONTENT_FACTORY_DEFAULT_ARTICLE_DELIVERY_MODE",
            DEFAULT_ARTICLE_DELIVERY_MODE,
        )
        or DEFAULT_ARTICLE_DELIVERY_MODE
    ).strip().lower()

    if raw_mode not in VALID_ARTICLE_DELIVERY_MODES:
        logger.warning(
            "Ignoring invalid CONTENT_FACTORY_DEFAULT_ARTICLE_DELIVERY_MODE=%s",
            raw_mode,
        )
        return DEFAULT_ARTICLE_DELIVERY_MODE

    return raw_mode


def _store_job_tracking_record(
    job_id: str,
    *,
    domain: str,
    slack_user_id: str,
    request_meta: Optional[dict] = None,
    slack_channel_id: str = "",
    slack_thread_ts: str = "",
    slack_root_message_ts: str = "",
    default_status: str = "queued",
):
    from core.models import ContentFactoryJob

    resolved_root_message_ts = slack_root_message_ts or slack_thread_ts or ""

    defaults = {
        "domain": domain or "",
        "slack_user_id": slack_user_id or "",
        "status": default_status,
        "request_meta": request_meta or {},
        "slack_channel_id": slack_channel_id or "",
        "slack_thread_ts": slack_thread_ts or "",
        "slack_root_message_ts": resolved_root_message_ts,
    }
    job, created = ContentFactoryJob.objects.get_or_create(job_id=job_id, defaults=defaults)

    if created:
        return job

    update_fields = []
    if domain and job.domain != domain:
        job.domain = domain
        update_fields.append("domain")
    if slack_user_id and job.slack_user_id != slack_user_id:
        job.slack_user_id = slack_user_id
        update_fields.append("slack_user_id")
    if slack_channel_id and job.slack_channel_id != slack_channel_id:
        job.slack_channel_id = slack_channel_id
        update_fields.append("slack_channel_id")
    if slack_thread_ts and job.slack_thread_ts != slack_thread_ts:
        job.slack_thread_ts = slack_thread_ts
        update_fields.append("slack_thread_ts")
    if resolved_root_message_ts and job.slack_root_message_ts != resolved_root_message_ts:
        job.slack_root_message_ts = resolved_root_message_ts
        update_fields.append("slack_root_message_ts")
    if request_meta is not None:
        merged_request_meta = dict(job.request_meta or {})
        merged_request_meta.update(request_meta)
        if merged_request_meta != (job.request_meta or {}):
            job.request_meta = merged_request_meta
            update_fields.append("request_meta")

    if update_fields:
        update_fields.append("updated_at")
        job.save(update_fields=update_fields)

    return job


def _serialize_local_run_snapshot(run) -> dict:
    steps = {}
    for step in run.steps.order_by("display_order", "id"):
        steps[step.step_key] = {
            "name": step.step_key,
            "required": step.required,
            "status": step.status,
            "attempts": step.attempts,
            "message": step.message or None,
            "started_at": step.started_at.isoformat() if step.started_at else None,
            "completed_at": step.completed_at.isoformat() if step.completed_at else None,
            "artifacts": step.artifacts or [],
            "error": step.error or None,
            "latest_attempt_path": step.latest_attempt_path or None,
        }

    return {
        "job_id": run.run_id,
        "run_id": run.run_id,
        "workflow": run.workflow,
        "domain": run.domain,
        "github_repo": run.github_repo,
        "slack_user_id": run.slack_user_id,
        "status": run.status,
        "current_step": run.current_step,
        "approval_state": run.approval_state,
        "artifact_root": run.artifact_root,
        "step_order": run.step_order or [],
        "acceptance_summary": run.acceptance_summary or {},
        "verification_summary": run.verification_summary or {},
        "resume_available": run.resume_available,
        "error": run.error or None,
        "result": run.result or {},
        "run_request": run.run_request or {},
        "step_states": steps,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }


def _load_local_run_snapshot(job_id: str) -> Optional[dict]:
    from core.models import ContentFactoryRun

    run = ContentFactoryRun.objects.filter(run_id=job_id).first()
    if not run:
        return None
    return _serialize_local_run_snapshot(run)


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
    resolved_domain = normalize_domain(domain)

    config = None
    if resolved_domain:
        try:
            org = Organization.objects.get(domain=resolved_domain)
            config = getattr(org, 'content_config', None)
        except Organization.DoesNotExist:
            config = None
    
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
    slack_channel_id = article_request.get('slack_channel_id') or ''
    slack_thread_ts = article_request.get('slack_thread_ts') or ''
    slack_root_message_ts = article_request.get('slack_root_message_ts') or slack_thread_ts

    if not resolved_domain:
        raise ArticleGenerationError("Domain is required.")

    # Check prerequisites: scan must have completed before discovery or article generation.
    if not config or not config.scan_summary:
        raise ArticleGenerationError(
            f"PREREQUISITE_MISSING:scan:{resolved_domain}:"
            f"Repository must be scanned before writing articles. "
            f"Ask me to scan your codebase first."
        )

    # Retrieve competitors and seed_keywords early for Auto-Write or Payload
    competitors = []
    seed_keywords = []
    if config and hasattr(config, 'organization'):
        competitors = config.organization.competitors or []
        seed_keywords = config.organization.seed_keywords or []

    content_factory_url = getattr(settings, 'CONTENT_FACTORY_URL', 'http://209.38.83.23:80')
    api_key = getattr(settings, 'CONTENT_FACTORY_API_KEY', None)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-KEY"] = api_key

    # Research Mode: if topic is missing, use the dedicated discovery endpoint.
    if not topic:
        discovery_endpoint = f"{content_factory_url.rstrip('/')}/api/runs/discovery"
        payload = {
            "domain": resolved_domain,
            "slack_user_id": slack_user_id,
        }

        logger.info(f"Research mode enabled for {resolved_domain}. Triggering discovery at {discovery_endpoint}.")

        try:
            response = http_requests.post(
                discovery_endpoint,
                json=payload,
                headers=headers,
                timeout=600,
            )

            if response.status_code not in [200, 202]:
                logger.error(f"Content Factory discovery failed: {response.text}")
                raise ArticleGenerationError(f"Content Factory returned {response.status_code}: {response.text}")

            data = response.json()
            job_id = data.get('job_id') or data.get('task_id') or data.get('run_id')
            if not job_id:
                logger.warning("Content Factory returned discovery success but no job_id")
                return {
                    "job_id": "unknown",
                    "status": "completed",
                    "message": "Discovery completed immediately (unexpected)",
                }

            _store_job_tracking_record(
                job_id,
                domain=resolved_domain,
                slack_user_id=slack_user_id,
                request_meta=article_request,
                slack_channel_id=slack_channel_id,
                slack_thread_ts=slack_thread_ts,
                slack_root_message_ts=slack_root_message_ts,
                default_status="queued",
            )
            return {
                "job_id": job_id,
                "status": "queued",
                "message": "Discovery started",
                "job_status_url": f"{content_factory_url.rstrip('/')}/api/runs/{job_id}",
            }
        except http_requests.exceptions.RequestException as e:
            logger.error(f"Failed to connect to Content Factory discovery endpoint: {e}")
            raise ArticleGenerationError(f"Failed to trigger discovery: {str(e)}")

    article_system, article_system_source = resolve_article_system_with_source(config)
    logger.info(
        "Article-system readiness for %s resolved via %s: state=%s path=%s",
        resolved_domain,
        article_system_source,
        article_system.get('state'),
        article_system.get('directory_path') or article_system.get('directory_name'),
    )
    if not article_system_ready(article_system):
        _raise_article_system_action_required(
            resolved_domain,
            article_system,
            resolution_source=article_system_source,
        )

    creds = get_github_credentials_for_domain(domain, slack_user_id)
    fresh_token = creds['token']
    github_repo = creds['repo']
    logger.info(f"Using {creds['source']}-level GitHub credentials for article generation")

    if config is None:
        config = (
            OrganizationContentConfig.objects
            .select_related('organization')
            .filter(github_repo=github_repo)
            .first()
        )

    if config and not existing_artifacts:
        if config.article_template: existing_artifacts['article_template'] = config.article_template
        if config.design_guide: existing_artifacts['design_guide'] = config.design_guide
        if config.resource_prompt: existing_artifacts['resource_prompt'] = config.resource_prompt
        if config.tech_stack: existing_artifacts['tech_stack'] = config.tech_stack
        if config.brand_name: existing_artifacts['brand_name'] = config.brand_name
        if config.article_path_pattern: existing_artifacts['article_path_pattern'] = config.article_path_pattern
        if config.registry_path: existing_artifacts['registry_path'] = config.registry_path
        if config.company_context: existing_artifacts['company_context'] = config.company_context

    # Auto-fill target_keyword from topic if missing
    if not target_keyword and topic:
        target_keyword = topic

    # Ensure CF receives strings, not nulls (CF requires string fields even in research mode)
    if topic is None:
        topic = ""
    if target_keyword is None:
        target_keyword = ""

    # 4. Call Content Factory
    generate_endpoint = f"{content_factory_url.rstrip('/')}/api/runs/article"

    # Debug logging
    payload = {
        "domain": resolved_domain,
        "topic": topic,
        "target_keyword": target_keyword,
        "context": context,
        "slack_user_id": slack_user_id,
        "github_repo": github_repo,
        "competitors": competitors,
        "delivery_mode": get_default_article_delivery_mode(),
    }
    if article_request.get("custom_title"):
        payload["custom_title"] = article_request["custom_title"]
    if article_request.get("skip_alternatives"):
        payload["skip_alternatives"] = article_request["skip_alternatives"]
    if article_request.get("source_run_id"):
        payload["source_run_id"] = article_request["source_run_id"]

    masked_payload = payload.copy()
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
            job_id = data.get('job_id') or data.get('task_id') or data.get('run_id')
            if not job_id:
                logger.warning("Content Factory returned success but no job_id")
                # If CF returns completed result immediately
                return {"job_id": "unknown", "status": "completed", "message": "Generation completed immediately (unexpected)"}
            # Create or refresh job tracking without clobbering callback-driven state.
            _store_job_tracking_record(
                job_id,
                domain=resolved_domain,
                slack_user_id=slack_user_id,
                request_meta=article_request,
                slack_channel_id=slack_channel_id,
                slack_thread_ts=slack_thread_ts,
                slack_root_message_ts=slack_root_message_ts,
                default_status="queued",
            )

            status_url = f"{content_factory_url.rstrip('/')}/api/runs/{job_id}"
            return {
                "job_id": job_id,
                "run_id": job_id,
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


def set_article_delivery_mode(job_id: str, delivery_mode: Optional[str] = None) -> dict:
    """
    Select a delivery mode for an article run paused before queueing.
    """
    selected_mode = delivery_mode or get_default_article_delivery_mode()
    if selected_mode not in VALID_ARTICLE_DELIVERY_MODES:
        raise ArticleGenerationError(f"Unsupported delivery mode: {selected_mode}")

    content_factory_url = getattr(settings, 'CONTENT_FACTORY_URL', 'http://209.38.83.23:80')
    endpoint = f"{content_factory_url.rstrip('/')}/api/runs/{job_id}/delivery-mode"

    api_key = getattr(settings, 'CONTENT_FACTORY_API_KEY', None)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-KEY"] = api_key

    logger.info("Selecting article delivery mode %s for %s", selected_mode, job_id)

    try:
        response = http_requests.post(
            endpoint,
            json={"delivery_mode": selected_mode},
            headers=headers,
            timeout=120,
        )
        if response.status_code == 200:
            return response.json()

        logger.error(f"Content Factory delivery mode selection failed: {response.text}")
        raise ArticleGenerationError(f"Delivery mode selection failed: {response.text}")
    except http_requests.exceptions.RequestException as e:
        logger.error(f"Failed to connect to Content Factory: {e}")
        raise ArticleGenerationError(f"Failed to set delivery mode: {str(e)}")


def _maybe_auto_advance_run(job_id: str, result: dict) -> dict:
    status_value = str(result.get("status") or "").strip().lower()

    if status_value == "awaiting_delivery_mode":
        try:
            auto_result = set_article_delivery_mode(job_id)
            auto_result.setdefault("job_id", job_id)
            auto_result.setdefault("run_id", job_id)
            return auto_result
        except ArticleGenerationError as exc:
            logger.warning("Auto-selecting delivery mode for %s failed: %s", job_id, exc)
            return result

    if status_value in APPROVAL_PENDING_STATUSES:
        try:
            auto_result = publish_article(
                job_id,
                slack_user_id=result.get("slack_user_id"),
                domain=result.get("domain"),
            )
            auto_result.setdefault("job_id", job_id)
            auto_result.setdefault("run_id", job_id)
            return auto_result
        except ArticleGenerationError as exc:
            logger.warning("Auto-approving preview for %s failed: %s", job_id, exc)
            return result

    return result


def check_generation_status(job_id: str) -> dict:
    """
    Check status of a generation job.

    Returns:
        dict: { "job_id": "...", "status": "...", "progress": int, "current_step": "...", "error": ... }
    """

    content_factory_url = getattr(settings, 'CONTENT_FACTORY_URL', 'http://209.38.83.23:80')
    status_endpoint = f"{content_factory_url.rstrip('/')}/api/runs/{job_id}"
    status_endpoint_legacy = f"{content_factory_url.rstrip('/')}/api/pipeline/publish/status/{job_id}"
    status_endpoint_old_backend = f"{content_factory_url.rstrip('/')}/api/v1/content/jobs/{job_id}"

    api_key = getattr(settings, 'CONTENT_FACTORY_API_KEY', None)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-KEY"] = api_key

    try:
        response = http_requests.get(status_endpoint, headers=headers, timeout=30)
        if response.status_code == 200:
            result = _maybe_auto_advance_run(job_id, response.json())
            # Detect failure and update local state + notify user
            if result.get('status') in FAILURE_RUN_STATUSES:
                _handle_status_failure(job_id, result)
            return result
        for fallback_endpoint in (status_endpoint_legacy, status_endpoint_old_backend):
            response = http_requests.get(
                fallback_endpoint,
                headers=headers,
                timeout=30
            )
            if response.status_code == 200:
                result = _maybe_auto_advance_run(job_id, response.json())
                if result.get('status') in FAILURE_RUN_STATUSES:
                    _handle_status_failure(job_id, result)
                return result

        local_result = _load_local_run_snapshot(job_id)
        if local_result:
            if local_result.get("status") in FAILURE_RUN_STATUSES:
                _handle_status_failure(job_id, local_result)
            return local_result

        if response.status_code == 404:
            raise ArticleGenerationError(f"Job not found: {job_id}")
        logger.error(f"Content Factory status check failed: {response.text}")
        raise ArticleGenerationError(f"Status check returned {response.status_code}")

    except http_requests.exceptions.RequestException as e:
        local_result = _load_local_run_snapshot(job_id)
        if local_result:
            logger.warning(
                "Content Factory unreachable during status check for %s; returning mirrored local run",
                job_id,
            )
            if local_result.get("status") in FAILURE_RUN_STATUSES:
                _handle_status_failure(job_id, local_result)
            return local_result

        logger.error(f"Failed to connect to Content Factory: {e}")
        raise ArticleGenerationError(f"Failed to check status: {str(e)}")


def publish_article(job_id: str, slack_user_id: str = None, domain: str = None) -> dict:
    """
    Trigger publication (PR creation) for a job.

    Args:
        job_id: The Content Factory job ID.
        slack_user_id: The Slack user ID (used to lookup GitHub credentials).
        domain: Optional domain to use for org-level credentials.

    Returns:
        dict: { "status": "published", "preview_url": "...", "pr_url": "...", "branch_name": "..." }
    """
    # Publishing is now an approval transition on an existing run.
    content_factory_url = getattr(settings, 'CONTENT_FACTORY_URL', 'http://209.38.83.23:80')
    publish_endpoint = f"{content_factory_url.rstrip('/')}/api/runs/{job_id}/approve"
    
    api_key = getattr(settings, 'CONTENT_FACTORY_API_KEY', None)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-KEY"] = api_key

    logger.info(f"Publishing article to: {publish_endpoint}")

    try:
        response = http_requests.post(
            publish_endpoint,
            json={},
            headers=headers,
            timeout=120,
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
    skip_alternatives: list = None,
    source_run_id: str = None,
    slack_channel_id: str = "",
    slack_thread_ts: str = "",
    slack_root_message_ts: str = "",
) -> dict:
    """
    Confirm topic selection and trigger Phase 2 generation.
    POST /api/runs/article

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
    normalized_domain = normalize_domain(domain)
    if source_run_id and not (slack_channel_id or slack_thread_ts or slack_root_message_ts):
        from core.models import ContentFactoryJob

        source_job = ContentFactoryJob.objects.filter(job_id=source_run_id).first()
        if source_job:
            slack_channel_id = source_job.slack_channel_id or ""
            slack_thread_ts = source_job.slack_thread_ts or ""
            slack_root_message_ts = source_job.slack_root_message_ts or slack_thread_ts

    config = None
    try:
        org = Organization.objects.get(domain=normalized_domain)
        config = getattr(org, 'content_config', None)
    except Organization.DoesNotExist:
        config = None

    if not config or not config.scan_summary:
        raise ArticleGenerationError(
            f"PREREQUISITE_MISSING:scan:{normalized_domain}:"
            f"Repository must be scanned before writing articles. "
            f"Ask me to scan your codebase first."
        )

    article_system, article_system_source = resolve_article_system_with_source(config)
    logger.info(
        "Confirmed-topic article-system readiness for %s resolved via %s: state=%s path=%s",
        normalized_domain,
        article_system_source,
        article_system.get('state'),
        article_system.get('directory_path') or article_system.get('directory_name'),
    )
    if not article_system_ready(article_system):
        _raise_article_system_action_required(
            normalized_domain,
            article_system,
            resolution_source=article_system_source,
        )

    # Get GitHub repo context (server-side credentials stay in mlai-backend/content-factory)
    creds = get_github_credentials_for_domain(domain, slack_user_id)
    github_repo = creds['repo']
    logger.info(f"Using {creds['source']}-level GitHub repo context for topic confirmation")

    payload = {
        "domain": domain,
        "slack_user_id": slack_user_id,
        "github_repo": github_repo,
        "topic": custom_title or confirmed_keyword,
        "target_keyword": confirmed_keyword,
        "custom_title": custom_title,
        "delivery_mode": get_default_article_delivery_mode(),
    }

    # Include skip_alternatives if provided (temporary rejection/cooldown feedback)
    if skip_alternatives:
        payload["skip_alternatives"] = skip_alternatives
    if source_run_id:
        payload["source_run_id"] = source_run_id

    # 3. Call Content Factory
    content_factory_url = getattr(settings, 'CONTENT_FACTORY_URL', 'http://209.38.83.23:80')
    confirm_endpoint = f"{content_factory_url.rstrip('/')}/api/runs/article"
    
    api_key = getattr(settings, 'CONTENT_FACTORY_API_KEY', None)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-KEY"] = api_key

    # Debug logging
    masked_payload = payload.copy()
    logger.info(f"Confirming topic at {confirm_endpoint} with payload: {masked_payload}")

    try:
        response = http_requests.post(
            confirm_endpoint,
            json=payload,
            headers=headers,
            timeout=30
        )
        
        if response.status_code in [200, 202]:
            data = response.json()
            if data.get('run_id') and not data.get('job_id'):
                data['job_id'] = data['run_id']
            job_id = data.get("job_id") or data.get("run_id")
            if job_id:
                _store_job_tracking_record(
                    job_id,
                    domain=normalized_domain,
                    slack_user_id=slack_user_id,
                    request_meta={
                        "domain": normalized_domain,
                        "topic": custom_title or confirmed_keyword,
                        "target_keyword": confirmed_keyword,
                        "custom_title": custom_title,
                        "skip_alternatives": skip_alternatives or [],
                        "source_run_id": source_run_id,
                        "slack_channel_id": slack_channel_id or "",
                        "slack_thread_ts": slack_thread_ts or "",
                        "slack_root_message_ts": slack_root_message_ts or slack_thread_ts or "",
                    },
                    slack_channel_id=slack_channel_id,
                    slack_thread_ts=slack_thread_ts,
                    slack_root_message_ts=slack_root_message_ts,
                    default_status="queued",
                )
            return data
        else:
            logger.error(f"Content Factory confirm topic failed: {response.text}")
            raise ArticleGenerationError(f"Topic confirmation failed: {response.text}")
            
    except http_requests.exceptions.RequestException as e:
        logger.error(f"Failed to connect to Content Factory: {e}")
        raise ArticleGenerationError(f"Failed to confirm topic: {str(e)}")
