import secrets
import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.conf import settings
from django.urls import reverse

from .models import UserIntegration
from core.permissions import HasRooApiKey
from integrations.utils import normalize_domain

logger = logging.getLogger(__name__)

class GithubTokenIdentityView(APIView):
    authentication_classes = []
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
            access_revoked = False
            token_refreshed = False

            try:
                from integrations.services.github import get_latest_repo_sha, is_token_expired, refresh_github_token, TokenRefreshError
                import requests

                # Auto-refresh token if expired
                if integration.github_access_token and is_token_expired(integration):
                    if integration.github_refresh_token:
                        try:
                            logger.info(f"Auto-refreshing expired token for {slack_user_id}")
                            refresh_github_token(slack_user_id)
                            integration.refresh_from_db()  # Reload to get new token
                            token_refreshed = True
                        except TokenRefreshError as e:
                            logger.warning(f"Auto-refresh failed for {slack_user_id}: {e}")
                            error_message = "Token expired, refresh failed"
                            access_revoked = True

                if integration.github_access_token and integration.github_repo and not access_revoked:
                    latest_sha = get_latest_repo_sha(integration.github_access_token, integration.github_repo)
                    if latest_sha and latest_sha != integration.last_scanned_sha:
                        has_updates = True

                    # Check if recently scanned (within 5 minutes) - avoid re-scanning
                    # even if there are minor updates
                    if has_updates and integration.last_scanned_at:
                        from django.utils import timezone
                        from datetime import timedelta
                        scan_cooldown = timedelta(minutes=5)
                        if timezone.now() - integration.last_scanned_at < scan_cooldown:
                            # Mark as recently scanned - bot can skip re-scan
                            has_updates = False  # Override: don't trigger re-scan
                            logger.info(f"Suppressing has_updates for {slack_user_id}: scanned {(timezone.now() - integration.last_scanned_at).seconds}s ago")
            except requests.exceptions.HTTPError as e:
                # Handle expired token (401) or revoked access (404)
                if e.response.status_code == 401:
                    logger.warning(f"GitHub Token Expired for {slack_user_id}")
                    error_message = "GitHub token expired"
                    access_revoked = True
                elif e.response.status_code in [403, 404]:
                    logger.warning(f"GitHub Access Revoked for {slack_user_id}: {e.response.status_code}")
                    error_message = "GitHub access revoked or repository not found"
                    access_revoked = True
                else:
                    logger.warning(f"Status check failed to fetch GH SHA: {e}")

                # Generate Re-Auth URL for any access issue
                if access_revoked:
                    try:
                        connect_path = reverse('github_connect')
                        full_connect_url = request.build_absolute_uri(connect_path)
                        auth_url = f"{full_connect_url}?slack_user_id={slack_user_id}"
                    except Exception as url_err:
                        logger.error(f"Failed to build auth url: {url_err}")
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
                "token_expires_at": integration.github_token_expires_at,
                "token_refreshed": token_refreshed,
                "user_name": integration.github_user_name,
                "scopes": integration.github_scopes,
                "github_repo": integration.github_repo,
                "github_installation_id": integration.github_installation_id,
                "project_scanned": integration.project_scanned,
                "last_scanned_at": integration.last_scanned_at,
                "last_scanned_sha": integration.last_scanned_sha,
                "has_updates": has_updates,
                "current_sha": latest_sha,
                "last_article": last_article,
                "pending_intent": integration.pending_intent,
                "error": error_message,
                "auth_url": auth_url,
                "access_revoked": access_revoked,
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
    authentication_classes = []
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
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def patch(self, request):
        """
        Legacy endpoint for updating status.
        Use GithubTokenIdentityView.patch instead.
        """
        return GithubTokenIdentityView.as_view()(request)


class GithubAuthUrlView(APIView):
    authentication_classes = []
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


class GithubTokenRefreshView(APIView):
    """
    Silently refresh GitHub access token using stored refresh token.
    POST /api/v1/integrations/github/refresh
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        from integrations.services.github import refresh_github_token, TokenRefreshError

        slack_user_id = request.data.get('slack_user_id')
        if not slack_user_id:
            return Response(
                {"error": "slack_user_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            result = refresh_github_token(slack_user_id)
            return Response({
                "status": "success",
                "message": "Token refreshed successfully",
                "expires_at": result["expires_at"],
            }, status=status.HTTP_200_OK)
        except TokenRefreshError as e:
            return Response({
                "error": str(e),
                "requires_reauth": True,
                "reauth_url": self._build_reauth_url(request, slack_user_id),
            }, status=status.HTTP_401_UNAUTHORIZED)
        except Exception as e:
            logger.error(f"Token refresh error for {slack_user_id}: {e}")
            return Response(
                {"error": "Token refresh failed"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _build_reauth_url(self, request, slack_user_id):
        try:
            connect_path = reverse('github_connect')
            full_connect_url = request.build_absolute_uri(connect_path)
            return f"{full_connect_url}?slack_user_id={slack_user_id}"
        except Exception:
            return None


class GithubReauthUrlView(APIView):
    """
    Get a quick re-authentication URL (doesn't require app reinstall).
    GET /api/v1/integrations/github/reauth-url?slack_user_id=...

    This returns a URL that will re-authorize the user without requiring
    them to uninstall and reinstall the GitHub App.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        slack_user_id = request.query_params.get('slack_user_id')
        if not slack_user_id:
            return Response(
                {"error": "slack_user_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if user has an existing integration with installation_id
        integration = UserIntegration.objects.filter(slack_user_id=slack_user_id).first()

        # Build the OAuth authorization URL (not the app installation URL)
        # This allows re-authorization without reinstalling the app
        rand_token = secrets.token_urlsafe(16)
        state = f"{slack_user_id}::{rand_token}"

        # Store state in response for client to handle (stateless approach)
        # Or we can use the existing github_connect flow

        # Option 1: Direct OAuth URL (faster, no session needed)
        if integration and integration.github_installation_id:
            # If already installed, we can use a simpler re-auth flow
            oauth_url = (
                f"https://github.com/login/oauth/authorize?"
                f"client_id={settings.GITHUB_OAUTH_CLIENT_ID}&"
                f"state={state}"
            )
            return Response({
                "reauth_url": oauth_url,
                "message": "Use this URL to quickly re-authorize without reinstalling the app.",
                "has_existing_installation": True,
            })

        # Option 2: Full connect flow (if no installation exists)
        connect_path = reverse('github_connect')
        full_connect_url = request.build_absolute_uri(connect_path)
        auth_url = f"{full_connect_url}?slack_user_id={slack_user_id}"

        return Response({
            "reauth_url": auth_url,
            "message": "Use this URL to connect GitHub (will show app installation flow).",
            "has_existing_installation": False,
        })


class GithubScanView(APIView):
    """
    Trigger a repository scan via Content Factory.
    POST /api/v1/integrations/github/scan
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        from integrations.services.github import trigger_scan_async, ScanError
        from integrations.models import UserIntegration
        from integrations.services.article_generation import get_github_credentials_for_domain, ArticleGenerationError

        slack_user_id = request.data.get('slack_user_id')
        slack_channel_id = request.data.get('slack_channel_id')
        slack_thread_ts = request.data.get('slack_thread_ts')
        domain = request.data.get('domain') or request.query_params.get('domain')

        if not slack_user_id:
            return Response(
                {"error": "slack_user_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Resolve domain
        normalized_domain = normalize_domain(domain)

        # Try to resolve credentials: org-level first, then user-level
        github_repo = None
        has_credentials = False

        if normalized_domain:
            try:
                creds = get_github_credentials_for_domain(normalized_domain, slack_user_id)
                github_repo = creds['repo']
                has_credentials = True
                logger.info(f"Scan credentials resolved via {creds['source']}-level for {normalized_domain}")
            except ArticleGenerationError:
                pass

        # Fall back to UserIntegration
        if not has_credentials:
            integration = UserIntegration.objects.filter(slack_user_id=slack_user_id).first()
            if integration and integration.github_access_token and integration.github_repo:
                github_repo = integration.github_repo
                has_credentials = True

        if not has_credentials:
            from integrations.services.article_generation import build_github_oauth_url
            oauth_url = build_github_oauth_url(normalized_domain or '', slack_user_id)
            return Response({
                "error": f"No GitHub credentials found for domain '{normalized_domain or 'unknown'}'. Please connect GitHub first.",
                "needs_github_auth": True,
                "oauth_url": oauth_url,
                "domain": normalized_domain,
            }, status=status.HTTP_400_BAD_REQUEST)

        # Resolve domain from config if not provided in request
        if not normalized_domain and github_repo:
            try:
                from core.models import OrganizationContentConfig
                config = (
                    OrganizationContentConfig.objects
                    .select_related('organization')
                    .filter(github_repo=github_repo)
                    .first()
                )
                if config and config.organization and config.organization.domain:
                    normalized_domain = normalize_domain(config.organization.domain)
            except Exception as e:
                logger.warning(f"Error resolving domain for scan: {e}")

        if not normalized_domain:
            return Response({"error": "domain is required"}, status=status.HTTP_400_BAD_REQUEST)

        # Ensure org/config exist (but don't overwrite existing github_repo)
        try:
            from core.models import Organization, OrganizationContentConfig
            org, _ = Organization.objects.get_or_create(
                domain=normalized_domain,
                defaults={"name": normalized_domain}
            )
            config, created = OrganizationContentConfig.objects.get_or_create(organization=org)
            # Only set github_repo if the config doesn't already have one
            if not config.github_repo and github_repo:
                config.github_repo = github_repo
                config.save(update_fields=['github_repo'])
        except Exception as e:
            logger.warning(f"Failed to persist organization mapping for scan: {e}")

        # Trigger in background
        trigger_scan_async(
            slack_user_id,
            slack_channel_id=slack_channel_id,
            slack_thread_ts=slack_thread_ts,
            domain=normalized_domain,
        )

        return Response({
            "status": "scan_initiated",
            "message": "Scan running in background. You will be notified via Slack when complete.",
            "domain": normalized_domain,
            "github_repo": github_repo,
        }, status=status.HTTP_202_ACCEPTED)
