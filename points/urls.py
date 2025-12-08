from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import MinterViewSet, TaskViewSet, UserBalanceViewSet

router = DefaultRouter()
router.register(r'minters', MinterViewSet)
router.register(r'tasks', TaskViewSet)

urlpatterns = [
    path('', include(router.urls)),
    # Custom route for user balance as it's a ViewSet but treated slightly differently (read-only by ID)
    path('users/<str:pk>/balance/', UserBalanceViewSet.as_view({'get': 'retrieve'}), name='user-balance'),
]
