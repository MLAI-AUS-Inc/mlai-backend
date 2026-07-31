from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    PointsAdminViewSet, MinterViewSet, TaskViewSet, UserBalanceViewSet,
    CurrentUserBalanceView,
    KimiPromptUsageView,
    LedgerViewSet, CoworkingViewSet, RewardsViewSet, ManualAwardView,
    RateCardView, AdminAllowanceView, PointsRequestViewSet, PointsPurchaseViewSet,
    PointsPacksView, CurrentUserPurchaseView,
    StripeWebhookView, SystemAwardView, BoostPostAdmissionView,
    CommitteeCandidateEmailsView,
    # Activity views
    ChannelActivityView, FirstChannelPostAwardView,
    # Quest views
    QuestProgressView, UserQuestProgressView, QuestIncrementView,
    QuestCompleteView, QuestCompletionStatusView,
)
from .coding_views import (
    CodingCallAdmitView,
    CodingCallDispatchView,
    CodingCallFailView,
    CodingCallSettleView,
)
from .meeting_room_views import (
    MeetingRoomAvailabilityView,
    MeetingRoomBookView,
    MeetingRoomCancelView,
    MeetingRoomListView,
    MyMeetingRoomBookingsView,
)

router = DefaultRouter()
router.register(r'admins', PointsAdminViewSet, basename='points-admin')
router.register(r'minters', MinterViewSet, basename='minter')  # Backwards compat
router.register(r'tasks', TaskViewSet)
router.register(r'ledger', LedgerViewSet, basename='ledger')
router.register(r'requests', PointsRequestViewSet, basename='points-request')
router.register(r'purchases', PointsPurchaseViewSet, basename='points-purchase')

urlpatterns = [
    path('kimi/calls/admit/', CodingCallAdmitView.as_view(), name='kimi-call-admit'),
    path('kimi/calls/dispatch/', CodingCallDispatchView.as_view(), name='kimi-call-dispatch'),
    path('kimi/calls/settle/', CodingCallSettleView.as_view(), name='kimi-call-settle'),
    path('kimi/calls/fail/', CodingCallFailView.as_view(), name='kimi-call-fail'),
    path('stripe/webhook/', StripeWebhookView.as_view(), name='points-stripe-webhook'),
    path('boost-posts/admissions/', BoostPostAdmissionView.as_view(), name='boost-post-admission'),
    path('', include(router.urls)),
    
    # ============================================================
    # Top-up packs + self-serve purchase (web dashboard checkout)
    # ============================================================
    path('packs/', PointsPacksView.as_view(), name='points-packs'),
    path('me/purchases/', CurrentUserPurchaseView.as_view(), name='current-user-purchase'),

    # ============================================================
    # User Balance (by Slack ID)
    # ============================================================
    path('me/balance/', CurrentUserBalanceView.as_view(), name='current-user-balance'),
    path('kimi/usage/', KimiPromptUsageView.as_view(), name='kimi-prompt-usage'),
    path('users/<str:pk>/balance/', UserBalanceViewSet.as_view({'get': 'retrieve'}), name='user-balance'),

    # ============================================================
    # Committee candidate email export
    # ============================================================
    path(
        'committee-candidates/emails/',
        CommitteeCandidateEmailsView.as_view(),
        name='committee-candidate-emails',
    ),

    # ============================================================
    # Meeting room booking
    # ============================================================
    path('meeting-rooms/rooms/', MeetingRoomListView.as_view(), name='meeting-room-list'),
    path(
        'meeting-rooms/availability/',
        MeetingRoomAvailabilityView.as_view(),
        name='meeting-room-availability',
    ),
    path('meeting-rooms/book/', MeetingRoomBookView.as_view(), name='meeting-room-book'),
    path(
        'meeting-rooms/my-bookings/',
        MyMeetingRoomBookingsView.as_view(),
        name='meeting-room-my-bookings',
    ),
    path(
        'meeting-rooms/cancel/',
        MeetingRoomCancelView.as_view(),
        name='meeting-room-cancel',
    ),
    
    # ============================================================
    # Coworking
    # ============================================================
    path('coworking/availability/', CoworkingViewSet.as_view({'get': 'availability'}), name='coworking-availability'),
    path('coworking/report/', CoworkingViewSet.as_view({'get': 'report'}), name='coworking-report'),
    path('coworking/book/', CoworkingViewSet.as_view({'post': 'book'}), name='coworking-book'),
    path('coworking/book-many/', CoworkingViewSet.as_view({'post': 'book_many'}), name='coworking-book-many'),
    path(
        'coworking/office-manager/claim/',
        CoworkingViewSet.as_view({'post': 'office_manager_claim'}),
        name='coworking-office-manager-claim',
    ),
    path('coworking/cancel/', CoworkingViewSet.as_view({'post': 'cancel'}), name='coworking-cancel'),
    path('coworking/my-bookings/', CoworkingViewSet.as_view({'get': 'my_bookings'}), name='coworking-my-bookings'),
    path('coworking/booking-help/', CoworkingViewSet.as_view({'post': 'booking_help'}), name='coworking-booking-help'),
    path('coworking/set-capacity/', CoworkingViewSet.as_view({'post': 'set_capacity'}), name='coworking-set-capacity'),
    
    # ============================================================
    # Rewards
    # ============================================================
    path('rewards/', RewardsViewSet.as_view({'get': 'list'}), name='rewards-list'),
    path('rewards/request/', RewardsViewSet.as_view({'post': 'request'}), name='rewards-request'),
    path('rewards/approve/', RewardsViewSet.as_view({'post': 'approve'}), name='rewards-approve'),
    path('rewards/pending/', RewardsViewSet.as_view({'get': 'pending'}), name='rewards-pending'),
    path('rewards/my-redemptions/', RewardsViewSet.as_view({'get': 'my_redemptions'}), name='rewards-my-redemptions'),
    
    # ============================================================
    # Rate Card
    # ============================================================
    path('rate-card/', RateCardView.as_view({'get': 'list'}), name='rate-card'),

    # ============================================================
    # Admin
    # ============================================================
    path('admin/award/', ManualAwardView.as_view(), name='manual-award'),
    path('admin/allowance/', AdminAllowanceView.as_view(), name='admin-allowance'),
    path('system/award/', SystemAwardView.as_view(), name='system-award'),
    
    # ============================================================
    # Activity Tracking
    # ============================================================
    path('activity/first-post-award/', FirstChannelPostAwardView.as_view(), name='first_post_award'),
    path('activity/first-post/', ChannelActivityView.as_view(), name='first_post_record'),
    path('activity/first-post/<str:slack_user_id>/<str:channel_id>/', ChannelActivityView.as_view(), name='first_post_check'),
    
    # ============================================================
    # Quests
    # ============================================================
    # Increment progress
    path('quests/progress/', QuestIncrementView.as_view(), name='quest_increment'),
    # Mark as completed
    path('quests/complete/', QuestCompleteView.as_view(), name='quest_complete'),
    # Quick completion check
    path('quests/<str:slack_user_id>/<str:quest_id>/completed/', QuestCompletionStatusView.as_view(), name='quest_completed'),
    # Get specific quest progress
    path('quests/<str:slack_user_id>/<str:quest_id>/', QuestProgressView.as_view(), name='quest_progress'),
    # Get all quests for a user
    path('quests/<str:slack_user_id>/', UserQuestProgressView.as_view(), name='user_quests'),
]
