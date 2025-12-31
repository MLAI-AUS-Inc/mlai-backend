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
            
            # Check for updates on GitHub
            has_updates = False
            latest_sha = None
            auth_url = None
            error_message = None

            try:
                from integrations.services.github import get_latest_repo_sha
                import requests
                if integration.github_access_token and integration.github_repo:
                    latest_sha = get_latest_repo_sha(integration.github_access_token, integration.github_repo)
                    if latest_sha and latest_sha != integration.last_scanned_sha:
                        has_updates = True
            except requests.exceptions.HTTPError as e:
                # Handle expired token (401)
                if e.response.status_code == 401:
                    logger.warning(f"GitHub Token Expired for {slack_user_id}")
                    error_message = "GitHub token expired"
                    
                    # Generate Re-Auth URL
                    try:
                        connect_path = reverse('github_connect')
                        full_connect_url = request.build_absolute_uri(connect_path)
                        auth_url = f"{full_connect_url}?slack_user_id={slack_user_id}"
                    except Exception as url_err:
                        logger.error(f"Failed to build auth url: {url_err}")
                else:
                    # Other HTTP errors
                    logger.warning(f"Status check failed to fetch GH SHA: {e}")
            except Exception as e:
                logger.warning(f"Status check failed to fetch GH SHA: {e}")

            # Get last generated article (Task)
            last_article = None
            # Assuming 'Task' model is in roo.models and has a clear way to identify "articles" for this user
            # For now, we'll fetch the most recent completed task for this user
            try:
                from roo.models import Task
                from roo.services import PointsService
                user = PointsService.get_user_by_slack_id(slack_user_id)
                if user:
                    recent_task = Task.objects.filter(
                        assigned_user=user, 
                        status='approved'
                    ).order_by('-closed_at').first()
                    
                    if recent_task:
                        last_article = {
                            "title": recent_task.title,
                            "date": recent_task.closed_at,
                            "points": recent_task.points
                        }
            except Exception as e:
                logger.warning(f"Failed to fetch last article: {e}")

            return Response({
                "slack_user_id": integration.slack_user_id,
                "token": integration.github_access_token,
                "user_name": integration.github_user_name,
                "scopes": integration.github_scopes,
                "github_repo": integration.github_repo,
                "project_scanned": integration.project_scanned,
                "last_scanned_at": integration.last_scanned_at,
                "last_scanned_sha": integration.last_scanned_sha,
                "has_updates": has_updates,
                "current_sha": latest_sha,
                "last_article": last_article,
                "pending_intent": integration.pending_intent,
                "error": error_message,
                "auth_url": auth_url,
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


class GithubScanView(APIView):
    """
    Trigger a repository scan via Content Factory.
    POST /api/v1/integrations/github/scan
    """
    permission_classes = [HasRooApiKey]

    def post(self, request):
        from integrations.services.github import scan_github_project, ScanError
        
        slack_user_id = request.data.get('slack_user_id')
        if not slack_user_id:
            return Response(
                {"error": "slack_user_id is required"}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            result = scan_github_project(slack_user_id)
            return Response(result, status=status.HTTP_200_OK)
        except ScanError as e:
            error_msg = str(e)
            if "No integration found" in error_msg:
                return Response({"error": error_msg}, status=status.HTTP_404_NOT_FOUND)
            elif "No GitHub token" in error_msg or "No GitHub repository" in error_msg:
                return Response({"error": error_msg}, status=status.HTTP_400_BAD_REQUEST)
            else:
                return Response({"error": error_msg}, status=status.HTTP_502_BAD_GATEWAY)

