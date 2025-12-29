from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.conf import settings
from django.urls import reverse
import urllib.parse
from django.shortcuts import get_object_or_404
from .models import UserIntegration
from core.permissions import HasRooApiKey
import logging

logger = logging.getLogger(__name__)

class GithubTokenIdentityView(APIView):
    permission_classes = [HasRooApiKey]

    def post(self, request):
        """
        Create or update GitHub token.
        Path: POST /api/v1/integrations/github/
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

    def get(self, request, slack_user_id=None):
        """
        Get integration record.
        Path: GET /api/v1/integrations/github/{slack_user_id}/
        Also supports legacy query param: ?slack_user_id=...
        """
        if not slack_user_id:
            slack_user_id = request.query_params.get('slack_user_id')
        
        if not slack_user_id:
            return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            integration = UserIntegration.objects.get(slack_user_id=slack_user_id)
            return Response({
                "slack_user_id": integration.slack_user_id,
                "token": integration.github_access_token,
                "user_name": integration.github_user_name,
                "scopes": integration.github_scopes,
                "project_scanned": integration.project_scanned,
                "pending_intent": integration.pending_intent,
            })
        except UserIntegration.DoesNotExist:
            return Response({"error": "Integration not found"}, status=status.HTTP_404_NOT_FOUND)

    def patch(self, request, slack_user_id=None):
        """
        Update fields (e.g., project_scanned).
        Path: PATCH /api/v1/integrations/github/{slack_user_id}/
        """
        if not slack_user_id:
            slack_user_id = request.data.get('slack_user_id')

        if not slack_user_id:
            return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            integration = UserIntegration.objects.get(slack_user_id=slack_user_id)
            
            project_scanned = request.data.get('project_scanned')
            if project_scanned is not None:
                integration.project_scanned = project_scanned
            
            integration.save()
            return Response({"status": "updated"}, status=status.HTTP_200_OK)
        except UserIntegration.DoesNotExist:
            return Response({"error": "Integration not found"}, status=status.HTTP_404_NOT_FOUND)


class IntentView(APIView):
    permission_classes = [HasRooApiKey]

    def post(self, request):
        """
        Save pending intent.
        Path: POST /api/v1/integrations/pending-intent/
        """
        slack_user_id = request.data.get('slack_user_id')
        intent = request.data.get('intent')

        if not slack_user_id:
            return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        integration, _ = UserIntegration.objects.get_or_create(slack_user_id=slack_user_id)
        integration.pending_intent = intent
        integration.save()

        return Response({"status": "updated"}, status=status.HTTP_200_OK)

    def delete(self, request, slack_user_id=None):
        """
        Clear pending intent.
        Path: DELETE /api/v1/integrations/pending-intent/{slack_user_id}/
        Also supports legacy body param.
        """
        if not slack_user_id:
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
    permission_classes = [HasRooApiKey]

    def patch(self, request):
        """
        Legacy endpoint for updating status.
        Use GithubTokenIdentityView.patch instead.
        """
        return GithubTokenIdentityView.as_view()(request)


class GithubAuthUrlView(APIView):
    permission_classes = [HasRooApiKey]

    def get(self, request):
        """
        Get the GitHub OAuth URL for a specific slack user.
        Path: GET /api/v1/integrations/github/auth-url?slack_user_id=...
        """
        slack_user_id = request.query_params.get('slack_user_id')
        if not slack_user_id:
            return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        # Construct the connect URL (hosted by us)
        # We need the full absolute URL since Slack is external
        base_url = settings.MEDHACK_URL.rstrip('/') # Or wherever this django app is hosted publically
        if 'localhost' in base_url or '127.0.0.1' in base_url:
             # If MEDHACK_URL is localhost (default), we might need ngrok or just assume localhost for dev
             pass
             
        # Actually, let's just use the path and let the caller prepend domain if needed, 
        # OR attempt to build absolute URI from request if possible.
        # But request.build_absolute_uri() is best.
        
        connect_path = reverse('github_connect')
        full_connect_url = request.build_absolute_uri(connect_path)
        
        # Append param
        auth_url = f"{full_connect_url}?slack_user_id={slack_user_id}"
        
        return Response({
            "auth_url": auth_url,
            "message": "Send this URL to the user to authorize GitHub."
        })
