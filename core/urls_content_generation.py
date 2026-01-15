from django.urls import path
from integrations import api_views_content as views

urlpatterns = [
    path('generate', views.ContentGenerateView.as_view(), name='content_generate'),
    path('jobs/<str:job_id>', views.ContentStatusView.as_view(), name='content_status'),
    path('publish/<str:job_id>', views.ContentPublishView.as_view(), name='content_publish'),
    path('confirm', views.ContentConfirmView.as_view(), name='content_confirm'),
]
