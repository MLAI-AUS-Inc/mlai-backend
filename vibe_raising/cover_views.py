"""Founder-scoped cover uploads and asynchronous image generation."""
import logging
from uuid import UUID

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from startup_updates.covers import MAX_UPLOAD_BYTES, generation_status, start_generation, store_cover

logger = logging.getLogger(__name__)


class CoverUploadThrottle(UserRateThrottle):
    scope = "update_cover_upload"
    rate = "20/hour"


class CoverGenerationThrottle(UserRateThrottle):
    scope = "update_cover_generation"
    rate = "6/hour"


class CoverPollThrottle(UserRateThrottle):
    scope = "update_cover_poll"
    rate = "30/minute"


class CoverView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def company_context(self, request):
        # Require a pinned company: polling must not follow a different tab's
        # mutable active-company selection.
        if not request.query_params.get("company_id"):
            return None, Response({"detail": "company_id is required."}, status=400)
        from .views import _get_founder_company_context_or_response
        from founder_tools.services import ensure_company_organization
        context, error = _get_founder_company_context_or_response(request)
        if error:
            return None, error
        context["organization"] = ensure_company_organization(context["company"])
        return context, None

    def safe_result(self, operation, success_status=200):
        try:
            return Response(operation(), status=success_status)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        except Exception as exc:
            from rest_framework.exceptions import APIException
            if isinstance(exc, APIException):
                raise
            # Provider errors can contain the draft prompt. Do not log them.
            logger.warning("Update cover operation failed (%s)", type(exc).__name__)
            return Response({"detail": "We couldn't finish that image request. Please try again, or upload a cover."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


class UpdateCoverUploadView(CoverView):
    throttle_classes = [CoverUploadThrottle]

    def post(self, request):
        context, error = self.company_context(request)
        if error:
            return error
        image = request.FILES.get("image")
        if image is None or image.size > MAX_UPLOAD_BYTES:
            return Response({"detail": "Choose a PNG, JPEG or WebP image under 10 MB."}, status=400)
        return self.safe_result(lambda: {"coverImage": store_cover(image.read(MAX_UPLOAD_BYTES + 1), organization_id=context["organization"].pk)}, 201)


class UpdateCoverGenerateView(CoverView):
    throttle_classes = [CoverGenerationThrottle]

    def post(self, request):
        context, error = self.company_context(request)
        if error:
            return error
        try:
            request_id = str(UUID(str(request.data.get("requestId") or "")))
        except ValueError:
            return Response({"detail": "A valid requestId is required."}, status=400)
        return self.safe_result(lambda: start_generation(
            organization_id=context["organization"].pk, company_id=context["company"].pk,
            user_id=request.user.pk, company_name=context["company"].name,
            update_text=request.data.get("updateText"), direction=request.data.get("direction"),
            request_id=request_id,
        ), 202)


class UpdateCoverStatusView(CoverView):
    throttle_classes = [CoverPollThrottle]

    def post(self, request):
        context, error = self.company_context(request)
        if error:
            return error
        return self.safe_result(lambda: generation_status(
            job_token=request.data.get("jobToken"), organization_id=context["organization"].pk,
            company_id=context["company"].pk, user_id=request.user.pk,
        ))
