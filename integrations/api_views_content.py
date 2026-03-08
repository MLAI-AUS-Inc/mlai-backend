from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from core.permissions import HasRooApiKey
from django.utils import timezone
import logging

from integrations.services.article_generation import (
    trigger_article_generation,
    check_generation_status,
    publish_article,
    confirm_topic,
    ArticleGenerationError
)

logger = logging.getLogger(__name__)


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

class ContentGenerateView(APIView):
    """
    Trigger content generation pipeline.
    POST /api/v1/content/generate
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        slack_user_id = request.data.get('slack_user_id')
        
        # Validate required fields
        if not slack_user_id:
             return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        # Pass specific fields (the service will validate domain/topic)
        article_request = {
            "domain": request.data.get('domain'),
            "topic": request.data.get('topic'),
            "target_keyword": request.data.get('target_keyword'),
            "context": request.data.get('context'),
        }

        try:
            result = trigger_article_generation(slack_user_id, article_request)
            return Response(result, status=status.HTTP_202_ACCEPTED)
        except ArticleGenerationError as e:
            logger.warning(f"Generation error: {e}")
            error_str = str(e)

            # Handle prerequisite errors — store pending intent and return structured 412
            if error_str.startswith("PREREQUISITE_MISSING:"):
                return _handle_prerequisite_error(error_str, slack_user_id, article_request)

            response_data = {"error": error_str}
            # Include structured auth fields so the bot can prompt for GitHub connection
            if "Please connect GitHub:" in error_str:
                response_data["needs_github_auth"] = True
                response_data["oauth_url"] = error_str.split("Please connect GitHub: ")[-1]
                response_data["domain"] = article_request.get("domain")
            return Response(response_data, status=status.HTTP_400_BAD_REQUEST)
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
    permission_classes = [HasRooApiKey]

    def post(self, request):
        domain = request.data.get('domain')
        confirmed_keyword = request.data.get('confirmed_keyword')
        slack_user_id = request.data.get('slack_user_id')
        custom_title = request.data.get('custom_title')
        skip_alternatives = request.data.get('skip_alternatives')
        source_run_id = request.data.get('source_run_id')

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
            result = confirm_topic(
                domain=domain,
                confirmed_keyword=confirmed_keyword,
                slack_user_id=slack_user_id,
                custom_title=custom_title,
                skip_alternatives=skip_alternatives,
                source_run_id=source_run_id,
            )
            return Response(result, status=status.HTTP_202_ACCEPTED)
        except ArticleGenerationError as e:
            error_str = str(e)
            if error_str.startswith("PREREQUISITE_MISSING:"):
                return _handle_prerequisite_error(error_str, slack_user_id, {"domain": domain, "topic": confirmed_keyword})
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
    permission_classes = [HasRooApiKey]

    def post(self, request, job_id):
        from core.models import ContentFactoryJob
        from integrations.services.article_generation import confirm_topic, ArticleGenerationError

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

        # Build skip_alternatives: all options except the confirmed keyword
        skip_alternatives = []
        for opt in options:
            opt_keyword = opt.get('keyword') or opt.get('selected_keyword')
            if opt_keyword and opt_keyword != confirmed_keyword:
                skip_alternatives.append(opt_keyword)

        # Update job status
        job.status = 'confirmed'
        job.selected_keyword = confirmed_keyword
        job.slack_user_id = slack_user_id
        job.save()

        # Trigger article generation via Content Factory HTTP API
        try:
            result = confirm_topic(
                domain=resolved_domain,
                confirmed_keyword=confirmed_keyword,
                slack_user_id=slack_user_id,
                custom_title=custom_title,
                skip_alternatives=skip_alternatives if skip_alternatives else None,
                source_run_id=job_id,
            )
            return Response({
                "status": "confirmed",
                "job_id": job.job_id,
                "message": "Topic confirmed, article generation started.",
                "skip_alternatives": skip_alternatives,
                "cf_response": result
            }, status=status.HTTP_200_OK)
        except ArticleGenerationError as e:
            error_str = str(e)
            if error_str.startswith("PREREQUISITE_MISSING:"):
                return _handle_prerequisite_error(error_str, slack_user_id, {"domain": resolved_domain, "topic": confirmed_keyword})
            logger.exception(f"Failed to trigger generation for job {job_id}: {e}")
            return Response({"error": error_str}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            logger.exception(f"Unexpected error triggering generation for job {job_id}: {e}")
            return Response({"error": "Failed to trigger generation"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
