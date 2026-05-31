from django.urls import path

from . import views


urlpatterns = [
    path("scenarios/", views.ScenarioListView.as_view(), name="watt-the-hack-sim-scenarios"),
    path("init/", views.InitView.as_view(), name="watt-the-hack-sim-init"),
    path("step/", views.StepView.as_view(), name="watt-the-hack-sim-step"),
    path("run/", views.RunView.as_view(), name="watt-the-hack-sim-run"),
]

