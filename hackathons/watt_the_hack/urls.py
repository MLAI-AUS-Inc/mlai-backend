from django.urls import path

try:
    from . import views
except ModuleNotFoundError as exc:
    if exc.name != "watt_the_hack":
        raise

    from rest_framework import status
    from rest_framework.response import Response
    from rest_framework.views import APIView

    class WattTheHackEngineUnavailableView(APIView):
        def get(self, request, *args, **kwargs):
            return self._unavailable()

        def post(self, request, *args, **kwargs):
            return self._unavailable()

        @staticmethod
        def _unavailable():
            return Response(
                {
                    "detail": (
                        "Watt The Hack simulation engine is not installed in this deployment."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

    urlpatterns = [
        path(
            "scenarios/",
            WattTheHackEngineUnavailableView.as_view(),
            name="watt-the-hack-sim-scenarios",
        ),
        path(
            "init/",
            WattTheHackEngineUnavailableView.as_view(),
            name="watt-the-hack-sim-init",
        ),
        path(
            "step/",
            WattTheHackEngineUnavailableView.as_view(),
            name="watt-the-hack-sim-step",
        ),
        path(
            "run/",
            WattTheHackEngineUnavailableView.as_view(),
            name="watt-the-hack-sim-run",
        ),
    ]
else:
    urlpatterns = [
        path("scenarios/", views.ScenarioListView.as_view(), name="watt-the-hack-sim-scenarios"),
        path("init/", views.InitView.as_view(), name="watt-the-hack-sim-init"),
        path("step/", views.StepView.as_view(), name="watt-the-hack-sim-step"),
        path("run/", views.RunView.as_view(), name="watt-the-hack-sim-run"),
    ]
