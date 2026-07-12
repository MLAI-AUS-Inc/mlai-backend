from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from core.permissions import HasRooApiKey, HasStrictRooApiKey
from django.db.models import Q
from django.utils import timezone
from typing import Optional
import logging

from integrations.services.article_generation import (
    attach_progress_message,
    trigger_article_generation,
    check_generation_status,
    publish_article,
    publish_article_as_pr,
    confirm_topic,
    set_article_delivery_mode,
    ArticleGenerationError,
    ArticleSystemActionRequiredError,
    ContentFactoryBackendUnavailableError,
    GitHubReconnectRequiredError,
    InsufficientRooPointsError,
    CONTENT_FACTORY_BILLING_STATUS_DEFERRED,
    SCHEDULED_DAILY_TRIGGER_SOURCE,
)
from content_factory.progress import maybe_send_still_working_ping, upsert_live_progress_card
from content_factory.contract import (
    CONTENT_FACTORY_REQUEST_SOURCE,
    require_roo_request_source,
)

logger = logging.getLogger(__name__)
PUBLISH_READY_STAGE = "content_ready"
PUBLISH_IN_PROGRESS_SOURCE_STAGES = {
    "promotion_requested",
    "awaiting_preview",
    "preview_ready",
    "needs_review",
    "pr_opened",
    "auto_approved",
}
PUBLISH_IN_PROGRESS_CHILD_STAGES = PUBLISH_IN_PROGRESS_SOURCE_STAGES - {"promotion_requested"}
CONFIRM_THREAD_ACTIVE_STATUSES = {
    "queued",
    "generating",
    "blocked",
    "auth_required",
    "awaiting_delivery_mode",
    "awaiting_approval",
    "needs_review",
    "pr_opened",
}


def _validate_roo_content_request(request, *, require_client_request_id: bool = False) -> Optional[Response]:
    try:
        require_roo_request_source(request.data.get("request_source"))
    except ValueError as exc:
        return Response(
            {"error": str(exc)},
            status=status.HTTP_403_FORBIDDEN,
        )

    if require_client_request_id and not str(request.data.get("client_request_id") or "").strip():
        return Response(
            {"error": "client_request_id is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return None


def _handle_backend_unavailable_error(exc: ContentFactoryBackendUnavailableError) -> Response:
    payload = dict(getattr(exc, "payload", {}) or {})
    if "error" not in payload:
        payload["error"] = str(exc)
    payload.setdefault("message", payload["error"])
    payload.setdefault("error_code", "CONTENT_FACTORY_UNAVAILABLE")
    payload.setdefault("status", "backend_unavailable")
    payload.setdefault("retryable", True)
    return Response(payload, status=status.HTTP_503_SERVICE_UNAVAILABLE)


def _handle_insufficient_roo_points_error(exc: InsufficientRooPointsError) -> Response:
    payload = dict(getattr(exc, "payload", {}) or {})
    payload.setdefault("error", str(exc))
    payload.setdefault("detail", payload["error"])
    payload.setdefault("error_code", "INSUFFICIENT_ROO_POINTS")
    return Response(payload, status=status.HTTP_402_PAYMENT_REQUIRED)


def _resolve_requested_by_slack_user_id(
    *,
    slack_user_id: Optional[str],
    article_request: Optional[dict] = None,
    request_data=None,
    job=None,
) -> str:
    candidates = []
    if request_data is not None:
        try:
            candidates.append(request_data.get("requested_by_slack_user_id"))
        except Exception:
            pass
    if isinstance(article_request, dict):
        candidates.append(article_request.get("requested_by_slack_user_id"))
    if job is not None:
        candidates.append((getattr(job, "request_meta", {}) or {}).get("requested_by_slack_user_id"))
    candidates.append(slack_user_id)

    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def _is_delegated_content_request(*, slack_user_id: Optional[str], requested_by_slack_user_id: Optional[str]) -> bool:
    effective = str(slack_user_id or "").strip()
    requester = str(requested_by_slack_user_id or "").strip()
    return bool(requester and effective and requester != effective)


def _resolve_explicit_requested_by_slack_user_id(
    *,
    slack_user_id: Optional[str],
    request_data=None,
    job=None,
) -> str:
    effective = str(slack_user_id or "").strip()
    explicit_request_supplied = False
    explicit_request_candidate = None
    if request_data is not None:
        try:
            explicit_request_supplied = "requested_by_slack_user_id" in request_data
        except Exception:
            pass
        try:
            explicit_request_candidate = request_data.get("requested_by_slack_user_id")
        except Exception:
            explicit_request_candidate = None

    if explicit_request_supplied:
        value = str(explicit_request_candidate or "").strip()
        return value if value and value != effective else ""

    if job is not None:
        value = str((getattr(job, "request_meta", {}) or {}).get("requested_by_slack_user_id") or "").strip()
        if value and value != effective:
            return value
    return ""


def _handle_prerequisite_error(error_str: str, slack_user_id: str, article_request: dict = None) -> Response:
    """
    Parse a PREREQUISITE_MISSING error, store pending intent, and return a structured 412 response.

    Error format: "PREREQUISITE_MISSING:{missing_step}:{domain}:{message}"
    """
    parts = error_str.split(":", 3)
    missing_step = parts[1] if len(parts) > 1 else 'unknown'
    domain = parts[2] if len(parts) > 2 else ''
    message = parts[3] if len(parts) > 3 else error_str

    # Store the article request as pending intent so it can be auto-resumed
    pending_stored = False
    if slack_user_id and article_request:
        try:
            from integrations.models import UserIntegration
            integration, _ = UserIntegration.objects.get_or_create(slack_user_id=slack_user_id)
            integration.pending_intent = {
                "type": "write_article",
                "article_request": article_request,
                "stored_at": timezone.now().isoformat(),
            }
            integration.save()
            pending_stored = True
        except Exception as e:
            logger.warning(f"Failed to store pending intent for {slack_user_id}: {e}")

    step_label = 'Scan' if missing_step == 'scan' else 'Scaffold'
    return Response({
        "error": message,
        "error_code": "PREREQUISITE_MISSING",
        "missing_step": missing_step,
        "domain": domain,
        "pending_intent_stored": pending_stored,
        "hint": f"{step_label} the codebase first, then retry.",
    }, status=status.HTTP_412_PRECONDITION_FAILED)


def _store_pending_article_intent(slack_user_id: str, article_request: dict = None) -> bool:
    return _store_pending_intent(
        slack_user_id,
        {
            "type": "write_article",
            "article_request": article_request,
            "stored_at": timezone.now().isoformat(),
        } if article_request else None,
    )


def _store_pending_intent(slack_user_id: str, pending_intent: dict = None) -> bool:
    pending_stored = False
    if slack_user_id and pending_intent:
        try:
            from integrations.models import UserIntegration
            integration, _ = UserIntegration.objects.get_or_create(slack_user_id=slack_user_id)
            integration.pending_intent = pending_intent
            integration.save()
            pending_stored = True
        except Exception as e:
            logger.warning(f"Failed to store pending intent for {slack_user_id}: {e}")
    return pending_stored


def _handle_auth_required_error(
    error: GitHubReconnectRequiredError,
    slack_user_id: str,
    *,
    article_request: dict = None,
    pending_intent: dict = None,
    requested_by_slack_user_id: Optional[str] = None,
) -> Response:
    resolved_requested_by_slack_user_id = (
        str(requested_by_slack_user_id or "").strip()
        or _resolve_requested_by_slack_user_id(
            slack_user_id=slack_user_id,
            article_request=article_request,
        )
    )
    delegated_request = _is_delegated_content_request(
        slack_user_id=slack_user_id,
        requested_by_slack_user_id=resolved_requested_by_slack_user_id,
    )
    intent = pending_intent
    if intent is None and article_request:
        intent = {
            "type": "write_article",
            "article_request": article_request,
            "stored_at": timezone.now().isoformat(),
        }

    pending_stored = False
    if not delegated_request:
        pending_stored = _store_pending_intent(slack_user_id, intent)
    payload = dict(error.payload)
    if resolved_requested_by_slack_user_id:
        payload["requested_by_slack_user_id"] = resolved_requested_by_slack_user_id
    payload["pending_intent_stored"] = pending_stored
    return Response(payload, status=status.HTTP_412_PRECONDITION_FAILED)


def _resume_pending_article_intent(slack_user_id: str, domain: str) -> Optional[dict]:
    from integrations.models import UserIntegration
    from integrations.utils import normalize_domain

    integration = UserIntegration.objects.filter(slack_user_id=slack_user_id).first()
    if not integration or not integration.pending_intent:
        return None

    intent = integration.pending_intent
    article_request = intent.get("article_request") or {}
    if intent.get("type") != "write_article":
        return None
    if normalize_domain(article_request.get("domain", "")) != normalize_domain(domain):
        return None

    integration.pending_intent = None
    integration.save(update_fields=["pending_intent"])
    return trigger_article_generation(slack_user_id, article_request)


def _handle_article_system_action_required(error: ArticleSystemActionRequiredError, slack_user_id: str, article_request: dict = None) -> Response:
    requested_by_slack_user_id = _resolve_requested_by_slack_user_id(
        slack_user_id=slack_user_id,
        article_request=article_request,
    )
    pending_stored = _store_pending_article_intent(requested_by_slack_user_id or slack_user_id, article_request)
    return Response(
        {
            "error": str(error),
            "error_code": "ARTICLE_SYSTEM_ACTION_REQUIRED",
            "domain": error.domain,
            "article_system": error.article_system,
            "recommended_action": error.recommended_action,
            "article_system_resolution_source": error.resolution_source,
            "pending_intent_stored": pending_stored,
            "hint": error.hint,
        },
        status=status.HTTP_412_PRECONDITION_FAILED,
    )


def _resolve_publishable_job_for_thread(
    *,
    slack_user_id: str,
    slack_channel_id: str,
    slack_thread_ts: str,
    domain: Optional[str] = None,
):
    from content_factory.models import ContentFactoryJob

    resolved_user_id = str(slack_user_id or "").strip()
    resolved_channel_id = str(slack_channel_id or "").strip()
    resolved_thread_ts = str(slack_thread_ts or "").strip()
    resolved_domain = str(domain or "").strip().lower()

    if not (resolved_user_id and resolved_channel_id and resolved_thread_ts):
        return None

    jobs = list(
        ContentFactoryJob.objects.filter(
            slack_channel_id=resolved_channel_id,
        ).filter(
            Q(slack_thread_ts=resolved_thread_ts) | Q(slack_root_message_ts=resolved_thread_ts)
        )
    )
    jobs = [
        job
        for job in jobs
        if resolved_user_id in {
            str(job.slack_user_id or "").strip(),
            str((job.request_meta or {}).get("requested_by_slack_user_id") or "").strip(),
        }
    ]
    if resolved_domain:
        domain_matched = [job for job in jobs if str(job.domain or "").strip().lower() == resolved_domain]
        if domain_matched:
            jobs = domain_matched

    source_jobs = [job for job in jobs if not str((job.request_meta or {}).get("source_run_id") or "").strip()]
    if not source_jobs:
        return None

    def sort_key(job):
        request_meta = dict(job.request_meta or {})
        publish_stage = str(request_meta.get("publish_stage") or "").strip()
        promoted_publish_job_id = str(request_meta.get("promoted_publish_job_id") or "").strip()
        return (
            0 if publish_stage in PUBLISH_IN_PROGRESS_SOURCE_STAGES else 1,
            0 if publish_stage == PUBLISH_READY_STAGE else 1,
            0 if promoted_publish_job_id else 1,
            -int(job.created_at.timestamp()),
        )

    source_job = sorted(source_jobs, key=sort_key)[0]
    source_meta = dict(source_job.request_meta or {})
    source_publish_stage = str(source_meta.get("publish_stage") or "").strip() or "unknown"
    promoted_publish_job_id = str(source_meta.get("promoted_publish_job_id") or "").strip()

    if source_publish_stage == PUBLISH_READY_STAGE:
        resolved = {
            "resolution": "ready",
            "job_id": source_job.job_id,
            "domain": source_job.domain,
            "publish_stage": source_publish_stage,
        }
        requested_by_slack_user_id = str((source_job.request_meta or {}).get("requested_by_slack_user_id") or "").strip()
        if requested_by_slack_user_id:
            resolved["requested_by_slack_user_id"] = requested_by_slack_user_id
        return resolved

    if source_publish_stage in PUBLISH_IN_PROGRESS_SOURCE_STAGES and promoted_publish_job_id:
        child_job = ContentFactoryJob.objects.filter(job_id=promoted_publish_job_id).first()
        child_meta = dict(getattr(child_job, "request_meta", {}) or {})
        child_publish_stage = str(child_meta.get("publish_stage") or "").strip()
        effective_publish_stage = child_publish_stage or source_publish_stage
        if (
            child_publish_stage in PUBLISH_IN_PROGRESS_CHILD_STAGES
            or source_publish_stage in PUBLISH_IN_PROGRESS_SOURCE_STAGES
        ):
            resolved = {
                "resolution": "in_progress",
                "job_id": source_job.job_id,
                "domain": source_job.domain,
                "publish_stage": effective_publish_stage,
                "promoted_publish_job_id": promoted_publish_job_id,
            }
            requested_by_slack_user_id = str((source_job.request_meta or {}).get("requested_by_slack_user_id") or "").strip()
            if requested_by_slack_user_id:
                resolved["requested_by_slack_user_id"] = requested_by_slack_user_id
            return resolved

    return None


def _resolve_confirm_job_for_thread(
    *,
    slack_user_id: str,
    slack_channel_id: str,
    slack_thread_ts: str,
    domain: Optional[str] = None,
    source_job_id: Optional[str] = None,
):
    from content_factory.models import ContentFactoryJob

    resolved_user_id = str(slack_user_id or "").strip()
    resolved_channel_id = str(slack_channel_id or "").strip()
    resolved_thread_ts = str(slack_thread_ts or "").strip()
    resolved_domain = str(domain or "").strip().lower()
    resolved_source_job_id = str(source_job_id or "").strip()

    if not (resolved_user_id and resolved_channel_id and resolved_thread_ts):
        return None

    jobs = list(
        ContentFactoryJob.objects.filter(
            slack_channel_id=resolved_channel_id,
        ).filter(
            Q(slack_thread_ts=resolved_thread_ts) | Q(slack_root_message_ts=resolved_thread_ts)
        )
    )
    jobs = [
        job
        for job in jobs
        if resolved_user_id in {
            str(job.slack_user_id or "").strip(),
            str((job.request_meta or {}).get("requested_by_slack_user_id") or "").strip(),
        }
    ]
    if resolved_domain:
        domain_matched = [job for job in jobs if str(job.domain or "").strip().lower() == resolved_domain]
        if domain_matched:
            jobs = domain_matched

    if resolved_source_job_id:
        chained = []
        for job in jobs:
            request_meta = dict(job.request_meta or {})
            if job.job_id == resolved_source_job_id or str(request_meta.get("source_run_id") or "").strip() == resolved_source_job_id:
                chained.append(job)
        if chained:
            jobs = chained

    candidates = [job for job in jobs if str(job.status or "").strip() in CONFIRM_THREAD_ACTIVE_STATUSES]
    if not candidates:
        return None

    status_rank = {
        "awaiting_delivery_mode": 0,
        "blocked": 1,
        "auth_required": 2,
        "generating": 3,
        "queued": 4,
        "awaiting_approval": 5,
        "needs_review": 6,
        "pr_opened": 7,
    }

    def sort_key(job):
        request_meta = dict(job.request_meta or {})
        is_exact_source_match = (
            resolved_source_job_id
            and (
                job.job_id == resolved_source_job_id
                or str(request_meta.get("source_run_id") or "").strip() == resolved_source_job_id
            )
        )
        is_child = bool(str(request_meta.get("source_run_id") or "").strip())
        return (
            0 if is_exact_source_match else 1,
            0 if is_child else 1,
            status_rank.get(str(job.status or "").strip(), 99),
            -int(job.updated_at.timestamp()),
            -int(job.created_at.timestamp()),
        )

    resolved_job = sorted(candidates, key=sort_key)[0]
    request_meta = dict(resolved_job.request_meta or {})
    error_code = str(request_meta.get("blocked_error_code") or "").strip()
    if not error_code and str(resolved_job.status or "").strip() == "auth_required":
        error_code = "AUTH_REQUIRED"

    payload = {
        "resolution": "in_progress",
        "requested_action": "confirm_topic",
        "job_id": resolved_job.job_id,
        "active_job_id": resolved_job.job_id,
        "status": resolved_job.status,
        "domain": resolved_job.domain,
        "source_run_id": str(request_meta.get("source_run_id") or resolved_job.job_id).strip(),
    }
    requested_by_slack_user_id = str(request_meta.get("requested_by_slack_user_id") or "").strip()
    if requested_by_slack_user_id:
        payload["requested_by_slack_user_id"] = requested_by_slack_user_id
    if error_code:
        payload["error_code"] = error_code
    if resolved_job.error_message:
        payload["error"] = resolved_job.error_message
        payload["message"] = resolved_job.error_message
    return payload

class ContentGenerateView(APIView):
    """
    Trigger content generation pipeline.
    POST /api/v1/content/generate
    """
    authentication_classes = []
    permission_classes = [HasStrictRooApiKey]

    def post(self, request):
        slack_user_id = request.data.get('slack_user_id')
        validation_error = _validate_roo_content_request(request, require_client_request_id=True)
        if validation_error:
            return validation_error
        
        # Validate required fields
        if not slack_user_id:
             return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        # Pass specific fields (the service will validate domain/topic)
        article_request = {
            "domain": request.data.get('domain'),
            "topic": request.data.get('topic'),
            "target_keyword": request.data.get('target_keyword'),
            "context": request.data.get('context'),
            "delivery_mode": request.data.get('delivery_mode'),
            "delivery_mode_confirmed": request.data.get('delivery_mode_confirmed'),
            "slack_channel_id": request.data.get('slack_channel_id'),
            "slack_thread_ts": request.data.get('slack_thread_ts'),
            "slack_root_message_ts": request.data.get('slack_root_message_ts') or request.data.get('slack_thread_ts'),
            "progress_message_ts": request.data.get('progress_message_ts'),
            "request_source": request.data.get("request_source"),
            "client_request_id": request.data.get("client_request_id"),
            "user_email": request.data.get("user_email"),
            "user_first_name": request.data.get("user_first_name"),
            "user_last_name": request.data.get("user_last_name"),
            "user_avatar_url": request.data.get("user_avatar_url"),
        }
        if request.data.get("notification_context"):
            article_request["notification_context"] = request.data.get("notification_context")
        requested_by_slack_user_id = _resolve_explicit_requested_by_slack_user_id(
            slack_user_id=slack_user_id,
            request_data=request.data,
        )
        if requested_by_slack_user_id:
            article_request["requested_by_slack_user_id"] = requested_by_slack_user_id

        try:
            result = trigger_article_generation(slack_user_id, article_request)
            return Response(result, status=status.HTTP_202_ACCEPTED)
        except ContentFactoryBackendUnavailableError as e:
            logger.warning(f"Content Factory backend unavailable during generation: {e}")
            return _handle_backend_unavailable_error(e)
        except GitHubReconnectRequiredError as e:
            logger.warning(f"GitHub reconnect required: {e}")
            return _handle_auth_required_error(
                e,
                slack_user_id,
                article_request=article_request,
                requested_by_slack_user_id=request.data.get("requested_by_slack_user_id"),
            )
        except ArticleSystemActionRequiredError as e:
            logger.warning(f"Article system action required: {e}")
            return _handle_article_system_action_required(e, slack_user_id, article_request)
        except InsufficientRooPointsError as e:
            return _handle_insufficient_roo_points_error(e)
        except ArticleGenerationError as e:
            logger.warning(f"Generation error: {e}")
            error_str = str(e)

            # Handle prerequisite errors — store pending intent and return structured 412
            if error_str.startswith("PREREQUISITE_MISSING:"):
                return _handle_prerequisite_error(error_str, slack_user_id, article_request)

            return Response({"error": error_str}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception(f"Unexpected error in generation view: {e}")
            return Response({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ContentStatusView(APIView):
    """
    Get generation job status.
    GET /api/v1/content/jobs/{job_id}
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, job_id):
        if not job_id:
            return Response({"error": "job_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = check_generation_status(job_id)
            return Response(result, status=status.HTTP_200_OK)
        except ArticleGenerationError as e:
            if "not found" in str(e).lower():
                return Response({"error": str(e)}, status=status.HTTP_404_NOT_FOUND)
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception(f"Unexpected error in status view: {e}")
            return Response({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ContentResolveThreadView(APIView):
    """
    Resolve the promotable content job for a Slack thread.
    POST /api/v1/content/jobs/resolve-thread
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        slack_user_id = str(request.data.get("slack_user_id") or "").strip()
        requested_by_slack_user_id = str(request.data.get("requested_by_slack_user_id") or "").strip()
        slack_channel_id = str(request.data.get("slack_channel_id") or "").strip()
        slack_thread_ts = str(request.data.get("slack_thread_ts") or "").strip()
        domain = str(request.data.get("domain") or "").strip()
        requested_action = str(request.data.get("requested_action") or "").strip()
        thread_lookup_slack_user_id = requested_by_slack_user_id or slack_user_id

        if requested_action not in {"publish_pr", "confirm_topic"}:
            return Response(
                {"error": "requested_action must be publish_pr or confirm_topic"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not all([thread_lookup_slack_user_id, slack_channel_id, slack_thread_ts]):
            return Response(
                {"error": "slack_user_id, slack_channel_id, and slack_thread_ts are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if requested_action == "confirm_topic":
            resolved = _resolve_confirm_job_for_thread(
                slack_user_id=thread_lookup_slack_user_id,
                slack_channel_id=slack_channel_id,
                slack_thread_ts=slack_thread_ts,
                domain=domain or None,
                source_job_id=request.data.get("job_id"),
            )
        else:
            resolved = _resolve_publishable_job_for_thread(
                slack_user_id=thread_lookup_slack_user_id,
                slack_channel_id=slack_channel_id,
                slack_thread_ts=slack_thread_ts,
                domain=domain or None,
            )
        if not resolved:
            return Response(
                {
                    "error": (
                        "No active content-factory article was found for this Slack thread."
                        if requested_action == "confirm_topic"
                        else "No promotable content-ready article was found for this Slack thread."
                    ),
                    "requested_action": requested_action,
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        resolved["effective_slack_user_id"] = slack_user_id
        if requested_by_slack_user_id:
            resolved.setdefault("requested_by_slack_user_id", requested_by_slack_user_id)
        return Response(resolved, status=status.HTTP_200_OK)


class ContentPublishView(APIView):
    """
    Approve a generated article run for ready-for-review publication.
    POST /api/v1/content/publish/{job_id}
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, job_id):
        if not job_id:
            return Response({"error": "job_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        slack_user_id = request.data.get('slack_user_id')

        try:
            result = publish_article(job_id, slack_user_id=slack_user_id)
            return Response(result, status=status.HTTP_200_OK)
        except ArticleGenerationError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception(f"Unexpected error in publish view: {e}")
            return Response({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ContentPublishPrView(APIView):
    """
    Promote a completed content-only article into a child publish run that creates a draft PR.
    POST /api/v1/content/jobs/{job_id}/publish-pr
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, job_id):
        from content_factory.models import ContentFactoryJob

        if not job_id:
            return Response({"error": "job_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        slack_user_id = request.data.get('slack_user_id')
        job = ContentFactoryJob.objects.filter(job_id=job_id).first()
        if not job:
            return Response({"error": "Job not found"}, status=status.HTTP_404_NOT_FOUND)
        resolved_slack_user_id = slack_user_id or job.slack_user_id
        requested_by_slack_user_id = _resolve_explicit_requested_by_slack_user_id(
            slack_user_id=slack_user_id or job.slack_user_id,
            request_data=request.data,
            job=job,
        )

        try:
            publish_kwargs = {
                "slack_user_id": resolved_slack_user_id,
                "domain": job.domain,
                "slack_channel_id": job.slack_channel_id or "",
                "slack_thread_ts": job.slack_thread_ts or "",
                "slack_root_message_ts": job.slack_root_message_ts or "",
            }
            if requested_by_slack_user_id:
                publish_kwargs["requested_by_slack_user_id"] = requested_by_slack_user_id

            result = publish_article_as_pr(
                job_id,
                **publish_kwargs,
            )
            return Response(result, status=status.HTTP_200_OK)
        except GitHubReconnectRequiredError as e:
            return _handle_auth_required_error(
                e,
                slack_user_id or job.slack_user_id,
                requested_by_slack_user_id=requested_by_slack_user_id,
                pending_intent={
                    "type": "publish_article_as_pr",
                    "job_id": job_id,
                    "domain": job.domain,
                    "requested_by_slack_user_id": requested_by_slack_user_id,
                    "stored_at": timezone.now().isoformat(),
                },
            )
        except ArticleGenerationError as e:
            return Response({"error": str(e)}, status=status.HTTP_409_CONFLICT)
        except Exception as e:
            logger.exception(f"Unexpected error in publish-pr view: {e}")
            return Response({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ContentConfirmView(APIView):
    """
    Confirm topic selection and trigger Phase 2 generation.
    POST /api/v1/content/confirm

    Request Body:
        domain: str (required) - Organization domain
        confirmed_keyword: str (required) - Keyword to generate article for
        slack_user_id: str (required) - Slack user ID
        custom_title: str (optional) - Custom article title
        skip_alternatives: list[str] (optional) - Keywords to mark as temporarily rejected/cooldown topics
    """
    authentication_classes = []
    permission_classes = [HasStrictRooApiKey]

    def post(self, request):
        from content_factory.models import ContentFactoryJob

        validation_error = _validate_roo_content_request(request)
        if validation_error:
            return validation_error
        domain = request.data.get('domain')
        confirmed_keyword = request.data.get('confirmed_keyword')
        slack_user_id = request.data.get('slack_user_id')
        custom_title = request.data.get('custom_title')
        skip_alternatives = request.data.get('skip_alternatives')
        source_run_id = request.data.get('source_run_id')
        slack_channel_id = request.data.get('slack_channel_id')
        slack_thread_ts = request.data.get('slack_thread_ts')
        slack_root_message_ts = request.data.get('slack_root_message_ts') or slack_thread_ts
        progress_message_ts = request.data.get('progress_message_ts')
        requested_by_slack_user_id = _resolve_explicit_requested_by_slack_user_id(
            slack_user_id=slack_user_id,
            request_data=request.data,
        )

        if not all([domain, confirmed_keyword, slack_user_id]):
            return Response(
                {"error": "domain, confirmed_keyword, and slack_user_id are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate skip_alternatives is a list if provided
        if skip_alternatives is not None and not isinstance(skip_alternatives, list):
            return Response(
                {"error": "skip_alternatives must be a list of strings"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            confirm_kwargs = {
                "domain": domain,
                "confirmed_keyword": confirmed_keyword,
                "slack_user_id": slack_user_id,
                "custom_title": custom_title,
                "skip_alternatives": skip_alternatives,
                "source_run_id": source_run_id,
                "slack_channel_id": slack_channel_id,
                "slack_thread_ts": slack_thread_ts,
                "slack_root_message_ts": slack_root_message_ts,
                "progress_message_ts": progress_message_ts,
                "delivery_mode": request.data.get("delivery_mode"),
                "delivery_mode_confirmed": request.data.get("delivery_mode_confirmed"),
                "request_source": request.data.get("request_source"),
            }
            if request.data.get("notification_context"):
                confirm_kwargs["notification_context"] = request.data.get("notification_context")
            if requested_by_slack_user_id:
                confirm_kwargs["requested_by_slack_user_id"] = requested_by_slack_user_id

            result = confirm_topic(
                **confirm_kwargs,
            )
            new_job_id = result.get("job_id") or result.get("run_id")
            if source_run_id and new_job_id:
                source_job = ContentFactoryJob.objects.filter(job_id=source_run_id).first()
                if source_job:
                    child_request_meta = {
                        "domain": domain,
                        "topic": custom_title or confirmed_keyword,
                        "target_keyword": confirmed_keyword,
                        "custom_title": custom_title,
                        "skip_alternatives": skip_alternatives,
                        "source_run_id": source_run_id,
                        "request_source": request.data.get("request_source"),
                        "requested_by_slack_user_id": requested_by_slack_user_id,
                        "slack_channel_id": slack_channel_id,
                        "slack_thread_ts": slack_thread_ts,
                        "slack_root_message_ts": slack_root_message_ts,
                    }
                    if request.data.get("notification_context"):
                        child_request_meta["notification_context"] = request.data.get("notification_context")
                    ContentFactoryJob.objects.update_or_create(
                        job_id=new_job_id,
                        defaults={
                            "domain": domain,
                            "slack_user_id": slack_user_id,
                            "status": "generating",
                            "request_meta": child_request_meta,
                            "slack_channel_id": slack_channel_id or source_job.slack_channel_id,
                            "slack_thread_ts": slack_thread_ts or source_job.slack_thread_ts,
                            "slack_root_message_ts": (
                                slack_root_message_ts
                                or source_job.slack_root_message_ts
                                or source_job.slack_thread_ts
                            ),
                            "progress_message_ts": progress_message_ts or source_job.progress_message_ts,
                            "client_request_id": source_job.client_request_id,
                            "billing_source_job_id": source_job.billing_source_job_id or source_job.job_id,
                            "billing_amount": source_job.billing_amount,
                            "billing_status": "reused",
                            "billing_ledger_id": source_job.billing_ledger_id,
                        },
                    )
            if source_run_id:
                from integrations.services.daily_discovery import mark_scheduled_dispatch_confirmed

                mark_scheduled_dispatch_confirmed(job_id=source_run_id)
            return Response(result, status=status.HTTP_202_ACCEPTED)
        except ContentFactoryBackendUnavailableError as e:
            return _handle_backend_unavailable_error(e)
        except GitHubReconnectRequiredError as e:
            return _handle_auth_required_error(
                e,
                slack_user_id,
                article_request={
                    "domain": domain,
                    "topic": confirmed_keyword,
                    "requested_by_slack_user_id": requested_by_slack_user_id,
                },
                requested_by_slack_user_id=requested_by_slack_user_id,
            )
        except ArticleSystemActionRequiredError as e:
            return _handle_article_system_action_required(
                e,
                slack_user_id,
                {
                    "domain": domain,
                    "topic": confirmed_keyword,
                    "requested_by_slack_user_id": requested_by_slack_user_id,
                },
            )
        except InsufficientRooPointsError as e:
            return _handle_insufficient_roo_points_error(e)
        except ArticleGenerationError as e:
            error_str = str(e)
            if error_str.startswith("PREREQUISITE_MISSING:"):
                return _handle_prerequisite_error(
                    error_str,
                    slack_user_id,
                    {
                        "domain": domain,
                        "topic": confirmed_keyword,
                        "requested_by_slack_user_id": requested_by_slack_user_id,
                    },
                )
            return Response({"error": error_str}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception(f"Unexpected error in confirm view: {e}")
            return Response({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ContentJobConfirmView(APIView):
    """
    Confirm topic for a specific job and trigger generation.
    POST /api/v1/content/jobs/{job_id}/confirm

    When a user confirms a topic, non-selected alternatives are sent back as
    temporary rejection/cooldown feedback so they can be deprioritized in
    future research runs without being permanently skipped.
    """
    authentication_classes = []
    permission_classes = [HasStrictRooApiKey]

    def post(self, request, job_id):
        from content_factory.models import ContentFactoryJob
        from integrations.services.article_generation import confirm_topic, ArticleGenerationError

        validation_error = _validate_roo_content_request(request)
        if validation_error:
            return validation_error
        keyword = request.data.get('keyword')
        option_index = request.data.get('option_index')
        slack_user_id = request.data.get('slack_user_id')
        domain = request.data.get('domain')
        custom_title = request.data.get('custom_title')

        if not all([job_id, slack_user_id]):
            return Response({"error": "job_id and slack_user_id are required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            job = ContentFactoryJob.objects.get(job_id=job_id)
        except ContentFactoryJob.DoesNotExist:
            return Response({"error": "Job not found"}, status=status.HTTP_404_NOT_FOUND)
        requested_by_slack_user_id = _resolve_explicit_requested_by_slack_user_id(
            slack_user_id=slack_user_id,
            request_data=request.data,
            job=job,
        )

        # Extract all options from selection_data for building skip_alternatives
        options = []
        if job.selection_data:
            options = job.selection_data.get('options', [])

        # Handle option_index selection
        confirmed_keyword = None
        if option_index is not None:
            try:
                idx = int(option_index)
                if 0 <= idx < len(options):
                    option = options[idx]
                    confirmed_keyword = option.get('keyword') or option.get('selected_keyword')
            except (ValueError, TypeError):
                logger.warning(f"Invalid option_index for job {job_id}: {option_index}")

        # Use keyword from request, or fall back to job's selected_keyword
        if not confirmed_keyword:
            confirmed_keyword = keyword or job.selected_keyword

        # Use domain from request, or fall back to job's domain
        resolved_domain = domain or job.domain

        if not confirmed_keyword:
            return Response({"error": "No keyword specified and job has no selected_keyword"}, status=status.HTTP_400_BAD_REQUEST)
        if not resolved_domain:
            return Response({"error": "No domain specified and job has no domain"}, status=status.HTTP_400_BAD_REQUEST)
        request_meta = dict(job.request_meta or {})
        if requested_by_slack_user_id:
            request_meta["requested_by_slack_user_id"] = requested_by_slack_user_id
        billing_status = str(job.billing_status or "").strip()
        uses_deferred_billing = billing_status == CONTENT_FACTORY_BILLING_STATUS_DEFERRED or (
            billing_status == ""
            and str(request_meta.get("trigger_source") or "").strip() == SCHEDULED_DAILY_TRIGGER_SOURCE
        )
        if job.billing_status not in {"charged", "reused"} and not uses_deferred_billing:
            return Response(
                {"error": "Job has not been billed for Content Factory generation."},
                status=status.HTTP_409_CONFLICT,
            )

        # Build skip_alternatives: all options except the confirmed keyword
        skip_alternatives = []
        for opt in options:
            opt_keyword = opt.get('keyword') or opt.get('selected_keyword')
            if opt_keyword and opt_keyword != confirmed_keyword:
                skip_alternatives.append(opt_keyword)

        # Record the user's selection, but do NOT flip status to 'confirmed' yet:
        # confirm_topic below can fail (backend unavailable, generation error), and no
        # failure handler restores the status — an early write would leave a phantom
        # 'confirmed' job with no run behind it. Status is stamped after success.
        job.selected_keyword = confirmed_keyword
        job.slack_user_id = slack_user_id
        job.request_meta = request_meta
        job.save(update_fields=["selected_keyword", "slack_user_id", "request_meta", "updated_at"])

        # Trigger article generation via Content Factory HTTP API
        try:
            confirm_kwargs = {
                "domain": resolved_domain,
                "confirmed_keyword": confirmed_keyword,
                "slack_user_id": slack_user_id,
                "custom_title": custom_title,
                "skip_alternatives": skip_alternatives if skip_alternatives else None,
                "source_run_id": job_id,
                "slack_channel_id": job.slack_channel_id,
                "slack_thread_ts": job.slack_thread_ts,
                "slack_root_message_ts": job.slack_root_message_ts or job.slack_thread_ts,
                "progress_message_ts": job.progress_message_ts,
                "delivery_mode": request.data.get("delivery_mode"),
                "delivery_mode_confirmed": request.data.get("delivery_mode_confirmed"),
                "request_source": request.data.get("request_source"),
            }
            notification_context = request.data.get("notification_context") or request_meta.get("notification_context")
            if notification_context:
                confirm_kwargs["notification_context"] = notification_context
            if requested_by_slack_user_id:
                confirm_kwargs["requested_by_slack_user_id"] = requested_by_slack_user_id

            result = confirm_topic(**confirm_kwargs)
            # Content Factory accepted the confirmation — only now is 'confirmed' true.
            # Narrow update_fields: confirm_topic's deferred-charge path may have
            # rewritten request_meta/billing on a fresh instance of this row.
            job.status = 'confirmed'
            job.save(update_fields=["status", "updated_at"])
            result_status = str(result.get("status") or "").strip()
            new_job_id = result.get("job_id") or result.get("run_id")
            active_job_id = new_job_id or job.job_id
            if new_job_id and new_job_id != job.job_id:
                child_request_meta = {
                    "domain": resolved_domain,
                    "topic": custom_title or confirmed_keyword,
                    "target_keyword": confirmed_keyword,
                    "custom_title": custom_title,
                    "skip_alternatives": skip_alternatives,
                    "source_run_id": job_id,
                    "request_source": request.data.get("request_source"),
                    "requested_by_slack_user_id": requested_by_slack_user_id,
                    "slack_channel_id": job.slack_channel_id,
                    "slack_thread_ts": job.slack_thread_ts,
                    "slack_root_message_ts": job.slack_root_message_ts or job.slack_thread_ts,
                }
                if notification_context:
                    child_request_meta["notification_context"] = notification_context
                ContentFactoryJob.objects.update_or_create(
                    job_id=new_job_id,
                    defaults={
                        "domain": resolved_domain,
                        "slack_user_id": slack_user_id,
                        "status": "generating",
                        "request_meta": child_request_meta,
                        "slack_channel_id": job.slack_channel_id,
                        "slack_root_message_ts": job.slack_root_message_ts or job.slack_thread_ts,
                        "slack_thread_ts": job.slack_thread_ts,
                        "progress_message_ts": job.progress_message_ts,
                        "client_request_id": job.client_request_id,
                        "billing_source_job_id": job.billing_source_job_id or job.job_id,
                        "billing_amount": job.billing_amount,
                        "billing_status": "reused",
                        "billing_ledger_id": job.billing_ledger_id,
                    },
                )
                child_job = ContentFactoryJob.objects.filter(job_id=new_job_id).first()
                target_job = child_job
            else:
                target_job = ContentFactoryJob.objects.filter(job_id=active_job_id).first()
            if target_job and result_status in {"blocked", "auth_required"}:
                if target_job.status != result_status:
                    target_job.status = result_status
                    target_job.save(update_fields=["status", "updated_at"])
            if target_job and result_status not in {"awaiting_delivery_mode", "blocked", "auth_required"}:
                upsert_live_progress_card(
                    target_job,
                    summary_text="Topic confirmed. Roo is writing the draft now.",
                )
            if target_job and result_status == "awaiting_delivery_mode":
                target_job.status = "awaiting_delivery_mode"
                target_job.save(update_fields=["status", "updated_at"])
            from integrations.services.daily_discovery import mark_scheduled_dispatch_confirmed

            mark_scheduled_dispatch_confirmed(job_id=job.job_id)
            if result_status == "awaiting_delivery_mode":
                return Response(
                    {
                        "status": "awaiting_delivery_mode",
                        "job_id": active_job_id,
                        "run_id": active_job_id,
                        **({"source_run_id": job.job_id} if active_job_id != job.job_id else {}),
                        "message": result.get("message") or "Choose a delivery mode to continue.",
                        "cf_response": result,
                    },
                    status=status.HTTP_200_OK,
                )
            if result_status in {"blocked", "auth_required"}:
                return Response(
                    {
                        "status": result_status,
                        "job_id": active_job_id,
                        "run_id": active_job_id,
                        **({"source_run_id": job.job_id} if active_job_id != job.job_id else {}),
                        "message": result.get("message") or result.get("error") or "Article generation is waiting on a dependency.",
                        "cf_response": result,
                    },
                    status=status.HTTP_200_OK,
                )
            return Response({
                "status": "confirmed",
                "job_id": active_job_id,
                "run_id": active_job_id,
                **({"source_run_id": job.job_id} if active_job_id != job.job_id else {}),
                "message": "Topic confirmed, article generation started.",
                "skip_alternatives": skip_alternatives,
                "cf_response": result
            }, status=status.HTTP_200_OK)
        except GitHubReconnectRequiredError as e:
            return _handle_auth_required_error(
                e,
                slack_user_id,
                article_request={
                    "domain": resolved_domain,
                    "topic": confirmed_keyword,
                    "requested_by_slack_user_id": requested_by_slack_user_id,
                },
                requested_by_slack_user_id=requested_by_slack_user_id,
            )
        except ContentFactoryBackendUnavailableError as e:
            return _handle_backend_unavailable_error(e)
        except ArticleSystemActionRequiredError as e:
            return _handle_article_system_action_required(
                e,
                slack_user_id,
                {
                    "domain": resolved_domain,
                    "topic": confirmed_keyword,
                    "requested_by_slack_user_id": requested_by_slack_user_id,
                },
            )
        except InsufficientRooPointsError as e:
            return _handle_insufficient_roo_points_error(e)
        except ArticleGenerationError as e:
            error_str = str(e)
            if error_str.startswith("PREREQUISITE_MISSING:"):
                return _handle_prerequisite_error(
                    error_str,
                    slack_user_id,
                    {
                        "domain": resolved_domain,
                        "topic": confirmed_keyword,
                        "requested_by_slack_user_id": requested_by_slack_user_id,
                    },
                )
            logger.exception(f"Failed to trigger generation for job {job_id}: {e}")
            return Response({"error": error_str}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            logger.exception(f"Unexpected error triggering generation for job {job_id}: {e}")
            return Response({"error": "Failed to trigger generation"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ContentJobDeliveryModeView(APIView):
    """
    Select the delivery mode for an awaiting article run.
    POST /api/v1/content/jobs/{job_id}/delivery-mode
    """
    authentication_classes = []
    permission_classes = [HasStrictRooApiKey]

    def post(self, request, job_id):
        from content_factory.models import ContentFactoryJob

        validation_error = _validate_roo_content_request(request)
        if validation_error:
            return validation_error

        delivery_mode = request.data.get("delivery_mode")
        if not delivery_mode:
            return Response({"error": "delivery_mode is required"}, status=status.HTTP_400_BAD_REQUEST)

        job = ContentFactoryJob.objects.filter(job_id=job_id).first()
        if not job:
            return Response({"error": "Job not found"}, status=status.HTTP_404_NOT_FOUND)

        request_meta = dict(job.request_meta or {})
        request_meta["delivery_mode"] = delivery_mode
        request_meta["delivery_mode_confirmed"] = True
        job.request_meta = request_meta
        job.save(update_fields=["request_meta", "updated_at"])

        try:
            result = set_article_delivery_mode(job_id, delivery_mode)
        except ArticleGenerationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        status_code = int(result.pop("status_code", 200) or 200)
        if status_code == status.HTTP_200_OK:
            job.status = "generating"
            job.error_message = ""
            job.save(update_fields=["status", "error_message", "updated_at"])
        else:
            job.status = "awaiting_delivery_mode"
            job.error_message = result.get("error") or result.get("message") or ""
            job.save(update_fields=["status", "error_message", "updated_at"])

        return Response(result, status=status_code)


class ContentJobCancelView(APIView):
    """
    Mark a discovery job as cancelled.
    POST /api/v1/content/jobs/{job_id}/cancel
    """
    authentication_classes = []
    permission_classes = [HasStrictRooApiKey]

    def post(self, request, job_id):
        from content_factory.models import ContentFactoryJob
        from integrations.services.daily_discovery import mark_scheduled_dispatch_cancelled

        validation_error = _validate_roo_content_request(request)
        if validation_error:
            return validation_error

        slack_user_id = request.data.get("slack_user_id")
        if not job_id or not slack_user_id:
            return Response(
                {"error": "job_id and slack_user_id are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            job = ContentFactoryJob.objects.get(job_id=job_id)
        except ContentFactoryJob.DoesNotExist:
            return Response({"error": "Job not found"}, status=status.HTTP_404_NOT_FOUND)

        job.status = "cancelled"
        job.slack_user_id = slack_user_id
        job.last_progress_milestone_key = "cancelled"
        job.last_progress_updated_at = timezone.now()
        job.still_working_pinged_at = None
        job.save(
            update_fields=[
                "status",
                "slack_user_id",
                "last_progress_milestone_key",
                "last_progress_updated_at",
                "still_working_pinged_at",
                "updated_at",
            ]
        )
        mark_scheduled_dispatch_cancelled(job_id=job_id)

        return Response(
            {
                "status": "cancelled",
                "job_id": job_id,
                "message": "Topic selection cancelled.",
            },
            status=status.HTTP_200_OK,
        )


class ArticleSystemDecisionView(APIView):
    """
    Persist an article-system decision and optionally resume pending article intent.
    POST /api/v1/content/article-system/decision
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        from content_factory.article_setup_reset import clear_article_setup_reset_markers
        from content_factory.article_system import normalize_article_system, resolve_article_system
        from organizations.models import Organization
        from integrations.services.github import scaffold_articles_directory, trigger_scan_async
        from integrations.services.article_generation import get_github_credentials_for_domain
        from integrations.utils import normalize_domain

        domain = request.data.get('domain')
        slack_user_id = request.data.get('slack_user_id')
        decision = request.data.get('decision')

        if not all([domain, slack_user_id, decision]):
            return Response(
                {"error": "domain, slack_user_id, and decision are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if decision not in {'use_detected', 'rescan', 'scaffold'}:
            return Response({"error": "decision must be use_detected, rescan, or scaffold"}, status=status.HTTP_400_BAD_REQUEST)

        normalized_domain = normalize_domain(domain)
        org = Organization.objects.filter(domain=normalized_domain).first()
        config = getattr(org, 'content_config', None) if org else None
        if not config:
            return Response({"error": f"No content config found for {normalized_domain}"}, status=status.HTTP_404_NOT_FOUND)

        if decision == 'use_detected':
            article_system = resolve_article_system(config)
            article_system.update(
                {
                    'state': 'existing',
                    'source': 'manual_confirmed',
                    'reason': article_system.get('reason') or 'User manually confirmed the detected article system',
                }
            )
            # Adopting the detected system is an explicit exit from a reset: drop the
            # reset watermark so the wizard reads this surface as published again
            # (carrying it forward would keep it suppressed — see _article_system_is_published).
            config.article_system = clear_article_setup_reset_markers(
                normalize_article_system(article_system)
            )
            config.save(update_fields=['article_system', 'updated_at'])
            resumed = _resume_pending_article_intent(slack_user_id, normalized_domain)
            return Response(
                {
                    "status": "updated",
                    "domain": normalized_domain,
                    "article_system": config.article_system,
                    "resume_triggered": bool(resumed),
                    "result": resumed,
                },
                status=status.HTTP_200_OK,
            )

        if decision == 'rescan':
            trigger_scan_async(slack_user_id, domain=normalized_domain)
            return Response(
                {
                    "status": "queued",
                    "domain": normalized_domain,
                    "decision": decision,
                    "article_system": resolve_article_system(config),
                },
                status=status.HTTP_202_ACCEPTED,
            )

        creds = get_github_credentials_for_domain(normalized_domain, slack_user_id)
        scaffold_result = scaffold_articles_directory(
            domain=normalized_domain,
            slack_user_id=slack_user_id,
            github_token=creds['token'],
            github_repo=creds['repo'],
        )
        return Response(
            {
                "status": "queued",
                "domain": normalized_domain,
                "decision": decision,
                "job": scaffold_result,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class ContentJobProgressMessageView(APIView):
    authentication_classes = []
    permission_classes = [HasStrictRooApiKey]

    def post(self, request, job_id):
        validation_error = _validate_roo_content_request(request)
        if validation_error:
            return validation_error

        progress_message_ts = str(request.data.get("progress_message_ts") or "").strip()
        if not progress_message_ts:
            return Response({"error": "progress_message_ts is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            job = attach_progress_message(
                job_id,
                progress_message_ts=progress_message_ts,
                slack_channel_id=request.data.get("slack_channel_id") or "",
                slack_thread_ts=request.data.get("slack_thread_ts") or "",
                slack_root_message_ts=request.data.get("slack_root_message_ts") or "",
            )
            return Response(
                {
                    "status": "attached",
                    "job_id": job.job_id,
                    "progress_message_ts": job.progress_message_ts,
                },
                status=status.HTTP_200_OK,
            )
        except ArticleGenerationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class ContentJobStillWorkingView(APIView):
    authentication_classes = []
    permission_classes = [HasStrictRooApiKey]

    def post(self, request, job_id):
        from content_factory.models import ContentFactoryJob

        validation_error = _validate_roo_content_request(request)
        if validation_error:
            return validation_error

        job = ContentFactoryJob.objects.filter(job_id=job_id).first()
        if not job:
            return Response({"error": "Job not found"}, status=status.HTTP_404_NOT_FOUND)

        sent = maybe_send_still_working_ping(job)
        return Response(
            {
                "status": "updated" if sent else "noop",
                "job_id": job_id,
            },
            status=status.HTTP_200_OK,
        )
