from django.urls import path
from integrations import api_views_content as views

urlpatterns = [
    path('generate', views.ContentGenerateView.as_view(), name='content_generate'),
    path('article-system/decision', views.ArticleSystemDecisionView.as_view(), name='content_article_system_decision'),
    path('jobs/<str:job_id>', views.ContentStatusView.as_view(), name='content_status'),
    path('jobs/<str:job_id>/progress-message', views.ContentJobProgressMessageView.as_view(), name='content_job_progress_message'),
    path('jobs/<str:job_id>/still-working', views.ContentJobStillWorkingView.as_view(), name='content_job_still_working'),
    path('publish/<str:job_id>', views.ContentPublishView.as_view(), name='content_publish'),
    path('confirm', views.ContentConfirmView.as_view(), name='content_confirm'),
    path('jobs/<str:job_id>/confirm', views.ContentJobConfirmView.as_view(), name='content_job_confirm'),
]
