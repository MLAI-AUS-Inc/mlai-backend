from django.urls import path
from . import views

urlpatterns = [
    # Keywords
    path('keywords/', views.SEOKeywordListView.as_view(), name='seo_keyword_list'),
    path('keywords/bulk/', views.SEOKeywordBulkUpsertView.as_view(), name='seo_keyword_bulk_upsert'),
    path('keywords/research-feedback/', views.SEOKeywordResearchFeedbackView.as_view(), name='seo_keyword_research_feedback'),
    path('keywords/<uuid:pk>/', views.SEOKeywordDetailView.as_view(), name='seo_keyword_detail'),
    path('keywords/<uuid:pk>/status/', views.SEOKeywordStatusUpdateView.as_view(), name='seo_keyword_status_update'),

    # Clusters
    path('clusters/', views.SEOClusterListView.as_view(), name='seo_cluster_list'),
    path('clusters/bulk/', views.SEOClusterBulkUpsertView.as_view(), name='seo_cluster_bulk_upsert'),

    # Articles
    path('articles/', views.SEOWrittenArticleCreateView.as_view(), name='seo_article_create'),

    # Dashboard
    path('dashboard/', views.SEODashboardView.as_view(), name='seo_dashboard'),
]
