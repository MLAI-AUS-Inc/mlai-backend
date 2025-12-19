from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .models import ChannelFirstPost
from core.permissions import HasRooApiKey
import logging

logger = logging.getLogger(__name__)

class ChannelActivityView(APIView):
    """
    Track first posts in channels.
    """
    # Override global authentication to allow API key access
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, slack_user_id=None, channel_id=None):
        """
        Check if posted.
        Path: GET /api/v1/activity/first-post/{slack_user_id}/{channel_id}/
        """
        if not slack_user_id or not channel_id:
            return Response({"error": "slack_user_id and channel_id are required"}, status=status.HTTP_400_BAD_REQUEST)
        
        has_posted = ChannelFirstPost.objects.filter(
            slack_user_id=slack_user_id, 
            channel_id=channel_id
        ).exists()
        
        return Response({"has_posted": has_posted})

    def post(self, request):
        """
        Record first post.
        Path: POST /api/v1/activity/first-post/
        Body: {"slack_user_id": "...", "channel_id": "..."}
        """
        slack_user_id = request.data.get('slack_user_id')
        channel_id = request.data.get('channel_id')

        if not slack_user_id or not channel_id:
             return Response({"error": "slack_user_id and channel_id are required"}, status=status.HTTP_400_BAD_REQUEST)

        if ChannelFirstPost.objects.filter(slack_user_id=slack_user_id, channel_id=channel_id).exists():
            return Response(
                {"error": "Activity already recorded", "has_posted": True}, 
                status=status.HTTP_409_CONFLICT
            )
        
        # Create record
        ChannelFirstPost.objects.create(slack_user_id=slack_user_id, channel_id=channel_id)

        # Award points if user is linked
        from .services import PointsService
        user = PointsService.get_user_by_slack_id(slack_user_id)
        
        points_awarded = False
        if user:
            try:
                idempotency_key = f"first_post_award:{slack_user_id}:{channel_id}"
                PointsService.award(
                    user=user,
                    delta=1,
                    source='COMMUNITY',
                    description=f"First post in channel {channel_id}",
                    created_by_slack_id="SYSTEM",
                    idempotency_key=idempotency_key
                )
                points_awarded = True
            except Exception as e:
                logger.error(f"Failed to award points for first post: {e}")

        return Response({
            "status": "recorded", 
            "points_awarded": points_awarded
        }, status=status.HTTP_201_CREATED)
