import logging
import time
from typing import Optional
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from content_factory.article_system import (
    resolve_article_system_with_source,
)
from content_factory.models import ContentFactoryJob, OrganizationContentConfig
from content_factory.progress import upsert_live_progress_card
from organizations.models import Organization
from integrations.models import UserIntegration
from integrations.utils import normalize_domain
from integrations.services.github import ensure_valid_token, refresh_github_token, TokenRefreshError
from integrations.services.github_connections import build_github_oauth_url
from content_factory.contract import (
    CONTENT_FACTORY_REQUEST_SOURCE,
    require_roo_request_source,
)
from integrations import http_client as http_requests

logger = logging.getLogger(__name__)

DEFAULT_ARTICLE_DELIVERY_MODE = "publish_code"
VALID_ARTICLE_DELIVERY_MODES = {"publish_code", "content_only"}
FAILURE_RUN_STATUSES = {"failed", "error", "denied"}
BLOCKED_RUN_STATUSES = {"blocked", "blocked_verification"}
REVIEWABLE_RUN_STATUSES = {"pr_opened", "needs_review"}
APPROVAL_PENDING_STATUSES = {"approval_required", "awaiting_approval"}
CONTENT_FACTORY_ARTICLE_COST_POINTS = 4
FREE_CONTENT_FACTORY_DOMAINS = {"mlai.au"}
CONTENT_FACTORY_LEDGER_SOURCE = "CONTENT_FACTORY"
CONTENT_FACTORY_BILLING_STATUS_CHARGED = "charged"
CONTENT_FACTORY_BILLING_STATUS_REUSED = "reused"
CONTENT_FACTORY_BILLING_STATUS_REFUNDED = "refunded"
CONTENT_FACTORY_BILLING_STATUS_DEFERRED = "deferred"
SCHEDULED_DAILY_TRIGGER_SOURCE = "scheduled_daily"
AUTO_REFUND_ERROR_CODES = {
    "NO_OPPORTUNITIES",
    "PUBLISH_TARGET_ACTION_REQUIRED",
}
AUTH_REQUIRED_ERROR_CODE = "AUTH_REQUIRED"
AUTH_REQUIRED_MISSING_STEP = "github_auth"
AUTH_REQUIRED_NEXT_ACTION = "reconnect_github"
AUTH_REQUIRED_RESUME_HINT = "reconnect_github_then_retry"
ARTICLE_DEPENDENCY_STRATEGY_UNRESOLVED_ERROR_CODE = "article_dependency_strategy_unresolved"
CONTENT_FACTORY_FAST_QUEUE_CONNECT_TIMEOUT_SECONDS = 3
CONTENT_FACTORY_FAST_QUEUE_READ_TIMEOUT_SECONDS = 8
CONTENT_FACTORY_FAST_QUEUE_RETRY_COUNT = 1
CONTENT_FACTORY_FAST_QUEUE_RETRYABLE_STATUSES = {502, 503, 504}
CONTENT_FACTORY_TERMINAL_JOB_STATUSES = {"completed", "error", "cancelled"}
CONTENT_FACTORY_BACKEND_UNAVAILABLE_ERROR_CODE = "CONTENT_FACTORY_UNAVAILABLE"


class ArticleGenerationError(Exception):
    """Exception raised when article generation fails."""
    pass


class GitHubReconnectRequiredError(ArticleGenerationError):
    """Raised when GitHub must be reconnected before the workflow can continue."""

    def __init__(self, payload: dict):
        self.payload = dict(payload or {})
        super().__init__(self.payload.get("message") or "GitHub authentication is required.")


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


class ContentFactoryBackendUnavailableError(ArticleGenerationError):
    """Raised when mlai-backend cannot reliably reach Content Factory."""

    def __init__(self, payload: dict):
        self.payload = dict(payload or {})
        super().__init__(
            self.payload.get("message") or "Content Factory is unavailable right now."
        )


def _coerce_optional_int(value) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _blocked_notification_key(
    *,
    error_code: str,
    blocked_step: str,
    recovery_attempt: Optional[int],
    recovery_exhausted: bool,
) -> str:
    return (
        f"blocked:{error_code}:{blocked_step}:{int(recovery_attempt or 0)}:"
        f"{1 if recovery_exhausted else 0}"
    )


def _blocked_summary_text(
    *,
    error_code: str,
    blocked_step: str,
    error_message: str,
    retry_after_seconds: Optional[int],
    recovery_attempt: Optional[int],
    recovery_exhausted: bool,
) -> str:
    if error_code == ARTICLE_DEPENDENCY_STRATEGY_UNRESOLVED_ERROR_CODE:
        if recovery_exhausted:
            return (
                f"Run blocked at {blocked_step}: repository contract refresh did not resolve the "
                "article dependency strategy. Run a fresh repo scan, then retry the article."
            )
        if recovery_attempt:
            return (
                f"Run blocked at {blocked_step}: repository contract ambiguity detected. "
                "Refreshing the repository contract automatically."
            )

    retry_suffix = ""
    if retry_after_seconds is not None:
        retry_suffix = f" Retrying when capacity returns (next check in ~{retry_after_seconds}s)."
    return f"Run blocked at {blocked_step}: {error_message}{retry_suffix}"


def _blocked_visible_message(*, domain: Optional[str]) -> str:
    subject = domain or "this repository"
    return (
        f"I refreshed the repository contract once, but the article surface is still ambiguous for "
        f"{subject}. Run a fresh repo scan, then retry the article."
    )


def _maybe_notify_exhausted_block(
    job: ContentFactoryJob,
    *,
    error_code: str,
    blocked_step: str,
    recovery_attempt: Optional[int],
    recovery_exhausted: bool,
) -> None:
    if error_code != ARTICLE_DEPENDENCY_STRATEGY_UNRESOLVED_ERROR_CODE or not recovery_exhausted:
        return
    if not job.slack_user_id:
        return

    request_meta = dict(job.request_meta or {})
    notification_key = _blocked_notification_key(
        error_code=error_code,
        blocked_step=blocked_step,
        recovery_attempt=recovery_attempt,
        recovery_exhausted=recovery_exhausted,
    )
    if request_meta.get("blocked_visible_notification_key") == notification_key:
        return

    from integrations.services.slack import SlackService

    thread_ts = str(job.slack_thread_ts or job.slack_root_message_ts or "").strip()
    text = _blocked_visible_message(domain=job.domain)
    try:
        if job.slack_channel_id and thread_ts:
            SlackService.send_message(job.slack_channel_id, text, thread_ts=thread_ts)
        else:
            SlackService.send_dm(job.slack_user_id, text)
    except Exception as exc:
        logger.warning("Failed to send exhausted blocked notification for job %s: %s", job.job_id, exc)
        return

    request_meta["blocked_visible_notification_key"] = notification_key
    job.request_meta = request_meta
    job.save(update_fields=["request_meta", "updated_at"])


def sync_blocked_job_state(
    job: ContentFactoryJob,
    result: dict,
    *,
    update_card: bool = True,
    allow_visible_notification: bool = True,
) -> None:
    error_message = result.get('error') or result.get('error_message') or 'Generation is blocked waiting for capacity'
    error_code = str(result.get("error_code") or "blocked").strip() or "blocked"
    blocked_step = str(result.get("blocked_step") or result.get("current_step") or "unknown").strip() or "unknown"
    retry_after_seconds = _coerce_optional_int(result.get("retry_after_seconds"))
    recovery_attempt = _coerce_optional_int(result.get("recovery_attempt"))
    recovery_exhausted = bool(result.get("recovery_exhausted"))

    update_fields = []
    result_domain = str(result.get("domain") or "").strip()
    if result_domain and job.domain != result_domain:
        job.domain = result_domain
        update_fields.append("domain")
    result_slack_user_id = str(result.get("slack_user_id") or "").strip()
    if result_slack_user_id and job.slack_user_id != result_slack_user_id:
        job.slack_user_id = result_slack_user_id
        update_fields.append("slack_user_id")
    if job.status != 'blocked':
        job.status = 'blocked'
        update_fields.append('status')

    formatted_error = f"[{error_code}] {error_message}"
    if job.error_message != formatted_error:
        job.error_message = formatted_error
        update_fields.append('error_message')

    request_meta = dict(job.request_meta or {})
    blocked_meta = {
        "blocked_step": blocked_step,
        "blocked_error_code": error_code,
        "blocked_retry_after_seconds": retry_after_seconds,
        "blocked_preferred_queue": result.get("preferred_queue"),
        "blocked_fallback_policy": result.get("fallback_policy"),
        "blocked_next_step": result.get("next_step"),
        "blocked_rerunnable_step": result.get("rerunnable_step"),
        "blocked_recovery_attempt": recovery_attempt,
        "blocked_recovery_exhausted": recovery_exhausted,
    }
    if any(request_meta.get(key) != value for key, value in blocked_meta.items()):
        request_meta.update(blocked_meta)
        job.request_meta = request_meta
        update_fields.append('request_meta')

    if update_fields:
        job.save(update_fields=[*update_fields, 'updated_at'])

    if update_card:
        upsert_live_progress_card(
            job,
            data=result,
            summary_text=_blocked_summary_text(
                error_code=error_code,
                blocked_step=blocked_step,
                error_message=error_message,
                retry_after_seconds=retry_after_seconds,
                recovery_attempt=recovery_attempt,
                recovery_exhausted=recovery_exhausted,
            ),
            failed=False,
        )

    if allow_visible_notification:
        _maybe_notify_exhausted_block(
            job,
            error_code=error_code,
            blocked_step=blocked_step,
            recovery_attempt=recovery_attempt,
            recovery_exhausted=recovery_exhausted,
        )


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


def _build_auth_required_payload(
    payload: Optional[dict],
    *,
    resolved_domain: Optional[str],
    slack_user_id: Optional[str],
) -> dict:
    source = dict(payload or {})
    normalized_domain = normalize_domain(source.get("domain") or resolved_domain or "")
    message = str(
        source.get("message")
        or source.get("error")
        or "GitHub authentication is required before Roo can continue."
    ).strip()

    from integrations.services.github import build_github_auth_url

    auth_url = source.get("auth_url") or build_github_auth_url(
        slack_user_id or "",
        domain=normalized_domain or None,
    )

    normalized = {
        "status": source.get("status") or "precondition_failed",
        "error_code": source.get("error_code") or AUTH_REQUIRED_ERROR_CODE,
        "missing_step": source.get("missing_step") or AUTH_REQUIRED_MISSING_STEP,
        "next_action": source.get("next_action") or AUTH_REQUIRED_NEXT_ACTION,
        "requires_user_action": bool(source.get("requires_user_action", True)),
        "resume_hint": source.get("resume_hint") or AUTH_REQUIRED_RESUME_HINT,
        "domain": normalized_domain or source.get("domain") or resolved_domain,
        "github_repo": source.get("github_repo"),
        "reason_code": source.get("reason_code") or "missing_or_expired_credentials",
        "message": message,
        "error": message,
        "auth_url": auth_url,
    }
    for key in ("workflow", "job_id", "run_id"):
        value = source.get(key)
        if value:
            normalized[key] = value
    return normalized


def _raise_content_factory_precondition(
    response,
    *,
    resolved_domain: Optional[str],
    slack_user_id: Optional[str],
) -> None:
    try:
        data = response.json()
    except Exception:
        data = {}

    if str(data.get("error_code") or "").strip() == AUTH_REQUIRED_ERROR_CODE:
        raise GitHubReconnectRequiredError(
            _build_auth_required_payload(
                data,
                resolved_domain=resolved_domain,
                slack_user_id=slack_user_id,
            )
        )

    missing_step = str(data.get("missing_step") or "unknown").strip()
    message = str(data.get("message") or data.get("error") or response.text or "Prerequisite step missing").strip()
    raise ArticleGenerationError(
        f"PREREQUISITE_MISSING:{missing_step}:{resolved_domain}:{message}"
    )


def get_content_factory_article_cost_points(domain: Optional[str]) -> int:
    normalized_domain = normalize_domain(domain or "")
    if normalized_domain in FREE_CONTENT_FACTORY_DOMAINS:
        return 0
    return CONTENT_FACTORY_ARTICLE_COST_POINTS


def is_free_content_factory_domain(domain: Optional[str]) -> bool:
    return get_content_factory_article_cost_points(domain) == 0


def _append_refund_instruction(message: str, domain: Optional[str]) -> str:
    cost_points = get_content_factory_article_cost_points(domain)
    if cost_points == 0:
        return message

    refund_line = (
        f"If this run failed and you want your {cost_points} Roo points back, "
        "message Dr Sam on Slack."
    )
    if refund_line in message:
        return message
    return f"{message}\n\n{refund_line}"


def _append_auto_refund_message(message: str, refund_points: int) -> str:
    if refund_points <= 0:
        return message

    refund_line = f"Your {refund_points} Roo points were refunded automatically."
    if refund_line in message:
        return message
    return f"{message}\n\n{refund_line}"


def _normalize_requested_delivery_mode(value: Optional[str]) -> Optional[str]:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized not in VALID_ARTICLE_DELIVERY_MODES:
        raise ArticleGenerationError(f"Unsupported delivery mode: {normalized}")
    return normalized


def _is_scheduled_daily_request(article_request: Optional[dict]) -> bool:
    if not isinstance(article_request, dict):
        return False
    return str(article_request.get("trigger_source") or "").strip() == SCHEDULED_DAILY_TRIGGER_SOURCE


def _resolve_delivery_mode_confirmation(article_request: Optional[dict], *, requested_mode: Optional[str]) -> bool:
    if not isinstance(article_request, dict):
        return False

    raw_confirmation = article_request.get("delivery_mode_confirmed")
    if raw_confirmation is None:
        return bool(requested_mode)
    return bool(raw_confirmation)


def _resolve_saved_article_delivery_mode(config: Optional[OrganizationContentConfig]) -> Optional[str]:
    if not config:
        return None
    return _normalize_requested_delivery_mode(getattr(config, "article_delivery_mode", None))


def resolve_article_delivery_mode(
    *,
    article_request: Optional[dict] = None,
    config: Optional[OrganizationContentConfig] = None,
) -> tuple[Optional[str], bool]:
    requested_mode = _normalize_requested_delivery_mode(
        article_request.get("delivery_mode") if isinstance(article_request, dict) else None
    )
    if requested_mode:
        return requested_mode, _resolve_delivery_mode_confirmation(
            article_request,
            requested_mode=requested_mode,
        )

    saved_mode = _resolve_saved_article_delivery_mode(config)
    if saved_mode:
        return saved_mode, False

    return None, False


def _require_roo_request_source(article_request: dict) -> str:
    try:
        return require_roo_request_source(
            article_request.get("request_source"),
            error_message="Content Factory article requests must originate from Roo Slackbot.",
        )
    except ValueError as exc:
        raise ArticleGenerationError(str(exc))


def _get_client_request_id(article_request: dict) -> str:
    client_request_id = str(article_request.get("client_request_id") or "").strip()
    if not client_request_id:
        raise ArticleGenerationError("client_request_id is required for Content Factory article requests.")
    return client_request_id


def _resolve_requested_by_slack_user_id(
    slack_user_id: Optional[str],
    article_request: Optional[dict],
) -> str:
    if isinstance(article_request, dict):
        requested_by = str(article_request.get("requested_by_slack_user_id") or "").strip()
        if requested_by:
            return requested_by
    return str(slack_user_id or "").strip()


def _is_delegated_content_request(
    slack_user_id: Optional[str],
    article_request: Optional[dict],
) -> bool:
    requested_by_slack_user_id = _resolve_requested_by_slack_user_id(slack_user_id, article_request)
    effective_slack_user_id = str(slack_user_id or "").strip()
    return bool(
        requested_by_slack_user_id
        and effective_slack_user_id
        and requested_by_slack_user_id != effective_slack_user_id
    )


def _ensure_content_factory_user(slack_user_id: str, article_request: dict):
    from core.slack_users import ensure_slack_user
    from roo.services import PointsService

    requested_by_slack_user_id = _resolve_requested_by_slack_user_id(slack_user_id, article_request)
    existing_user = PointsService.get_user_by_slack_id(requested_by_slack_user_id)
    if existing_user:
        PointsService.get_or_create_account(existing_user)
        return existing_user

    email = str(article_request.get("user_email") or "").strip().lower()
    if not email:
        raise ArticleGenerationError("A Slack email is required before starting this Content Factory run.")

    user = ensure_slack_user(
        slack_id=requested_by_slack_user_id,
        email=email,
        first_name=article_request.get("user_first_name") or "",
        last_name=article_request.get("user_last_name") or "",
        avatar_url=article_request.get("user_avatar_url"),
    ).user
    PointsService.get_or_create_account(user)
    return user


def _build_content_factory_charge_description(resolved_domain: str, article_request: dict) -> str:
    topic = str(article_request.get("topic") or article_request.get("target_keyword") or "").strip()
    if topic:
        return f"Content Factory article run for {resolved_domain}: {topic}"
    return f"Content Factory article research for {resolved_domain}"


def _get_existing_billed_source_job(client_request_id: str):
    from content_factory.models import ContentFactoryJob

    return (
        ContentFactoryJob.objects.filter(
            client_request_id=client_request_id,
            billing_status=CONTENT_FACTORY_BILLING_STATUS_CHARGED,
        )
        .order_by("-updated_at", "-created_at")
        .first()
    )


def _serialize_existing_billed_job(job) -> dict:
    request_meta = job.request_meta or {}
    if request_meta.get("source_run_id"):
        workflow = "confirmed_topic"
    elif request_meta.get("topic"):
        workflow = "direct_generate"
    else:
        workflow = "auto_discovery"

    return {
        "job_id": job.job_id,
        "run_id": job.job_id,
        "workflow": workflow,
        "status": job.status or "queued",
        "message": "Article request already started",
    }


def _charge_content_factory_request(slack_user_id: str, article_request: dict, resolved_domain: str):
    from content_factory.models import ContentFactoryJob
    from roo.permissions import InsufficientBalanceError
    from roo.services import PointsService

    requested_by_slack_user_id = _resolve_requested_by_slack_user_id(slack_user_id, article_request)
    client_request_id = _get_client_request_id(article_request)
    cost_points = get_content_factory_article_cost_points(resolved_domain)
    existing_refunded = (
        ContentFactoryJob.objects.filter(
            client_request_id=client_request_id,
            billing_status=CONTENT_FACTORY_BILLING_STATUS_REFUNDED,
        )
        .order_by("-updated_at", "-created_at")
        .first()
    )
    if existing_refunded:
        raise ArticleGenerationError(
            "This Content Factory run was already refunded after a failed start. Please start a new article request."
        )

    user = _ensure_content_factory_user(slack_user_id, article_request)
    if cost_points == 0:
        return user, None, 0

    try:
        ledger, _ = PointsService.spend(
            user=user,
            delta=cost_points,
            source=CONTENT_FACTORY_LEDGER_SOURCE,
            description=_build_content_factory_charge_description(resolved_domain, article_request),
            created_by_slack_id=requested_by_slack_user_id,
            idempotency_key=f"content_factory:charge:{client_request_id}",
            reference_type="CONTENT_FACTORY",
            reference_id=client_request_id,
        )
    except InsufficientBalanceError:
        raise ArticleGenerationError(
            f"Creating an article costs {cost_points} Roo points, and this user does not have enough."
        )

    return user, ledger, cost_points


def _refund_content_factory_request(
    *,
    user,
    slack_user_id: str,
    article_request: dict,
    resolved_domain: str,
    reason: str,
):
    from content_factory.models import ContentFactoryJob
    from roo.services import PointsService

    client_request_id = _get_client_request_id(article_request)
    cost_points = get_content_factory_article_cost_points(resolved_domain)
    if cost_points == 0:
        return None

    requested_by_slack_user_id = _resolve_requested_by_slack_user_id(slack_user_id, article_request)
    ledger, _ = PointsService.refund(
        user=user,
        delta=cost_points,
        source=CONTENT_FACTORY_LEDGER_SOURCE,
        description=f"Automatic refund for failed Content Factory start for {resolved_domain}: {reason}",
        created_by_slack_id=requested_by_slack_user_id,
        idempotency_key=f"content_factory:refund:{client_request_id}",
        reference_type="CONTENT_FACTORY",
        reference_id=client_request_id,
    )
    ContentFactoryJob.objects.filter(client_request_id=client_request_id).update(
        billing_status=CONTENT_FACTORY_BILLING_STATUS_REFUNDED,
        billing_amount=cost_points,
        billing_ledger_id=ledger.id,
    )
    return ledger


def _get_content_factory_user_for_job(job):
    from django.contrib.auth import get_user_model

    UserModel = get_user_model()
    request_meta = getattr(job, "request_meta", {}) or {}
    slack_user_id = str(
        request_meta.get("requested_by_slack_user_id")
        or getattr(job, "slack_user_id", "")
        or ""
    ).strip()
    if slack_user_id:
        user = UserModel.objects.filter(slack_id=slack_user_id).first()
        if user:
            return user

    user_email = str(request_meta.get("user_email") or "").strip().lower()
    if user_email:
        return UserModel.objects.filter(email__iexact=user_email).first()

    return None


def _get_content_factory_base_url() -> str:
    base_url = str(getattr(settings, "CONTENT_FACTORY_URL", "") or "").strip()
    if base_url:
        return base_url.rstrip("/")
    if getattr(settings, "IS_LOCAL_ENV", False):
        return "http://localhost:8001"
    raise ArticleGenerationError("CONTENT_FACTORY_URL is not configured.")


def _build_content_factory_headers() -> dict:
    api_key = getattr(settings, "CONTENT_FACTORY_API_KEY", None)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-KEY"] = api_key
    return headers


def _build_content_factory_backend_unavailable_payload(
    *,
    operation: str,
    endpoint: str,
    domain: Optional[str],
    source_run_id: Optional[str] = None,
    job_id: Optional[str] = None,
    reason: Optional[str] = None,
    status_code: Optional[int] = None,
) -> dict:
    message = str(reason or "Content Factory is unavailable right now.").strip()
    payload = {
        "status": "backend_unavailable",
        "error_code": CONTENT_FACTORY_BACKEND_UNAVAILABLE_ERROR_CODE,
        "service": "content_factory",
        "operation": operation,
        "endpoint": endpoint,
        "domain": domain,
        "retryable": True,
        "message": message,
        "error": message,
    }
    if source_run_id:
        payload["source_run_id"] = source_run_id
    if job_id:
        payload["job_id"] = job_id
        payload["run_id"] = job_id
    if status_code is not None:
        payload["status_code"] = status_code
    return payload


def _raise_content_factory_backend_unavailable(
    *,
    operation: str,
    endpoint: str,
    domain: Optional[str],
    source_run_id: Optional[str] = None,
    job_id: Optional[str] = None,
    reason: Optional[str] = None,
    status_code: Optional[int] = None,
) -> None:
    raise ContentFactoryBackendUnavailableError(
        _build_content_factory_backend_unavailable_payload(
            operation=operation,
            endpoint=endpoint,
            domain=domain,
            source_run_id=source_run_id,
            job_id=job_id,
            reason=reason,
            status_code=status_code,
        )
    )


def _post_content_factory_queue_request(
    endpoint: str,
    *,
    payload: dict,
    headers: dict,
    operation: str,
    domain: Optional[str],
    source_run_id: Optional[str] = None,
) -> object:
    attempts = CONTENT_FACTORY_FAST_QUEUE_RETRY_COUNT + 1
    timeout = (
        CONTENT_FACTORY_FAST_QUEUE_CONNECT_TIMEOUT_SECONDS,
        CONTENT_FACTORY_FAST_QUEUE_READ_TIMEOUT_SECONDS,
    )

    for attempt in range(1, attempts + 1):
        try:
            response = http_requests.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
        except http_requests.exceptions.RequestException as exc:
            logger.warning(
                "Content Factory queue request failed attempt %s/%s operation=%s endpoint=%s exc=%r",
                attempt,
                attempts,
                operation,
                endpoint,
                exc,
            )
            if attempt < attempts:
                continue
            _raise_content_factory_backend_unavailable(
                operation=operation,
                endpoint=endpoint,
                domain=domain,
                source_run_id=source_run_id,
                reason=str(exc),
            )

        if response.status_code in CONTENT_FACTORY_FAST_QUEUE_RETRYABLE_STATUSES:
            logger.warning(
                "Content Factory queue request returned retryable status attempt %s/%s operation=%s endpoint=%s status=%s body=%s",
                attempt,
                attempts,
                operation,
                endpoint,
                response.status_code,
                response.text,
            )
            if attempt < attempts:
                continue
            _raise_content_factory_backend_unavailable(
                operation=operation,
                endpoint=endpoint,
                domain=domain,
                source_run_id=source_run_id,
                reason=response.text,
                status_code=response.status_code,
            )

        return response

    _raise_content_factory_backend_unavailable(
        operation=operation,
        endpoint=endpoint,
        domain=domain,
        source_run_id=source_run_id,
    )


def _find_active_confirm_child_job(
    *,
    source_run_id: Optional[str],
    confirmed_keyword: Optional[str] = None,
    custom_title: Optional[str] = None,
):
    if not source_run_id:
        return None

    candidates = list(
        ContentFactoryJob.objects.filter(request_meta__source_run_id=source_run_id)
        .exclude(status__in=CONTENT_FACTORY_TERMINAL_JOB_STATUSES)
        .order_by("-updated_at", "-created_at")
    )
    if not candidates:
        return None

    expected_targets = {
        str(custom_title or "").strip().lower(),
        str(confirmed_keyword or "").strip().lower(),
    }
    expected_targets.discard("")

    def sort_key(job):
        request_meta = dict(job.request_meta or {})
        topic = str(request_meta.get("topic") or "").strip().lower()
        target_keyword = str(request_meta.get("target_keyword") or "").strip().lower()
        return (
            0 if expected_targets and {topic, target_keyword} & expected_targets else 1,
            -int(job.updated_at.timestamp()),
            -int(job.created_at.timestamp()),
        )

    return sorted(candidates, key=sort_key)[0]


def _serialize_existing_confirm_child_job(job, *, source_run_id: Optional[str]) -> dict:
    request_meta = dict(job.request_meta or {})
    payload = {
        "job_id": job.job_id,
        "run_id": job.job_id,
        "status": job.status or "queued",
        "message": "Topic confirmation is already in progress for this thread.",
        "domain": job.domain,
    }
    if source_run_id:
        payload["source_run_id"] = source_run_id
    blocked_error = str(request_meta.get("blocked_error_code") or "").strip()
    if blocked_error:
        payload["error_code"] = blocked_error
    if job.error_message:
        payload["error"] = job.error_message
        payload["message"] = job.error_message
    return payload


def maybe_auto_refund_terminal_failure(job, *, error_code: Optional[str], error_message: Optional[str]) -> tuple[bool, int]:
    if not job:
        return False, 0

    resolved_error_code = str(error_code or "").strip().upper()
    if resolved_error_code not in AUTO_REFUND_ERROR_CODES:
        return False, 0

    client_request_id = str(getattr(job, "client_request_id", "") or "").strip()
    if not client_request_id:
        return False, 0

    billed_job = job
    if getattr(job, "billing_status", "") != CONTENT_FACTORY_BILLING_STATUS_CHARGED:
        billed_job = _get_existing_billed_source_job(client_request_id)
        if not billed_job:
            return False, 0

    if getattr(billed_job, "billing_status", "") != CONTENT_FACTORY_BILLING_STATUS_CHARGED:
        return False, 0

    user = _get_content_factory_user_for_job(billed_job)
    if not user:
        logger.warning(
            "Unable to auto-refund Content Factory failure for %s: no user resolved",
            client_request_id,
        )
        return False, 0

    request_meta = dict(getattr(billed_job, "request_meta", {}) or {})
    request_meta.setdefault("client_request_id", client_request_id)
    resolved_domain = getattr(billed_job, "domain", "") or request_meta.get("domain")
    refund_points = get_content_factory_article_cost_points(resolved_domain)
    if refund_points <= 0:
        return False, 0

    refund_reason = str(error_message or resolved_error_code or "deterministic failure").strip()
    _refund_content_factory_request(
        user=user,
        slack_user_id=getattr(billed_job, "slack_user_id", "") or getattr(job, "slack_user_id", ""),
        article_request=request_meta,
        resolved_domain=resolved_domain,
        reason=refund_reason,
    )
    return True, refund_points


def _job_uses_deferred_billing(job) -> bool:
    if not job:
        return False
    if getattr(job, "billing_status", "") not in {"", CONTENT_FACTORY_BILLING_STATUS_DEFERRED}:
        return False
    request_meta = getattr(job, "request_meta", {}) or {}
    return _is_scheduled_daily_request(request_meta)


def _charge_deferred_discovery_job_if_needed(
    *,
    source_job,
    slack_user_id: str,
    domain: str,
    confirmed_keyword: str,
    custom_title: Optional[str] = None,
):
    if not _job_uses_deferred_billing(source_job):
        return source_job

    request_meta = dict(getattr(source_job, "request_meta", {}) or {})
    request_meta.setdefault("domain", normalize_domain(domain))
    request_meta.setdefault("topic", custom_title or confirmed_keyword)
    request_meta.setdefault("target_keyword", confirmed_keyword)
    request_meta.setdefault("request_source", CONTENT_FACTORY_REQUEST_SOURCE)
    request_meta.setdefault(
        "client_request_id",
        getattr(source_job, "client_request_id", "") or f"scheduled-confirm:{source_job.job_id}",
    )

    charged_user, charge_ledger, charge_amount = _charge_content_factory_request(
        slack_user_id,
        request_meta,
        normalize_domain(domain),
    )
    source_job.billing_source_job_id = source_job.job_id
    source_job.billing_amount = charge_amount
    source_job.billing_status = CONTENT_FACTORY_BILLING_STATUS_CHARGED
    source_job.billing_ledger = charge_ledger
    if getattr(source_job, "client_request_id", "") != request_meta.get("client_request_id"):
        source_job.client_request_id = request_meta["client_request_id"]
    source_job.request_meta = request_meta
    source_job.save(
        update_fields=[
            "billing_source_job_id",
            "billing_amount",
            "billing_status",
            "billing_ledger",
            "client_request_id",
            "request_meta",
            "updated_at",
        ]
    )
    return source_job


def _refund_deferred_discovery_job_on_confirm_failure(
    *,
    source_job,
    slack_user_id: str,
    domain: str,
    reason: str,
):
    if not source_job:
        return None

    refreshed_source_job = source_job.__class__.objects.filter(pk=source_job.pk).first()
    if not refreshed_source_job:
        return None
    if getattr(refreshed_source_job, "billing_status", "") != CONTENT_FACTORY_BILLING_STATUS_CHARGED:
        return None

    user = _get_content_factory_user_for_job(refreshed_source_job)
    if not user:
        logger.warning(
            "Unable to refund deferred scheduled discovery job %s after confirm failure: no user resolved",
            getattr(refreshed_source_job, "job_id", ""),
        )
        return None

    request_meta = dict(getattr(refreshed_source_job, "request_meta", {}) or {})
    request_meta.setdefault(
        "client_request_id",
        getattr(refreshed_source_job, "client_request_id", "") or f"scheduled-confirm:{refreshed_source_job.job_id}",
    )
    request_meta.setdefault("domain", normalize_domain(domain))
    return _refund_content_factory_request(
        user=user,
        slack_user_id=slack_user_id,
        article_request=request_meta,
        resolved_domain=normalize_domain(domain),
        reason=reason,
    )


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
    client_request_id: Optional[str] = None,
    billing_source_job_id: Optional[str] = None,
    billing_amount: Optional[int] = None,
    billing_status: Optional[str] = None,
    billing_ledger_id: Optional[int] = None,
    progress_message_ts: Optional[str] = None,
    last_progress_milestone_key: Optional[str] = None,
    last_progress_updated_at=None,
    still_working_pinged_at=None,
):
    from content_factory.models import ContentFactoryJob

    resolved_root_message_ts = slack_root_message_ts or slack_thread_ts or ""

    defaults = {
        "domain": domain or "",
        "slack_user_id": slack_user_id or "",
        "status": default_status,
        "request_meta": request_meta or {},
        "slack_channel_id": slack_channel_id or "",
        "slack_thread_ts": slack_thread_ts or "",
        "slack_root_message_ts": resolved_root_message_ts,
        "client_request_id": client_request_id,
        "billing_source_job_id": billing_source_job_id,
        "billing_amount": billing_amount or 0,
        "billing_status": billing_status or "",
        "billing_ledger_id": billing_ledger_id,
        "progress_message_ts": progress_message_ts or "",
        "last_progress_milestone_key": last_progress_milestone_key or "",
        "last_progress_updated_at": last_progress_updated_at,
        "still_working_pinged_at": still_working_pinged_at,
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
    if client_request_id is not None and job.client_request_id != client_request_id:
        job.client_request_id = client_request_id
        update_fields.append("client_request_id")
    if billing_source_job_id is not None and job.billing_source_job_id != billing_source_job_id:
        job.billing_source_job_id = billing_source_job_id
        update_fields.append("billing_source_job_id")
    if billing_amount is not None and job.billing_amount != billing_amount:
        job.billing_amount = billing_amount
        update_fields.append("billing_amount")
    if billing_status is not None and job.billing_status != billing_status:
        job.billing_status = billing_status
        update_fields.append("billing_status")
    if billing_ledger_id is not None and job.billing_ledger_id != billing_ledger_id:
        job.billing_ledger_id = billing_ledger_id
        update_fields.append("billing_ledger")
    if progress_message_ts is not None and job.progress_message_ts != (progress_message_ts or ""):
        job.progress_message_ts = progress_message_ts or ""
        update_fields.append("progress_message_ts")
    if last_progress_milestone_key is not None and job.last_progress_milestone_key != (last_progress_milestone_key or ""):
        job.last_progress_milestone_key = last_progress_milestone_key or ""
        update_fields.append("last_progress_milestone_key")
    if last_progress_updated_at is not None and job.last_progress_updated_at != last_progress_updated_at:
        job.last_progress_updated_at = last_progress_updated_at
        update_fields.append("last_progress_updated_at")
    if still_working_pinged_at is not None and job.still_working_pinged_at != still_working_pinged_at:
        job.still_working_pinged_at = still_working_pinged_at
        update_fields.append("still_working_pinged_at")

    if update_fields:
        update_fields.append("updated_at")
        job.save(update_fields=update_fields)

    return job


def attach_progress_message(
    job_id: str,
    *,
    progress_message_ts: str,
    slack_channel_id: str = "",
    slack_thread_ts: str = "",
    slack_root_message_ts: str = "",
):
    from content_factory.models import ContentFactoryJob

    progress_ts = str(progress_message_ts or "").strip()
    if not progress_ts:
        raise ArticleGenerationError("progress_message_ts is required.")

    job = ContentFactoryJob.objects.filter(job_id=job_id).first()
    if not job:
        raise ArticleGenerationError(f"Job not found: {job_id}")

    update_fields = []
    if job.progress_message_ts != progress_ts:
        job.progress_message_ts = progress_ts
        update_fields.append("progress_message_ts")

    resolved_thread_ts = str(slack_thread_ts or "").strip()
    resolved_root_ts = str(slack_root_message_ts or "").strip() or resolved_thread_ts
    resolved_channel_id = str(slack_channel_id or "").strip()

    if resolved_channel_id and job.slack_channel_id != resolved_channel_id:
        job.slack_channel_id = resolved_channel_id
        update_fields.append("slack_channel_id")
    if resolved_thread_ts and job.slack_thread_ts != resolved_thread_ts:
        job.slack_thread_ts = resolved_thread_ts
        update_fields.append("slack_thread_ts")
    if resolved_root_ts and job.slack_root_message_ts != resolved_root_ts:
        job.slack_root_message_ts = resolved_root_ts
        update_fields.append("slack_root_message_ts")

    now = timezone.now()
    if not job.last_progress_updated_at:
        job.last_progress_updated_at = now
        update_fields.append("last_progress_updated_at")
    if not job.last_progress_milestone_key:
        job.last_progress_milestone_key = "queued"
        update_fields.append("last_progress_milestone_key")
    if job.still_working_pinged_at is not None:
        job.still_working_pinged_at = None
        update_fields.append("still_working_pinged_at")

    if update_fields:
        update_fields.append("updated_at")
        job.save(update_fields=update_fields)

    return job


def augment_status_with_job_tracking(job_id: str, result: Optional[dict]) -> dict:
    from content_factory.models import ContentFactoryJob

    payload = dict(result or {})
    job = ContentFactoryJob.objects.filter(job_id=job_id).first()
    if not job:
        return payload

    payload.setdefault("job_id", job_id)
    payload["progress_message_ts"] = job.progress_message_ts or ""
    payload["last_progress_milestone_key"] = job.last_progress_milestone_key or ""
    payload["last_progress_updated_at"] = (
        job.last_progress_updated_at.isoformat() if job.last_progress_updated_at else None
    )
    payload["still_working_pinged_at"] = (
        job.still_working_pinged_at.isoformat() if job.still_working_pinged_at else None
    )
    return payload


def _serialize_local_run_snapshot(run) -> dict:
    result_payload = run.result or {}
    status_value = run.status
    reviewable_status = str(result_payload.get("status") or "").strip()
    if status_value == "completed" and reviewable_status in REVIEWABLE_RUN_STATUSES:
        status_value = reviewable_status

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
        "status": status_value,
        "current_step": run.current_step,
        "approval_state": run.approval_state,
        "artifact_root": run.artifact_root,
        "step_order": run.step_order or [],
        "acceptance_summary": run.acceptance_summary or {},
        "verification_summary": run.verification_summary or {},
        "resume_available": run.resume_available,
        "error": run.error or None,
        "result": result_payload,
        "run_request": run.run_request or {},
        "step_states": steps,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }


def _load_local_run_snapshot(job_id: str) -> Optional[dict]:
    from workflow_runs.models import ContentFactoryRun

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
    started_at = time.monotonic()
    try:
        token_resp = http_requests.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
                "client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": config.github_refresh_token_encrypted,
            },
            timeout=(3, 20),
        )
    except Exception as exc:
        duration_ms = (time.monotonic() - started_at) * 1000
        logger.warning(
            "github_http_call_failed action=refresh_org_token domain=%s duration_ms=%.2f exc_type=%s exc=%r",
            domain,
            duration_ms,
            exc.__class__.__name__,
            exc,
        )
        raise

    duration_ms = (time.monotonic() - started_at) * 1000
    logger.info(
        "github_http_call action=refresh_org_token domain=%s status=%s duration_ms=%.2f",
        domain,
        token_resp.status_code,
        duration_ms,
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


def _resolve_github_repo_for_domain(
    domain: str,
    slack_user_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[OrganizationContentConfig], str]:
    normalized_domain = normalize_domain(domain) if domain else None
    config = None

    if normalized_domain:
        try:
            org = Organization.objects.get(domain=normalized_domain)
            config = getattr(org, "content_config", None)
            github_repo = str(getattr(config, "github_repo", "") or "").strip()
            if github_repo:
                return github_repo, config, "org"
        except Organization.DoesNotExist:
            config = None

    if slack_user_id and normalized_domain:
        integration = UserIntegration.objects.filter(slack_user_id=slack_user_id).first()
        if integration and integration.github_repo:
            repo_matches_domain = OrganizationContentConfig.objects.filter(
                github_repo=integration.github_repo,
                organization__domain=normalized_domain,
            ).exists()
            if repo_matches_domain:
                return integration.github_repo, config, "user"

    return None, config, "unresolved"


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
                if (
                    slack_user_id
                    and config.connected_slack_user_id
                    and config.connected_slack_user_id != slack_user_id
                ):
                    logger.info(
                        "Skipping org-level GitHub credentials for %s: owned by %s, requested by %s",
                        normalized_domain,
                        config.connected_slack_user_id,
                        slack_user_id,
                    )
                else:
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


def resolve_content_factory_connection_for_domain(
    domain: str,
    slack_user_id: Optional[str] = None,
) -> dict:
    normalized_domain = normalize_domain(domain) if domain else None
    github_repo, config, _repo_source = _resolve_github_repo_for_domain(
        normalized_domain,
        slack_user_id,
    )
    connection_state = "auth_required"
    credential_source = "none"

    try:
        creds = get_github_credentials_for_domain(normalized_domain, slack_user_id)
        github_repo = creds.get("repo") or github_repo
        credential_source = str(creds.get("source") or "").strip() or "none"
        connection_state = "connected" if github_repo else "repo_selection_required"
        config = creds.get("config") or config
    except ArticleGenerationError as exc:
        forced_user_refresh_attempted = False
        if slack_user_id:
            integration = UserIntegration.objects.filter(slack_user_id=slack_user_id).first()
            if integration and integration.github_refresh_token:
                try:
                    logger.info(
                        "Attempting forced user-token refresh for %s during Content Factory connection resolution on %s",
                        slack_user_id,
                        normalized_domain,
                    )
                    refresh_github_token(slack_user_id)
                    forced_user_refresh_attempted = True
                except Exception as refresh_exc:
                    logger.warning(
                        "Forced user-token refresh failed for %s during Content Factory connection resolution on %s: %s",
                        slack_user_id,
                        normalized_domain,
                        refresh_exc,
                    )

        if forced_user_refresh_attempted:
            try:
                creds = get_github_credentials_for_domain(normalized_domain, slack_user_id)
                github_repo = creds.get("repo") or github_repo
                credential_source = str(creds.get("source") or "").strip() or "none"
                connection_state = "connected" if github_repo else "repo_selection_required"
                config = creds.get("config") or config
            except ArticleGenerationError:
                logger.warning(
                    "Content Factory connection resolution still requires auth after forced refresh for %s on %s: %s",
                    slack_user_id,
                    normalized_domain,
                    exc,
                )
        if config and config.github_token_encrypted and not config.github_repo:
            connection_state = "repo_selection_required"

    return {
        "domain": normalized_domain,
        "config": config,
        "github_repo": github_repo,
        "connection_state": connection_state,
        "needs_github_auth": connection_state == "auth_required",
        "domain_connected": connection_state == "connected",
        "credential_source": credential_source,
    }


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
    _require_roo_request_source(article_request)
    client_request_id = _get_client_request_id(article_request)
    existing_job = _get_existing_billed_source_job(client_request_id)
    if existing_job:
        logger.info(
            "Reusing existing Content Factory source job %s for client_request_id=%s",
            existing_job.job_id,
            client_request_id,
        )
        return _serialize_existing_billed_job(existing_job)

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
        if config.article_system: existing_artifacts['article_system'] = config.article_system
        if config.publish_targets: existing_artifacts['publish_targets'] = config.publish_targets
        if config.default_publish_target_id: existing_artifacts['default_publish_target_id'] = config.default_publish_target_id
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
    progress_message_ts = article_request.get('progress_message_ts') or ''

    if not resolved_domain:
        raise ArticleGenerationError("Domain is required.")

    # Retrieve competitors and seed_keywords early for Auto-Write or Payload
    competitors = []
    seed_keywords = []
    if config and hasattr(config, 'organization'):
        competitors = config.organization.competitors or []
        seed_keywords = config.organization.seed_keywords or []

    content_factory_url = _get_content_factory_base_url()
    headers = _build_content_factory_headers()

    # Research Mode: if topic is missing, use the dedicated discovery endpoint.
    if not topic:
        discovery_endpoint = f"{content_factory_url.rstrip('/')}/api/runs/discovery"
        requested_by_slack_user_id = _resolve_requested_by_slack_user_id(slack_user_id, article_request)
        scheduled_daily_request = _is_scheduled_daily_request(article_request)
        charged_user = None
        charge_ledger = None
        charge_amount = 0
        billing_status = CONTENT_FACTORY_BILLING_STATUS_DEFERRED if scheduled_daily_request else CONTENT_FACTORY_BILLING_STATUS_CHARGED
        if not scheduled_daily_request:
            charged_user, charge_ledger, charge_amount = _charge_content_factory_request(
                slack_user_id,
                article_request,
                resolved_domain,
            )
        payload = {
            "domain": resolved_domain,
            "slack_user_id": slack_user_id,
            "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
        }
        if requested_by_slack_user_id and requested_by_slack_user_id != str(slack_user_id or "").strip():
            payload["requested_by_slack_user_id"] = requested_by_slack_user_id

        logger.info(f"Research mode enabled for {resolved_domain}. Triggering discovery at {discovery_endpoint}.")

        try:
            response = _post_content_factory_queue_request(
                discovery_endpoint,
                payload=payload,
                headers=headers,
                operation="queue_discovery",
                domain=resolved_domain,
            )

            if response.status_code not in [200, 202]:
                logger.error(f"Content Factory discovery failed: {response.text}")
                if charged_user is not None:
                    _refund_content_factory_request(
                        user=charged_user,
                        slack_user_id=slack_user_id,
                        article_request=article_request,
                        resolved_domain=resolved_domain,
                        reason=f"discovery queue failed with status {response.status_code}",
                    )
                raise ArticleGenerationError(f"Content Factory returned {response.status_code}: {response.text}")

            data = response.json()
            job_id = data.get('job_id') or data.get('task_id') or data.get('run_id')
            if not job_id:
                logger.warning("Content Factory returned discovery success but no job_id")
                if charged_user is not None:
                    _refund_content_factory_request(
                        user=charged_user,
                        slack_user_id=slack_user_id,
                        article_request=article_request,
                        resolved_domain=resolved_domain,
                        reason="missing job id from discovery queue response",
                    )
                raise ArticleGenerationError("Content Factory did not return a run id for the discovery request.")

            _store_job_tracking_record(
                job_id,
                domain=resolved_domain,
                slack_user_id=slack_user_id,
                request_meta=article_request,
                slack_channel_id=slack_channel_id,
                slack_thread_ts=slack_thread_ts,
                slack_root_message_ts=slack_root_message_ts,
                default_status="queued",
                client_request_id=client_request_id,
                billing_source_job_id=job_id,
                billing_amount=charge_amount,
                billing_status=billing_status,
                billing_ledger_id=charge_ledger.id if charge_ledger else None,
                progress_message_ts=progress_message_ts,
                last_progress_milestone_key="queued",
                last_progress_updated_at=timezone.now(),
            )
            return {
                "job_id": job_id,
                "run_id": job_id,
                "workflow": data.get("workflow") or "auto_discovery",
                "status": "queued",
                "message": "Discovery started",
                "job_status_url": f"{content_factory_url.rstrip('/')}/api/runs/{job_id}",
            }
        except ContentFactoryBackendUnavailableError:
            if charged_user is not None:
                _refund_content_factory_request(
                    user=charged_user,
                    slack_user_id=slack_user_id,
                    article_request=article_request,
                    resolved_domain=resolved_domain,
                    reason="Content Factory queue path unavailable during discovery start",
                )
            raise
        except ArticleGenerationError:
            raise

    article_system, article_system_source = resolve_article_system_with_source(config)
    logger.info(
        "Article-system readiness for %s resolved via %s: state=%s path=%s",
        resolved_domain,
        article_system_source,
        article_system.get('state'),
        article_system.get('directory_path') or article_system.get('directory_name'),
    )
    delivery_mode, delivery_mode_confirmed = resolve_article_delivery_mode(
        article_request=article_request,
        config=config,
    )
    github_repo, resolved_config, github_repo_source = _resolve_github_repo_for_domain(
        resolved_domain,
        slack_user_id,
    )
    if config is None:
        config = resolved_config
    if github_repo:
        logger.info("Using %s-level GitHub repo context for article generation", github_repo_source)
    else:
        logger.info("No GitHub repo context resolved for article generation on %s", resolved_domain)

    if config and not existing_artifacts:
        if config.article_template: existing_artifacts['article_template'] = config.article_template
        if config.design_guide: existing_artifacts['design_guide'] = config.design_guide
        if config.resource_prompt: existing_artifacts['resource_prompt'] = config.resource_prompt
        if config.tech_stack: existing_artifacts['tech_stack'] = config.tech_stack
        if config.brand_name: existing_artifacts['brand_name'] = config.brand_name
        if config.article_path_pattern: existing_artifacts['article_path_pattern'] = config.article_path_pattern
        if config.registry_path: existing_artifacts['registry_path'] = config.registry_path
        if config.article_system: existing_artifacts['article_system'] = config.article_system
        if config.publish_targets: existing_artifacts['publish_targets'] = config.publish_targets
        if config.default_publish_target_id: existing_artifacts['default_publish_target_id'] = config.default_publish_target_id
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
        "competitors": competitors,
        "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
    }
    requested_by_slack_user_id = _resolve_requested_by_slack_user_id(slack_user_id, article_request)
    if requested_by_slack_user_id and requested_by_slack_user_id != str(slack_user_id or "").strip():
        payload["requested_by_slack_user_id"] = requested_by_slack_user_id
    if github_repo:
        payload["github_repo"] = github_repo
    if delivery_mode is not None:
        payload["delivery_mode"] = delivery_mode
        payload["delivery_mode_confirmed"] = delivery_mode_confirmed
    if article_request.get("custom_title"):
        payload["custom_title"] = article_request["custom_title"]
    if article_request.get("skip_alternatives"):
        payload["skip_alternatives"] = article_request["skip_alternatives"]
    if article_request.get("source_run_id"):
        payload["source_run_id"] = article_request["source_run_id"]

    masked_payload = payload.copy()
    logger.info(f"Triggering article generation at {generate_endpoint} with payload: {masked_payload}")
    charged_user, charge_ledger, charge_amount = _charge_content_factory_request(
        slack_user_id,
        article_request,
        resolved_domain,
    )

    try:
        response = _post_content_factory_queue_request(
            generate_endpoint,
            payload=payload,
            headers=headers,
            operation="queue_article",
            domain=resolved_domain,
            source_run_id=article_request.get("source_run_id"),
        )
        
        if response.status_code in [200, 202]:
            data = response.json()
            job_id = data.get('job_id') or data.get('task_id') or data.get('run_id')
            if not job_id:
                logger.warning("Content Factory returned success but no job_id")
                _refund_content_factory_request(
                    user=charged_user,
                    slack_user_id=slack_user_id,
                    article_request=article_request,
                    resolved_domain=resolved_domain,
                    reason="missing job id from article queue response",
                )
                raise ArticleGenerationError("Content Factory did not return a run id for the article request.")
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
                client_request_id=client_request_id,
                billing_source_job_id=job_id,
                billing_amount=charge_amount,
                billing_status=CONTENT_FACTORY_BILLING_STATUS_CHARGED,
                billing_ledger_id=charge_ledger.id if charge_ledger else None,
                progress_message_ts=progress_message_ts,
                last_progress_milestone_key="queued",
                last_progress_updated_at=timezone.now(),
            )

            status_url = f"{content_factory_url.rstrip('/')}/api/runs/{job_id}"
            return {
                "job_id": job_id,
                "run_id": job_id,
                "workflow": data.get("workflow") or "direct_generate",
                "status": data.get("status") or "queued",
                "message": data.get("message") or "Generation started",
                "job_status_url": status_url,
                "deduped": bool(data.get("deduped")),
                "recovered": bool(data.get("recovered")),
                "resume_from": data.get("resume_from"),
            }
        elif response.status_code == 412:
            _refund_content_factory_request(
                user=charged_user,
                slack_user_id=slack_user_id,
                article_request=article_request,
                resolved_domain=resolved_domain,
                reason="article prerequisite response",
            )
            _raise_content_factory_precondition(
                response,
                resolved_domain=resolved_domain,
                slack_user_id=slack_user_id,
            )
        else:
            logger.error(f"Content Factory generate failed: {response.text}")
            _refund_content_factory_request(
                user=charged_user,
                slack_user_id=slack_user_id,
                article_request=article_request,
                resolved_domain=resolved_domain,
                reason=f"article queue failed with status {response.status_code}",
            )
            raise ArticleGenerationError(f"Content Factory returned {response.status_code}: {response.text}")

    except ContentFactoryBackendUnavailableError:
        _refund_content_factory_request(
            user=charged_user,
            slack_user_id=slack_user_id,
            article_request=article_request,
            resolved_domain=resolved_domain,
            reason="Content Factory queue path unavailable during article start",
        )
        raise
    except ArticleGenerationError:
        raise


def _handle_status_failure(job_id: str, result: dict):
    """
    Handle a failed job detected during status polling.
    Updates the local ContentFactoryJob and sends a Slack notification (once).
    """
    from content_factory.models import ContentFactoryJob
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
    error_code = str(result.get("error_code") or "INTERNAL_ERROR")
    job.status = 'error'
    job.error_message = f"[{error_code}] {error_message}"
    job.save(update_fields=['status', 'error_message', 'updated_at'])
    logger.info(f"Updated job {job_id} to error: {error_code}: {error_message}")

    auto_refunded, refund_points = maybe_auto_refund_terminal_failure(
        job,
        error_code=error_code,
        error_message=error_message,
    )

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

            if auto_refunded:
                slack_text = _append_auto_refund_message(slack_text, refund_points)
            else:
                slack_text = _append_refund_instruction(slack_text, job.domain)
            SlackService.send_dm(job.slack_user_id, slack_text)
        except Exception as e:
            logger.warning(f"Failed to send failure notification for job {job_id}: {e}")


def _handle_status_blocked(job_id: str, result: dict):
    """
    Handle a blocked job detected during status polling.
    Updates the local ContentFactoryJob without treating the run as terminal.
    """
    try:
        job = ContentFactoryJob.objects.get(job_id=job_id)
    except ContentFactoryJob.DoesNotExist:
        logger.warning(f"Blocked status for unknown job {job_id}")
        return

    sync_blocked_job_state(job, result, update_card=True, allow_visible_notification=True)
    logger.info(
        "Updated job %s to blocked: %s: %s",
        job_id,
        result.get("error_code") or "blocked",
        result.get('error') or result.get('error_message') or 'Generation is blocked waiting for capacity',
    )


def _handle_status_reviewable(job_id: str, result: dict):
    """
    Handle a reviewable terminal outcome detected during status polling.
    """
    from content_factory.models import ContentFactoryJob

    try:
        job = ContentFactoryJob.objects.get(job_id=job_id)
    except ContentFactoryJob.DoesNotExist:
        logger.warning(f"Reviewable status for unknown job {job_id}")
        return

    status_value = str(result.get("status") or "needs_review").strip() or "needs_review"
    update_fields = ["updated_at"]
    if job.status != status_value:
        job.status = status_value
        update_fields.append("status")

    pr_url = result.get("pr_url") or ((result.get("result") or {}).get("pr_url"))
    if pr_url and job.pr_url != pr_url:
        job.pr_url = pr_url
        update_fields.append("pr_url")

    request_meta = dict(job.request_meta or {})
    changed = False
    for key, value in (
        ("publish_stage", status_value),
        ("review_required", status_value == "needs_review"),
        ("verification_state", result.get("verification_state")),
        ("reason_code", result.get("reason_code")),
    ):
        if request_meta.get(key) != value:
            request_meta[key] = value
            changed = True
    if changed:
        job.request_meta = request_meta
        update_fields.append("request_meta")

    if job.error_message:
        job.error_message = ""
        update_fields.append("error_message")

    if len(update_fields) > 1:
        job.save(update_fields=update_fields)

    logger.info("Updated job %s to reviewable status %s", job_id, status_value)


def set_article_delivery_mode(job_id: str, delivery_mode: Optional[str] = None) -> dict:
    """
    Select a delivery mode for an article run paused before queueing.
    """
    if delivery_mode:
        selected_mode = _normalize_requested_delivery_mode(delivery_mode)
    else:
        from content_factory.models import ContentFactoryJob
        from organizations.models import Organization

        selected_mode = get_default_article_delivery_mode()
        job = ContentFactoryJob.objects.filter(job_id=job_id).first()
        if job:
            domain = str(job.domain or "").strip()
            if domain:
                org = Organization.objects.filter(domain=normalize_domain(domain)).first()
                config = getattr(org, "content_config", None) if org else None
            else:
                config = None
            resolved_mode, _ = resolve_article_delivery_mode(
                article_request=job.request_meta or {},
                config=config,
            )
            if resolved_mode:
                selected_mode = resolved_mode

    content_factory_url = _get_content_factory_base_url()
    endpoint = f"{content_factory_url.rstrip('/')}/api/runs/{job_id}/delivery-mode"

    headers = _build_content_factory_headers()

    logger.info("Selecting article delivery mode %s for %s", selected_mode, job_id)

    try:
        response = http_requests.post(
            endpoint,
            json={"delivery_mode": selected_mode},
            headers=headers,
            timeout=(3, 30),
        )
        if response.status_code in {200, 412}:
            payload = response.json()
            payload["status_code"] = response.status_code
            return payload

        logger.error(f"Content Factory delivery mode selection failed: {response.text}")
        raise ArticleGenerationError(f"Delivery mode selection failed: {response.text}")
    except http_requests.exceptions.RequestException as e:
        logger.error(f"Failed to connect to Content Factory: {e}")
        raise ArticleGenerationError(f"Failed to set delivery mode: {str(e)}")


def _maybe_auto_advance_run(job_id: str, result: dict) -> dict:
    status_value = str(result.get("status") or "").strip().lower()

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

    content_factory_url = _get_content_factory_base_url()
    status_endpoint = f"{content_factory_url.rstrip('/')}/api/runs/{job_id}"
    status_endpoint_legacy = f"{content_factory_url.rstrip('/')}/api/pipeline/publish/status/{job_id}"
    status_endpoint_old_backend = f"{content_factory_url.rstrip('/')}/api/v1/content/jobs/{job_id}"

    headers = _build_content_factory_headers()

    try:
        response = http_requests.get(status_endpoint, headers=headers, timeout=(3, 30))
        if response.status_code == 200:
            result = augment_status_with_job_tracking(job_id, _maybe_auto_advance_run(job_id, response.json()))
            # Detect failure and update local state + notify user
            if result.get('status') in BLOCKED_RUN_STATUSES:
                _handle_status_blocked(job_id, result)
            elif result.get('status') in REVIEWABLE_RUN_STATUSES:
                _handle_status_reviewable(job_id, result)
            elif result.get('status') in FAILURE_RUN_STATUSES:
                _handle_status_failure(job_id, result)
            return result
        for fallback_endpoint in (status_endpoint_legacy, status_endpoint_old_backend):
            response = http_requests.get(
                fallback_endpoint,
                headers=headers,
                timeout=(3, 30)
            )
            if response.status_code == 200:
                result = augment_status_with_job_tracking(job_id, _maybe_auto_advance_run(job_id, response.json()))
                if result.get('status') in BLOCKED_RUN_STATUSES:
                    _handle_status_blocked(job_id, result)
                elif result.get('status') in REVIEWABLE_RUN_STATUSES:
                    _handle_status_reviewable(job_id, result)
                elif result.get('status') in FAILURE_RUN_STATUSES:
                    _handle_status_failure(job_id, result)
                return result

        local_result = _load_local_run_snapshot(job_id)
        if local_result:
            local_result = augment_status_with_job_tracking(job_id, local_result)
            if local_result.get("status") in BLOCKED_RUN_STATUSES:
                _handle_status_blocked(job_id, local_result)
            elif local_result.get("status") in REVIEWABLE_RUN_STATUSES:
                _handle_status_reviewable(job_id, local_result)
            elif local_result.get("status") in FAILURE_RUN_STATUSES:
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
            if local_result.get("status") in BLOCKED_RUN_STATUSES:
                _handle_status_blocked(job_id, local_result)
            elif local_result.get("status") in REVIEWABLE_RUN_STATUSES:
                _handle_status_reviewable(job_id, local_result)
            elif local_result.get("status") in FAILURE_RUN_STATUSES:
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
    content_factory_url = _get_content_factory_base_url()
    publish_endpoint = f"{content_factory_url.rstrip('/')}/api/runs/{job_id}/approve"

    headers = _build_content_factory_headers()

    logger.info(f"Publishing article to: {publish_endpoint}")

    try:
        response = http_requests.post(
            publish_endpoint,
            json={},
            headers=headers,
            timeout=(3, 30),
        )
        
        if response.status_code == 200:
            return response.json()
        else:
            logger.error(f"Content Factory publish failed: {response.text}")
            raise ArticleGenerationError(f"Publish failed: {response.text}")
            
    except http_requests.exceptions.RequestException as e:
        logger.error(f"Failed to connect to Content Factory: {e}")
        raise ArticleGenerationError(f"Failed to publish: {str(e)}")


def promote_article_bundle(
    job_id: str,
    *,
    slack_user_id: str = None,
    requested_by_slack_user_id: str = None,
    domain: str = None,
    slack_channel_id: str = "",
    slack_thread_ts: str = "",
    slack_root_message_ts: str = "",
) -> dict:
    """
    Promote a completed content package or publish bundle into a child publish run.
    """
    from content_factory.models import ContentFactoryJob

    content_factory_url = _get_content_factory_base_url()
    promote_endpoint = f"{content_factory_url.rstrip('/')}/api/runs/{job_id}/promote-bundle"

    headers = _build_content_factory_headers()

    source_job = ContentFactoryJob.objects.filter(job_id=job_id).first()
    resolved_slack_user_id = slack_user_id or (source_job.slack_user_id if source_job else "") or ""
    source_request_meta = dict(getattr(source_job, "request_meta", {}) or {}) if source_job else {}
    resolved_requested_by_slack_user_id = (
        requested_by_slack_user_id
        or str(source_request_meta.get("requested_by_slack_user_id") or "").strip()
        or resolved_slack_user_id
    )
    resolved_domain = domain or (source_job.domain if source_job else "") or ""
    resolved_channel_id = slack_channel_id or (source_job.slack_channel_id if source_job else "") or ""
    resolved_thread_ts = slack_thread_ts or (source_job.slack_thread_ts if source_job else "") or ""
    resolved_root_ts = (
        slack_root_message_ts
        or (source_job.slack_root_message_ts if source_job else "")
        or resolved_thread_ts
    )

    logger.info(f"Promoting article bundle via: {promote_endpoint}")

    try:
        response = http_requests.post(
            promote_endpoint,
            json={},
            headers=headers,
            timeout=(3, 30),
        )

        if response.status_code != 200:
            if response.status_code == 412:
                _raise_content_factory_precondition(
                    response,
                    resolved_domain=resolved_domain,
                    slack_user_id=resolved_slack_user_id,
                )
            logger.error(f"Content Factory promote-bundle failed: {response.text}")
            raise ArticleGenerationError(f"Promote bundle failed: {response.text}")

        payload = response.json()
        child_job_id = str(payload.get("job_id") or payload.get("run_id") or "").strip()
        if child_job_id:
            _store_job_tracking_record(
                child_job_id,
                domain=resolved_domain,
                slack_user_id=resolved_slack_user_id,
                request_meta={
                    "source_run_id": job_id,
                    "promotion_source": "promote_bundle",
                    "requested_delivery_mode": "publish_code",
                    "requested_by_slack_user_id": resolved_requested_by_slack_user_id,
                },
                slack_channel_id=resolved_channel_id,
                slack_thread_ts=resolved_thread_ts,
                slack_root_message_ts=resolved_root_ts,
                default_status="queued",
            )
            if source_job:
                request_meta = dict(source_job.request_meta or {})
                request_meta["promoted_publish_job_id"] = child_job_id
                request_meta["publish_stage"] = "promotion_requested"
                if resolved_requested_by_slack_user_id:
                    request_meta["requested_by_slack_user_id"] = resolved_requested_by_slack_user_id
                if request_meta != (source_job.request_meta or {}):
                    source_job.request_meta = request_meta
                    source_job.save(update_fields=["request_meta", "updated_at"])

        return payload

    except http_requests.exceptions.RequestException as e:
        logger.error(f"Failed to connect to Content Factory: {e}")
        raise ArticleGenerationError(f"Failed to promote bundle: {str(e)}")


def publish_article_as_pr(
    job_id: str,
    *,
    slack_user_id: str = None,
    requested_by_slack_user_id: str = None,
    domain: str = None,
    slack_channel_id: str = "",
    slack_thread_ts: str = "",
    slack_root_message_ts: str = "",
) -> dict:
    """
    Promote a completed content-only article into a child publish run that creates a draft PR.
    """
    return promote_article_bundle(
        job_id,
        slack_user_id=slack_user_id,
        requested_by_slack_user_id=requested_by_slack_user_id,
        domain=domain,
        slack_channel_id=slack_channel_id,
        slack_thread_ts=slack_thread_ts,
        slack_root_message_ts=slack_root_message_ts,
    )


def confirm_topic(
    domain: str,
    confirmed_keyword: str,
    slack_user_id: str,
    requested_by_slack_user_id: str = None,
    custom_title: str = None,
    skip_alternatives: list = None,
    source_run_id: str = None,
    slack_channel_id: str = "",
    slack_thread_ts: str = "",
    slack_root_message_ts: str = "",
    progress_message_ts: str = "",
    delivery_mode: str = None,
    delivery_mode_confirmed: Optional[bool] = None,
    request_source: str = CONTENT_FACTORY_REQUEST_SOURCE,
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
    if request_source != CONTENT_FACTORY_REQUEST_SOURCE:
        raise ArticleGenerationError("Content Factory article requests must originate from Roo Slackbot.")
    source_job = None
    if source_run_id:
        from content_factory.models import ContentFactoryJob

        source_job = ContentFactoryJob.objects.filter(job_id=source_run_id).first()
    if source_job and not (slack_channel_id or slack_thread_ts or slack_root_message_ts):
        if source_job:
            slack_channel_id = source_job.slack_channel_id or ""
            slack_thread_ts = source_job.slack_thread_ts or ""
            slack_root_message_ts = source_job.slack_root_message_ts or slack_thread_ts
    deferred_charge_started = bool(source_job and _job_uses_deferred_billing(source_job))
    if source_job:
        source_job = _charge_deferred_discovery_job_if_needed(
            source_job=source_job,
            slack_user_id=slack_user_id,
            domain=normalized_domain,
            confirmed_keyword=confirmed_keyword,
            custom_title=custom_title,
        )
        progress_message_ts = source_job.progress_message_ts or progress_message_ts

    existing_child_job = _find_active_confirm_child_job(
        source_run_id=source_run_id,
        confirmed_keyword=confirmed_keyword,
        custom_title=custom_title,
    )
    if existing_child_job:
        logger.info(
            "Reusing existing confirmed-topic child job %s for source_run_id=%s status=%s",
            existing_child_job.job_id,
            source_run_id,
            existing_child_job.status,
        )
        return _serialize_existing_confirm_child_job(
            existing_child_job,
            source_run_id=source_run_id,
        )

    config = None
    try:
        org = Organization.objects.get(domain=normalized_domain)
        config = getattr(org, 'content_config', None)
    except Organization.DoesNotExist:
        config = None

    article_system, article_system_source = resolve_article_system_with_source(config)
    logger.info(
        "Confirmed-topic article-system readiness for %s resolved via %s: state=%s path=%s",
        normalized_domain,
        article_system_source,
        article_system.get('state'),
        article_system.get('directory_path') or article_system.get('directory_name'),
    )
    source_request_meta = dict(getattr(source_job, "request_meta", {}) or {}) if source_job else {}
    resolved_requested_by_slack_user_id = (
        str(requested_by_slack_user_id or "").strip()
        or str(source_request_meta.get("requested_by_slack_user_id") or "").strip()
        or str(slack_user_id or "").strip()
    )
    request_context = {
        "delivery_mode": delivery_mode if delivery_mode is not None else source_request_meta.get("delivery_mode"),
        "delivery_mode_confirmed": (
            delivery_mode_confirmed
            if delivery_mode_confirmed is not None
            else source_request_meta.get("delivery_mode_confirmed")
        ),
    }
    delivery_mode, delivery_mode_confirmed = resolve_article_delivery_mode(
        article_request=request_context,
        config=config,
    )

    github_repo, _resolved_config, github_repo_source = _resolve_github_repo_for_domain(
        normalized_domain,
        slack_user_id,
    )
    if github_repo:
        logger.info("Using %s-level GitHub repo context for topic confirmation", github_repo_source)
    else:
        logger.info("No GitHub repo context resolved for topic confirmation on %s", normalized_domain)

    payload = {
        "domain": domain,
        "slack_user_id": slack_user_id,
        "topic": custom_title or confirmed_keyword,
        "target_keyword": confirmed_keyword,
        "custom_title": custom_title,
        "request_source": request_source,
    }
    if resolved_requested_by_slack_user_id and resolved_requested_by_slack_user_id != str(slack_user_id or "").strip():
        payload["requested_by_slack_user_id"] = resolved_requested_by_slack_user_id
    if github_repo:
        payload["github_repo"] = github_repo
    if delivery_mode is not None:
        payload["delivery_mode"] = delivery_mode
        payload["delivery_mode_confirmed"] = delivery_mode_confirmed

    # Include skip_alternatives if provided (temporary rejection/cooldown feedback)
    if skip_alternatives:
        payload["skip_alternatives"] = skip_alternatives
    if source_run_id:
        payload["source_run_id"] = source_run_id

    # 3. Call Content Factory
    content_factory_url = _get_content_factory_base_url()
    confirm_endpoint = f"{content_factory_url.rstrip('/')}/api/runs/article"
    headers = _build_content_factory_headers()

    # Debug logging
    masked_payload = payload.copy()
    logger.info(f"Confirming topic at {confirm_endpoint} with payload: {masked_payload}")

    try:
        response = _post_content_factory_queue_request(
            confirm_endpoint,
            payload=payload,
            headers=headers,
            operation="confirm_topic",
            domain=normalized_domain,
            source_run_id=source_run_id,
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
                        "request_source": request_source,
                        "requested_by_slack_user_id": resolved_requested_by_slack_user_id,
                        "slack_channel_id": slack_channel_id or "",
                        "slack_thread_ts": slack_thread_ts or "",
                        "slack_root_message_ts": slack_root_message_ts or slack_thread_ts or "",
                    },
                    slack_channel_id=slack_channel_id,
                    slack_thread_ts=slack_thread_ts,
                    slack_root_message_ts=slack_root_message_ts,
                    default_status="queued",
                    progress_message_ts=progress_message_ts,
                    last_progress_milestone_key="queued",
                    last_progress_updated_at=timezone.now(),
                )
            return data
        else:
            if response.status_code == 412:
                if deferred_charge_started:
                    _refund_deferred_discovery_job_on_confirm_failure(
                        source_job=source_job,
                        slack_user_id=slack_user_id,
                        domain=normalized_domain,
                        reason="topic confirmation prerequisite response",
                    )
                _raise_content_factory_precondition(
                    response,
                    resolved_domain=normalized_domain,
                    slack_user_id=slack_user_id,
                )
            logger.error(f"Content Factory confirm topic failed: {response.text}")
            if deferred_charge_started:
                _refund_deferred_discovery_job_on_confirm_failure(
                    source_job=source_job,
                    slack_user_id=slack_user_id,
                    domain=normalized_domain,
                    reason=response.text,
                )
            raise ArticleGenerationError(f"Topic confirmation failed: {response.text}")
            
    except ContentFactoryBackendUnavailableError:
        if deferred_charge_started:
            _refund_deferred_discovery_job_on_confirm_failure(
                source_job=source_job,
                slack_user_id=slack_user_id,
                domain=normalized_domain,
                reason="Content Factory queue path unavailable during topic confirmation",
            )
        raise
