"""Member-scoped data for the MLAI Chat Community Home dashboard."""

from django.conf import settings
from django.db.models import Q
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from hospital.authentication import CustomJWTAuthentication
from roo.models import RewardsCatalog, Task, TaskAssignment
from roo.services import PointsService

from .authentication import (
    CommunityChatAccountAuthentication,
    CommunityChatBootstrapAuthentication,
)
from .throttles import CommunityChatScopedThrottle
from .token_usage import local_usage_date


HOME_ITEM_LIMIT = 12


class CommunityHomeView(APIView):
    """Return the authenticated member's Roo dashboard data.

    The response deliberately contains only the caller's aggregate balance and
    public/volunteer catalog fields. Slack ids, reviewers, assignees, internal
    tasks, other members' balances, and redemption records never leave this
    endpoint.
    """

    authentication_classes = (
        CommunityChatAccountAuthentication,
        CommunityChatBootstrapAuthentication,
        CustomJWTAuthentication,
    )
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_home"

    def get(self, request):
        balance = PointsService.get_balance(request.user)
        available_microroo = PointsService.get_available_microroo(request.user)

        today = local_usage_date()
        opportunities = (
            Task.objects.filter(
                status="open",
                volunteer_ready=True,
                visibility__in=("volunteer", "public"),
            )
            .filter(Q(due_date__isnull=True) | Q(due_date__gte=today))
            .exclude(assignments__status__in=TaskAssignment.ACTIVE_STATUSES)
            .distinct()
            .order_by("due_date", "id")[:HOME_ITEM_LIMIT]
        )
        rewards = (
            RewardsCatalog.objects.filter(is_active=True)
            .filter(Q(stock_remaining__isnull=True) | Q(stock_remaining__gt=0))
            .order_by("cost_points", "name")[:HOME_ITEM_LIMIT]
        )

        earn_actions = [
            {
                "id": "intro",
                "name": "Introduce yourself",
                "description": "Post your first message in #_start-here.",
                "points": 4,
            }
        ]
        monthly_update_points = int(
            getattr(settings, "ROO_POINTS_MONTHLY_UPDATE_REWARD", 0)
        )
        if monthly_update_points > 0:
            earn_actions.append(
                {
                    "id": "monthly_update",
                    "name": "Complete your monthly startup update",
                    "description": (
                        "Complete and save a ready monthly update for your "
                        "verified company."
                    ),
                    "points": monthly_update_points,
                }
            )
        earn_actions.extend(
            {
                "id": f"task:{task.task_code}",
                "name": task.title,
                "description": task.description,
                "points": task.points_estimate or task.points,
                "command": f"@Roo task claim {task.task_code}",
            }
            for task in opportunities
            if task.task_code
        )

        return Response(
            {
                "points": {
                    "balance": PointsService.microroo_to_legacy_whole(
                        available_microroo
                    ),
                    "earned_balance": balance["earned_balance"],
                    "purchased_topup_balance": balance[
                        "purchased_topup_balance"
                    ],
                    "lifetime_earned": balance["lifetime_earned"],
                    "lifetime_spent": balance["lifetime_spent"],
                },
                "earn_actions": earn_actions,
                "rewards": [
                    {
                        "code": reward.code,
                        "name": reward.name,
                        "description": reward.description,
                        "cost_points": reward.cost_points,
                        "stock_remaining": reward.stock_remaining,
                        "can_afford": (
                            PointsService.microroo_to_legacy_whole(
                                available_microroo
                            )
                            >= reward.cost_points
                        ),
                    }
                    for reward in rewards
                ],
                "feature_flags": {
                    "link_love": False,
                    "meeting_rooms": bool(
                        getattr(settings, "MEETING_ROOM_BOOKING_ENABLED", False)
                    ),
                },
            }
        )
