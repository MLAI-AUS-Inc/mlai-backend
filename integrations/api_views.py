import json
import logging
from datetime import timedelta

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db.models import Q
from django.urls import reverse

from core.content_factory_auth import content_factory_github_connection_state
from .models import UserIntegration
from core.article_system import article_system_ready, recommended_next_action as derive_recommended_next_action, resolve_article_system
from core.permissions import HasRooApiKey
from integrations.content_factory_contract import require_roo_request_source
from integrations.services.github_connections import build_github_oauth_url, get_owned_org_configs
from integrations.utils import normalize_domain

logger = logging.getLogger(__name__)


def trigger_scan_async(*args, **kwargs):
    from integrations.services.github import trigger_scan_async as _trigger_scan_async

    return _trigger_scan_async(*args, **kwargs)


def _derive_connection_state(config) -> str:
    return content_factory_github_connection_state(config)


def _serialize_connected_domain(config) -> dict:
    article_system = resolve_article_system(config)
    connection_state = _derive_connection_state(config)
    return {
        "domain": config.organization.domain,
        "github_repo": config.github_repo,
        "article_delivery_mode": getattr(config, "article_delivery_mode", None),
        "scanned": bool(config.scan_summary),
        "articles_scaffolded": config.articles_scaffolded,
        "article_system": article_system,
        "article_system_ready": article_system_ready(article_system),
        "connection_state": connection_state,
        "needs_github_auth": connection_state == "auth_required",
        "last_scanned_at": getattr(config, "last_scanned_at", None),
        "last_scanned_sha": getattr(config, "last_scanned_sha", None),
    }


def _query_param_enabled(raw_value, *, default: bool = True) -> bool:
    if raw_value is None:
        return default
    normalized = str(raw_value).strip().lower()
    if normalized in {"0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    return default

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
        Optional: ?domain=example.com to check domain-specific GitHub credentials.
        """
        if not slack_user_id:
            slack_user_id = request.query_params.get('slack_user_id')

        if not slack_user_id:
            return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        requested_domain = normalize_domain(request.query_params.get('domain') or "")
        requested_domain = requested_domain or None
        include_repo_freshness = _query_param_enabled(
            request.query_params.get("include_repo_freshness"),
            default=True,
        )

        integration = UserIntegration.objects.filter(slack_user_id=slack_user_id).first()
        owned_configs = list(get_owned_org_configs(slack_user_id))
        connected_domains = [
            _serialize_connected_domain(config)
            for config in owned_configs
            if getattr(config, "organization", None)
        ]

        active_config = None
        active_domain = requested_domain
        requires_domain_selection = False

        if requested_domain:
            for config in owned_configs:
                if config.organization and config.organization.domain == requested_domain:
                    active_config = config
                    break
            if active_config is None:
                try:
                    from core.models import Organization
                    org = Organization.objects.filter(domain=requested_domain).first()
                    fallback_config = getattr(org, "content_config", None) if org else None
                    if fallback_config and not fallback_config.connected_slack_user_id:
                        active_config = fallback_config
                except Exception as e:
                    logger.warning(f"Failed to resolve transitional config for {requested_domain}: {e}")
        elif len(owned_configs) == 1:
            active_config = owned_configs[0]
            active_domain = active_config.organization.domain
        elif len(owned_configs) > 1:
            requires_domain_selection = True

        token = integration.github_access_token if integration else None
        token_expires_at = integration.github_token_expires_at if integration else None
        user_name = integration.github_user_name if integration else None
        scopes = integration.github_scopes if integration else []
        github_repo = integration.github_repo if integration else None
        github_installation_id = integration.github_installation_id if integration else None
        project_scanned = bool(integration.project_scanned) if integration else False
        last_scanned_at = integration.last_scanned_at if integration else None
        last_scanned_sha = integration.last_scanned_sha if integration else None
        pending_intent = integration.pending_intent if integration else None

        if active_config is not None:
            token = active_config.github_token_encrypted
            token_expires_at = active_config.github_token_expires_at
            user_name = active_config.github_user_name or user_name
            scopes = active_config.github_scopes or []
            github_repo = active_config.github_repo
            github_installation_id = active_config.github_installation_id
            project_scanned = bool(active_config.scan_summary)
            last_scanned_at = getattr(active_config, "last_scanned_at", None) or last_scanned_at
            last_scanned_sha = getattr(active_config, "last_scanned_sha", None) or last_scanned_sha

        domain_info = {}
        if requested_domain:
            from integrations.services.article_generation import resolve_content_factory_connection_for_domain

            connection_details = resolve_content_factory_connection_for_domain(
                requested_domain,
                slack_user_id,
            )
            active_config = connection_details.get("config") or active_config
            connection_state = connection_details.get("connection_state") or (
                _derive_connection_state(active_config) if active_config else "auth_required"
            )
            domain_info = {
                "domain": requested_domain,
                "domain_connected": bool(connection_details.get("domain_connected")),
                "needs_github_auth": bool(connection_details.get("needs_github_auth")),
                "connection_state": connection_state,
                "credential_source": connection_details.get("credential_source") or "none",
                "oauth_url": build_github_oauth_url(requested_domain, slack_user_id),
            }
            resolved_domain_repo = str(connection_details.get("github_repo") or "").strip()
            if resolved_domain_repo:
                domain_info["domain_github_repo"] = resolved_domain_repo
                domain_info["domain_source"] = connection_details.get("credential_source") or "org"
                github_repo = resolved_domain_repo

        if integration is None and not connected_domains:
            if domain_info:
                error_data = {"error": "Integration not found"}
                error_data.update(domain_info)
                return Response(error_data, status=status.HTTP_404_NOT_FOUND)
            return Response({"error": "Integration not found"}, status=status.HTTP_404_NOT_FOUND)

        # Get last generated article (Task)
        last_article = None
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

        has_updates = False
        repo_has_new_commits = False
        latest_sha = None
        auth_url = None
        error_message = None
        access_revoked = False
        token_refreshed = False

        if include_repo_freshness and not requires_domain_selection:
            try:
                from integrations.services.article_generation import ArticleGenerationError, get_github_credentials_for_domain
                from integrations.services.github import get_latest_repo_sha, is_token_expired, refresh_github_token, TokenRefreshError
                from integrations import http_client as requests

                resolved_repo = github_repo
                resolved_token = token

                if active_domain:
                    try:
                        creds = get_github_credentials_for_domain(active_domain, slack_user_id)
                        resolved_repo = creds['repo']
                        resolved_token = creds['token']
                        domain_info["domain_connected"] = True
                        domain_info["domain_github_repo"] = resolved_repo
                        domain_info["domain_source"] = creds['source']
                        domain_info["credential_source"] = creds['source']
                        domain_info["connection_state"] = "connected" if resolved_repo else "repo_selection_required"
                        domain_info["needs_github_auth"] = False
                        github_repo = resolved_repo
                    except ArticleGenerationError:
                        resolved_repo = None
                        resolved_token = None
                        auth_url = build_github_oauth_url(active_domain, slack_user_id)
                elif integration and integration.github_access_token and is_token_expired(integration):
                    if integration.github_refresh_token:
                        try:
                            logger.info(f"Auto-refreshing expired token for {slack_user_id}")
                            refresh_github_token(slack_user_id)
                            integration.refresh_from_db()
                            token_refreshed = True
                            resolved_token = integration.github_access_token
                            token_expires_at = integration.github_token_expires_at
                            github_installation_id = integration.github_installation_id
                            user_name = integration.github_user_name
                            scopes = integration.github_scopes
                        except TokenRefreshError as e:
                            logger.warning(f"Auto-refresh failed for {slack_user_id}: {e}")
                            error_message = "Token expired, refresh failed"
                            access_revoked = True

                if resolved_token and resolved_repo and not access_revoked:
                    latest_sha = get_latest_repo_sha(resolved_token, resolved_repo)
                    repo_has_new_commits = bool(latest_sha and latest_sha != last_scanned_sha)
                    has_updates = repo_has_new_commits

                    if has_updates and last_scanned_at:
                        scan_cooldown = timedelta(minutes=5)
                        from django.utils import timezone
                        if timezone.now() - last_scanned_at < scan_cooldown:
                            has_updates = False
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 401:
                    error_message = "GitHub token expired"
                    access_revoked = True
                elif e.response.status_code in [403, 404]:
                    error_message = "GitHub access revoked or repository not found"
                    access_revoked = True
                else:
                    logger.warning(f"Status check failed to fetch GH SHA: {e}")

                if access_revoked:
                    try:
                        from integrations.services.github import build_github_auth_url
                        auth_url = build_github_auth_url(slack_user_id, domain=active_domain, request=request)
                    except Exception as url_err:
                        logger.error(f"Failed to build auth url: {url_err}")
            except Exception as e:
                logger.warning(f"Status check failed to fetch GH SHA: {e}")

        scan_completed = bool(project_scanned)
        articles_scaffolded = False
        article_system = {}
        article_system_ready_flag = False

        try:
            from core.models import GeneratedComponent, Organization

            if active_domain:
                org = Organization.objects.filter(domain=active_domain).first()
                config = getattr(org, 'content_config', None) if org else None
                if config:
                    scan_completed = bool(config.scan_summary)
                    if not scan_completed:
                        scan_completed = GeneratedComponent.objects.filter(organization=org).exists()
                    articles_scaffolded = bool(config.articles_scaffolded)
                    article_system = resolve_article_system(config)
                    article_system_ready_flag = article_system_ready(article_system)
                    last_scanned_at = getattr(config, "last_scanned_at", None) or last_scanned_at
                    last_scanned_sha = getattr(config, "last_scanned_sha", None) or last_scanned_sha
            elif requires_domain_selection:
                scan_completed = False
                article_system = {}
                article_system_ready_flag = False
        except Exception as e:
            logger.warning(f"Failed to derive scan readiness for {active_domain}: {e}")

        if scan_completed:
            has_updates = False

        if requires_domain_selection:
            recommended_next_action = "select_domain"
            error_message = error_message or "Multiple domains connected. Please specify which domain to use."
        elif access_revoked or (requested_domain and domain_info.get("domain_connected") is False):
            recommended_next_action = "connect_github"
        else:
            recommended_next_action = derive_recommended_next_action(scan_completed, article_system)

        response_data = {
            "slack_user_id": slack_user_id,
            "token": token,
            "token_expires_at": token_expires_at,
            "token_refreshed": token_refreshed,
            "user_name": user_name,
            "scopes": scopes,
            "github_repo": None if requires_domain_selection else github_repo,
            "github_installation_id": None if requires_domain_selection else github_installation_id,
            "project_scanned": project_scanned,
            "last_scanned_at": last_scanned_at,
            "last_scanned_sha": last_scanned_sha,
            "has_updates": False if requires_domain_selection else has_updates,
            "repo_has_new_commits": False if requires_domain_selection else repo_has_new_commits,
            "current_sha": latest_sha,
            "scan_completed": scan_completed,
            "scan_required": not scan_completed,
            "content_research_ready": scan_completed,
            "article_delivery_mode": getattr(active_config, "article_delivery_mode", None) if active_config else None,
            "articles_scaffolded": articles_scaffolded,
            "article_system": article_system,
            "article_system_ready": article_system_ready_flag,
            "recommended_next_action": recommended_next_action,
            "last_article": last_article,
            "pending_intent": pending_intent,
            "error": error_message,
            "auth_url": auth_url,
            "access_revoked": access_revoked,
            "connected_domains": connected_domains,
            "requires_domain_selection": requires_domain_selection,
            "selected_domain": active_domain,
        }
        if domain_info:
            response_data.update(domain_info)
        return Response(response_data)

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
        if intent is None and 'intent_data' in request.data:
            legacy_intent = request.data.get('intent_data')
            if isinstance(legacy_intent, str):
                try:
                    intent = json.loads(legacy_intent)
                except ValueError:
                    intent = legacy_intent
            else:
                intent = legacy_intent

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
        Path: GET /api/v1/integrations/github/auth-url?slack_user_id=...&domain=...

        When domain is provided, returns an org-level OAuth URL that preserves
        the domain through the OAuth flow. This ensures the domain is linked
        to the correct Organization after callback.
        """
        slack_user_id = request.query_params.get('slack_user_id')
        domain = request.query_params.get('domain')
        if not slack_user_id:
            return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        from integrations.services.github import build_github_auth_url
        auth_url = build_github_auth_url(slack_user_id, domain=domain, request=request)

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
        domain = normalize_domain(request.query_params.get('domain') or "")
        domain = domain or None
        if not slack_user_id:
            return Response(
                {"error": "slack_user_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        from integrations.services.github import build_github_auth_url
        auth_url = build_github_auth_url(slack_user_id, domain=domain, request=request)

        return Response({
            "reauth_url": auth_url,
            "message": "Use this URL to reconnect GitHub via the GitHub App installation flow.",
            "has_existing_installation": bool(domain or UserIntegration.objects.filter(slack_user_id=slack_user_id, github_installation_id__isnull=False).exclude(github_installation_id='').exists()),
        })


class GithubScanView(APIView):
    """
    Trigger a repository scan via Content Factory.
    POST /api/v1/integrations/github/scan
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        from integrations.models import UserIntegration
        from integrations.services.article_generation import get_github_credentials_for_domain, ArticleGenerationError

        slack_user_id = request.data.get('slack_user_id')
        slack_channel_id = request.data.get('slack_channel_id')
        slack_thread_ts = request.data.get('slack_thread_ts')
        domain = request.data.get('domain') or request.query_params.get('domain')

        logger.info(f"Scan request received: slack_user_id={slack_user_id}, domain={domain}, data_keys={list(request.data.keys())}")

        if not slack_user_id:
            return Response(
                {"error": "slack_user_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            request_source = require_roo_request_source(request.data.get("request_source"))
        except ValueError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
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
            except ArticleGenerationError as e:
                # No org-level config for this domain yet.
                # Bootstrap from UserIntegration credentials so the domain gets linked.
                logger.info(f"No existing config for {normalized_domain}, attempting bootstrap from user credentials. Error was: {e}")
                integration = UserIntegration.objects.filter(slack_user_id=slack_user_id).first()
                if integration:
                    logger.info(f"Found UserIntegration for {slack_user_id}: has_token={bool(integration.github_access_token)}, has_repo={bool(integration.github_repo)}, repo={integration.github_repo}")
                else:
                    logger.info(f"No UserIntegration found for {slack_user_id}")
                    oauth_url = build_github_oauth_url(normalized_domain, slack_user_id)
                    return Response({
                        "error": "Integration not found",
                        "needs_github_auth": True,
                        "oauth_url": oauth_url,
                        "domain": normalized_domain,
                    }, status=status.HTTP_404_NOT_FOUND)
                
                if integration and integration.github_access_token and integration.github_repo:
                    from core.models import Organization, OrganizationContentConfig
                    org, org_created = Organization.objects.get_or_create(
                        domain=normalized_domain,
                        defaults={"name": normalized_domain}
                    )
                    logger.info(f"Organization for {normalized_domain}: created={org_created}, id={org.id}")
                    config, config_created = OrganizationContentConfig.objects.get_or_create(organization=org)
                    logger.info(f"OrganizationContentConfig for {normalized_domain}: created={config_created}, id={config.id}")
                    if not config.github_token_encrypted:
                        config.github_token_encrypted = integration.github_access_token
                        config.github_refresh_token_encrypted = integration.github_refresh_token
                        config.github_token_expires_at = integration.github_token_expires_at
                        config.github_user_name = integration.github_user_name
                        config.github_installation_id = integration.github_installation_id
                    if not config.github_repo:
                        config.github_repo = integration.github_repo
                    if not config.connected_slack_user_id:
                        config.connected_slack_user_id = slack_user_id
                    config.save()
                    github_repo = config.github_repo
                    has_credentials = True
                    logger.info(f"Bootstrapped org config for {normalized_domain} from user-level credentials")
                elif integration and integration.github_access_token and not integration.github_repo:
                    # User has connected GitHub but hasn't selected a repo yet
                    logger.warning(f"User {slack_user_id} has GitHub token but no repo set - cannot bootstrap domain {normalized_domain}")
                    oauth_url = build_github_oauth_url(normalized_domain, slack_user_id)
                    return Response({
                        "error": f"Please complete GitHub setup for {normalized_domain}. You need to select a repository.",
                        "needs_github_auth": True,
                        "oauth_url": oauth_url,
                        "domain": normalized_domain,
                    }, status=status.HTTP_400_BAD_REQUEST)

        # Fall back to UserIntegration ONLY when no domain was specified.
        # When a domain IS specified, we must use the domain-specific repo —
        # falling back to the user's default repo would scan the wrong codebase.
        if not has_credentials and not normalized_domain:
            # Check if user has multiple connected domains — if so, require domain selection
            # rather than silently using the default repo (which may be the wrong one).
            user_org_configs = list(get_owned_org_configs(slack_user_id))
            if len(user_org_configs) > 1:
                available = [
                    {"domain": c.organization.domain, "github_repo": c.github_repo}
                    for c in user_org_configs if c.organization
                ]
                return Response({
                    "error": "You have multiple connected codebases. Please specify which domain to scan.",
                    "available_domains": available,
                    "hint": "Try: scan my codebase <domain>",
                }, status=status.HTTP_400_BAD_REQUEST)

            integration = UserIntegration.objects.filter(slack_user_id=slack_user_id).first()
            if integration and integration.github_access_token and integration.github_repo:
                github_repo = integration.github_repo
                has_credentials = True

        if not has_credentials:
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
                    .filter(
                        Q(connected_slack_user_id=slack_user_id)
                        | Q(connected_slack_user_id__isnull=True)
                    )
                    .first()
                )
                if config and config.organization and config.organization.domain:
                    normalized_domain = normalize_domain(config.organization.domain)
            except Exception as e:
                logger.warning(f"Error resolving domain for scan: {e}")

        if not normalized_domain:
            # Provide a helpful error when we have credentials but no domain could be resolved
            return Response({
                "error": "Please specify a domain to scan.",
                "hint": "Try: scan my codebase <domain>",
                "github_repo": github_repo,
            }, status=status.HTTP_400_BAD_REQUEST)

        # Ensure org/config exist (but don't overwrite existing github_repo)
        try:
            from core.models import Organization, OrganizationContentConfig
            org, _ = Organization.objects.get_or_create(
                domain=normalized_domain,
                defaults={"name": normalized_domain}
            )
            config, created = OrganizationContentConfig.objects.get_or_create(organization=org)
            # Only set github_repo if the config doesn't already have one
            update_fields = []
            if not config.connected_slack_user_id:
                config.connected_slack_user_id = slack_user_id
                update_fields.append('connected_slack_user_id')
            if not config.github_repo and github_repo:
                config.github_repo = github_repo
                update_fields.append('github_repo')
            if update_fields:
                config.save(update_fields=update_fields)
        except Exception as e:
            logger.warning(f"Failed to persist organization mapping for scan: {e}")

        # Trigger in background
        trigger_scan_async(
            slack_user_id,
            slack_channel_id=slack_channel_id,
            slack_thread_ts=slack_thread_ts,
            domain=normalized_domain,
            request_source=request_source,
        )

        return Response({
            "status": "scan_initiated",
            "message": "Scan running in background. You will be notified via Slack when complete.",
            "domain": normalized_domain,
            "github_repo": github_repo,
        }, status=status.HTTP_202_ACCEPTED)


class GithubScaffoldView(APIView):
    """
    Trigger article directory scaffolding for a domain.
    POST /api/v1/integrations/github/scaffold

    Called by the Roo slackbot when a user clicks "Create Articles Directory".
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        import threading
        from integrations.services.github import scaffold_articles_directory, ScanError
        from integrations.services.article_generation import get_github_credentials_for_domain, ArticleGenerationError
        from integrations.services.slack import SlackService
        from core.models import Organization, OrganizationContentConfig

        domain = request.data.get('domain')
        slack_user_id = request.data.get('slack_user_id')
        slack_channel_id = request.data.get('slack_channel_id', '')
        slack_thread_ts = request.data.get('slack_thread_ts', '')

        if not domain or not slack_user_id:
            return Response(
                {"error": "domain and slack_user_id are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        normalized_domain = normalize_domain(domain)
        if not normalized_domain:
            return Response({"error": "Invalid domain"}, status=status.HTTP_400_BAD_REQUEST)

        # Look up config
        try:
            org = Organization.objects.get(domain=normalized_domain)
            config = org.content_config
        except (Organization.DoesNotExist, OrganizationContentConfig.DoesNotExist):
            return Response(
                {"error": f"No configuration found for {normalized_domain}"},
                status=status.HTTP_404_NOT_FOUND
            )

        # Check scan prerequisite — scan must have completed before scaffolding
        from core.models import GeneratedComponent
        has_components = GeneratedComponent.objects.filter(organization=org).exists()
        if not has_components and not config.scan_summary:
            return Response({
                "error": "Repository must be scanned before scaffolding.",
                "error_code": "PREREQUISITE_MISSING",
                "missing_step": "scan",
                "domain": normalized_domain,
                "hint": "Scan the codebase first.",
            }, status=status.HTTP_412_PRECONDITION_FAILED)

        if config.articles_scaffolded:
            return Response({
                "status": "already_scaffolded",
                "message": f"Articles directory already exists for {normalized_domain}",
                "pr_url": config.articles_scaffold_pr_url,
                "preview_url": config.articles_scaffold_preview_url,
            }, status=status.HTTP_200_OK)

        # Resolve GitHub credentials
        try:
            creds = get_github_credentials_for_domain(normalized_domain, slack_user_id)
        except ArticleGenerationError as e:
            return Response(
                {"error": str(e), "needs_github_auth": True},
                status=status.HTTP_400_BAD_REQUEST
            )

        def _run_scaffold():
            try:
                scaffold_articles_directory(
                    domain=normalized_domain,
                    slack_user_id=slack_user_id,
                    github_token=creds['token'],
                    github_repo=creds['repo'],
                    slack_channel_id=slack_channel_id,
                    slack_thread_ts=slack_thread_ts,
                )
            except ScanError as e:
                logger.error(f"Scaffold failed for {normalized_domain}: {e}")
                error_str = str(e)
                if 'PREREQUISITE_MISSING' in error_str:
                    msg = (
                        f"⚠️ *{normalized_domain}* needs to be scanned first before setting up articles.\n\n"
                        f"Say: `@Roo scan my codebase {normalized_domain}`"
                    )
                else:
                    msg = f"❌ Could not start scaffolding for *{normalized_domain}*: {e}"
                if slack_channel_id and slack_thread_ts:
                    SlackService.send_message(slack_channel_id, msg, thread_ts=slack_thread_ts)
                else:
                    SlackService.send_dm(slack_user_id, msg)

        thread = threading.Thread(target=_run_scaffold, daemon=True)
        thread.start()

        return Response({
            "status": "scaffold_initiated",
            "message": "Scaffolding started. You will be notified when complete.",
            "domain": normalized_domain,
        }, status=status.HTTP_202_ACCEPTED)


class GithubScaffoldDecisionView(APIView):
    """
    Record scaffold approval or denial for a scan run.
    POST /api/v1/integrations/github/scaffold/decision
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        from integrations.services.github import decide_scan_scaffold, ScanError

        scan_run_id = str(request.data.get('scan_run_id') or request.data.get('run_id') or '').strip()
        decision = str(request.data.get('decision') or '').strip().lower()
        domain = normalize_domain(request.data.get('domain') or '')
        slack_user_id = str(request.data.get('slack_user_id') or '').strip()
        slack_channel_id = str(request.data.get('slack_channel_id') or '').strip()
        slack_thread_ts = str(request.data.get('slack_thread_ts') or '').strip()

        if not scan_run_id or not decision or not slack_user_id:
            return Response(
                {"error": "scan_run_id, decision, and slack_user_id are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if decision not in {'approve', 'deny'}:
            return Response(
                {"error": "decision must be 'approve' or 'deny'"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = decide_scan_scaffold(
                scan_run_id=scan_run_id,
                decision=decision,
                domain=domain,
                slack_user_id=slack_user_id,
                slack_channel_id=slack_channel_id,
                slack_thread_ts=slack_thread_ts,
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ScanError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response(result.get("data") or {}, status=result.get("status_code", status.HTTP_200_OK))
