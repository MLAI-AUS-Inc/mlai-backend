"""Authenticated MLAI Admin CRM inventory. Responses never disclose bearer codes."""

import logging

from rest_framework.response import Response

from ..admin_access import AdminError
from .errors import CrmError
from ..arcade_inventory import import_stock, stock_summary
from .views import CrmView


logger = logging.getLogger(__name__)


class ArcadeStockView(CrmView):
    def get(self, request):
        return Response(stock_summary())

    def post(self, request):
        data = request.data
        try:
            summary = import_stock(request.user, data)
        except (AdminError, CrmError):
            raise
        except Exception as exc:
            # Error-tracker frame locals for this request could contain a whole batch.
            logger.warning("Arcade stock import unavailable error_type=%s", type(exc).__name__)
            raise CrmError("arcade_stock_unavailable", 503) from None
        return Response(summary, status=201)
