from django.urls import path
from . import views

urlpatterns = [
    path('send-magic-link/', views.SendMagicLinkView.as_view(), name='send_magic_link'),
    path('create-user/', views.CreateUserView.as_view(), name='create_user'),
    path('verify-magic-link/', views.MagicLinkVerifyView.as_view(), name='verify_magic_link'),
    path('token/refresh/', views.CookieTokenRefreshView.as_view(), name='token_refresh'),
    path('get_current_user/', views.CurrentUserView.as_view(), name='get_current_user'),
    path('me/', views.CurrentUserView.as_view(), name='current_user'),
    path('update_user/', views.UpdateProfileView.as_view(), name='update_user'),
    path('update-profile/', views.UpdateProfileView.as_view(), name='update_profile'),
    path('logout/', views.logout_view, name='logout'),
    path('users/<int:pk>/', views.UserDetailView.as_view(), name='user_detail'),
    # Content Factory endpoints
    path('content-factory/org/config/', views.ContentFactoryOrgConfigView.as_view(), name='content_factory_org_config'),
]
