"""Owner-checked Chat purchase review, using the existing Roo checkout service."""

from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView
from roo.models import PointsPurchase
from roo.services import PointsPurchaseService
from roo.views import CurrentUserPurchaseView, PointsPurchaseViewSet
from .api import MyStartupViewMixin


class StartupPurchaseCreateView(MyStartupViewMixin, CurrentUserPurchaseView):
    def post(self, request):
        origin = request.data.get("purchase_from", {})
        if not isinstance(origin, dict):
            return Response({"error": "purchase_from must be an object"}, status=400)
        request._full_data = {
            **request.data,
            "purchase_from": {**origin, "surface": "my-startup"},
        }
        return super().post(request)


class StartupPurchaseView(MyStartupViewMixin, APIView):
    def get(self, request, purchase_id):
        purchase = get_object_or_404(PointsPurchase, pk=purchase_id, user=request.user)
        return Response(PointsPurchaseViewSet._response_data(purchase))


class StartupPurchaseCheckoutView(MyStartupViewMixin, APIView):
    def post(self, request, purchase_id):
        purchase = get_object_or_404(PointsPurchase, pk=purchase_id, user=request.user)
        try:
            result = PointsPurchaseService.create_checkout_session(
                purchase=purchase,
                terms_version_accepted=request.data.get("terms_version_accepted"),
                privacy_version_accepted=request.data.get("privacy_version_accepted"),
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=409)
        except RuntimeError as exc:
            return Response({"error": str(exc)}, status=503)
        return Response(
            {
                **PointsPurchaseViewSet._response_data(result["purchase"]),
                "checkout_session_url": result["checkout_session_url"],
            }
        )
