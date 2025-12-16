from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    PointsAdminViewSet, MinterViewSet, TaskViewSet, UserBalanceViewSet,
    LedgerViewSet, CoworkingViewSet, RewardsViewSet, ManualAwardView
)

router = DefaultRouter()
router.register(r'admins', PointsAdminViewSet, basename='points-admin')
router.register(r'minters', MinterViewSet, basename='minter')  # Backwards compat
router.register(r'tasks', TaskViewSet)
router.register(r'ledger', LedgerViewSet, basename='ledger')

urlpatterns = [
    path('', include(router.urls)),
    
    # User balance (by Slack ID)
    path('users/<str:pk>/balance/', UserBalanceViewSet.as_view({'get': 'retrieve'}), name='user-balance'),
    
    # Coworking
    path('coworking/availability/', CoworkingViewSet.as_view({'get': 'availability'}), name='coworking-availability'),
    path('coworking/book/', CoworkingViewSet.as_view({'post': 'book'}), name='coworking-book'),
    path('coworking/cancel/', CoworkingViewSet.as_view({'post': 'cancel'}), name='coworking-cancel'),
    path('coworking/my-bookings/', CoworkingViewSet.as_view({'get': 'my_bookings'}), name='coworking-my-bookings'),
    path('coworking/set-capacity/', CoworkingViewSet.as_view({'post': 'set_capacity'}), name='coworking-set-capacity'),
    
    # Rewards
    path('rewards/', RewardsViewSet.as_view({'get': 'list'}), name='rewards-list'),
    path('rewards/request/', RewardsViewSet.as_view({'post': 'request'}), name='rewards-request'),
    path('rewards/approve/', RewardsViewSet.as_view({'post': 'approve'}), name='rewards-approve'),
    path('rewards/pending/', RewardsViewSet.as_view({'get': 'pending'}), name='rewards-pending'),
    path('rewards/my-redemptions/', RewardsViewSet.as_view({'get': 'my_redemptions'}), name='rewards-my-redemptions'),
    
    # Admin manual award
    path('admin/award/', ManualAwardView.as_view(), name='manual-award'),
]
