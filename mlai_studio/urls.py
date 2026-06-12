from django.urls import path

from . import views


urlpatterns = [
    path('applications/', views.StudioApplicationSubmitView.as_view(), name='mlai-studio-applications'),
]
