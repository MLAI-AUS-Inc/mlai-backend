from django.urls import path

from . import views

urlpatterns = [
    path('applications/', views.VictorApplicationSubmitView.as_view(), name='victor-ai-applications'),
]
