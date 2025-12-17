from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from .models import UserIntegration
from core.permissions import HasAPIKey
import logging

logger = logging.getLogger(__name__)

class GithubTokenIdentityView(APIView):
    permission_classes = [HasAPIKey]

    def post(self, request):
        """
        Upsert a UserIntegration record with GitHub token info.
        Expecting:
        {
            "slack_user_id": "...",
            "token": "...",
            "user_name": "...",
            "scopes": [...]
        }
        """
        data = request.data
        slack_user_id = data.get('slack_user_id')
        token = data.get('token')
        user_name = data.get('user_name')
        scopes = data.get('scopes', [])

        if not slack_user_id:
            return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        # Upsert
        integration, created = UserIntegration.objects.update_or_create(
            slack_user_id=slack_user_id,
            defaults={
                'github_access_token': token,
                'github_user_name': user_name,
                'github_scopes': scopes,
            }
        )
        return Response({
            "status": "success", 
            "action": "created" if created else "updated",
            "slack_user_id": integration.slack_user_id
        }, status=status.HTTP_200_OK)

    def get(self, request):
        """
        Retrieve token info for a slack user.
        Query Param: slack_user_id
        """
        slack_user_id = request.query_params.get('slack_user_id')
        if not slack_user_id:
            return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            integration = UserIntegration.objects.get(slack_user_id=slack_user_id)
            # CAUTION: We are returning the token here as requested by requirements.
            # Ideally tokens should not leave the system, but Roo needs it.
            return Response({
                "slack_user_id": integration.slack_user_id,
                "token": integration.github_access_token,
                "user_name": integration.github_user_name,
                "scopes": integration.github_scopes,
            })
        except UserIntegration.DoesNotExist:
            return Response({"error": "Integration not found"}, status=status.HTTP_404_NOT_FOUND)


class IntentView(APIView):
    permission_classes = [HasAPIKey]

    def post(self, request):
        """
        Update pending_intent.
        Body: {"slack_user_id": "...", "intent": "..."}
        """
        slack_user_id = request.data.get('slack_user_id')
        intent = request.data.get('intent')

        if not slack_user_id:
            return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        # We need to ensure the record exists, or crate partial? 
        # Requirement implies this is for existing integrations, but safe to create if missing.
        integration, _ = UserIntegration.objects.get_or_create(slack_user_id=slack_user_id)
        integration.pending_intent = intent
        integration.save()

        return Response({"status": "updated"}, status=status.HTTP_200_OK)

    def delete(self, request):
        """
        Clear pending_intent.
        Body: {"slack_user_id": "..."}
        """
        slack_user_id = request.data.get('slack_user_id')
        if not slack_user_id:
            return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            integration = UserIntegration.objects.get(slack_user_id=slack_user_id)
            integration.pending_intent = None
            integration.save()
            return Response({"status": "cleared"}, status=status.HTTP_200_OK)
        except UserIntegration.DoesNotExist:
            return Response({"error": "Integration not found"}, status=status.HTTP_404_NOT_FOUND)


class StatusView(APIView):
    permission_classes = [HasAPIKey]

    def patch(self, request):
        """
        Update status flags (project_scanned).
        Body: {"slack_user_id": "...", "project_scanned": true}
        """
        slack_user_id = request.data.get('slack_user_id')
        project_scanned = request.data.get('project_scanned')

        if not slack_user_id:
            return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            integration = UserIntegration.objects.get(slack_user_id=slack_user_id)
            if project_scanned is not None:
                integration.project_scanned = project_scanned
            integration.save()
            return Response({"status": "updated"}, status=status.HTTP_200_OK)
        except UserIntegration.DoesNotExist:
            return Response({"error": "Integration not found"}, status=status.HTTP_404_NOT_FOUND)
