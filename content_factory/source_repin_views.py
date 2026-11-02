"""Service-authenticated source re-pin API for existing Content Factory runs."""
from rest_framework.response import Response
from rest_framework.views import APIView
from core.permissions import HasRooApiKey
from workflow_runs.models import ContentFactoryRun
from .source_repin import repin_source
from .website_contract import WebsiteAuthorityError
from .run_execution_control import ExecutionControlDenied
from .run_ownership import RunOwnershipConflict
import requests


class SourceRepinView(APIView):
    """Compare-and-swap a run's pinned repository source without new billing."""

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id):
        """Validate service authority and return only a bounded re-pin outcome."""
        try:
            return Response(repin_source(run_id, request.data))
        except ContentFactoryRun.DoesNotExist:
            return Response({"detail": "Not found.", "reason": "not_found"}, status=404)
        except WebsiteAuthorityError as exc:
            return Response(exc.as_dict(), status=exc.status)
        except ExecutionControlDenied as exc:
            return Response({"reason": exc.reason, "retryable": exc.pending}, status=409)
        except RunOwnershipConflict:
            return Response({"reason": "execution_control_refused", "retryable": False}, status=409)
        except requests.RequestException:
            return Response({"reason": "execution_history_pending", "retryable": True}, status=503)
