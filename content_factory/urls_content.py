from django.urls import path

from . import content_views as views

urlpatterns = [
    path('generate', views.ContentGenerateView.as_view(), name='content_generate'),
    path('article-system/decision', views.ArticleSystemDecisionView.as_view(), name='content_article_system_decision'),
    path('jobs/resolve-thread', views.ContentResolveThreadView.as_view(), name='content_job_resolve_thread'),
    path('jobs/<str:job_id>', views.ContentStatusView.as_view(), name='content_status'),
    path('jobs/<str:job_id>/delivery-mode', views.ContentJobDeliveryModeView.as_view(), name='content_job_delivery_mode'),
    path('jobs/<str:job_id>/progress-message', views.ContentJobProgressMessageView.as_view(), name='content_job_progress_message'),
    path('jobs/<str:job_id>/still-working', views.ContentJobStillWorkingView.as_view(), name='content_job_still_working'),
    path('publish/<str:job_id>', views.ContentPublishView.as_view(), name='content_publish'),
    path('jobs/<str:job_id>/publish-pr', views.ContentPublishPrView.as_view(), name='content_job_publish_pr'),
    path('confirm', views.ContentConfirmView.as_view(), name='content_confirm'),
    path('jobs/<str:job_id>/confirm', views.ContentJobConfirmView.as_view(), name='content_job_confirm'),
    path('jobs/<str:job_id>/cancel', views.ContentJobCancelView.as_view(), name='content_job_cancel'),
]
