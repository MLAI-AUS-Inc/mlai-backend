from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from core.permissions import HasRooApiKey
import logging

from integrations.services.article_generation import (
    trigger_article_generation, 
    check_generation_status, 
    publish_article,
    confirm_topic,
    ArticleGenerationError
)

logger = logging.getLogger(__name__)

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
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
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
    Publish a generated article.
    POST /api/v1/content/publish/{job_id}
    
    Request Body:
        { "slack_user_id": "U12345678" }  // Required for GitHub credential lookup
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, job_id):
        if not job_id:
            return Response({"error": "job_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        slack_user_id = request.data.get('slack_user_id')
        if not slack_user_id:
            return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = publish_article(job_id, slack_user_id)
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
        skip_alternatives: list[str] (optional) - Keywords to mark as 'skipped'
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        domain = request.data.get('domain')
        confirmed_keyword = request.data.get('confirmed_keyword')
        slack_user_id = request.data.get('slack_user_id')
        custom_title = request.data.get('custom_title')
        skip_alternatives = request.data.get('skip_alternatives')

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
                skip_alternatives=skip_alternatives
            )
            return Response(result, status=status.HTTP_202_ACCEPTED)
        except ArticleGenerationError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception(f"Unexpected error in confirm view: {e}")
            return Response({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ContentJobConfirmView(APIView):
    """
    Confirm topic for a specific job and trigger generation.
    POST /api/v1/content/jobs/{job_id}/confirm

    When a user confirms a topic, non-selected alternatives are marked as 'skipped'
    in content-factory so they don't appear in future research runs.
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
                skip_alternatives=skip_alternatives if skip_alternatives else None
            )
            return Response({
                "status": "confirmed",
                "job_id": job.job_id,
                "message": "Topic confirmed, article generation started.",
                "skip_alternatives": skip_alternatives,
                "cf_response": result
            }, status=status.HTTP_200_OK)
        except ArticleGenerationError as e:
            logger.exception(f"Failed to trigger generation for job {job_id}: {e}")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            logger.exception(f"Unexpected error triggering generation for job {job_id}: {e}")
            return Response({"error": "Failed to trigger generation"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
