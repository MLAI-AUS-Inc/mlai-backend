import logging
import os
import time
from datetime import date as calendar_date
from typing import Optional
from urllib.parse import urlparse
from django.core import signing
from django.db import OperationalError, connection, transaction
from django.db.models import Q
from django.contrib.auth import get_user_model, login as auth_login
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.views import APIView
from rest_framework.decorators import api_view, permission_classes
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from django.views.decorators.csrf import csrf_exempt
from django.views import View

from .serializers import MyTokenObtainPairSerializer
from .email_utils import (
    MAGIC_LINK_KIND_USER,
    generate_magic_link,
    send_magic_link_email,
    verify_magic_link,
)
from .article_system import (
    article_system_ready,
    merge_article_system,
    normalize_article_system,
    resolve_article_system,
)
from .content_factory_progress import (
    live_card_summary_for_job,
    maybe_send_still_working_ping,
    upsert_live_progress_card,
)
from .content_factory_auth import content_factory_github_connection_state
from .content_factory_delivery import (
    build_content_factory_preview_url,
    build_content_ready_blocks,
    build_draft_pr_created_blocks,
    build_progress_update_blocks,
    build_preview_ready_blocks,
    build_content_thread_messages,
    render_content_preview_error_page,
    render_content_preview_page,
    validate_content_factory_preview_signature,
)
from .models import Hackathon
from esafety.models import Team as EsafetyTeam
from hospital.models import Team as HospitalTeam
from .serializers import MyTokenObtainPairSerializer, HackathonSerializer, UserSerializer
from rest_framework.generics import ListAPIView, RetrieveAPIView, RetrieveUpdateAPIView
from .permissions import IsOwnerOrTeammateOrSuperuser, HasAPIKey, HasRooApiKey
from .models import (
    ComponentMapping,
    ContentFactoryApprovalState,
    ContentFactoryHealingRecord,
    ContentFactoryRun,
    ContentFactoryRunStatus,
    ContentFactoryRunStep,
    ContentFactoryRunStepAttempt,
    ContentFactoryStepStatus,
    GeneratedComponent,
    Organization,
    OrganizationContentConfig,
)
from integrations.services.github_connections import get_owned_org_configs
from .serializers import (
    ContentFactoryHealingRecordSerializer,
    ContentFactoryRunControlSerializer,
    ContentFactoryRunSyncSerializer,
    ContentFactoryRunValleyJobSerializer,
    GeneratedComponentListSerializer,
    GeneratedComponentSerializer,
)

logger = logging.getLogger(__name__)
User = get_user_model()
VALLEY_META_KEY = "_valley_meta"

ROLE_ALIASES = {
    'mentor': 'professional',
    'judge': 'professional',
    'organizer': 'professional',
}
ALLOWED_PERSONAS = {"hacker", "hustler", "hipster", "healer"}
MEDHACK_TEAM_MIN_MEMBERS = 2
MEDHACK_TEAM_MAX_MEMBERS = 6


def _normalize_app_context(app_value, default='hospital'):
    app = (app_value or default or '').strip().lower()
    if app in ('medhack', 'hospital'):
        return 'hospital'
    if app in ('esafety', 'e-safety'):
        return 'esafety'
    if app in ('innovate-connect-alliance', 'innovate_connect_alliance', 'ica'):
        return 'innovate-connect-alliance'
    if app in ('vibe-raising', 'vibe_raising', 'viberaising'):
        return 'vibe-raising'
    return default


def _normalize_next_path(next_path):
    if not next_path:
        return None

    normalized = str(next_path).strip()
    if not normalized:
        return None

    # Prevent open redirects. We only allow relative paths.
    if '://' in normalized:
        return None

    if not normalized.startswith('/'):
        normalized = f'/{normalized}'

    return normalized


def _append_auth_query_params(magic_link, app, next_path=None):
    separator = '&' if '?' in magic_link else '?'
    magic_link = f"{magic_link}{separator}app={app}"
    if next_path:
        magic_link += f"&next={next_path}"
    return magic_link


def _origin_from_url(url, fallback):
    if not url:
        return fallback

    parsed = urlparse(str(url).strip())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return fallback


def _frontend_base_url(app_context):
    from django.conf import settings

    fallback = "http://localhost:5173" if settings.DEBUG else "https://mlai.au"
    default_origin = _origin_from_url(getattr(settings, 'DEFAULT_FRONTEND_URL', None), fallback)
    if app_context == 'hospital':
        return _origin_from_url(getattr(settings, 'MEDHACK_URL', None), default_origin)
    if app_context == 'esafety':
        return _origin_from_url(getattr(settings, 'ESAFETY_URL', None), default_origin)
    if app_context == 'innovate-connect-alliance':
        return _origin_from_url(getattr(settings, 'INNOVATE_CONNECT_ALLIANCE_URL', None), default_origin)
    if app_context == 'vibe-raising':
        return _origin_from_url(getattr(settings, 'VIBE_RAISING_URL', None), default_origin)
    return default_origin


def _normalize_discovery_diagnostics(value):
    if not isinstance(value, dict):
        return {}

    diagnostics = {}
    for key, raw_value in value.items():
        try:
            diagnostics[str(key)] = int(raw_value)
        except (TypeError, ValueError):
            continue
    return diagnostics


def _format_discovery_diagnostics(diagnostics):
    normalized = _normalize_discovery_diagnostics(diagnostics)
    if not normalized:
        return ""

    seed_count = normalized.get("seed_count", 0)
    competitor_count = normalized.get("competitor_count", 0)
    seed_results = (
        normalized.get("keyword_ideas_count", 0)
        + normalized.get("keyword_suggestions_count", 0)
        + normalized.get("related_keywords_count", 0)
        + normalized.get("ai_question_count", 0)
    )
    competitor_candidates = normalized.get("competitor_candidate_count", 0)
    relevance_rejections = (
        normalized.get("keyword_relevance_rejected_count", 0)
        + normalized.get("competitor_relevance_rejected_count", 0)
    )
    already_used = (
        normalized.get("written_exclusion_count", 0)
        + normalized.get("already_used_exclusion_count", 0)
        + normalized.get("semantic_dedup_exclusion_count", 0)
    )
    remaining = normalized.get("remaining_opportunity_count", normalized.get("deduplicated_count", 0))

    lines = []
    if seed_count or competitor_count:
        lines.append(
            f"Checked {seed_count} seed keywords and {competitor_count} competitors."
        )
    if seed_results or competitor_candidates:
        lines.append(
            f"Found {seed_results} seed-derived candidates and {competitor_candidates} competitor candidates."
        )
    if relevance_rejections:
        lines.append(f"Filtered out {relevance_rejections} candidates as irrelevant.")
    if already_used:
        lines.append(f"Excluded {already_used} candidates that were already used or too close to existing topics.")
    lines.append(f"{remaining} viable topics remained.")
    return "\n".join(lines)


def _scan_destination_summary(article_system, publish_targets):
    resolved = normalize_article_system(article_system)
    location = resolved.get("directory_path") or resolved.get("directory_name") or "your content directory"
    targets = [item for item in (publish_targets or []) if isinstance(item, dict)]

    if any(
        str(item.get("kind") or "").strip() == "bundle_only_article_directory"
        or str(item.get("publish_capability") or "").strip() == "bundle_only"
        for item in targets
    ):
        return (
            f"I found a content directory at `{location}`.\n\n"
            f"Roo can draft content for it now, and direct publish can be added later with a supported target or `.content-factory/target.yml`."
        )

    if any(str(item.get("kind") or "").strip() == "hook_publish" for item in targets):
        return (
            f"I found a configured content target at `{location}`.\n\n"
            f"This repo can publish through the existing Content Factory hook configuration."
        )

    if article_system_ready(resolved):
        return (
            f"I found a ready article system at "
            f"`{location}`."
        )

    return ""


def _normalize_content_factory_domain(domain: str) -> str:
    if not domain:
        return ""
    domain = str(domain).lower().strip()
    if domain.startswith('https://'):
        domain = domain[8:]
    elif domain.startswith('http://'):
        domain = domain[7:]
    if domain.startswith('www.'):
        domain = domain[4:]
    if '/' in domain:
        domain = domain.split('/')[0]
    return domain


def _content_factory_github_connection_state(config) -> str:
    return content_factory_github_connection_state(config)


def _content_factory_github_auth_url(*, slack_user_id: str, domain: Optional[str] = None) -> str:
    from integrations.services.github import build_github_auth_url

    normalized_domain = _normalize_content_factory_domain(domain or "")
    return build_github_auth_url(slack_user_id or "", domain=normalized_domain or None)


class CheckUserView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        data = request.data
        email = data.get('email')
        _normalize_app_context(data.get('app'), default='hospital')

        if not email:
            return Response({"error": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {"user_exists": User.objects.filter(email__iexact=email).exists()},
            status=status.HTTP_200_OK,
        )

class SendMagicLinkView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        data = request.data
        email = data.get('email')
        app = _normalize_app_context(data.get('app'), default='hospital')
        next_path = _normalize_next_path(data.get('next'))
        
        if not email:
            return Response({"error": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.filter(email__iexact=email).first()

            if not user:
                # User does not exist, return specific response to frontend
                return Response(
                    {"user_exists": False, "message": "User does not exist."}, 
                    status=status.HTTP_200_OK
                )

            # User exists, send magic link
            if not user.is_active:
                # Optionally handle inactive users differently, but for now we'll allow them to re-verify
                pass

            base_url = _frontend_base_url(app)

            magic_link = generate_magic_link(user, base_url=base_url)
            magic_link = _append_auth_query_params(magic_link, app, next_path=next_path)

            logger.info(f"Generated magic link for {email}: {magic_link}")
            send_magic_link_email(user, magic_link, message_id="2")
            logger.info(f"Sent magic link to existing user: {email} for app {app}")

            return Response(
                {"user_exists": True, "magic_link_sent": True, "message": "Magic link sent to your email."},
                status=status.HTTP_200_OK
            )

        except Exception as e:
            logger.exception(f"Error in SendMagicLinkView: {str(e)}")
            return Response({"error": "An error occurred while processing your request."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CreateUserView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        data = request.data
        email = data.get('email')
        first_name = data.get('firstName') or data.get('first_name') or ''
        last_name = data.get('lastName') or data.get('last_name') or ''
        phone = data.get('phone')
        requested_role = (data.get('role') or 'participant').strip().lower()
        role = ROLE_ALIASES.get(requested_role, requested_role)
        allowed_roles = {choice[0] for choice in User.ROLE_CHOICES}
        if role not in allowed_roles:
            role = 'participant'
        app = _normalize_app_context(data.get('app'), default='hospital')
        next_path = _normalize_next_path(data.get('next'))

        if not email:
            return Response({"error": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                if User.objects.filter(email__iexact=email).exists():
                    return Response(
                        {"error": "User with this email already exists."}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )

                user = User.objects.create_user(
                    email=email,
                    role=role,
                    first_name=first_name,
                    last_name=last_name,
                    phone=phone
                )
                user.is_active = False # Require verification
                user.save()
                
                logger.info(f"Created new user: {email}")

            # Generate magic link and send email OUTSIDE the transaction
            # so if email fails, user is still created.
            
            base_url = _frontend_base_url(app)

            magic_link = generate_magic_link(user, base_url=base_url)
            magic_link = _append_auth_query_params(magic_link, app, next_path=next_path)
            try:
                send_magic_link_email(user, magic_link, message_id="2")
                logger.info(f"Sent magic link to new user: {email} for app {app}")
                message = "Account created and magic link sent."
            except Exception as e:
                logger.error(f"Failed to send magic link to {email}: {e}")
                # In development/hackathon, we might want to return the link if email fails
                # or just log it. For now, we'll return a warning.
                message = "Account created, but failed to send email. Please check logs for magic link."

            return Response(
                {
                    "id": user.id,
                    "email": user.email,
                    "full_name": user.full_name,
                    "message": message,
                    "magic_link": magic_link,
                },
                status=status.HTTP_201_CREATED
            )

        except Exception as e:
            logger.exception(f"Error in CreateUserView: {str(e)}")
            return Response({"error": "An error occurred while creating the account."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class MagicLinkVerifyView(APIView):
    """
    Verifies the token from the magic link, activates the user (if needed),
    and issues JWT tokens.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        token = request.query_params.get('token')
        logger.info("Received magic link verification request.")
        token_data = verify_magic_link(token)
        if token_data:
            try:
                email = token_data.get('email')
                if not email:
                    logger.warning("Magic link token payload missing email.")
                    return Response({"error": "Invalid or expired token."}, status=status.HTTP_400_BAD_REQUEST)

                token_kind = token_data.get('kind')
                if token_kind not in (None, MAGIC_LINK_KIND_USER):
                    logger.warning("Unsupported magic link token kind: %s", token_kind)
                    return Response({"error": "Invalid or expired token."}, status=status.HTTP_400_BAD_REQUEST)

                user = User.objects.get(email__iexact=email)
                logger.info("Verified magic link for existing user: %s", email)

                if not user.is_active:
                    user.is_active = True
                    user.save()
                    logger.info(f"Activated user account for {email}")

                auth_login(
                    request._request,
                    user,
                    backend="django.contrib.auth.backends.ModelBackend",
                )

                # Generate JWT tokens
                refresh = RefreshToken.for_user(user)
                access_token = str(refresh.access_token)
                refresh_token = str(refresh)

                # Build the response payload
                # Determine next_url based on app context
                
                app_param = _normalize_app_context(request.query_params.get('app'), default='hospital')
                next_param = _normalize_next_path(request.query_params.get('next'))
                
                base_url = _frontend_base_url(app_param)

                # Construct app-aware redirect path
                if next_param:
                    redirect_path = next_param
                elif app_param == 'esafety':
                    redirect_path = "/esafety/dashboard"
                elif app_param == 'innovate-connect-alliance':
                    redirect_path = "/innovate-connect-alliance"
                elif app_param == 'vibe-raising':
                    redirect_path = "/vibe-raising"
                else:
                    redirect_path = "/hospital/app"

                next_url = f"{base_url}{redirect_path}"

                response_data = {
                    'message': 'Login successful',
                    'user': {
                        'id': user.id,
                        'email': user.email,
                        'first_name': user.first_name,
                        'last_name': user.last_name,
                        'full_name': user.full_name,
                        'role': user.role,
                        'is_superuser': user.is_superuser,
                        'is_active': user.is_active,
                        'has_team': user.has_team,
                        'avatar_url': user.avatar_url,
                    },
                    'redirect': redirect_path,
                    'next_url': next_url, 
                }

                response = Response(response_data, status=status.HTTP_200_OK)

                # Set cookies
                # Use settings for secure flag to support local dev (HTTP) vs prod (HTTPS)
                from django.conf import settings
                
                # For local development, set domain to None (host only)
                # In production, set domain to .mlai.au with Secure=True and SameSite=None
                is_production = not settings.DEBUG
                
                cookie_domain = None if not is_production else '.mlai.au'
                
                cookie_kwargs = {
                    'httponly': True,
                    'path': '/',
                    'domain': cookie_domain,
                    'secure': is_production,
                    'samesite': 'None' if is_production else 'Lax',
                }
                
                response.set_cookie(
                    key='access_token',
                    value=access_token,
                    max_age=86400,  # 1 day
                    **cookie_kwargs
                )
                response.set_cookie(
                    key='refresh_token',
                    value=refresh_token,
                    max_age=172800,  # 2 days
                    **cookie_kwargs
                )

                logger.info(f"Set cookies for {email}: kwargs={cookie_kwargs}")
                return response

            except User.DoesNotExist:
                logger.error(f"User with email {email} does not exist.")
                return Response({"error": "User does not exist."}, status=status.HTTP_400_BAD_REQUEST)
        else:
            logger.warning("Invalid or expired magic link token.")
            return Response({"error": "Invalid or expired token."}, status=status.HTTP_400_BAD_REQUEST)

class MyTokenObtainPairView(TokenObtainPairView):
    serializer_class = MyTokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        access_token = response.data.get('access')
        refresh_token = response.data.get('refresh')

        from django.conf import settings
        is_production = not settings.DEBUG
        response.set_cookie(
            key='access_token',
            value=access_token,
            httponly=True,
            secure=is_production,
            samesite='None' if is_production else 'Lax',
        )
        response.set_cookie(
            key='refresh_token',
            value=refresh_token,
            httponly=True,
            secure=is_production,
            samesite='None' if is_production else 'Lax',
        )
        # Remove tokens from response body
        response.data = {}
        return response

class CookieTokenRefreshView(TokenRefreshView):
    def post(self, request, *args, **kwargs):
        refresh_token = request.COOKIES.get('refresh_token')
        if refresh_token is None:
            return Response({'error': 'Refresh token not found in cookies'}, status=400)
        serializer = self.get_serializer(data={'refresh': refresh_token})
        try:
            serializer.is_valid(raise_exception=True)
        except:
            return Response({'error': 'Invalid token'}, status=401)
        access_token = serializer.validated_data['access']
        response = Response()
        from django.conf import settings
        is_production = not settings.DEBUG
        response.set_cookie(
            key='access_token',
            value=access_token,
            httponly=True,
            secure=is_production,
            samesite='None' if is_production else 'Lax',
            path='/',
        )
        return response

class CurrentUserView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        user = request.user
        if not user.is_authenticated:
            response = Response({'error': 'Not authenticated'}, status=status.HTTP_401_UNAUTHORIZED)
            response._has_been_logged = True
            return response
        
        # Retrieve hospital team
        hospital_team = user.hospital_teams.first() if hasattr(user, 'hospital_teams') else None
        hospital_team_data = None
        if hospital_team:
            members = hospital_team.members.all().values("first_name", "last_name", "avatar_url", "role")
            hospital_team_data = {
                "team_name": hospital_team.team_name,
                "team_id": hospital_team.team_id,
                "avatar_url": hospital_team.avatar_url,
                "member_count": hospital_team.members.count(),
                "is_valid_team_size": MEDHACK_TEAM_MIN_MEMBERS <= hospital_team.members.count() <= MEDHACK_TEAM_MAX_MEMBERS,
                "members": [{"full_name": f"{m['first_name']} {m['last_name']}".strip(), "avatar_url": m["avatar_url"], "role": m["role"]} for m in members]
            }

        # Retrieve esafety team
        esafety_team = user.esafety_teams.first() if hasattr(user, 'esafety_teams') else None
        esafety_team_data = None
        if esafety_team:
            members = esafety_team.members.all().values("first_name", "last_name", "avatar_url", "role")
            esafety_team_data = {
                "team_name": esafety_team.team_name,
                "team_id": esafety_team.team_id,
                "avatar_url": esafety_team.avatar_url,
                "members": [{"full_name": f"{m['first_name']} {m['last_name']}".strip(), "avatar_url": m["avatar_url"], "role": m["role"]} for m in members]
            }

        innovate_connect_alliance_team = user.innovate_connect_alliance_teams.first() if hasattr(user, 'innovate_connect_alliance_teams') else None
        innovate_connect_alliance_team_data = None
        if innovate_connect_alliance_team:
            members = innovate_connect_alliance_team.members.all().values("first_name", "last_name", "avatar_url", "role")
            innovate_connect_alliance_team_data = {
                "team_name": innovate_connect_alliance_team.team_name,
                "team_id": innovate_connect_alliance_team.team_id,
                "avatar_url": innovate_connect_alliance_team.avatar_url,
                "member_count": innovate_connect_alliance_team.members.count(),
                "is_valid_team_size": MEDHACK_TEAM_MIN_MEMBERS <= innovate_connect_alliance_team.members.count() <= MEDHACK_TEAM_MAX_MEMBERS,
                "members": [{"full_name": f"{m['first_name']} {m['last_name']}".strip(), "avatar_url": m["avatar_url"], "role": m["role"]} for m in members]
            }
        
        # Determine primary team for backward compatibility (prefer hospital)
        primary_team_data = hospital_team_data or innovate_connect_alliance_team_data or esafety_team_data

        data = {
            'first_name': user.first_name,
            'last_name': user.last_name,
            'full_name': user.full_name,
            'email': user.email,
            'phone': user.phone,
            'about': user.about,
            'role': user.role,
            'is_superuser': user.is_superuser,
            'has_team': user.has_team,
            'team': primary_team_data,  # Backward compatibility
            'hospital_team': hospital_team_data,
            'innovate_connect_alliance_team': innovate_connect_alliance_team_data,
            'esafety_team': esafety_team_data,
            'avatar_url': user.avatar_url,
            'personas': user.personas,
        }

        return Response(data, status=status.HTTP_200_OK)

class UpdateProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        user = request.user
        full_name = request.data.get("full_name")
        first_name = request.data.get("first_name")
        last_name = request.data.get("last_name")
        email = request.data.get("email")
        phone = request.data.get("phone")
        about = request.data.get("about")

        # Handle personas (list of strings)
        if hasattr(request.data, 'getlist'):
            personas = request.data.getlist("personas")
        else:
            personas = request.data.get("personas")

        logger.info(f"UpdateProfileView PATCH: user={user.email}, data_keys={list(request.data.keys())}, files_keys={list(request.FILES.keys())}")

        # Update the user's profile information.
        if first_name:
            user.first_name = first_name
        if last_name:
            user.last_name = last_name
            
        if full_name:
            user.full_name = full_name
        
        if phone is not None:
            user.phone = phone
            
        if about is not None:
            user.about = about

        if personas is not None:
            # Ensure it's a list and validate against known persona choices.
            if isinstance(personas, str):
                personas = [p.strip() for p in personas.split(',') if p.strip()]

            normalized_personas = []
            for persona in personas:
                normalized = str(persona).strip().lower()
                if normalized:
                    normalized_personas.append(normalized)

            invalid_personas = [p for p in normalized_personas if p not in ALLOWED_PERSONAS]
            if invalid_personas:
                return Response(
                    {
                        "error": f"Invalid personas: {invalid_personas}",
                        "allowed_personas": sorted(ALLOWED_PERSONAS),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # De-duplicate while preserving user-provided order.
            deduped_personas = list(dict.fromkeys(normalized_personas))
            if len(deduped_personas) > 4:
                return Response(
                    {"error": "A maximum of 4 personas may be selected."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            user.personas = deduped_personas

        # Only update the email if it's changed.
        if email and email != user.email:
            # TODO: Consider marking the email as unverified and sending a verification email.
            user.email = email

        user.save()

        # Handle avatar upload
        avatar_file = request.FILES.get('avatar')
        if avatar_file:
            try:
                from PIL import Image
                from io import BytesIO
                from .firebase_utils import upload_file_to_storage

                img = Image.open(avatar_file)
                img.thumbnail((300, 300))

                output_buffer = BytesIO()
                img_format = img.format if img.format else 'PNG'
                img.save(output_buffer, format=img_format)
                output_buffer.seek(0)

                filename = f"avatars/{user.id}_{int(timezone.now().timestamp())}.{img_format.lower()}"
                avatar_url = upload_file_to_storage(output_buffer, filename, content_type=f'image/{img_format.lower()}')

                if avatar_url:
                    user.avatar_url = avatar_url
                    user.save()

            except Exception as e:
                logger.error(f"Error uploading avatar: {e}")

        # Handle team avatar upload
        team_avatar_file = request.FILES.get('team_avatar')
        if team_avatar_file:
            app_context = _normalize_app_context(request.data.get('app'), default='')
            if app_context == 'hospital':
                team = user.hospital_teams.first()
            elif app_context == 'esafety':
                team = user.esafety_teams.first()
            elif app_context == 'innovate-connect-alliance':
                team = user.innovate_connect_alliance_teams.first()
            else:
                team = (
                    user.hospital_teams.first()
                    or (user.innovate_connect_alliance_teams.first() if hasattr(user, 'innovate_connect_alliance_teams') else None)
                    or (user.esafety_teams.first() if hasattr(user, 'esafety_teams') else None)
                )
            if team:
                try:
                    from PIL import Image
                    from io import BytesIO
                    from .firebase_utils import upload_file_to_storage

                    img = Image.open(team_avatar_file)
                    img.thumbnail((300, 300))

                    output_buffer = BytesIO()
                    img_format = img.format if img.format else 'PNG'
                    img.save(output_buffer, format=img_format)
                    output_buffer.seek(0)

                    filename = f"team-avatars/{team.id}_{int(timezone.now().timestamp())}.{img_format.lower()}"
                    team_avatar_url = upload_file_to_storage(output_buffer, filename, content_type=f'image/{img_format.lower()}')

                    if team_avatar_url:
                        team.avatar_url = team_avatar_url
                        team.save(update_fields=['avatar_url'])
                        logger.info(f"Team avatar uploaded for team {team.id}: {team_avatar_url}")

                except Exception as e:
                    logger.error(f"Error uploading team avatar: {e}")
            else:
                logger.warning(f"User {user.email} uploaded team_avatar but has no team")

        # Return the updated profile — team data is read-only here,
        # managed via /api/v1/hackathons/{app}/teams/ endpoints.
        hospital_team = user.hospital_teams.first()
        hospital_team_data = None
        if hospital_team:
            members = hospital_team.members.all().values("first_name", "last_name", "avatar_url", "role")
            hospital_team_data = {
                "team_name": hospital_team.team_name,
                "team_id": hospital_team.team_id,
                "avatar_url": hospital_team.avatar_url,
                "member_count": hospital_team.members.count(),
                "is_valid_team_size": MEDHACK_TEAM_MIN_MEMBERS <= hospital_team.members.count() <= MEDHACK_TEAM_MAX_MEMBERS,
                "members": [{"full_name": f"{m['first_name']} {m['last_name']}".strip(), "avatar_url": m["avatar_url"], "role": m["role"]} for m in members]
            }

        esafety_team = user.esafety_teams.first()
        esafety_team_data = None
        if esafety_team:
            members = esafety_team.members.all().values("first_name", "last_name", "avatar_url", "role")
            esafety_team_data = {
                "team_name": esafety_team.team_name,
                "team_id": esafety_team.team_id,
                "avatar_url": esafety_team.avatar_url,
                "members": [{"full_name": f"{m['first_name']} {m['last_name']}".strip(), "avatar_url": m["avatar_url"], "role": m["role"]} for m in members]
            }

        innovate_connect_alliance_team = user.innovate_connect_alliance_teams.first()
        innovate_connect_alliance_team_data = None
        if innovate_connect_alliance_team:
            members = innovate_connect_alliance_team.members.all().values("first_name", "last_name", "avatar_url", "role")
            innovate_connect_alliance_team_data = {
                "team_name": innovate_connect_alliance_team.team_name,
                "team_id": innovate_connect_alliance_team.team_id,
                "avatar_url": innovate_connect_alliance_team.avatar_url,
                "member_count": innovate_connect_alliance_team.members.count(),
                "is_valid_team_size": MEDHACK_TEAM_MIN_MEMBERS <= innovate_connect_alliance_team.members.count() <= MEDHACK_TEAM_MAX_MEMBERS,
                "members": [{"full_name": f"{m['first_name']} {m['last_name']}".strip(), "avatar_url": m["avatar_url"], "role": m["role"]} for m in members]
            }

        primary_team_data = hospital_team_data or innovate_connect_alliance_team_data or esafety_team_data

        data = {
            'full_name': user.full_name,
            'email': user.email,
            'phone': user.phone,
            'about': user.about,
            'role': user.role,
            'is_superuser': user.is_superuser,
            'team': primary_team_data,
            'hospital_team': hospital_team_data,
            'innovate_connect_alliance_team': innovate_connect_alliance_team_data,
            'esafety_team': esafety_team_data,
            'has_team': user.has_team,
            'avatar_url': user.avatar_url,
            'personas': user.personas,
        }

        return Response(data, status=status.HTTP_200_OK)

@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout_view(request):
    try:
        # Create response object
        response = Response({'message': 'Logged out successfully'}, status=status.HTTP_200_OK)
        
        from django.conf import settings
        is_production = not settings.DEBUG
        cookie_domain = None if not is_production else '.mlai.au'
        samesite = 'None' if is_production else 'Lax'

        # Delete authentication cookies
        # We must provide the same domain and samesite as when they were set
        response.delete_cookie('access_token', path='/', domain=cookie_domain, samesite=samesite)
        response.delete_cookie('refresh_token', path='/', domain=cookie_domain, samesite=samesite)
        
        # Delete any session-related cookies
        response.delete_cookie('sessionid', path='/')
        response.delete_cookie('csrftoken', path='/')
        
        return response
    except Exception as e:
        return Response({'error': 'Logout failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class HackathonListView(ListAPIView):
    queryset = Hackathon.objects.all()
    serializer_class = HackathonSerializer
    permission_classes = [AllowAny]

class HackathonDetailView(RetrieveAPIView):
    queryset = Hackathon.objects.all()
    serializer_class = HackathonSerializer
    permission_classes = [AllowAny]
    lookup_field = 'slug'


class UserDetailView(RetrieveUpdateAPIView):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated, IsOwnerOrTeammateOrSuperuser]


class ContentFactoryOrgConfigView(APIView):
    """
    GET/PUT org config for Content Factory service.
    Used by external Content Factory service to read/write organization templates.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def _normalize_domain(self, domain: str) -> str:
        """
        Strip www., https://, http://, and trailing paths from domain.
        Examples:
            https://www.mlai.au/about → mlai.au
            http://mlai.au → mlai.au
            www.mlai.au → mlai.au
        """
        if not domain:
            return domain
        
        # Remove protocol
        domain = domain.lower().strip()
        if domain.startswith('https://'):
            domain = domain[8:]
        elif domain.startswith('http://'):
            domain = domain[7:]
        
        # Remove www.
        if domain.startswith('www.'):
            domain = domain[4:]
        
        # Remove trailing path (everything after first /)
        if '/' in domain:
            domain = domain.split('/')[0]
        
        return domain

    def get(self, request):
        """
        Lookup org config by domain, github_repo, or slack_user_id query param.
        Returns 404 if organization not found.
        """
        domain = request.query_params.get('domain')
        github_repo = request.query_params.get('github_repo')
        slack_user_id = request.query_params.get('slack_user_id')
        
        org = None

        # 0. Try lookup via explicit owned-domain mapping when only slack_user_id is provided
        if slack_user_id and not github_repo and not domain:
            try:
                owned_configs = list(get_owned_org_configs(slack_user_id))
                if len(owned_configs) == 1:
                    org = owned_configs[0].organization
                elif len(owned_configs) > 1:
                    return Response(
                        {
                            'error': 'Multiple domains found for this Slack user. Please provide a domain.',
                            'requires_domain_selection': True,
                            'connected_domains': [
                                {
                                    'domain': cfg.organization.domain,
                                    'github_repo': cfg.github_repo,
                                }
                                for cfg in owned_configs
                                if cfg.organization
                            ],
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            except Exception as e:
                logger.warning(f"Error looking up owned domains for slack_user_id {slack_user_id}: {e}")

        # 1. Try lookup by github_repo if provided
        if github_repo and not org:
            try:
                # Find the config that matches this repo
                config_qs = OrganizationContentConfig.objects.filter(github_repo=github_repo)
                if slack_user_id:
                    config_qs = config_qs.filter(
                        Q(connected_slack_user_id=slack_user_id)
                        | Q(connected_slack_user_id__isnull=True)
                    )
                config = config_qs.first()
                if config:
                    org = config.organization
            except Exception as e:
                logger.warning(f"Error looking up org by repo {github_repo}: {e}")

        # 2. Try lookup by domain if no org found yet
        if not org and domain:
            normalized_domain = self._normalize_domain(domain)
            try:
                org = Organization.objects.get(domain=normalized_domain)
            except Organization.DoesNotExist:
                pass

        if not org:
            return Response(
                {'error': 'Organization not found. Please provide valid domain or github_repo.'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get config if exists (might have already fetched it, but get fresh ref)
        config = getattr(org, 'content_config', None)
        
        response_data = {
            'org_id': org.id,
            'org_name': org.name,
            'domain': org.domain,
            'competitors': org.competitors,
            'seed_keywords': org.seed_keywords,
            'connected_slack_user_id': config.connected_slack_user_id if config else None,
            'default_timezone': config.default_timezone if config else "",
            'daily_discovery_enabled': config.daily_discovery_enabled if config else False,
            'daily_discovery_priority': config.daily_discovery_priority if config else 0,
            'article_template': config.article_template if config else None,
            'design_guide': config.design_guide if config else None,
            'resource_prompt': config.resource_prompt if config else None,
            'company_context': config.company_context if config else None,
            'github_repo': config.github_repo if config else None,
            'article_delivery_mode': config.article_delivery_mode if config else None,
            'brand_name': config.brand_name if config else None,
            'scan_summary': config.scan_summary if config else None,
            'tech_stack': config.tech_stack if config else {},
            'installed_packages': config.installed_packages if config else {},
            'pillar_strategy': config.pillar_strategy if config else {},
            'build_healing_hints': config.build_healing_hints if config else [],
            'repo_execution_contract': config.repo_execution_contract if config else {},
            'article_path_pattern': config.article_path_pattern if config else None,
            'registry_path': config.registry_path if config else None,
            'publish_targets': config.publish_targets if config else [],
            'default_publish_target_id': config.default_publish_target_id if config else None,
            'article_system': resolve_article_system(config),
        }

        return Response(response_data, status=status.HTTP_200_OK)

    def put(self, request):
        """
        Create org if not exists, then upsert config.
        Supports partial updates (only fields present in request are updated).
        Also handles component generation data:
        - generated_components: array of component objects to upsert
        - component_generation: summary of generation pipeline result
        - component_mapping: dict of component name -> match result
        """
        data = request.data
        domain = data.get('domain')
        name = data.get('name')
        
        if not domain:
            return Response(
                {'error': 'domain is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        normalized_domain = self._normalize_domain(domain)
        
        # Get or create organization
        org, org_created = Organization.objects.get_or_create(
            domain=normalized_domain,
            defaults={'name': name or normalized_domain}
        )
        
        # Update org fields if provided
        org_updated = False
        competitors = data.get('competitors')
        seed_keywords = data.get('seed_keywords')

        if not org_created and name and org.name != name:
            org.name = name
            org_updated = True

        if competitors is not None:
            org.competitors = competitors
            org_updated = True

        if seed_keywords is not None:
            org.seed_keywords = seed_keywords
            org_updated = True

        if org_updated:
            org.save()

        existing_config = getattr(org, 'content_config', None)
        current_enabled = bool(getattr(existing_config, 'daily_discovery_enabled', False))
        current_priority = int(getattr(existing_config, 'daily_discovery_priority', 0) or 0)
        current_owner = str(getattr(existing_config, 'connected_slack_user_id', '') or '').strip()
        current_github_repo = str(getattr(existing_config, 'github_repo', '') or '').strip()

        if 'daily_discovery_priority' in data:
            try:
                resulting_priority = int(data.get('daily_discovery_priority'))
            except (TypeError, ValueError):
                return Response(
                    {'error': 'daily_discovery_priority must be an integer'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if resulting_priority < 0:
                return Response(
                    {'error': 'daily_discovery_priority must be 0 or greater'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            resulting_priority = current_priority

        resulting_enabled = bool(data.get('daily_discovery_enabled')) if 'daily_discovery_enabled' in data else current_enabled
        raw_owner = data.get('connected_slack_user_id') if 'connected_slack_user_id' in data else current_owner
        resulting_owner = str(raw_owner or '').strip()
        resulting_github_repo = (
            str(data.get('github_repo') or '').strip()
            if 'github_repo' in data
            else current_github_repo
        )

        from integrations.services.daily_discovery import (
            count_enabled_daily_discovery_configs,
            get_daily_discovery_max_targets,
            infer_daily_discovery_owner,
        )

        inferred_owner = infer_daily_discovery_owner(
            domain=normalized_domain,
            connected_slack_user_id=resulting_owner,
            github_repo=resulting_github_repo,
            config=existing_config,
        )

        if resulting_enabled and not inferred_owner:
            return Response(
                {'error': 'connected_slack_user_id is required when daily_discovery_enabled is true'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if resulting_enabled and not current_enabled:
            enabled_count = count_enabled_daily_discovery_configs(
                exclude_config_id=getattr(existing_config, 'id', None),
            )
            max_targets = get_daily_discovery_max_targets()
            if enabled_count >= max_targets:
                return Response(
                    {'error': f'No more than {max_targets} organizations may have daily_discovery_enabled=true'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Prepare defaults dynamically to allow partial updates
        # Only include fields that are present in the request data
        defaults = {}
        target_fields = [
            'connected_slack_user_id',
            'default_timezone',
            'daily_discovery_enabled',
            'daily_discovery_priority',
            'article_template',
            'design_guide',
            'resource_prompt',
            'company_context',
            'github_repo',
            'article_delivery_mode',
            'brand_name',
            'scan_summary',
            'tech_stack',
            'installed_packages',
            'pillar_strategy',
            'build_healing_hints',
            'repo_execution_contract',
            'article_path_pattern',
            'registry_path',
            'publish_targets',
            'default_publish_target_id',
        ]
        
        for field in target_fields:
            if field in data:
                defaults[field] = data[field]

        if 'connected_slack_user_id' in defaults:
            defaults['connected_slack_user_id'] = resulting_owner or None
        if resulting_enabled and inferred_owner:
            defaults['connected_slack_user_id'] = inferred_owner
        if 'daily_discovery_priority' in data:
            defaults['daily_discovery_priority'] = resulting_priority

        if 'article_system' in data:
            current_article_system = resolve_article_system(getattr(org, 'content_config', None))
            defaults['article_system'] = merge_article_system(current_article_system, data.get('article_system'))

        # Upsert config
        config, config_created = OrganizationContentConfig.objects.update_or_create(
            organization=org,
            defaults=defaults
        )
        
        # Handle generated_components array
        generated_components_data = data.get('generated_components', [])
        components_created = 0
        components_updated = 0
        
        for comp_data in generated_components_data:
            comp_name = comp_data.get('name')
            if not comp_name:
                continue
            
            comp_defaults = {
                'content': comp_data.get('content', ''),
                'source': comp_data.get('source', 'generated'),
                'original_path': comp_data.get('original_path'),
                'similarity_score': comp_data.get('similarity_score', 0.0),
                'matched_component': comp_data.get('matched_component'),
                'adaptation_notes': comp_data.get('adaptation_notes', ''),
            }
            
            _, created = GeneratedComponent.objects.update_or_create(
                organization=org,
                name=comp_name,
                defaults=comp_defaults
            )
            
            if created:
                components_created += 1
            else:
                components_updated += 1
        
        # Handle component_generation summary and component_mapping
        component_generation = data.get('component_generation', {})
        component_mapping_data = data.get('component_mapping', {})
        
        if component_generation or component_mapping_data:
            mapping_defaults = {
                'mapping_data': component_mapping_data,
            }
            
            # Extract stats from component_generation
            if component_generation:
                mapping_defaults['generation_status'] = component_generation.get('status')
                mapping_defaults['design_guide_path'] = component_generation.get('design_guide_path')
                mapping_defaults['failed_components'] = component_generation.get('failed_components', [])
                
                # Calculate totals from component_generation
                generated = component_generation.get('components_generated', 0)
                adapted = component_generation.get('components_adapted', 0)
                mapping_defaults['generated_count'] = generated
                mapping_defaults['matched_count'] = adapted
                mapping_defaults['total_components'] = generated + adapted
                
                # Storage info
                storage = component_generation.get('storage', {})
                if storage:
                    mapping_defaults['storage_local_path'] = storage.get('local_path')
                    mapping_defaults['storage_pr_url'] = storage.get('pr_url')
                    mapping_defaults['storage_branch_url'] = storage.get('branch_url')
            
            ComponentMapping.objects.update_or_create(
                organization=org,
                defaults=mapping_defaults
            )
        
        status_text = 'created' if org_created else 'updated'
        
        response_data = {
            'status': status_text,
            'org_id': org.id,
            'org_name': org.name,
            'domain': org.domain,
        }
        
        # Include component stats if components were processed
        if generated_components_data:
            response_data['components'] = {
                'created': components_created,
                'updated': components_updated,
                'total': len(generated_components_data)
            }
        
        return Response(response_data, status=status.HTTP_201_CREATED if org_created else status.HTTP_200_OK)


class ContentFactoryHealingRecordView(APIView):
    """
    GET/POST reusable healing records for Content Factory publish-time verification.
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    @staticmethod
    def _normalize_domain(domain: str) -> str:
        if not domain:
            return domain
        domain = domain.lower().strip()
        if domain.startswith('https://'):
            domain = domain[8:]
        elif domain.startswith('http://'):
            domain = domain[7:]
        if domain.startswith('www.'):
            domain = domain[4:]
        if '/' in domain:
            domain = domain.split('/')[0]
        return domain

    def get(self, request):
        records = ContentFactoryHealingRecord.objects.all()
        domain = self._normalize_domain(request.query_params.get("domain") or "")
        github_repo = str(request.query_params.get("github_repo") or "").strip()
        failure_kind = str(request.query_params.get("failure_kind") or "").strip()
        failure_family_key = str(request.query_params.get("failure_family_key") or "").strip()
        promotion_state = str(request.query_params.get("promotion_state") or "").strip()
        limit_raw = str(request.query_params.get("limit") or "").strip()

        if domain:
            records = records.filter(domain=domain)
        if github_repo:
            records = records.filter(github_repo=github_repo)
        if failure_kind:
            records = records.filter(failure_kind=failure_kind)
        if failure_family_key:
            records = records.filter(failure_family_key=failure_family_key)
        if promotion_state:
            records = records.filter(promotion_state=promotion_state)

        limit = 50
        if limit_raw:
            try:
                limit = max(1, min(int(limit_raw), 200))
            except ValueError:
                limit = 50

        serializer = ContentFactoryHealingRecordSerializer(records.order_by("-updated_at")[:limit], many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request):
        payload = dict(request.data or {})
        if "domain" in payload:
            payload["domain"] = self._normalize_domain(payload.get("domain"))

        serializer = ContentFactoryHealingRecordSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        domain = data["domain"]
        github_repo = data.get("github_repo") or ""
        failure_kind = data["failure_kind"]
        failure_family_key = data["failure_family_key"]
        organization = Organization.objects.filter(domain=domain).first()

        defaults = {
            "organization": organization,
            "exact_signature": data.get("exact_signature") or "",
            "summary": data.get("summary") or "",
            "normalized_failure": data.get("normalized_failure") or {},
            "changed_files": data.get("changed_files") or [],
            "patch_manifest": data.get("patch_manifest") or {},
            "validation_results": data.get("validation_results") or {},
            "evidence_artifacts": data.get("evidence_artifacts") or {},
            "snippet_or_rule": data.get("snippet_or_rule") or "",
            "applies_to": data.get("applies_to") or [],
            "promoted_payload": data.get("promoted_payload") or {},
            "promotion_state": data.get("promotion_state") or "candidate",
            "latest_run_id": data.get("latest_run_id") or "",
        }

        record, created = ContentFactoryHealingRecord.objects.update_or_create(
            domain=domain,
            github_repo=github_repo,
            failure_kind=failure_kind,
            failure_family_key=failure_family_key,
            defaults=defaults,
        )
        response_payload = ContentFactoryHealingRecordSerializer(record).data
        response_payload["sync_status"] = "created" if created else "updated"
        return Response(
            response_payload,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ContentFactoryComponentsView(APIView):
    """
    GET components for an organization.
    Path: GET /api/content-factory/org/components?domain=mlai.au
    Optional filters: name (partial match), source (generated/adapted)
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def _normalize_domain(self, domain: str) -> str:
        """Same domain normalization as ContentFactoryOrgConfigView."""
        if not domain:
            return domain
        domain = domain.lower().strip()
        if domain.startswith('https://'):
            domain = domain[8:]
        elif domain.startswith('http://'):
            domain = domain[7:]
        if domain.startswith('www.'):
            domain = domain[4:]
        if '/' in domain:
            domain = domain.split('/')[0]
        return domain

    def get(self, request):
        domain = request.query_params.get('domain')
        name_filter = request.query_params.get('name')
        source_filter = request.query_params.get('source')
        
        if not domain:
            return Response(
                {'error': 'domain query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        normalized_domain = self._normalize_domain(domain)
        
        try:
            org = Organization.objects.get(domain=normalized_domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found', 'domain': domain},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Build component queryset with filters
        components = GeneratedComponent.objects.filter(organization=org)
        
        if name_filter:
            components = components.filter(name__icontains=name_filter)
        
        if source_filter:
            components = components.filter(source=source_filter)
        
        # Get mapping stats if exists
        mapping_stats = {
            'matched_count': 0,
            'generated_count': 0,
            'total': 0
        }
        
        try:
            mapping = org.component_mapping
            mapping_stats = {
                'matched_count': mapping.matched_count,
                'generated_count': mapping.generated_count,
                'total': mapping.total_components
            }
        except ComponentMapping.DoesNotExist:
            pass
        
        # Get last updated timestamp from most recently updated component
        last_updated = None
        latest_component = components.order_by('-updated_at').first()
        if latest_component:
            last_updated = latest_component.updated_at.isoformat()
        
        # Serialize components (lightweight, without content)
        serializer = GeneratedComponentListSerializer(components, many=True)
        
        return Response({
            'domain': org.domain,
            'org_id': org.id,
            'component_count': components.count(),
            'last_updated': last_updated,
            'components': serializer.data,
            'mapping': mapping_stats
        }, status=status.HTTP_200_OK)


class ContentFactoryComponentDetailView(APIView):
    """
    GET a single component by name for an organization.
    Path: GET /api/content-factory/org/components/<name>?domain=mlai.au
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def _normalize_domain(self, domain: str) -> str:
        """Same domain normalization as ContentFactoryOrgConfigView."""
        if not domain:
            return domain
        domain = domain.lower().strip()
        if domain.startswith('https://'):
            domain = domain[8:]
        elif domain.startswith('http://'):
            domain = domain[7:]
        if domain.startswith('www.'):
            domain = domain[4:]
        if '/' in domain:
            domain = domain.split('/')[0]
        return domain

    def get(self, request, name):
        domain = request.query_params.get('domain')
        
        if not domain:
            return Response(
                {'error': 'domain query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        normalized_domain = self._normalize_domain(domain)
        
        try:
            org = Organization.objects.get(domain=normalized_domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found', 'domain': domain},
                status=status.HTTP_404_NOT_FOUND
            )
        
        try:
            component = GeneratedComponent.objects.get(organization=org, name=name)
        except GeneratedComponent.DoesNotExist:
            return Response(
                {'error': 'Component not found', 'name': name, 'domain': domain},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Serialize with full content
        serializer = GeneratedComponentSerializer(component)
        
        return Response(serializer.data, status=status.HTTP_200_OK)



class LinkSlackView(APIView):
    """
    Link a Slack ID to an existing user found by email.
    Path: POST /api/v1/users/link-slack/
    """
    permission_classes = [HasRooApiKey]

    def post(self, request):
        slack_id = request.data.get('slack_id')
        email = request.data.get('email')

        if not slack_id or not email:
            return Response({"error": "slack_id and email are required"}, status=status.HTTP_400_BAD_REQUEST)

        # Case-insensitive lookup
        try:
            user = User.objects.get(email__iexact=email)
            user.slack_id = slack_id
            user.save()
            return Response({"user_id": user.id, "linked": True}, status=status.HTTP_200_OK)
        except User.DoesNotExist:
            return Response({"error": "User not found by email"}, status=status.HTTP_404_NOT_FOUND)


class GetOrCreateSlackUserView(APIView):
    """
    Get or create a user from Slack data (for Roo bot interactions).

    POST /api/v1/users/slack-user/

    Creates a user if they don't exist, or returns existing user by slack_id or email.
    This allows Roo to interact with users who haven't formally signed up yet.

    Request body:
        {
            "slack_id": "U12345678",
            "email": "user@example.com",
            "first_name": "John",  // optional
            "last_name": "Doe",    // optional
            "avatar_url": "https://..."  // optional
        }

    Returns:
        {
            "user_id": 123,
            "email": "user@example.com",
            "slack_id": "U12345678",
            "created": true/false
        }
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        slack_id = request.data.get('slack_id')
        email = request.data.get('email')
        first_name = request.data.get('first_name', '')
        last_name = request.data.get('last_name', '')
        avatar_url = request.data.get('avatar_url')

        if not slack_id or not email:
            return Response(
                {"error": "slack_id and email are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            from .slack_users import ensure_slack_user

            result = ensure_slack_user(
                slack_id=slack_id,
                email=email,
                first_name=first_name,
                last_name=last_name,
                avatar_url=avatar_url,
            )
            user = result.user

            if result.linked:
                logger.info(f"Linked Slack ID {slack_id} to existing user {email}")
            elif result.created:
                logger.info(f"Auto-created user from Slack: {email} (Slack ID: {slack_id})")

            return Response({
                "user_id": user.id,
                "email": user.email,
                "slack_id": user.slack_id,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "created": result.created,
                "linked": result.linked,
            }, status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK)

        except Exception as e:
            logger.exception(f"Error creating user from Slack data: {str(e)}")
            return Response(
                {"error": "Failed to create user"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class ContentFactoryTokenView(APIView):
    """
    On-demand token refresh endpoint for content-factory.

    GET /api/content-factory/token?domain=mlai.au
    GET /api/content-factory/token?slack_user_id=U12345

    Content-factory can call this endpoint mid-job to get a fresh GitHub token
    without needing to restart the entire pipeline.

    Supports both:
    - domain: Fetches org-level token (preferred)
    - slack_user_id: Fetches user-level token (legacy fallback)

    Returns:
        {
            "github_token": "ghu_xxxx...",
            "github_repo": "owner/repo",
            "expires_at": "2024-01-16T12:00:00Z" (optional),
            "source": "org" | "user"
        }
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def _normalize_domain(self, domain: str) -> str:
        if not domain:
            return domain
        domain = domain.lower().strip()
        if domain.startswith('https://'):
            domain = domain[8:]
        elif domain.startswith('http://'):
            domain = domain[7:]
        if domain.startswith('www.'):
            domain = domain[4:]
        if '/' in domain:
            domain = domain.split('/')[0]
        return domain

    def get(self, request):
        from integrations.services.github import ensure_valid_token, TokenRefreshError
        from integrations.services.article_generation import ensure_valid_org_token, ArticleGenerationError
        from integrations.models import UserIntegration

        domain = request.query_params.get('domain')
        slack_user_id = request.query_params.get('slack_user_id')

        if not domain and not slack_user_id:
            return Response(
                {'error': 'Either domain or slack_user_id query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Try domain-based lookup first (org-level)
        if domain:
            normalized_domain = self._normalize_domain(domain)
            try:
                fresh_token = ensure_valid_org_token(normalized_domain)

                # Fetch config for additional context
                org = Organization.objects.get(domain=normalized_domain)
                config = org.content_config

                response_data = {
                    'github_token': fresh_token,
                    'github_repo': config.github_repo,
                    'domain': normalized_domain,
                    'source': 'org',
                }

                if config.github_token_expires_at:
                    response_data['expires_at'] = config.github_token_expires_at.isoformat()

                logger.info(f"Provided fresh org-level GitHub token for {normalized_domain}")
                return Response(response_data, status=status.HTTP_200_OK)

            except Organization.DoesNotExist:
                if not slack_user_id:
                    return Response(
                        {'error': f'Organization not found: {domain}'},
                        status=status.HTTP_404_NOT_FOUND
                    )
                # Fall through to user-level lookup
            except (ArticleGenerationError, TokenRefreshError) as e:
                if not slack_user_id:
                    logger.warning(f"Token refresh failed for org {domain}: {e}")
                    return Response(
                        {
                            'error': 'Token refresh failed',
                            'message': str(e),
                            'action_required': 'auth_required'
                        },
                        status=status.HTTP_401_UNAUTHORIZED
                    )
                # Fall through to user-level lookup

        # User-level lookup (legacy fallback)
        if slack_user_id:
            try:
                fresh_token = ensure_valid_token(slack_user_id)

                integration = UserIntegration.objects.get(slack_user_id=slack_user_id)

                response_data = {
                    'github_token': fresh_token,
                    'github_repo': integration.github_repo,
                    'slack_user_id': slack_user_id,
                    'source': 'user',
                }

                if integration.github_token_expires_at:
                    response_data['expires_at'] = integration.github_token_expires_at.isoformat()

                logger.info(f"Provided fresh user-level GitHub token for {slack_user_id}")
                return Response(response_data, status=status.HTTP_200_OK)

            except UserIntegration.DoesNotExist:
                return Response(
                    {'error': 'No integration found for this user'},
                    status=status.HTTP_404_NOT_FOUND
                )
            except TokenRefreshError as e:
                logger.warning(f"Token refresh failed for {slack_user_id}: {e}")
                return Response(
                    {
                        'error': 'Token refresh failed',
                        'message': str(e),
                        'action_required': 'auth_required'
                    },
                    status=status.HTTP_401_UNAUTHORIZED
                )

        return Response(
            {'error': 'No valid credentials found'},
            status=status.HTTP_404_NOT_FOUND
        )


class ContentFactoryGitHubStatusView(APIView):
    """
    Check GitHub connection status for an organization/domain.

    GET /api/content-factory/org/github-status?domain=mlai.au

    Returns:
        {
            "connected": true/false,
            "github_repo": "owner/repo",
            "github_user_name": "username",
            "token_valid": true/false,
            "expires_at": "2024-01-16T12:00:00Z" (optional)
        }
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        domain = request.query_params.get('domain')
        slack_user_id = str(request.query_params.get('slack_user_id') or '').strip()

        if not domain:
            return Response(
                {'error': 'domain query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        normalized_domain = _normalize_content_factory_domain(domain)

        try:
            org = Organization.objects.get(domain=normalized_domain)
        except Organization.DoesNotExist:
            return Response({
                'connected': False,
                'domain': normalized_domain,
                'message': 'Organization not found. Please set up the organization first.'
            }, status=status.HTTP_200_OK)

        from integrations.services.article_generation import resolve_content_factory_connection_for_domain

        connection_details = resolve_content_factory_connection_for_domain(
            normalized_domain,
            slack_user_id or None,
        )
        config = connection_details.get('config') or getattr(org, 'content_config', None)

        if not config and not connection_details.get('github_repo'):
            return Response({
                'connected': False,
                'domain': normalized_domain,
                'github_repo': None,
                'message': 'No GitHub token configured for this organization.'
            }, status=status.HTTP_200_OK)

        token_valid = not bool(connection_details.get('needs_github_auth'))

        response_data = {
            'connected': token_valid,
            'domain': normalized_domain,
            'github_repo': connection_details.get('github_repo') or getattr(config, 'github_repo', None),
            'github_user_name': getattr(config, 'github_user_name', None),
            'token_valid': token_valid,
            'connection_state': connection_details.get('connection_state'),
            'credential_source': connection_details.get('credential_source') or 'none',
        }

        if config.github_token_expires_at:
            response_data['expires_at'] = config.github_token_expires_at.isoformat()

        return Response(response_data, status=status.HTTP_200_OK)


class ContentFactoryGitHubReconnectView(APIView):
    """
    Start or confirm the Content Factory GitHub reconnect flow for a domain.
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        domain = request.data.get('domain')
        slack_user_id = str(request.data.get('slack_user_id') or '').strip()
        github_repo = str(request.data.get('github_repo') or '').strip() or None
        trigger = str(request.data.get('trigger') or 'manual').strip() or 'manual'
        pending_action = request.data.get('pending_action')

        normalized_domain = _normalize_content_factory_domain(domain)
        if not normalized_domain and not slack_user_id:
            return Response(
                {'error': 'domain or slack_user_id is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from integrations.services.article_generation import resolve_content_factory_connection_for_domain

        connection_details = (
            resolve_content_factory_connection_for_domain(
                normalized_domain,
                slack_user_id or None,
            )
            if normalized_domain
            else {
                'github_repo': github_repo,
                'connection_state': 'auth_required',
                'credential_source': 'none',
            }
        )

        resolved_repo = github_repo or (
            str(connection_details.get('github_repo') or '').strip() or None
        )
        connection_state = connection_details.get('connection_state') or 'auth_required'
        auth_url = _content_factory_github_auth_url(
            slack_user_id=slack_user_id,
            domain=normalized_domain or None,
        )

        response_payload = {
            'domain': normalized_domain or None,
            'github_repo': resolved_repo,
            'connection_state': connection_state,
            'credential_source': connection_details.get('credential_source') or 'none',
            'trigger': trigger,
            'pending_action': pending_action,
        }

        if connection_state == 'connected':
            response_payload.update(
                {
                    'status': 'already_connected',
                    'message': f"GitHub is already connected for {normalized_domain}.",
                }
            )
            return Response(response_payload, status=status.HTTP_200_OK)

        if connection_state == 'repo_selection_required':
            message = (
                f"GitHub is connected for {normalized_domain}, but Roo still needs a repository selected."
            )
        elif normalized_domain:
            message = f"GitHub needs to be connected for {normalized_domain} before Roo can continue."
        else:
            message = "GitHub needs to be connected before Roo can continue."

        response_payload.update(
            {
                'status': 'auth_started',
                'auth_url': auth_url,
                'message': message,
            }
        )
        return Response(response_payload, status=status.HTTP_200_OK)


class ScheduledDiscoveryReplayView(APIView):
    """
    Force a scheduled discovery enqueue for a specific user/domain/date.
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        from integrations.services.daily_discovery import enqueue_scheduled_discovery

        domain = request.data.get("domain")
        slack_user_id = request.data.get("slack_user_id")
        local_date_raw = request.data.get("local_date")
        force = bool(request.data.get("force"))

        if not domain or not slack_user_id:
            return Response(
                {"error": "domain and slack_user_id are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        local_date = None
        if local_date_raw:
            try:
                local_date = calendar_date.fromisoformat(str(local_date_raw))
            except ValueError:
                return Response(
                    {"error": "local_date must use YYYY-MM-DD format"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        result = enqueue_scheduled_discovery(
            slack_user_id=slack_user_id,
            domain=domain,
            local_date=local_date,
            force=force,
        )
        result_status = str(result.get("status") or "").strip().lower()
        http_status = status.HTTP_202_ACCEPTED if result_status == "queued" else status.HTTP_200_OK
        return Response(result, status=http_status)


class ContentFactoryOAuthInitiateView(APIView):
    """
    Initiate GitHub OAuth flow for a specific domain.

    POST /api/content-factory/oauth/initiate
    {
        "domain": "mlai.au",
        "slack_user_id": "U12345" (optional, for callback routing)
    }

    Returns:
        {
            "oauth_url": "https://github.com/apps/mlai-tools/installations/new?state=..."
        }
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def _normalize_domain(self, domain: str) -> str:
        if not domain:
            return domain
        domain = domain.lower().strip()
        if domain.startswith('https://'):
            domain = domain[8:]
        elif domain.startswith('http://'):
            domain = domain[7:]
        if domain.startswith('www.'):
            domain = domain[4:]
        if '/' in domain:
            domain = domain.split('/')[0]
        return domain

    def post(self, request):
        import secrets
        import urllib.parse
        from django.conf import settings

        domain = request.data.get('domain')
        slack_user_id = request.data.get('slack_user_id', '')

        if not domain:
            return Response(
                {'error': 'domain is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        normalized_domain = self._normalize_domain(domain)

        # Ensure organization exists (create if needed)
        org, _ = Organization.objects.get_or_create(
            domain=normalized_domain,
            defaults={'name': normalized_domain}
        )

        # Build state: domain::random_token::slack_user_id::type
        # The 'org' type distinguishes this from user-level OAuth
        rand_token = secrets.token_urlsafe(16)
        state = f"{normalized_domain}::{rand_token}::{slack_user_id}::org"

        # Store state in cache for validation (optional but recommended)
        from django.core.cache import cache
        cache.set(f"github_oauth_state:{rand_token}", state, timeout=600)  # 10 min expiry

        # GitHub App installation URL
        app_slug = "mlai-tools"
        install_url = f"https://github.com/apps/{app_slug}/installations/new"

        params = {"state": state}
        oauth_url = install_url + "?" + urllib.parse.urlencode(params)

        return Response({
            'oauth_url': oauth_url,
            'domain': normalized_domain,
            'state': state,
        }, status=status.HTTP_200_OK)


class ContentFactoryConnectGitHubView(APIView):
    """
    Save GitHub credentials for an organization after OAuth completion.

    POST /api/content-factory/org/connect-github
    {
        "domain": "mlai.au",
        "github_token": "ghu_xxx...",
        "github_refresh_token": "ghr_xxx...",
        "github_token_expires_at": "2024-01-16T12:00:00Z",
        "github_user_name": "username",
        "github_repo": "owner/repo",
        "github_installation_id": "12345"
    }

    Called by the OAuth callback to store org-level GitHub credentials.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def _normalize_domain(self, domain: str) -> str:
        if not domain:
            return domain
        domain = domain.lower().strip()
        if domain.startswith('https://'):
            domain = domain[8:]
        elif domain.startswith('http://'):
            domain = domain[7:]
        if domain.startswith('www.'):
            domain = domain[4:]
        if '/' in domain:
            domain = domain.split('/')[0]
        return domain

    def post(self, request):
        from django.utils.dateparse import parse_datetime

        data = request.data
        domain = data.get('domain')
        github_token = data.get('github_token')
        slack_user_id = data.get('slack_user_id')

        if not domain:
            return Response(
                {'error': 'domain is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not github_token:
            return Response(
                {'error': 'github_token is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        normalized_domain = self._normalize_domain(domain)

        # Get or create organization
        org, org_created = Organization.objects.get_or_create(
            domain=normalized_domain,
            defaults={'name': normalized_domain}
        )

        # Get or create config
        config, config_created = OrganizationContentConfig.objects.get_or_create(
            organization=org
        )

        # Update GitHub credentials
        config.github_token_encrypted = github_token

        if 'github_refresh_token' in data:
            config.github_refresh_token_encrypted = data['github_refresh_token']

        if 'github_token_expires_at' in data:
            expires_at = data['github_token_expires_at']
            if isinstance(expires_at, str):
                parsed_expires_at = parse_datetime(expires_at)
                if parsed_expires_at is not None:
                    config.github_token_expires_at = parsed_expires_at
            else:
                config.github_token_expires_at = expires_at

        if 'github_user_name' in data:
            config.github_user_name = data['github_user_name']

        if 'github_repo' in data:
            config.github_repo = data['github_repo']

        if 'github_installation_id' in data:
            config.github_installation_id = data['github_installation_id']

        if 'github_scopes' in data:
            config.github_scopes = data['github_scopes']

        if slack_user_id:
            config.connected_slack_user_id = slack_user_id

        config.save()

        logger.info(f"Connected GitHub for organization {normalized_domain}: repo={config.github_repo}, user={config.github_user_name}")

        return Response({
            'status': 'connected',
            'domain': normalized_domain,
            'github_repo': config.github_repo,
            'github_user_name': config.github_user_name,
        }, status=status.HTTP_200_OK)


def _content_package_from_run(run: Optional[ContentFactoryRun]) -> dict:
    if not run:
        return {}
    result = run.result or {}
    candidates = [
        result.get("content_package"),
        (result.get("result") or {}).get("content_package") if isinstance(result.get("result"), dict) else None,
    ]
    for package in candidates:
        if isinstance(package, dict) and package:
            return package
    return {}


def _load_content_package_for_callback(run_id: str, *, attempts: int = 3, delay_seconds: float = 0.35):
    run = None
    package = {}
    for attempt in range(1, attempts + 1):
        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        package = _content_package_from_run(run)
        if package:
            return run, package
        if attempt < attempts:
            time.sleep(delay_seconds)
    return run, package


class ContentFactoryCallbackView(APIView):
    """
    Receives callbacks from content-factory for various pipeline events.
    
    POST /api/content-factory/callback
    
    Event types:
    - topic_selection: Research complete, topic selected, awaiting confirmation
    - scan_progress: Non-terminal repository scan milestone update
    - discovery_progress: Non-terminal discovery milestone update
    - article_progress: Non-terminal article milestone update
    - generation_blocked: Non-terminal capacity or verifier block update
    - generation_pr_opened: Draft PR opened as the terminal reviewable outcome
    - article_complete: Article generated and published successfully
    - publish_bundle_ready: Delivery bundle packaged and ready
    - error: Pipeline failed with error
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        from .models import ContentFactoryJob
        
        data = request.data
        event_type = data.get('event_type') or data.get('event')
        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id')

        if not event_type:
            return Response(
                {'error': 'event_type is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not job_id:
            return Response(
                {'error': 'job_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        logger.info(f"Content Factory callback received: event={event_type}, job_id={job_id}, domain={domain}")
        
        try:
            if event_type == 'topic_selection':
                return self._handle_topic_selection(data)
            elif event_type == 'article_complete':
                return self._handle_article_complete(data)
            elif event_type == 'error':
                return self._handle_error(data)
            elif event_type == 'auth_required':
                return self._handle_auth_required(data)
            elif event_type == 'scan_complete':
                return self._handle_scan_complete(data)
            elif event_type == 'generation_failed':
                return self._handle_generation_failed(data)
            elif event_type == 'generation_blocked':
                return self._handle_generation_blocked(data)
            elif event_type == 'generation_pr_opened':
                return self._handle_generation_pr_opened(data)
            elif event_type == 'scaffold_complete':
                return self._handle_scaffold_complete(data)
            elif event_type == 'delivery_mode_required':
                return self._handle_delivery_mode_required(data)
            elif event_type == 'draft_pr_created':
                return self._handle_draft_pr_created(data)
            elif event_type == 'preview_ready':
                return self._handle_preview_ready(data)
            elif event_type == 'content_ready':
                return self._handle_content_ready(data)
            elif event_type == 'publish_bundle_ready':
                return self._handle_publish_bundle_ready(data)
            elif event_type == 'discovery_progress':
                return self._handle_discovery_progress(data)
            elif event_type == 'article_progress':
                return self._handle_article_progress(data)
            elif event_type == 'scan_progress':
                return self._handle_scan_progress(data)
            else:
                logger.warning(f"Unknown event_type: {event_type}")
                return Response(
                    {'status': 'ignored', 'message': f'Unknown event_type: {event_type}'},
                    status=status.HTTP_200_OK
                )
        except Exception as e:
            logger.exception(f"Error processing callback: {e}")
            return Response(
                {'error': 'Internal server error processing callback'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _update_content_factory_job(self, *, job_id, domain, slack_user_id, status_value, error_message=None):
        from .models import ContentFactoryJob

        defaults = {
            'domain': domain or '',
            'slack_user_id': slack_user_id or '',
            'status': status_value,
        }
        if error_message is not None:
            defaults['error_message'] = error_message

        job, _ = ContentFactoryJob.objects.update_or_create(
            job_id=job_id,
            defaults=defaults,
        )
        requested_by_slack_user_id = str(
            (self.request.data or {}).get('requested_by_slack_user_id')
            or ''
        ).strip()
        if requested_by_slack_user_id:
            request_meta = dict(job.request_meta or {})
            if request_meta.get('requested_by_slack_user_id') != requested_by_slack_user_id:
                request_meta['requested_by_slack_user_id'] = requested_by_slack_user_id
                job.request_meta = request_meta
                job.save(update_fields=['request_meta', 'updated_at'])
        return job

    def _resolve_job_thread_context(self, *, job, data):
        channel_id = (job.slack_channel_id if job else None) or data.get('slack_channel_id') or ''
        root_message_ts = (
            (job.slack_root_message_ts if job else None)
            or data.get('slack_root_message_ts')
            or data.get('root_message_ts')
            or ''
        )
        thread_ts = (job.slack_thread_ts if job else None) or data.get('slack_thread_ts') or root_message_ts or ''
        if not root_message_ts:
            root_message_ts = thread_ts or ''
        return channel_id, root_message_ts, thread_ts

    def _callback_requested_by_slack_user_id(self, *, job, data):
        request_meta = dict(getattr(job, 'request_meta', {}) or {}) if job else {}
        return str(
            data.get('requested_by_slack_user_id')
            or request_meta.get('requested_by_slack_user_id')
            or ''
        ).strip()

    def _callback_recipient_slack_user_id(self, *, job, data, fallback_slack_user_id=None):
        requested_by_slack_user_id = self._callback_requested_by_slack_user_id(job=job, data=data)
        if requested_by_slack_user_id:
            return requested_by_slack_user_id
        return str(fallback_slack_user_id or '').strip()

    def _send_job_message(self, *, job, data, slack_user_id, text, blocks=None, allow_dm_fallback=True):
        from integrations.services.slack import SlackService

        channel_id, _root_message_ts, thread_ts = self._resolve_job_thread_context(job=job, data=data)
        recipient_slack_user_id = self._callback_recipient_slack_user_id(
            job=job,
            data=data,
            fallback_slack_user_id=slack_user_id,
        )

        if channel_id and thread_ts:
            SlackService.send_message(channel_id, text, blocks=blocks, thread_ts=thread_ts)
            return True
        if allow_dm_fallback and recipient_slack_user_id:
            SlackService.send_dm(recipient_slack_user_id, text, blocks=blocks)
            return True
        return False

    def _callback_dedupe_key(self, data, *, event_name):
        raw_key = str(data.get('dedupe_key') or '').strip()
        if raw_key:
            return raw_key
        pr_token = str(data.get('pr_number') or data.get('pr_url') or data.get('job_id') or 'unknown').strip()
        preview_token = str(data.get('preview_url') or '').strip() or 'no-preview'
        return f"{event_name}:{pr_token}:{preview_token}"

    def _callback_marker_present(self, *, job, bucket, event_name, dedupe_key):
        request_meta = dict(job.request_meta or {})
        bucket_payload = request_meta.get(bucket) or {}
        marker_list = bucket_payload.get(event_name) or []
        return dedupe_key in marker_list

    def _record_callback_marker(self, *, job, bucket, event_name, dedupe_key, extra_request_meta=None):
        request_meta = dict(job.request_meta or {})
        bucket_payload = dict(request_meta.get(bucket) or {})
        marker_list = [str(item) for item in (bucket_payload.get(event_name) or []) if str(item).strip()]
        if dedupe_key not in marker_list:
            marker_list.append(dedupe_key)
        bucket_payload[event_name] = marker_list[-25:]
        request_meta[bucket] = bucket_payload
        if extra_request_meta:
            request_meta.update(extra_request_meta)
        job.request_meta = request_meta
        job.save(update_fields=['request_meta', 'updated_at'])

    def _store_publish_callback_state(self, *, job, data, publish_stage, status_value):
        request_meta = dict(job.request_meta or {})
        request_meta['publish_stage'] = publish_stage
        for field in (
            'route_path',
            'intended_route_path',
            'preview_url',
            'preview_screenshot_urls',
            'preview_surface_kind',
            'preview_content_verified',
            'repo_preview_candidate_url',
            'preview_failure_reason',
            'primary_review_url',
            'primary_review_label',
            'review_surface_kind',
            'bundle_primary_path',
        ):
            if field in data:
                value = data.get(field)
                if field == 'preview_content_verified':
                    request_meta[field] = bool(value)
                elif field == 'preview_screenshot_urls':
                    normalized_urls = [
                        str(item).strip()
                        for item in (value or [])
                        if str(item).strip()
                    ]
                    if normalized_urls:
                        request_meta[field] = normalized_urls
                    else:
                        request_meta.pop(field, None)
                elif value:
                    request_meta[field] = value
                else:
                    request_meta.pop(field, None)
        if 'route_is_live' in data:
            request_meta['route_is_live'] = bool(data.get('route_is_live'))
        if data.get('resolved_delivery_mode'):
            request_meta['resolved_delivery_mode'] = data.get('resolved_delivery_mode')
        if data.get('publish_resolution'):
            request_meta['publish_resolution'] = data.get('publish_resolution')

        update_fields = ['updated_at']
        if job.status != status_value:
            job.status = status_value
            update_fields.append('status')
        if data.get('pr_url') and job.pr_url != data.get('pr_url'):
            job.pr_url = data.get('pr_url')
            update_fields.append('pr_url')
        if job.error_message:
            job.error_message = ''
            update_fields.append('error_message')
        if request_meta != (job.request_meta or {}):
            job.request_meta = request_meta
            update_fields.append('request_meta')
        if len(update_fields) > 1:
            job.save(update_fields=update_fields)

    def _enrich_review_preview_payload(self, data):
        payload = dict(data or {})
        run_id = str(payload.get('run_id') or payload.get('job_id') or '').strip()
        preview_url = str(payload.get('preview_url') or '').strip()
        route_is_live = bool(payload.get('route_is_live')) if payload.get('route_is_live') is not None else bool(preview_url)
        preview_surface_kind = str(payload.get('preview_surface_kind') or '').strip()
        review_surface_kind = str(payload.get('review_surface_kind') or '').strip()
        review_bundle_surface_kinds = {'fallback_bundle', 'patch_bundle', 'content_bundle'}
        is_review_bundle = review_surface_kind in review_bundle_surface_kinds
        is_content_factory_artifact_preview = (
            '/api/content-factory/runs/' in preview_url and '/preview' in preview_url
        )
        if not preview_surface_kind and preview_url and is_content_factory_artifact_preview:
            preview_surface_kind = 'artifact_preview'
            payload['preview_surface_kind'] = preview_surface_kind
        elif not preview_surface_kind and preview_url:
            preview_surface_kind = 'repo_preview'
            payload['preview_surface_kind'] = preview_surface_kind
        preview_content_verified_raw = payload.get('preview_content_verified')
        preview_content_verified = (
            bool(preview_content_verified_raw)
            if preview_content_verified_raw is not None
            else bool(preview_url) and preview_surface_kind == 'artifact_preview'
        )

        if preview_url and preview_surface_kind == 'artifact_preview':
            payload['preview_content_verified'] = True
            payload['route_is_live'] = False
            return payload

        if preview_url and preview_surface_kind == 'repo_preview' and preview_content_verified and not is_review_bundle:
            return payload

        if preview_url and (is_review_bundle or (preview_surface_kind == 'repo_preview' and not preview_content_verified)):
            payload['repo_preview_candidate_url'] = str(payload.get('repo_preview_candidate_url') or preview_url).strip()
            payload['preview_url'] = ''
            payload['preview_content_verified'] = False
            payload['route_is_live'] = False

        if not run_id:
            return payload

        run, content_package = _load_content_package_for_callback(run_id)
        if not run or not content_package:
            return payload

        try:
            artifact_preview_url = build_content_factory_preview_url(
                request=self.request,
                run_id=run.run_id,
            )
        except Exception as exc:
            logger.warning("Failed to build artifact preview URL for %s: %s", run_id, exc)
            return payload

        payload['preview_url'] = artifact_preview_url
        payload['preview_surface_kind'] = 'artifact_preview'
        payload['preview_content_verified'] = True
        payload['primary_review_url'] = artifact_preview_url
        payload['primary_review_label'] = 'Open Preview'
        payload['route_is_live'] = False
        return payload

    def _handle_progress_callback(self, data, *, event_name, status_value, stage_titles, response_message):
        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        milestone_key = data.get('milestone_key', '')
        progress_id = data.get('progress_id') or f"{job_id}:{milestone_key}"
        message = data.get('message') or 'Progress update received.'

        try:
            milestone_index = int(data.get('milestone_index') or 0)
        except (TypeError, ValueError):
            milestone_index = 0
        try:
            milestone_count = int(data.get('milestone_count') or 0)
        except (TypeError, ValueError):
            milestone_count = 0

        job = self._update_content_factory_job(
            job_id=job_id,
            domain=domain,
            slack_user_id=slack_user_id,
            status_value=status_value,
        )

        posted_progress_ids = list(job.posted_progress_ids or [])
        if progress_id in posted_progress_ids:
            logger.info("Ignoring duplicate %s callback for %s (%s)", event_name, job_id, progress_id)
            return Response(
                {
                    'status': 'ignored',
                    'reason': 'duplicate_progress_id',
                    'job_id': job_id,
                    'progress_id': progress_id,
                },
                status=status.HTTP_200_OK,
            )

        if milestone_index and milestone_index <= int(job.last_progress_milestone_index or 0):
            logger.info(
                "Ignoring stale %s callback for %s (%s <= %s)",
                event_name,
                job_id,
                milestone_index,
                job.last_progress_milestone_index,
            )
            return Response(
                {
                    'status': 'ignored',
                    'reason': 'stale_milestone',
                    'job_id': job_id,
                    'progress_id': progress_id,
                },
                status=status.HTTP_200_OK,
            )

        channel_id, _root_message_ts, thread_ts = self._resolve_job_thread_context(job=job, data=data)
        if not channel_id or not thread_ts:
            logger.warning("Unable to route %s callback for %s: missing Slack thread context", event_name, job_id)
            return Response(
                {
                    'status': 'ignored',
                    'reason': 'missing_thread_context',
                    'job_id': job_id,
                    'progress_id': progress_id,
                },
                status=status.HTTP_200_OK,
            )

        stage_title = stage_titles.get(milestone_key, 'Progress update')
        fallback_text = f"{stage_title}: {message}"
        summary_text = message
        progress_blocks = build_progress_update_blocks(
            domain=domain,
            stage_title=stage_title,
            message=message,
            milestone_index=milestone_index or None,
            milestone_count=milestone_count or None,
        )

        try:
            job.last_progress_milestone_key = milestone_key
            job.last_progress_updated_at = timezone.now()
            job.still_working_pinged_at = None
            self._send_job_message(
                job=job,
                data=data,
                slack_user_id=slack_user_id,
                text=fallback_text,
                blocks=progress_blocks,
                allow_dm_fallback=False,
            )
            upsert_live_progress_card(
                job,
                data=data,
                summary_text=summary_text,
            )
        except Exception as exc:
            logger.warning("Failed to send %s notification for %s: %s", event_name, job_id, exc)
            return Response(
                {
                    'status': 'processed_with_error',
                    'job_id': job_id,
                    'progress_id': progress_id,
                    'message': str(exc),
                },
                status=status.HTTP_200_OK,
            )

        job.posted_progress_ids = posted_progress_ids + [progress_id]
        if milestone_index:
            job.last_progress_milestone_index = milestone_index
        job.save(
            update_fields=[
                'posted_progress_ids',
                'last_progress_milestone_index',
                'last_progress_milestone_key',
                'last_progress_updated_at',
                'still_working_pinged_at',
                'updated_at',
            ]
        )

        return Response(
            {
                'status': 'received',
                'message': response_message,
                'job_id': job_id,
                'progress_id': progress_id,
            },
            status=status.HTTP_200_OK,
        )

    def _handle_article_progress(self, data):
        return self._handle_progress_callback(
            data,
            event_name='article_progress',
            status_value='generating',
            stage_titles={
                'research_locked': 'Research locked',
                'draft_grounded': 'Draft grounded',
                'finishing_pass': 'Finishing pass',
            },
            response_message='Article progress callback processed',
        )

    def _handle_discovery_progress(self, data):
        return self._handle_progress_callback(
            data,
            event_name='discovery_progress',
            status_value='researching',
            stage_titles={
                'research_started': 'Research started',
                'candidate_pool_ready': 'Candidate pool ready',
            },
            response_message='Discovery progress callback processed',
        )

    def _handle_scan_progress(self, data):
        return self._handle_progress_callback(
            data,
            event_name='scan_progress',
            status_value='researching',
            stage_titles={
                'repo_analysis': 'Inspecting repository',
                'template_generation': 'Generating guidance',
                'finalizing': 'Finalizing scan',
            },
            response_message='Scan progress callback processed',
        )

    def _handle_delivery_mode_required(self, data):
        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        job = self._update_content_factory_job(
            job_id=job_id,
            domain=domain,
            slack_user_id=slack_user_id,
            status_value='awaiting_delivery_mode',
        )
        request_meta = dict(job.request_meta or {})
        if data.get('recommended_delivery_mode'):
            request_meta['recommended_delivery_mode'] = data.get('recommended_delivery_mode')
        requested_by_slack_user_id = self._callback_requested_by_slack_user_id(job=job, data=data)
        if requested_by_slack_user_id:
            request_meta['requested_by_slack_user_id'] = requested_by_slack_user_id
        if request_meta != (job.request_meta or {}):
            job.request_meta = request_meta
            job.save(update_fields=['request_meta', 'updated_at'])

        return Response(
            {
                'status': 'processed',
                'job_id': job_id,
                'delivery_mode': None,
                'recommended_delivery_mode': data.get('recommended_delivery_mode'),
                'auto_selected': False,
            },
            status=status.HTTP_200_OK,
        )

    def _handle_draft_pr_created(self, data):
        data = self._enrich_review_preview_payload(data)
        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        pr_url = str(data.get('pr_url') or '').strip()
        pr_number = data.get('pr_number')
        route_path = str(data.get('route_path') or '').strip()
        preview_url = str(data.get('preview_url') or '').strip()
        preview_screenshot_urls = [
            str(item).strip()
            for item in (data.get('preview_screenshot_urls') or [])
            if str(item).strip()
        ]
        review_surface_kind = str(data.get('review_surface_kind') or '').strip()
        primary_review_url = str(data.get('primary_review_url') or '').strip()
        primary_review_label = str(data.get('primary_review_label') or '').strip()
        intended_route_path = str(data.get('intended_route_path') or '').strip()
        bundle_primary_path = str(data.get('bundle_primary_path') or '').strip()
        route_is_live = bool(data.get('route_is_live')) if data.get('route_is_live') is not None else bool(preview_url)
        dedupe_key = self._callback_dedupe_key(data, event_name='draft_pr_created')

        job = self._update_content_factory_job(
            job_id=job_id,
            domain=domain,
            slack_user_id=slack_user_id,
            status_value='generating',
        )
        self._store_publish_callback_state(
            job=job,
            data=data,
            publish_stage='awaiting_preview',
            status_value='generating',
        )

        if self._callback_marker_present(
            job=job,
            bucket='callback_notifications',
            event_name='draft_pr_created',
            dedupe_key=dedupe_key,
        ):
            logger.info("Ignoring duplicate draft_pr_created callback for %s (%s)", job_id, dedupe_key)
            return Response(
                {
                    'status': 'ignored',
                    'reason': 'duplicate_notification',
                    'job_id': job_id,
                    'dedupe_key': dedupe_key,
                },
                status=status.HTTP_200_OK,
            )

        slack_sent = False
        if pr_url:
            blocks = build_draft_pr_created_blocks(
                domain=domain,
                pr_url=pr_url,
                pr_number=pr_number,
                route_path=route_path,
                preview_url=preview_url,
                review_surface_kind=review_surface_kind,
                primary_review_url=primary_review_url,
                primary_review_label=primary_review_label,
                route_is_live=route_is_live,
                intended_route_path=intended_route_path,
                bundle_primary_path=bundle_primary_path,
                preview_screenshot_urls=preview_screenshot_urls,
            )
            try:
                slack_sent = self._send_job_message(
                    job=job,
                    data=data,
                    slack_user_id=slack_user_id,
                    text=(
                        f"Review bundle preview ready for {domain}: {primary_review_url}"
                        if review_surface_kind in {'fallback_bundle', 'patch_bundle', 'content_bundle'} and preview_url and primary_review_url
                        else f"Preview ready for {domain}: {primary_review_url}"
                        if preview_url and primary_review_url
                        else f"Draft PR ready for {domain}: {pr_url}"
                    ),
                    blocks=blocks,
                    allow_dm_fallback=True,
                )
            except Exception as exc:
                logger.warning("Failed to send draft_pr_created notification for %s: %s", job_id, exc)

        if slack_sent:
            self._record_callback_marker(
                job=job,
                bucket='callback_notifications',
                event_name='draft_pr_created',
                dedupe_key=dedupe_key,
            )

        return Response(
            {
                'status': 'processed',
                'job_id': job_id,
                'pr_url': pr_url or None,
                'slack_sent': slack_sent,
            },
            status=status.HTTP_200_OK,
        )

    def _handle_generation_pr_opened(self, data):
        data = self._enrich_review_preview_payload(data)
        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        pr_url = str(data.get('pr_url') or '').strip()
        pr_number = data.get('pr_number')
        route_path = str(data.get('route_path') or '').strip()
        preview_url = str(data.get('preview_url') or '').strip()
        preview_screenshot_urls = [
            str(item).strip()
            for item in (data.get('preview_screenshot_urls') or [])
            if str(item).strip()
        ]
        review_surface_kind = str(data.get('review_surface_kind') or '').strip()
        primary_review_url = str(data.get('primary_review_url') or '').strip()
        primary_review_label = str(data.get('primary_review_label') or '').strip()
        intended_route_path = str(data.get('intended_route_path') or '').strip()
        bundle_primary_path = str(data.get('bundle_primary_path') or '').strip()
        route_is_live = bool(data.get('route_is_live')) if data.get('route_is_live') is not None else bool(preview_url)
        verification_state = str(data.get('verification_state') or '').strip()
        reason_code = str(data.get('reason_code') or '').strip()
        review_required = bool(data.get('review_required', True))
        dedupe_key = self._callback_dedupe_key(data, event_name='generation_pr_opened')
        status_value = 'needs_review' if review_required else 'pr_opened'
        publish_stage = 'needs_review' if review_required else 'pr_opened'
        review_summary = (
            "Review bundle PR opened and ready for human review."
            if review_surface_kind in {'fallback_bundle', 'patch_bundle', 'content_bundle'}
            else "Draft PR opened and ready for human review."
            if review_required
            else "Draft PR opened."
        )

        job = self._update_content_factory_job(
            job_id=job_id,
            domain=domain,
            slack_user_id=slack_user_id,
            status_value=status_value,
            error_message='',
        )
        self._store_publish_callback_state(
            job=job,
            data=data,
            publish_stage=publish_stage,
            status_value=status_value,
        )

        request_meta = dict(job.request_meta or {})
        request_meta.update(
            {
                'review_required': review_required,
                'verification_state': verification_state,
                'reason_code': reason_code,
            }
        )
        if data.get('artifact_links') is not None:
            request_meta['artifact_links'] = data.get('artifact_links')
        if request_meta != (job.request_meta or {}):
            job.request_meta = request_meta
            job.save(update_fields=['request_meta', 'updated_at'])

        upsert_live_progress_card(
            job,
            data=data,
            summary_text=review_summary,
            failed=False,
        )

        if self._callback_marker_present(
            job=job,
            bucket='callback_notifications',
            event_name='generation_pr_opened',
            dedupe_key=dedupe_key,
        ):
            logger.info("Ignoring duplicate generation_pr_opened callback for %s (%s)", job_id, dedupe_key)
            return Response(
                {
                    'status': 'ignored',
                    'reason': 'duplicate_notification',
                    'job_id': job_id,
                    'dedupe_key': dedupe_key,
                },
                status=status.HTTP_200_OK,
            )

        slack_sent = False
        if pr_url:
            blocks = build_draft_pr_created_blocks(
                domain=domain,
                pr_url=pr_url,
                pr_number=pr_number,
                route_path=route_path,
                preview_url=preview_url,
                review_surface_kind=review_surface_kind,
                primary_review_url=primary_review_url,
                primary_review_label=primary_review_label,
                route_is_live=route_is_live,
                intended_route_path=intended_route_path,
                bundle_primary_path=bundle_primary_path,
                preview_screenshot_urls=preview_screenshot_urls,
            )
            try:
                slack_sent = self._send_job_message(
                    job=job,
                    data=data,
                    slack_user_id=slack_user_id,
                    text=(
                        f"Review bundle preview ready for {domain}: {primary_review_url}"
                        if review_surface_kind in {'fallback_bundle', 'patch_bundle', 'content_bundle'} and preview_url and primary_review_url
                        else f"Review bundle ready for {domain}: {pr_url}"
                        if review_surface_kind in {'fallback_bundle', 'patch_bundle', 'content_bundle'}
                        else f"Preview ready for review for {domain}: {primary_review_url}"
                        if preview_url and primary_review_url
                        else f"Draft PR ready for review for {domain}: {pr_url}"
                        if review_required
                        else f"Draft PR opened for {domain}: {pr_url}"
                    ),
                    blocks=blocks,
                    allow_dm_fallback=True,
                )
            except Exception as exc:
                logger.warning("Failed to send generation_pr_opened notification for %s: %s", job_id, exc)

        if slack_sent:
            self._record_callback_marker(
                job=job,
                bucket='callback_notifications',
                event_name='generation_pr_opened',
                dedupe_key=dedupe_key,
            )

        return Response(
            {
                'status': 'processed',
                'job_id': job_id,
                'pr_url': pr_url or None,
                'review_required': review_required,
                'job_status': status_value,
                'slack_sent': slack_sent,
            },
            status=status.HTTP_200_OK,
        )

    def _handle_preview_ready(self, data):
        from integrations.services.article_generation import ArticleGenerationError, publish_article

        data = self._enrich_review_preview_payload(data)
        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        pr_url = str(data.get('pr_url') or '').strip()
        preview_url = str(data.get('preview_url') or '').strip()
        preview_screenshot_urls = [
            str(item).strip()
            for item in (data.get('preview_screenshot_urls') or [])
            if str(item).strip()
        ]
        pr_number = data.get('pr_number')
        route_path = str(data.get('route_path') or '').strip()
        primary_review_url = str(data.get('primary_review_url') or '').strip()
        primary_review_label = str(data.get('primary_review_label') or '').strip()
        route_is_live = bool(data.get('route_is_live')) if data.get('route_is_live') is not None else bool(preview_url)
        dedupe_key = self._callback_dedupe_key(data, event_name='preview_ready')

        job = self._update_content_factory_job(
            job_id=job_id,
            domain=domain,
            slack_user_id=slack_user_id,
            status_value='awaiting_approval',
        )
        self._store_publish_callback_state(
            job=job,
            data=data,
            publish_stage='preview_ready',
            status_value='awaiting_approval',
        )

        notification_sent = False
        if not self._callback_marker_present(
            job=job,
            bucket='callback_notifications',
            event_name='preview_ready',
            dedupe_key=dedupe_key,
        ) and pr_url and preview_url:
            blocks = build_preview_ready_blocks(
                domain=domain,
                pr_url=pr_url,
                preview_url=preview_url,
                pr_number=pr_number,
                route_path=route_path,
                primary_review_url=primary_review_url,
                primary_review_label=primary_review_label,
                route_is_live=route_is_live,
                preview_screenshot_urls=preview_screenshot_urls,
            )
            try:
                notification_sent = self._send_job_message(
                    job=job,
                    data=data,
                    slack_user_id=slack_user_id,
                    text=f"Preview ready for {domain}: {primary_review_url or preview_url}",
                    blocks=blocks,
                    allow_dm_fallback=True,
                )
            except Exception as exc:
                logger.warning("Failed to send preview_ready notification for %s: %s", job_id, exc)
            if notification_sent:
                self._record_callback_marker(
                    job=job,
                    bucket='callback_notifications',
                    event_name='preview_ready',
                    dedupe_key=dedupe_key,
                )

        if self._callback_marker_present(
            job=job,
            bucket='callback_actions',
            event_name='preview_ready_auto_approve',
            dedupe_key=dedupe_key,
        ):
            update_fields = ['updated_at']
            if job.status != 'generating':
                job.status = 'generating'
                update_fields.append('status')
            if job.error_message:
                job.error_message = ''
                update_fields.append('error_message')
            request_meta = dict(job.request_meta or {})
            if request_meta.get('publish_stage') != 'auto_approved':
                request_meta['publish_stage'] = 'auto_approved'
                job.request_meta = request_meta
                update_fields.append('request_meta')
            if len(update_fields) > 1:
                job.save(update_fields=update_fields)
            logger.info("Ignoring duplicate preview_ready auto-approve for %s (%s)", job_id, dedupe_key)
            return Response(
                {
                    'status': 'processed',
                    'job_id': job_id,
                    'auto_approved': True,
                    'deduped_auto_approve': True,
                    'slack_sent': notification_sent,
                },
                status=status.HTTP_200_OK,
            )

        try:
            result = publish_article(job_id, slack_user_id=slack_user_id, domain=domain)
            job.status = 'generating'
            job.error_message = ''
            update_fields = ['status', 'error_message', 'updated_at']
            if pr_url and job.pr_url != pr_url:
                job.pr_url = pr_url
                update_fields.append('pr_url')
            job.save(update_fields=update_fields)
            self._record_callback_marker(
                job=job,
                bucket='callback_actions',
                event_name='preview_ready_auto_approve',
                dedupe_key=dedupe_key,
                extra_request_meta={'publish_stage': 'auto_approved'},
            )
            logger.info("Auto-approved preview for job %s", job_id)
            return Response(
                {
                    'status': 'processed',
                    'job_id': job_id,
                    'auto_approved': True,
                    'slack_sent': notification_sent,
                    'cf_response': result,
                },
                status=status.HTTP_200_OK,
            )
        except ArticleGenerationError as exc:
            logger.warning("Failed to auto-approve preview for %s: %s", job_id, exc)
            job.error_message = str(exc)
            job.save(update_fields=['error_message', 'updated_at'])
            return Response(
                {
                    'status': 'deferred',
                    'job_id': job_id,
                    'message': str(exc),
                },
                status=status.HTTP_200_OK,
            )

    def _handle_content_ready(self, data):
        from integrations.services.slack import SlackService

        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        title = data.get('title') or data.get('topic') or 'Untitled article'

        job = self._update_content_factory_job(
            job_id=job_id,
            domain=domain,
            slack_user_id=slack_user_id,
            status_value='completed',
            error_message='',
        )
        request_meta = dict(job.request_meta or {})
        request_meta['publish_stage'] = 'content_ready'
        request_meta['publish_pr_url'] = reverse('content_job_publish_pr', args=[job_id])
        if data.get('promote_bundle_url'):
            request_meta['promote_bundle_url'] = data.get('promote_bundle_url')
        if data.get('publish_pr_url'):
            request_meta['source_publish_pr_url'] = data.get('publish_pr_url')
        if request_meta != (job.request_meta or {}):
            job.request_meta = request_meta
            job.save(update_fields=['request_meta', 'updated_at'])

        logger.info("Content-only article complete for job %s (%s)", job_id, domain)

        upsert_live_progress_card(
            job,
            data=data,
            summary_text="Article content is ready.",
        )

        callback_content_package = data.get("content_package")
        run = None
        content_package = callback_content_package if isinstance(callback_content_package, dict) else None
        if not content_package:
            run, content_package = _load_content_package_for_callback(job_id)
        preview_url = ""
        if content_package and not run:
            run = ContentFactoryRun.objects.filter(run_id=job_id).first()
        if content_package and run:
            try:
                preview_url = build_content_factory_preview_url(
                    request=self.request,
                    run_id=(run.run_id if run else job_id),
                )
            except Exception as exc:
                logger.warning("Failed to build preview URL for %s: %s", job_id, exc)
        else:
            logger.warning(
                "Content-only article ready for %s but durable content_package was unavailable after retries.",
                job_id,
            )
            content_package = {
                "title": title,
                "meta_description": data.get("meta_description") or "",
                "hero_image": data.get("hero_image") or {},
                "inline_images": data.get("inline_images") or [],
                "references": [],
                "article_json": {},
            }

        recipient_slack_user_id = self._callback_recipient_slack_user_id(
            job=job,
            data=data,
            fallback_slack_user_id=slack_user_id,
        )
        requested_by_slack_user_id = self._callback_requested_by_slack_user_id(job=job, data=data)

        if recipient_slack_user_id:
            channel_id, _root_message_ts, thread_ts = self._resolve_job_thread_context(job=job, data=data)
            publish_button_value = None
            if job_id and channel_id and thread_ts:
                publish_button_value = {
                    "job_id": job_id,
                    "domain": domain,
                    "slack_user_id": slack_user_id,
                    "requested_by_slack_user_id": requested_by_slack_user_id or recipient_slack_user_id,
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                }
            blocks = build_content_ready_blocks(
                domain=domain,
                content_package=content_package,
                preview_url=preview_url,
                publish_button_value=publish_button_value,
            )
            fallback_text = f"Article content ready for {domain}"
            try:
                if channel_id and thread_ts:
                    sent, _message_ts = SlackService.send_message(
                        channel_id,
                        fallback_text,
                        blocks=blocks,
                        thread_ts=thread_ts,
                    )
                    if sent and content_package:
                        for message in build_content_thread_messages(content_package):
                            SlackService.send_message(
                                channel_id,
                                message["text"],
                                blocks=message.get("blocks"),
                                thread_ts=thread_ts,
                            )
                else:
                    SlackService.send_dm(
                        recipient_slack_user_id,
                        fallback_text,
                        blocks=blocks,
                    )
            except Exception as exc:
                logger.warning(f"Failed to send content_ready notification to {recipient_slack_user_id}: {exc}")

        return Response(
            {
                'status': 'received',
                'message': 'Content ready callback processed',
                'job_id': job_id,
            },
            status=status.HTTP_200_OK,
        )

    def _handle_publish_bundle_ready(self, data):
        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        title = data.get('title') or data.get('topic') or 'Untitled article'
        publish_resolution = data.get('publish_resolution') or 'publish_bundle'
        suggested_target_path = (
            data.get('suggested_target_path')
            or data.get('primary_artifact_path')
            or data.get('article_markdown_path')
        )
        route_path = data.get('route_path')
        manual_apply_guidance = data.get('manual_apply_guidance') or []

        job = self._update_content_factory_job(
            job_id=job_id,
            domain=domain,
            slack_user_id=slack_user_id,
            status_value='completed',
            error_message='',
        )

        logger.info("Publish bundle ready for job %s (%s)", job_id, domain)

        upsert_live_progress_card(
            job,
            data=data,
            summary_text="Publish bundle is ready.",
        )

        if slack_user_id:
            details = []
            if route_path:
                details.append(f"*Route:* `{route_path}`")
            if suggested_target_path:
                details.append(f"*Target path:* `{suggested_target_path}`")
            if manual_apply_guidance:
                details.append(
                    "*Next step:* " + " ".join(str(item).strip() for item in manual_apply_guidance[:2] if str(item).strip())
                )
            details_text = "\n".join(details)
            if details_text:
                details_text = f"\n\n{details_text}"

            text = (
                f"✅ *Publish bundle ready* for {domain}\n\n"
                f"*{title}*\n\n"
                f"The article is packaged for `{publish_resolution}` delivery.{details_text}"
            )
            try:
                self._send_job_message(
                    job=job,
                    data=data,
                    slack_user_id=slack_user_id,
                    text=f"Publish bundle ready for {domain}",
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": text,
                            },
                        }
                    ],
                )
            except Exception as exc:
                logger.warning(f"Failed to send publish_bundle_ready notification to {slack_user_id}: {exc}")

        return Response(
            {
                'status': 'received',
                'message': 'Publish bundle ready callback processed',
                'job_id': job_id,
            },
            status=status.HTTP_200_OK,
        )

    def _handle_auth_required(self, data):
        """
        Handle 'auth_required' event: notify user to re-authenticate.
        Attempts automatic token refresh first.
        """
        from integrations.services.github import refresh_github_token
        from integrations.services.article_generation import trigger_article_generation, confirm_topic

        job_id = data.get('job_id')
        slack_user_id = data.get('slack_user_id')
        domain = data.get('domain')
        error_message = data.get('message') or data.get('error_message') or data.get('error')
        github_repo = data.get('github_repo')
        reason_code = data.get('reason_code')
        workflow = data.get('workflow')

        logger.info(
            "Received auth_required callback for job %s (user %s, workflow=%s, repo=%s, reason=%s)",
            job_id,
            slack_user_id,
            workflow,
            github_repo,
            reason_code,
        )

        # Update job status
        job, created = ContentFactoryJob.objects.update_or_create(
            job_id=job_id,
            defaults={
                'domain': domain,
                'slack_user_id': slack_user_id,
                'status': 'auth_required',
                'error_message': error_message,
            }
        )
        requested_by_slack_user_id = self._callback_requested_by_slack_user_id(job=job, data=data)
        recipient_slack_user_id = self._callback_recipient_slack_user_id(
            job=job,
            data=data,
            fallback_slack_user_id=slack_user_id,
        )
        if requested_by_slack_user_id:
            request_meta = dict(job.request_meta or {})
            if request_meta.get('requested_by_slack_user_id') != requested_by_slack_user_id:
                request_meta['requested_by_slack_user_id'] = requested_by_slack_user_id
                job.request_meta = request_meta
                job.save(update_fields=['request_meta', 'updated_at'])

        # 1. Attempt Automatic Token Refresh
        refreshed = False
        if slack_user_id:
            try:
                logger.info(f"Attempting automatic GitHub token refresh for {slack_user_id}")
                refresh_github_token(slack_user_id)
                logger.info(f"Successfully refreshed GitHub token for {slack_user_id}")
                refreshed = True
            except Exception as e:
                logger.warning(f"Automatic token refresh failed for {slack_user_id}: {e}")

        # 2. Retry the Job if Refreshed
        if refreshed:
            try:
                # Scenario A: Initial Generation (Phase 1)
                if job.request_meta:
                    logger.info(f"Retrying article generation for job {job_id}")
                    # Reuse request_meta which contains the original article_request
                    trigger_article_generation(slack_user_id, job.request_meta)
                    return Response({
                        'status': 'retried', 
                        'job_id': job_id, 
                        'message': 'Token refreshed and job retried'
                    }, status=status.HTTP_200_OK)
                
                # Scenario B: Topic Confirmation (Phase 2)
                elif job.selected_keyword:
                     logger.info(f"Retrying topic confirmation for job {job_id}")
                     confirm_topic(
                         domain=domain,
                         confirmed_keyword=job.selected_keyword,
                         slack_user_id=slack_user_id,
                         requested_by_slack_user_id=requested_by_slack_user_id or None,
                         slack_channel_id=job.slack_channel_id,
                         slack_thread_ts=job.slack_thread_ts,
                         slack_root_message_ts=job.slack_root_message_ts or job.slack_thread_ts,
                     )
                     return Response({
                         'status': 'retried', 
                         'job_id': job_id, 
                         'message': 'Token refreshed and job retried'
                     }, status=status.HTTP_200_OK)
                
                else:
                    logger.warning(f"Could not retry job {job_id} - no request metadata found")
                    # If we can't retry, we still notify the user, 
                    # but maybe we should update status to 'auth_refreshed_manual_retry_needed'?
                    
            except Exception as e:
                logger.error(f"Failed to retry job {job_id} after token refresh: {e}")
                # Fallthrough to manual notification
        
        # 3. Fallback: Notify user via Slack (Manual Re-auth)
        try:
             self._send_auth_required_notification(
                 recipient_slack_user_id,
                 domain,
                 error_message,
                 job_id,
                 effective_slack_user_id=slack_user_id,
                 github_repo=github_repo,
                 reason_code=reason_code,
             )
        except Exception as e:
             logger.error(f"Failed to send auth_required notification: {e}")
             # Return success anyway to avoid crashing the caller (Content Factory)
             # The job status is already updated in DB so we can track it.
             return Response({'status': 'processed_with_error', 'error': str(e)}, status=status.HTTP_200_OK)

        return Response({'status': 'processed', 'job_id': job_id}, status=status.HTTP_200_OK)

    def _send_auth_required_notification(
        self,
        slack_user_id,
        domain,
        error_message,
        job_id,
        *,
        effective_slack_user_id=None,
        github_repo=None,
        reason_code=None,
    ):
        from integrations.services.slack import SlackService

        try:
            recipient_slack_user_id = str(slack_user_id or '').strip()
            effective_slack_user_id = str(effective_slack_user_id or recipient_slack_user_id or '').strip()
            delegated_request = bool(
                recipient_slack_user_id
                and effective_slack_user_id
                and recipient_slack_user_id != effective_slack_user_id
            )
            if delegated_request:
                text = (
                    f"GitHub auth for <@{effective_slack_user_id}> isn't available for {domain}. "
                    "Ask them to reconnect GitHub, then retry the delegated run."
                )
                SlackService.send_dm(recipient_slack_user_id, text)
                return

            auth_url = _content_factory_github_auth_url(
                slack_user_id=recipient_slack_user_id,
                domain=domain,
            )

            text = f"⚠️ GitHub Authentication Failed for {domain}"
            repo_line = f"\n*Repository:* `{github_repo}`" if github_repo else ""
            reason_line = f"\n*Reason:* {reason_code}" if reason_code else ""
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "⚠️ GitHub Authentication Failed",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"The content pipeline could not access your repository for *{domain}*."
                            f"{repo_line}{reason_line}\n\n*Error:* {error_message}"
                        ),
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "🔐 Re-authenticate GitHub",
                                "emoji": True
                            },
                            "style": "primary",
                            "url": auth_url
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "❌ Cancel",
                                "emoji": True
                            },
                            "style": "danger",
                            "action_id": "cancel_auth_required", # We might not handle this yet, but good practice
                            "value": job_id
                        }
                    ]
                }
            ]

            SlackService.send_dm(recipient_slack_user_id, text, blocks=blocks)
            
        except Exception as e:
            logger.error(f"Error constructing/sending Slack notification: {e}")
            raise

    def _handle_scan_complete(self, data):
        """Handle scan_complete event from content-factory."""
        import json as _json
        from .models import ContentFactoryJob, Organization, OrganizationContentConfig
        from integrations.services.slack import SlackService

        job_id = data.get('job_id')
        run_id = data.get('run_id') or job_id
        workflow = data.get('workflow') or 'repo_scan'
        scaffold_queued = bool(data.get('scaffold_queued'))
        scaffold_job_id = data.get('scaffold_job_id') or ''
        requested_action = str(data.get('requested_action') or '').strip()
        scaffold_status = str(data.get('scaffold_status') or '').strip()
        approve_url = str(data.get('approve_url') or '').strip()
        deny_url = str(data.get('deny_url') or '').strip()
        approval_required = (
            requested_action == 'scaffold_publish_route'
            and scaffold_status == 'approval_required'
        )
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        components_generated = data.get('components_generated', False)
        components_count = data.get('components_count', 0)
        component_names = data.get('component_names', [])
        pillar_count = data.get('pillar_count', 0)
        pillar_names = data.get('pillar_names', [])
        publish_targets = data.get('publish_targets') if isinstance(data.get('publish_targets'), list) else []
        default_publish_target_id = data.get('default_publish_target_id')

        # Update job record if one exists
        job = ContentFactoryJob.objects.filter(job_id=job_id).first()
        if job:
            request_meta = dict(job.request_meta or {})
            request_meta.update(
                {
                    'type': request_meta.get('type') or 'scan',
                    'run_id': run_id,
                    'requested_action': requested_action,
                    'scaffold_status': scaffold_status,
                    'approve_url': approve_url,
                    'deny_url': deny_url,
                    'scaffold_plan': data.get('scaffold_plan') or request_meta.get('scaffold_plan'),
                }
            )
            job.status = 'awaiting_confirmation' if approval_required else 'completed'
            job.request_meta = request_meta
            job.save(update_fields=['status', 'request_meta', 'updated_at'])

        channel_id, _root_message_ts, thread_ts = self._resolve_job_thread_context(job=job, data=data)

        logger.info(
            "Scan complete for %s: run_id=%s workflow=%s components_generated=%s count=%s pillars=%s scaffold_queued=%s scaffold_job_id=%s",
            domain,
            run_id,
            workflow,
            components_generated,
            components_count,
            pillar_count,
            scaffold_queued,
            scaffold_job_id,
        )

        # Persist and resolve article-system readiness for messaging and auto-resume.
        has_pillars = False
        article_system = normalize_article_system(data.get('article_system'))
        try:
            from integrations.utils import normalize_domain

            org = Organization.objects.get(domain=normalize_domain(domain))
            config = org.content_config
            has_pillars = bool((config.pillar_strategy or {}).get('pillars'))
            if data.get('article_system') is not None:
                current_article_system = resolve_article_system(config)
                incoming_article_system = normalize_article_system(data.get('article_system'))
                if incoming_article_system.get('source') == 'scan':
                    if (
                        current_article_system.get('source') == 'manual_confirmed'
                        and current_article_system.get('state') in {'existing', 'roo_scaffolded'}
                        and incoming_article_system.get('state') in {'missing', 'ambiguous'}
                    ):
                        merged_article_system = current_article_system
                    elif (
                        current_article_system.get('state') == 'roo_scaffolded'
                        and incoming_article_system.get('state') == 'missing'
                        and incoming_article_system.get('confidence') == 'low'
                    ):
                        merged_article_system = current_article_system
                    else:
                        merged_article_system = incoming_article_system
                else:
                    merged_article_system = merge_article_system(current_article_system, incoming_article_system)

                update_fields = []
                if merged_article_system != (config.article_system or {}):
                    config.article_system = merged_article_system
                    update_fields.append('article_system')
                if publish_targets != (config.publish_targets or []):
                    config.publish_targets = publish_targets
                    update_fields.append('publish_targets')
                normalized_default_target_id = str(default_publish_target_id or '').strip() or None
                if normalized_default_target_id != config.default_publish_target_id:
                    config.default_publish_target_id = normalized_default_target_id
                    update_fields.append('default_publish_target_id')
                if update_fields:
                    update_fields.append('updated_at')
                    config.save(update_fields=update_fields)
                article_system = merged_article_system
            else:
                article_system = resolve_article_system(config)
        except (Organization.DoesNotExist, OrganizationContentConfig.DoesNotExist):
            pass

        article_system_state = article_system.get('state', 'missing')
        destination_summary = _scan_destination_summary(article_system, publish_targets)

        if not slack_user_id:
            return Response({'status': 'received', 'job_id': job_id}, status=status.HTTP_200_OK)

        try:
            pending_resumed = False
            if slack_user_id and destination_summary and not approval_required and not scaffold_queued:
                try:
                    from integrations.models import UserIntegration
                    from integrations.services.article_generation import trigger_article_generation

                    integration = UserIntegration.objects.filter(slack_user_id=slack_user_id).first()
                    if integration and integration.pending_intent:
                        intent = integration.pending_intent
                        article_req = intent.get('article_request') or {}
                        if (
                            intent.get('type') == 'write_article'
                            and normalize_domain(article_req.get('domain', '')) == normalize_domain(domain)
                        ):
                            integration.pending_intent = None
                            integration.save(update_fields=['pending_intent'])
                            trigger_article_generation(slack_user_id, article_req)
                            pending_resumed = True
                            logger.info(f"Auto-resumed pending article intent after scan for {slack_user_id}/{domain}")
                except Exception as e:
                    logger.warning(f"Failed to auto-resume pending intent after scan for {domain}: {e}")

            if components_generated and components_count > 0:
                component_list = "\n".join(f"  • {name}" for name in component_names[:8])
                if len(component_names) > 8:
                    component_list += f"\n  • ...and {len(component_names) - 8} more"

                # Build pillar summary line
                pillar_line = ""
                if pillar_count and pillar_names:
                    pillar_display = ", ".join(pillar_names[:6])
                    if len(pillar_names) > 6:
                        pillar_display += f", +{len(pillar_names) - 6} more"
                    pillar_line = f"\n\n*{pillar_count} content pillars:* {pillar_display}"
                elif pillar_count:
                    pillar_line = f"\n\n*{pillar_count} content pillars* identified"

                if approval_required:
                    text_body = (
                        f"✅ *Scan complete for {domain}!*\n\n"
                        f"I've analysed your codebase and generated "
                        f"*{components_count} article components* "
                        f"matched to your website's design:\n"
                        f"{component_list}{pillar_line}\n\n"
                        f"The next step is to create an articles directory in your repo. "
                        f"This will set up content pillar directories, article components, "
                        f"an index page, and a demo article — submitted as a PR for your review."
                    )
                    blocks = [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": text_body}
                        },
                        {
                            "type": "actions",
                            "block_id": f"scaffold_confirm_{domain}",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Create Articles Directory"},
                                    "style": "primary",
                                    "action_id": "scaffold_confirm",
                                    "value": _json.dumps({
                                        "domain": domain,
                                        "slack_user_id": slack_user_id,
                                        "channel_id": channel_id,
                                        "thread_ts": thread_ts,
                                        "scan_run_id": run_id,
                                    })
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Skip for now"},
                                    "action_id": "scaffold_skip",
                                    "value": _json.dumps({
                                        "domain": domain,
                                        "slack_user_id": slack_user_id,
                                        "channel_id": channel_id,
                                        "thread_ts": thread_ts,
                                        "scan_run_id": run_id,
                                    })
                                }
                            ]
                        }
                    ]
                    fallback_text = f"✅ Scan complete for {domain}! Generated {components_count} components."
                elif destination_summary:
                    text_body = (
                        f"✅ *Scan complete for {domain}!*\\n\\n"
                        f"I've analysed your codebase and generated "
                        f"*{components_count} article components* "
                        f"matched to your website's design:\\n"
                        f"{component_list}{pillar_line}\\n\\n"
                        f"{destination_summary}"
                    )
                    if pending_resumed:
                        text_body += "\\n\\n🔄 *Resuming your article request automatically!* You'll get a notification shortly."
                    else:
                        text_body += "\\n\\nYou can now ask me to research or write an article."
                    fallback_text = text_body
                    blocks = None
                elif article_system_state == 'ambiguous':
                    detected_location = article_system.get('directory_path') or article_system.get('directory_name') or 'an existing content directory'
                    fallback_text = (
                        f"✅ *Scan complete for {domain}!*\\n\\n"
                        f"I found what looks like an article system at `{detected_location}`, "
                        f"but the detection confidence is low.\\n\\n"
                        f"You can tell me to use the detected system, rescan the repo, or scaffold a new articles directory."
                    )
                    blocks = None
                elif scaffold_queued:
                    text_body = (
                        f"✅ *Scan complete for {domain}!*\n\n"
                        f"I've analysed your codebase and generated "
                        f"*{components_count} article components* "
                        f"matched to your website's design:\n"
                        f"{component_list}{pillar_line}\n\n"
                        f"I've already queued article-directory setup in your repo, and I'll update you again when that PR is ready."
                    )
                    fallback_text = text_body
                    blocks = None
                elif has_pillars:
                    fallback_text = (
                        f"✅ *Scan complete for {domain}!*\n\n"
                        f"I've analysed your codebase and generated "
                        f"*{components_count} article components* "
                        f"matched to your website's design:\n"
                        f"{component_list}{pillar_line}\n\n"
                        f"This scan did not include scaffold approval metadata. Please run a fresh scan before creating the articles directory."
                    )
                    blocks = None
                else:
                    fallback_text = (
                        f"✅ *Scan complete for {domain}!*\n\n"
                        f"I've analysed your codebase and generated "
                        f"*{components_count} article components* "
                        f"matched to your website's design:\n{component_list}\n\n"
                        f"These components will be used to create articles that look native to your site.\n\n"
                        f"Would you like me to write your first article? Just say:\n"
                        f"  `@Roo write me an article about [topic]`"
                    )
                    blocks = None
            else:
                if approval_required:
                    fallback_text = (
                        f"✅ *Scan complete for {domain}!*\n\n"
                        f"I've analysed your repository and the next step is to create an articles directory in your repo.\n\n"
                        f"This will set up the safe publish route as a PR for your review."
                    )
                    blocks = [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": fallback_text}
                        },
                        {
                            "type": "actions",
                            "block_id": f"scaffold_confirm_{domain}",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Create Articles Directory"},
                                    "style": "primary",
                                    "action_id": "scaffold_confirm",
                                    "value": _json.dumps({
                                        "domain": domain,
                                        "slack_user_id": slack_user_id,
                                        "channel_id": channel_id,
                                        "thread_ts": thread_ts,
                                        "scan_run_id": run_id,
                                    })
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Skip for now"},
                                    "action_id": "scaffold_skip",
                                    "value": _json.dumps({
                                        "domain": domain,
                                        "slack_user_id": slack_user_id,
                                        "channel_id": channel_id,
                                        "thread_ts": thread_ts,
                                        "scan_run_id": run_id,
                                    })
                                }
                            ]
                        }
                    ]
                elif destination_summary:
                    fallback_text = (
                        f"✅ *Scan complete for {domain}!*\n\n"
                        f"{destination_summary}\n\n"
                        f"You can now ask me to research or write an article."
                    )
                    if pending_resumed:
                        fallback_text += "\n\n🔄 *Resuming your article request automatically!* You'll get a notification shortly."
                elif article_system_state == 'ambiguous':
                    fallback_text = (
                        f"✅ *Scan complete for {domain}!*\n\n"
                        f"I found what looks like an existing article system, but I’m not fully confident.\n\n"
                        f"You can tell me to use the detected system, rescan the repo, or scaffold a new articles directory."
                    )
                else:
                    fallback_text = (
                        f"✅ *Scan complete for {domain}!*\n\n"
                        f"I've analysed your codebase and I'm ready to help. "
                        f"You can now ask me to create blog pages or other content.\n\n"
                        f"To get started, say:\n"
                        f"  `@Roo write me an article about [topic]`"
                    )
                    blocks = None

            # Reply in-thread if we have context, fall back to DM
            if channel_id and thread_ts:
                SlackService.send_message(channel_id, fallback_text, blocks=blocks, thread_ts=thread_ts)
            else:
                SlackService.send_dm(slack_user_id, fallback_text, blocks=blocks)
        except Exception as e:
            logger.warning(f"Failed to send scan_complete notification to {slack_user_id}: {e}")

        return Response({
            'status': 'received',
            'message': 'Scan complete callback processed',
            'job_id': job_id,
        }, status=status.HTTP_200_OK)

    def _handle_generation_failed(self, data):
        """Handle generation_failed event from content-factory."""
        from .models import ContentFactoryJob
        from integrations.services.article_generation import (
            get_content_factory_article_cost_points,
            maybe_auto_refund_terminal_failure,
        )
        from integrations.services.daily_discovery import (
            is_scheduled_daily_job,
            mark_scheduled_dispatch_failed,
        )
        from integrations.services.slack import SlackService

        job_id = data.get('job_id')
        run_id = data.get('run_id') or job_id
        workflow = data.get('workflow') or 'unknown'
        error_message = data.get('error', data.get('error_message', 'Unknown error'))
        error_code = data.get('error_code', 'INTERNAL_ERROR')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        diagnostics = _normalize_discovery_diagnostics(data.get('diagnostics'))
        diagnostics_text = _format_discovery_diagnostics(diagnostics)
        try:
            refund_points = int(data.get('refund_points') or 0)
        except (TypeError, ValueError):
            refund_points = 0
        auto_refunded = bool(data.get('auto_refunded'))

        # Update job record
        job, created = ContentFactoryJob.objects.update_or_create(
            job_id=job_id,
            defaults={
                'domain': domain,
                'slack_user_id': slack_user_id,
                'status': 'error',
                'error_message': f"[{error_code}] {error_message}",
            }
        )
        scheduled_daily_job = is_scheduled_daily_job(job)

        channel_id, _root_message_ts, thread_ts = self._resolve_job_thread_context(job=job, data=data)

        logger.error(
            "Generation failed for job %s run %s workflow=%s (%s): [%s] %s",
            job_id,
            run_id,
            workflow,
            domain,
            error_code,
            error_message,
        )

        upsert_live_progress_card(
            job,
            data=data,
            summary_text=f"Run failed: {error_message}",
            failed=True,
        )

        if workflow in {'auto_discovery', 'direct_generate', 'confirmed_topic'} and not auto_refunded:
            auto_refunded, refund_points = maybe_auto_refund_terminal_failure(
                job,
                error_code=error_code,
                error_message=error_message,
            )

        mark_scheduled_dispatch_failed(
            job_id=job_id,
            error_message=f"[{error_code}] {error_message}",
        )

        if scheduled_daily_job:
            return Response({
                'status': 'received',
                'message': 'Generation failed callback processed',
                'job_id': job_id,
                'scheduled_daily_suppressed': True,
            }, status=status.HTTP_200_OK)

        if slack_user_id:
            try:
                if error_code == 'PREREQUISITE_MISSING':
                    missing_step = data.get('missing_step', 'unknown')
                    if missing_step == 'scan':
                        message = (
                            f"⚠️ *{domain} needs to be scanned first.*\n\n"
                            f"{error_message}\n\n"
                            f"Say: `@Roo scan my codebase {domain}`"
                        )
                    elif missing_step == 'scaffold':
                        message = (
                            f"⚠️ *{domain} needs article scaffolding first.*\n\n"
                            f"{error_message}\n\n"
                            f"Say: `@Roo scaffold articles for {domain}`"
                        )
                    else:
                        message = (
                            f"⚠️ *Prerequisite missing for {domain}*\n\n"
                            f"{error_message}"
                        )
                elif error_code == 'MISSING_CONFIG':
                    message = (
                        f"❌ *Failed for {domain}*\n\n"
                        f"{error_message}\n\n"
                        f"Please make sure this domain is registered in the system."
                    )
                elif error_code == 'ARTICLE_SYSTEM_ACTION_REQUIRED':
                    recommended_action = data.get('recommended_action', 'scaffold')
                    if recommended_action == 'confirm_article_system':
                        message = (
                            f"⚠️ *{domain} needs article-system confirmation first.*\n\n"
                            f"{error_message}\n\n"
                            f"I found what may already be the right article directory, but I need confirmation before writing into it."
                        )
                    else:
                        message = (
                            f"⚠️ *{domain} needs an article system before writing.*\n\n"
                            f"{error_message}\n\n"
                            f"Ask me to scaffold articles for {domain}, or confirm the detected structure if one already exists."
                        )
                elif error_code == 'PUBLISH_TARGET_ACTION_REQUIRED':
                    message = (
                        f"⚠️ *{domain} needs a supported publish target before direct publish can continue.*\n\n"
                        f"{error_message}\n\n"
                        f"Roo stopped before changing the repository. You can retry in content-only mode, or add a supported publish target such as `.content-factory/target.yml`."
                    )
                elif error_code in ('INVALID_CREDENTIALS', 'REPO_NOT_FOUND'):
                    message = (
                        f"❌ *Failed for {domain}*\n\n"
                        f"{error_message}\n\n"
                        f"Please reconnect your GitHub account by saying:\n"
                        f"  `@Roo connect to my domain {domain}`"
                    )
                elif workflow == 'auto_discovery' and error_code == 'NO_OPPORTUNITIES':
                    message = (
                        f"⚠️ *Research for {domain} didn't find viable topics yet*\n\n"
                        f"{error_message}"
                    )
                    if diagnostics_text:
                        message += f"\n\n{diagnostics_text}"
                    message += (
                        "\n\nYou can still ask Roo to write about a specific topic, for example:\n"
                        f"  `@Roo write an article for {domain} about [topic]`\n\n"
                        f"This doesn't affect any scan or scaffold work already in progress."
                    )
                elif workflow == 'auto_discovery':
                    message = (
                        f"❌ *Research failed for {domain}*\n\n"
                        f"{error_message}\n\n"
                        f"This doesn't affect any scan or scaffold work already in progress."
                    )
                elif workflow == 'repo_scan':
                    message = (
                        f"❌ *Scan failed for {domain}*\n\n"
                        f"{error_message}\n\n"
                        f"If this keeps happening, please contact support."
                    )
                elif workflow == 'scaffold':
                    message = (
                        f"❌ *Articles directory setup failed for {domain}*\n\n"
                        f"{error_message}\n\n"
                        f"If this keeps happening, please contact support."
                    )
                else:
                    message = (
                        f"❌ *Task failed for {domain}*\n\n"
                        f"{error_message}\n\n"
                        f"If this keeps happening, please contact support."
                    )
                if workflow in {'auto_discovery', 'direct_generate', 'confirmed_topic'}:
                    if auto_refunded and refund_points > 0:
                        message += f"\n\nYour {refund_points} Roo points were refunded automatically."
                    else:
                        manual_refund_points = get_content_factory_article_cost_points(domain)
                        if manual_refund_points > 0:
                            message += (
                                f"\n\nIf this run failed and you want your {manual_refund_points} Roo points back, "
                                "message Dr Sam on Slack."
                            )
                # Reply in-thread if we have context, fall back to DM
                if channel_id and thread_ts:
                    SlackService.send_message(channel_id, message, thread_ts=thread_ts)
                else:
                    SlackService.send_dm(slack_user_id, message)
            except Exception as e:
                logger.warning(f"Failed to send generation_failed notification to {slack_user_id}: {e}")

        return Response({
            'status': 'received',
            'message': 'Generation failed callback processed',
            'job_id': job_id,
        }, status=status.HTTP_200_OK)

    def _handle_generation_blocked(self, data):
        """Handle generation_blocked event from content-factory."""
        from .models import ContentFactoryJob
        from integrations.services.article_generation import sync_blocked_job_state

        job_id = data.get('job_id')
        run_id = data.get('run_id') or job_id
        workflow = data.get('workflow') or 'unknown'
        error_message = data.get('error', data.get('error_message', 'Generation is blocked waiting for capacity.'))
        error_code = data.get('error_code', 'verifier_capacity_unavailable')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        blocked_step = data.get('blocked_step') or 'verify_build'
        preferred_queue = data.get('preferred_queue') or ''
        fallback_policy = data.get('fallback_policy') or ''
        retry_after_seconds = data.get('retry_after_seconds')

        job, _created = ContentFactoryJob.objects.update_or_create(
            job_id=job_id,
            defaults={
                'domain': domain,
                'slack_user_id': slack_user_id,
                'status': 'blocked',
                'error_message': f"[{error_code}] {error_message}",
            }
        )
        sync_blocked_job_state(job, data, update_card=True, allow_visible_notification=True)

        logger.warning(
            "Generation blocked for job %s run %s workflow=%s (%s): [%s] %s",
            job_id,
            run_id,
            workflow,
            domain,
            error_code,
            error_message,
        )

        return Response({
            'status': 'received',
            'message': 'Generation blocked callback processed',
            'job_id': job_id,
        }, status=status.HTTP_200_OK)

    def _handle_scaffold_complete(self, data):
        """Handle scaffold_complete event from content-factory."""
        from .models import ContentFactoryJob, Organization, OrganizationContentConfig
        from integrations.services.slack import SlackService
        from integrations.utils import normalize_domain

        job_id = data.get('job_id')
        parent_run_id = data.get('parent_run_id') or ''
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        pr_url = data.get('pr_url')
        pillar_count = data.get('pillar_count', 0)
        component_count = data.get('component_count', 0)
        files_created = data.get('files_created', 0)
        already_exists = data.get('already_exists', False)
        error = data.get('error')
        preview_url = data.get('preview_url')
        build_verified = data.get('build_verified', False)

        # Update job record
        job = ContentFactoryJob.objects.filter(job_id=job_id).first()
        if job:
            if error:
                job.status = 'error'
                job.error_message = error
            else:
                job.status = 'completed'
                job.pr_url = pr_url
            job.save()

        parent_job = ContentFactoryJob.objects.filter(job_id=parent_run_id).first() if parent_run_id else None

        # Resolve thread context: scaffold job first, then parent scan job, then callback payload.
        channel_id = (
            (job.slack_channel_id if job else None)
            or (parent_job.slack_channel_id if parent_job else None)
            or data.get('slack_channel_id')
            or ''
        )
        thread_ts = (
            (job.slack_thread_ts if job else None)
            or (parent_job.slack_thread_ts if parent_job else None)
            or (parent_job.slack_root_message_ts if parent_job else None)
            or data.get('slack_thread_ts')
            or data.get('slack_root_message_ts')
            or ''
        )

        def _send(text, blocks=None):
            if channel_id and thread_ts:
                SlackService.send_message(channel_id, text, blocks=blocks, thread_ts=thread_ts)
            elif slack_user_id:
                SlackService.send_dm(slack_user_id, text, blocks=blocks)

        if error:
            logger.error(f"Scaffold failed for {domain}: {error}")
            try:
                _send(
                    f"Note: Could not set up article directories for *{domain}*: {error}\n"
                    f"This won't affect article generation -- directories will be created as needed."
                )
            except Exception as e:
                logger.warning(f"Failed to send scaffold error notification: {e}")
            return Response({
                'status': 'received',
                'message': 'Scaffold error processed',
                'job_id': job_id,
            }, status=status.HTTP_200_OK)

        # Persist canonical article-system state from scaffold results.
        normalized_domain = ''
        try:
            normalized_domain = normalize_domain(domain)
            org = Organization.objects.get(domain=normalized_domain)
            config = org.content_config
            incoming_article_system = data.get('article_system')
            if incoming_article_system is not None:
                config.article_system = merge_article_system(resolve_article_system(config), incoming_article_system)
            elif already_exists:
                existing_system = resolve_article_system(config)
                existing_system.update(
                    {
                        'state': 'existing',
                        'source': existing_system.get('source') or 'scan',
                    }
                )
                config.article_system = normalize_article_system(existing_system)
            else:
                scaffolded_system = resolve_article_system(config)
                scaffolded_system.update(
                    {
                        'state': 'roo_scaffolded',
                        'confidence': 'high',
                        'source': 'scaffold',
                        'reason': scaffolded_system.get('reason') or 'Roo scaffolded the article system for this repository',
                    }
                )
                config.article_system = normalize_article_system(scaffolded_system)

            if not already_exists:
                config.articles_scaffolded = True
            if pr_url:
                config.articles_scaffold_pr_url = pr_url
            if preview_url:
                config.articles_scaffold_preview_url = preview_url
            config.save()
            logger.info(f"Updated article_system for {domain} after scaffold callback")
        except (Organization.DoesNotExist, OrganizationContentConfig.DoesNotExist) as e:
            logger.warning(f"Could not update article_system for {domain}: {e}")

        # Check for pending article intent to auto-resume
        pending_resumed = False
        if slack_user_id:
            try:
                from integrations.models import UserIntegration
                integration = UserIntegration.objects.filter(slack_user_id=slack_user_id).first()
                if integration and integration.pending_intent:
                    intent = integration.pending_intent
                    if intent.get('type') == 'write_article' and intent.get('article_request'):
                        article_req = intent['article_request']
                        # Only resume if intent is for the same domain
                        intent_domain = normalize_domain(article_req.get('domain', ''))
                        if intent_domain == normalized_domain:
                            # Clear intent first (prevent double-trigger)
                            integration.pending_intent = None
                            integration.save()

                            # Auto-trigger article generation
                            from integrations.services.article_generation import trigger_article_generation
                            trigger_article_generation(slack_user_id, article_req)
                            pending_resumed = True
                            logger.info(f"Auto-resumed pending article intent for {slack_user_id}/{domain}")
            except Exception as e:
                logger.warning(f"Failed to resume pending intent after scaffold: {e}")

        # Send Slack notification
        try:
            import json as _json

            if already_exists:
                details = []
                if pr_url:
                    details.append(f"🔗 *PR:* {pr_url}")
                if preview_url:
                    details.append(f"🔗 *Preview:* {preview_url}")
                detail_block = "\n\n".join(details)
                detail_suffix = f"{detail_block}\n\n" if detail_block else ""
                if pending_resumed:
                    _send(
                        f"📁 Articles directory already exists for *{domain}*.\n\n"
                        f"{detail_suffix}"
                        f"🔄 *Resuming your article request automatically!* You'll get a notification shortly."
                    )
                else:
                    _send(
                        f"📁 Articles directory already exists for *{domain}*.\n\n"
                        f"{detail_suffix}"
                        f"You're all set! To write your first article, say:\n"
                        f"  `@Roo write me an article about [topic]`"
                    )
            elif pr_url:
                preview_line = ""
                if preview_url:
                    preview_line = f"\n\n🔗 *Preview:* {preview_url}"
                build_status = "✅ Build passed" if build_verified else "⏳ Build pending"
                change_line = (
                    f"  • {files_created} total files\n"
                    if files_created
                    else "  • Reused the existing scaffold branch/PR\n"
                )
                text_body = (
                    f"📁 *Articles directory created for {domain}!*\n\n"
                    f"I've set up your content structure with:\n"
                    f"  • {pillar_count} content pillar directories\n"
                    f"  • {component_count} article components\n"
                    f"{change_line}"
                    f"  • {build_status}\n\n"
                    f"*Review the PR:* {pr_url}{preview_line}"
                )
                if pending_resumed:
                    text_body += (
                        f"\n\n🔄 *Resuming your article request automatically!* "
                        f"You'll get a notification shortly."
                    )
                    _send(text_body)
                else:
                    text_body += "\n\nOnce merged, I can write your first article."
                    blocks = [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": text_body}
                        },
                        {
                            "type": "actions",
                            "block_id": f"write_article_{domain}",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Write First Article"},
                                    "style": "primary",
                                    "action_id": "write_first_article",
                                    "value": _json.dumps({
                                        "domain": domain,
                                        "slack_user_id": slack_user_id,
                                        "channel_id": channel_id,
                                        "thread_ts": thread_ts,
                                    })
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Skip for now"},
                                    "action_id": "write_article_skip",
                                    "value": _json.dumps({"domain": domain})
                                }
                            ]
                        }
                    ]
                    fallback_text = f"📁 Articles directory created for {domain}! Review the PR: {pr_url}"
                    _send(fallback_text, blocks=blocks)
            else:
                _send(
                    f"📁 Articles directory scaffolded for *{domain}*, but "
                    f"the PR could not be created. Check the repo for a "
                    f"`feature/articles-scaffolding` branch."
                )
        except Exception as e:
            logger.warning(f"Failed to send scaffold_complete notification: {e}")

        return Response({
            'status': 'received',
            'message': 'Scaffold complete callback processed',
            'job_id': job_id,
        }, status=status.HTTP_200_OK)

    def _generate_topic_explanation(self, option_data, company_context=None, competitors=None):
        """Generate a user-friendly explanation for why this topic was chosen."""
        volume = option_data.get('volume', 0)
        difficulty = option_data.get('difficulty', 50)
        tier = option_data.get('tier', 'tier_4_discard')
        opportunity_index = option_data.get('opportunity_index', 0.0)
        
        parts = []
        
        # Volume assessment
        try:
            volume_val = int(volume)
        except (ValueError, TypeError):
            volume_val = 0
            
        if volume_val >= 2000:
            parts.append(f"High search volume ({volume_val:,}/mo)")
        elif volume_val >= 500:
            parts.append(f"Moderate search volume ({volume_val:,}/mo)")
        else:
            parts.append(f"Niche search volume ({volume_val:,}/mo)")
        
        # Difficulty assessment  
        try:
            diff_val = int(difficulty)
        except (ValueError, TypeError):
            diff_val = 50
            
        if diff_val <= 35:
            parts.append("with low competition.")
        elif diff_val <= 60:
            parts.append("with moderate competition.")
        else:
            parts.append("but highly competitive.")
        
        # Tier-based reasoning
        tier_reasons = {
            'tier_1_blue_ocean': "This is an untapped opportunity where AI overviews haven't saturated the search results.",
            'tier_2_authority': "This topic helps establish your authority in the space.",
            'tier_3_long_tail': "A focused long-tail opportunity that can drive targeted traffic.",
        }
        if tier in tier_reasons:
            parts.append(tier_reasons[tier])
        
        # Company relevance (if context available)
        if company_context and len(str(company_context)) > 10:
            parts.append("This aligns with your company's focus areas.")
        
        # Competitor gap (if competitors listed)
        if competitors and isinstance(competitors, list) and len(competitors) > 0:
            existing_presence = False
            # Check if competitors are targeting this (simplified check based on provided competitor list in option, if any)
            # But here we just mention the competitors context generally if we knew more.
            # Since content factory returns specific competitor data per keyword, we could use that if available.
            # For now, just a generic statement if it's a gap analysis result
            pass
        
        return " ".join(parts)

    def _handle_topic_selection(self, data):
        """Handle topic_selection event from content-factory."""
        from .models import ContentFactoryJob, Organization, ScheduledDiscoveryDispatch
        from integrations.services.article_generation import (
            CONTENT_FACTORY_BILLING_STATUS_DEFERRED,
            SCHEDULED_DAILY_TRIGGER_SOURCE,
        )
        from integrations.services.daily_discovery import (
            get_daily_discovery_schedule_channel_name,
            is_scheduled_daily_job,
            mark_scheduled_dispatch_failed,
            mark_scheduled_dispatch_topic_selection_sent,
        )
        from integrations.services.slack import SlackService
        
        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        selection = data.get('selection', {})
        
        # Extract options (or wrap single selection if new format not sent)
        options = selection.get('options', [])
        if not options and selection.get('selected_keyword'):
            # Backwards compatibility
            options = [selection.copy()]
            selection['options'] = options
            
        # Limit to top 4 options
        options = options[:4]
        
        # Get or create job tracking record
        job, created = ContentFactoryJob.objects.update_or_create(
            job_id=job_id,
            defaults={
                'domain': domain,
                'slack_user_id': slack_user_id,
                'status': 'awaiting_confirmation',
                'selected_keyword': selection.get('selected_keyword', ''),
                'selection_reason': selection.get('selection_reason', ''),
                'selection_data': selection,
            }
        )
        requested_by_slack_user_id = self._callback_requested_by_slack_user_id(job=job, data=data)
        if requested_by_slack_user_id:
            request_meta = dict(job.request_meta or {})
            if request_meta.get('requested_by_slack_user_id') != requested_by_slack_user_id:
                request_meta['requested_by_slack_user_id'] = requested_by_slack_user_id
                job.request_meta = request_meta
                job.save(update_fields=['request_meta', 'updated_at'])
        job.last_progress_milestone_key = 'awaiting_confirmation'
        job.last_progress_updated_at = timezone.now()
        job.still_working_pinged_at = None
        job.save(update_fields=['last_progress_milestone_key', 'last_progress_updated_at', 'still_working_pinged_at', 'updated_at'])
        dispatch = ScheduledDiscoveryDispatch.objects.filter(content_factory_job_id=job_id).first()
        scheduled_daily_job = is_scheduled_daily_job(job) or bool(dispatch)
        if scheduled_daily_job:
            request_meta = dict(job.request_meta or {})
            update_fields = []
            if request_meta.get("trigger_source") != SCHEDULED_DAILY_TRIGGER_SOURCE:
                request_meta["trigger_source"] = SCHEDULED_DAILY_TRIGGER_SOURCE
                update_fields.append("request_meta")
            if not job.billing_status:
                job.billing_status = CONTENT_FACTORY_BILLING_STATUS_DEFERRED
                update_fields.append("billing_status")
            if update_fields:
                job.request_meta = request_meta
                update_fields.append("updated_at")
                job.save(update_fields=update_fields)

        logger.info(f"Topic selection recorded for job {job_id}: {len(options)} options found")

        if slack_user_id and options:
            # Fetch organization context for explanations
            company_context = None
            competitors = []
            org = None
            try:
                # Simple normalization (should ideally match what other views do)
                normalized_domain = domain.lower().strip()
                if normalized_domain.startswith('https://'): normalized_domain = normalized_domain[8:]
                if normalized_domain.startswith('http://'): normalized_domain = normalized_domain[7:]
                if normalized_domain.startswith('www.'): normalized_domain = normalized_domain[4:]
                if '/' in normalized_domain: normalized_domain = normalized_domain.split('/')[0]
                
                org = Organization.objects.filter(domain__icontains=normalized_domain).first()
                if org:
                    config = getattr(org, 'content_config', None)
                    if config:
                        company_context = config.company_context
                    competitors = org.competitors or []
            except Exception as e:
                logger.warning(f"Could not fetch org context for explanations: {e}")

            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "📊 Article Topics Selected",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"I've researched content opportunities for *{domain}* and found {len(options)} great topics. Choose one to write:"
                    }
                }
            ]
            
            # Action buttons accumulator
            action_elements = []
            
            for idx, option in enumerate(options):
                keyword = option.get('keyword', option.get('selected_keyword', 'Unknown Topic'))
                display_title = option.get('suggested_title') or keyword
                volume = option.get('volume', 'N/A')
                difficulty = option.get('difficulty', 'N/A')
                score = option.get('opportunity_index', 'N/A')
                
                # Format score
                try:
                    score_val = float(score)
                    score_str = f"{score_val:.1f}"
                except (ValueError, TypeError):
                    score_str = str(score)

                # Use provided explanation or generate one
                explanation = option.get('explanation')
                if not explanation:
                    explanation = self._generate_topic_explanation(option, company_context, competitors)
                
                # Add section for this option
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{idx + 1}. {display_title}*\n"
                                f"`{keyword}`\n"
                                f"📈 {volume}/mo • 🎯 Difficulty: {difficulty}/100 • Score: {score_str}\n"
                                f"_{explanation}_"
                    }
                })
                
                # Create button for this option
                action_elements.append({
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": f"Op {idx + 1}: {keyword[:15]}..." if len(keyword) > 18 else f"Op {idx + 1}: {keyword}",
                        "emoji": True
                    },
                    "value": f"confirm_topic:{job_id}:{idx}",  # Include index in value
                    "action_id": f"confirm_topic_btn_{idx}"
                })

            # Add buttons row
            # Split into chunks of 5 if cleaner, but Slack allows 5 buttons per action block.
            # We add cancel at the end.
            
            # Add Cancel button
            action_elements.append({
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "❌ Cancel",
                    "emoji": True
                },
                "style": "danger",
                "value": f"cancel_topic:{job_id}",
                "action_id": "cancel_topic_btn"
            })
            
            # Add action block
            blocks.append({
                "type": "actions",
                "elements": action_elements
            })

            if scheduled_daily_job:
                owner_slack_user_id = str(
                    getattr(getattr(org, 'content_config', None), 'connected_slack_user_id', '') or slack_user_id
                ).strip()
                if owner_slack_user_id:
                    blocks.insert(
                        1,
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"<@{owner_slack_user_id}> your scheduled research for *{domain}* is ready.",
                            },
                        },
                    )

                channel_name = get_daily_discovery_schedule_channel_name()
                channel_id = SlackService.get_channel_id_by_name(channel_name)
                if not channel_id:
                    error_message = (
                        f"Scheduled discovery could not post to Slack because #{channel_name} could not be resolved."
                    )
                    mark_scheduled_dispatch_failed(job_id=job_id, error_message=error_message)
                    job.status = 'error'
                    job.error_message = error_message
                    job.save(update_fields=['status', 'error_message', 'updated_at'])
                    return Response(
                        {
                            'status': 'processed_with_error',
                            'message': error_message,
                            'job_id': job_id,
                        },
                        status=status.HTTP_200_OK,
                    )

                sent, message_ts = SlackService.send_message(
                    channel_id,
                    f"Scheduled topic selection ready for {domain}",
                    blocks=blocks,
                )
                if not sent or not message_ts:
                    error_message = (
                        f"Scheduled discovery could not post the topic selection card into #{channel_name}."
                    )
                    mark_scheduled_dispatch_failed(job_id=job_id, error_message=error_message)
                    job.status = 'error'
                    job.error_message = error_message
                    job.save(update_fields=['status', 'error_message', 'updated_at'])
                    return Response(
                        {
                            'status': 'processed_with_error',
                            'message': error_message,
                            'job_id': job_id,
                        },
                        status=status.HTTP_200_OK,
                    )

                job.slack_channel_id = channel_id
                job.slack_root_message_ts = message_ts
                job.slack_thread_ts = message_ts
                job.save(
                    update_fields=[
                        'slack_channel_id',
                        'slack_root_message_ts',
                        'slack_thread_ts',
                        'updated_at',
                    ]
                )
                mark_scheduled_dispatch_topic_selection_sent(
                    job_id=job_id,
                    slack_channel_id=channel_id,
                    slack_message_ts=message_ts,
                    slack_thread_ts=message_ts,
                )
            else:
                channel_id, _root_message_ts, thread_ts = self._resolve_job_thread_context(job=job, data=data)
                mark_scheduled_dispatch_topic_selection_sent(
                    job_id=job_id,
                    slack_channel_id=channel_id,
                    slack_thread_ts=thread_ts,
                )
                upsert_live_progress_card(
                    job,
                    data=data,
                    summary_text="Research complete. Choose one of the topic options below to continue.",
                )
                self._send_job_message(
                    job=job,
                    data=data,
                    slack_user_id=slack_user_id,
                    text="Topic selection ready for review",
                    blocks=blocks,
                )
        
        return Response({
            'status': 'received',
            'message': 'Topic selection callback processed',
            'job_id': job_id,
            'awaiting_confirmation': True,
        }, status=status.HTTP_200_OK)

    def _handle_article_complete(self, data):
        """Handle article_complete event from content-factory."""
        from .models import ContentFactoryJob
        from integrations.services.slack import SlackService

        job_id = data.get('job_id')
        article_url = data.get('article_url')
        pr_url = data.get('pr_url')
        article_title = data.get('article_title', '')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')

        # Update or create job record
        job, created = ContentFactoryJob.objects.update_or_create(
            job_id=job_id,
            defaults={
                'domain': domain,
                'slack_user_id': slack_user_id,
                'status': 'completed',
                'article_url': article_url,
                'pr_url': pr_url,
            }
        )

        # Resolve thread context: job first, then callback payload
        channel_id = (job.slack_channel_id if job else None) or data.get('slack_channel_id') or ''
        thread_ts = (job.slack_thread_ts if job else None) or data.get('slack_thread_ts') or ''

        logger.info(f"Article complete for job {job_id}: pr_url={pr_url}, title={article_title}")

        upsert_live_progress_card(
            job,
            data=data,
            summary_text="Article published and ready for review.",
        )

        if slack_user_id:
            title_line = f"*{article_title}*\n\n" if article_title else ""
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"✅ *Article Published!* for {domain}\n\n"
                            f"{title_line}"
                            f"The article has been generated and a Pull Request is ready.\n\n"
                            f"📄 *<{article_url}|View Article>*\n"
                            f"🔗 *<{pr_url}|View Pull Request>*"
                        )
                    }
                }
            ]
            fallback_text = f"Article generation complete for {domain}!"
            try:
                self._send_job_message(
                    job=job,
                    data=data,
                    slack_user_id=slack_user_id,
                    text=fallback_text,
                    blocks=blocks,
                )
            except Exception as e:
                logger.warning(f"Failed to send article_complete notification to {slack_user_id}: {e}")

        return Response({
            'status': 'received',
            'message': 'Article complete callback processed',
            'job_id': job_id,
        }, status=status.HTTP_200_OK)

    def _handle_error(self, data):
        """Handle error event from content-factory."""
        from .models import ContentFactoryJob
        from integrations.services.slack import SlackService

        job_id = data.get('job_id')
        error_message = data.get('error_message', 'Unknown error')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')

        # Update or create job record
        job, created = ContentFactoryJob.objects.update_or_create(
            job_id=job_id,
            defaults={
                'domain': domain,
                'slack_user_id': slack_user_id,
                'status': 'error',
                'error_message': error_message,
            }
        )

        logger.error(f"Error callback for job {job_id}: {error_message}")

        # Notify user via Slack
        if slack_user_id:
            try:
                blocks = [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": "❌ Content Pipeline Failed",
                            "emoji": True
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"The article generation pipeline encountered an error for *{domain}*.\n\n*Error:* {error_message}"
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "You can try again by requesting a new article."
                        }
                    }
                ]
                from integrations.services.article_generation import get_content_factory_article_cost_points

                refund_points = get_content_factory_article_cost_points(domain)
                if refund_points > 0:
                    blocks.append(
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"If this run failed and you want your {refund_points} Roo points back, "
                                    "message Dr Sam on Slack."
                                )
                            },
                        }
                    )
                self._send_job_message(
                    job=job,
                    data=data,
                    slack_user_id=slack_user_id,
                    text=f"Content pipeline error for {domain}",
                    blocks=blocks,
                )
                logger.info(f"Sent error notification to {slack_user_id} for job {job_id}")
            except Exception as e:
                logger.warning(f"Failed to send error notification to {slack_user_id}: {e}")

        return Response({
            'status': 'received',
            'message': 'Error callback processed',
            'job_id': job_id,
        }, status=status.HTTP_200_OK)


# =============================================================================
# SEO Research API Views
# =============================================================================

from .models import (
    ResearchedKeyword, KeywordVelocity, AISaturation, PAQuestion,
    SemanticCluster, ClusterMembership, TopicMap, WrittenArticle, ResearchSession,
    KeywordStatus
)
from .serializers import (
    ResearchedKeywordListSerializer, ResearchedKeywordDetailSerializer,
    KeywordBulkUpsertSerializer, SemanticClusterSerializer,
    ClusterBulkUpsertSerializer, TopicMapSerializer, WrittenArticleSerializer,
    WrittenArticleCreateSerializer, ResearchSessionSerializer,
    KeywordStatusUpdateSerializer, SEODashboardSerializer,
    ResearchFeedbackSerializer,
)


class SEOKeywordListView(APIView):
    """
    GET /api/seo/keywords/?domain=example.com&status=pending&tier=tier_1_blue_ocean

    List keywords with filtering and sorting.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        domain = request.query_params.get('domain')
        if not domain:
            return Response(
                {'error': 'domain query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            org = Organization.objects.get(domain=domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        qs = ResearchedKeyword.objects.filter(
            organization=org
        ).prefetch_related(
            'velocity_snapshots', 'ai_saturation_snapshots', 'paa_questions'
        )

        # Apply filters
        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)

        tier_filter = request.query_params.get('tier')
        if tier_filter:
            qs = qs.filter(tier=tier_filter)

        source_filter = request.query_params.get('source')
        if source_filter:
            qs = qs.filter(source=source_filter)

        # Sorting
        sort_by = request.query_params.get('sort', '-opportunity_index')
        qs = qs.order_by(sort_by)

        # Limit
        limit = request.query_params.get('limit', 100)
        try:
            limit = int(limit)
        except ValueError:
            limit = 100

        offset = request.query_params.get('offset', 0)
        try:
            offset = int(offset)
        except ValueError:
            offset = 0

        qs = qs[offset:offset + limit]

        serializer = ResearchedKeywordListSerializer(qs, many=True)
        return Response({
            'domain': domain,
            'count': len(serializer.data),
            'keywords': serializer.data
        }, status=status.HTTP_200_OK)


class SEOKeywordDetailView(APIView):
    """
    GET /api/seo/keywords/<uuid>/

    Get detailed keyword data including velocity/saturation history.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, pk):
        try:
            keyword = ResearchedKeyword.objects.prefetch_related(
                'velocity_snapshots', 'ai_saturation_snapshots',
                'paa_questions', 'cluster_memberships__cluster'
            ).get(pk=pk)
        except ResearchedKeyword.DoesNotExist:
            return Response(
                {'error': 'Keyword not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = ResearchedKeywordDetailSerializer(keyword)
        return Response(serializer.data, status=status.HTTP_200_OK)


class SEOKeywordBulkUpsertView(APIView):
    """
    POST /api/seo/keywords/bulk/

    Bulk upsert keywords from content-factory research results.
    This is the main endpoint called by content-factory after research.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        serializer = KeywordBulkUpsertSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        domain = serializer.validated_data['domain']
        keywords_data = serializer.validated_data['keywords']

        try:
            org = Organization.objects.get(domain=domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': f'Organization not found for domain: {domain}'},
                status=status.HTTP_404_NOT_FOUND
            )

        created_count = 0
        updated_count = 0

        with transaction.atomic():
            for kw_data in keywords_data:
                keyword_text = kw_data.get('keyword', '').strip()
                if not keyword_text:
                    continue

                keyword_normalized = keyword_text.lower().strip()

                defaults = {
                    'keyword': keyword_text,
                    'volume': kw_data.get('volume', 0),
                    'difficulty': kw_data.get('difficulty', 50),
                    'intent': kw_data.get('intent', 'informational'),
                    'tier': kw_data.get('tier', 'tier_4_discard'),
                    'opportunity_index': kw_data.get('opportunity_index', 0.0),
                    'source': kw_data.get('source', 'seed'),
                    'source_detail': kw_data.get('source_detail'),
                    'competitor_urls': kw_data.get('competitor_urls', []),
                    'cluster_fingerprint': kw_data.get('cluster_fingerprint', ''),
                }

                keyword_obj, created = ResearchedKeyword.objects.update_or_create(
                    organization=org,
                    keyword_normalized=keyword_normalized,
                    defaults=defaults
                )

                if created:
                    created_count += 1
                else:
                    updated_count += 1

                # Create velocity snapshot if provided
                velocity = kw_data.get('velocity_data') or kw_data.get('velocity')
                if velocity:
                    KeywordVelocity.objects.create(
                        keyword=keyword_obj,
                        absolute_volume=velocity.get('absolute_volume', 0),
                        velocity_score=velocity.get('velocity_score', 0.0),
                        trend_status=velocity.get('trend_status', 'stable'),
                        daily_volumes=velocity.get('daily_volumes', []),
                    )

                # Create AI saturation snapshot if provided
                ai_sat = kw_data.get('ai_saturation')
                if ai_sat:
                    AISaturation.objects.create(
                        keyword=keyword_obj,
                        domain=domain,
                        ai_overview_present=ai_sat.get('ai_overview_present', False),
                        ai_overview_quality=ai_sat.get('ai_overview_quality', 'none'),
                        featured_snippet_present=ai_sat.get('featured_snippet_present', False),
                        video_carousel_present=ai_sat.get('video_carousel_present', False),
                        knowledge_panel_present=ai_sat.get('knowledge_panel_present', False),
                        saturation_score=ai_sat.get('saturation_score', 0.0),
                        hostility_score=ai_sat.get('hostility_score', 0.0),
                        hostility_recommendation=ai_sat.get('hostility_recommendation', 'high_priority'),
                        serp_features=ai_sat.get('serp_features', []),
                    )

                # Create PAA questions if provided
                paa_questions = kw_data.get('paa_questions', [])
                for i, paa in enumerate(paa_questions):
                    question_text = paa.get('question', '').strip()
                    if not question_text:
                        continue
                    PAQuestion.objects.get_or_create(
                        keyword=keyword_obj,
                        question_normalized=question_text.lower().strip()[:500],
                        defaults={
                            'question': question_text,
                            'domain': domain,
                            'answer_snippet': paa.get('answer_snippet', ''),
                            'source_url': paa.get('source_url'),
                            'depth': paa.get('depth', 1),
                            'has_ai_overview': paa.get('has_ai_overview', False),
                            'order': i,
                        }
                    )

        return Response({
            'created': created_count,
            'updated': updated_count,
            'total': len(keywords_data)
        }, status=status.HTTP_200_OK)


class SEOKeywordStatusUpdateView(APIView):
    """
    PATCH /api/seo/keywords/<uuid>/status/

    Update keyword status (pending -> approved -> written, etc.)
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def patch(self, request, pk):
        try:
            keyword = ResearchedKeyword.objects.get(pk=pk)
        except ResearchedKeyword.DoesNotExist:
            return Response(
                {'error': 'Keyword not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = KeywordStatusUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        new_status = serializer.validated_data['status']
        written_article_id = serializer.validated_data.get('written_article_id')

        keyword.status = new_status
        keyword.status_changed_at = timezone.now()

        if written_article_id:
            try:
                article = WrittenArticle.objects.get(pk=written_article_id)
                keyword.written_article = article
            except WrittenArticle.DoesNotExist:
                pass

        keyword.save()

        return Response({
            'id': str(keyword.id),
            'status': keyword.status,
            'updated_at': keyword.status_changed_at
        }, status=status.HTTP_200_OK)


class SEOKeywordResearchFeedbackView(APIView):
    """
    POST /api/seo/keywords/research-feedback/

    Persist research exposure, selection, and temporary rejections without
    changing the keyword lifecycle status.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        serializer = ResearchFeedbackSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        domain = serializer.validated_data['domain']
        shown_keywords = serializer.validated_data.get('shown_keywords', [])
        selected_keyword = serializer.validated_data.get('selected_keyword')
        rejected_keywords = serializer.validated_data.get('rejected_keywords', [])

        try:
            org = Organization.objects.get(domain=domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': f'Organization not found for domain: {domain}'},
                status=status.HTTP_404_NOT_FOUND
            )

        cooldown_days = int(os.environ.get("RESEARCH_TOPIC_COOLDOWN_DAYS", "7"))
        now = timezone.now()
        shown_count = 0
        selected_count = 0
        rejected_count = 0

        with transaction.atomic():
            for keyword_text in shown_keywords:
                keyword = ResearchedKeyword.objects.filter(
                    organization=org,
                    keyword_normalized=keyword_text.lower().strip()
                ).first()
                if not keyword:
                    continue
                keyword.times_shown += 1
                keyword.last_shown_at = now
                keyword.save(update_fields=['times_shown', 'last_shown_at'])
                shown_count += 1

            if selected_keyword:
                keyword = ResearchedKeyword.objects.filter(
                    organization=org,
                    keyword_normalized=selected_keyword.lower().strip()
                ).first()
                if keyword:
                    keyword.times_selected += 1
                    keyword.last_selected_at = now
                    keyword.save(update_fields=['times_selected', 'last_selected_at'])
                    selected_count = 1

            for keyword_text in rejected_keywords:
                keyword = ResearchedKeyword.objects.filter(
                    organization=org,
                    keyword_normalized=keyword_text.lower().strip()
                ).first()
                if not keyword:
                    continue
                keyword.times_rejected += 1
                keyword.last_rejected_at = now
                keyword.cooldown_until = now + timezone.timedelta(days=cooldown_days)
                keyword.save(update_fields=['times_rejected', 'last_rejected_at', 'cooldown_until'])
                rejected_count += 1

        return Response({
            'shown_updated': shown_count,
            'selected_updated': selected_count,
            'rejected_updated': rejected_count,
            'cooldown_days': cooldown_days,
        }, status=status.HTTP_200_OK)


class SEOClusterListView(APIView):
    """
    GET /api/seo/clusters/?domain=example.com

    List semantic clusters for an organization.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        domain = request.query_params.get('domain')
        if not domain:
            return Response(
                {'error': 'domain query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            org = Organization.objects.get(domain=domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        clusters = SemanticCluster.objects.filter(
            organization=org
        ).prefetch_related('member_keywords__keyword')

        serializer = SemanticClusterSerializer(clusters, many=True)
        return Response({
            'domain': domain,
            'count': len(serializer.data),
            'clusters': serializer.data
        }, status=status.HTTP_200_OK)


class SEOClusterBulkUpsertView(APIView):
    """
    POST /api/seo/clusters/bulk/

    Bulk create/update clusters from content-factory topic map.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        serializer = ClusterBulkUpsertSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        domain = serializer.validated_data['domain']
        clusters_data = serializer.validated_data['clusters']

        try:
            org = Organization.objects.get(domain=domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': f'Organization not found for domain: {domain}'},
                status=status.HTTP_404_NOT_FOUND
            )

        created_count = 0
        updated_count = 0

        with transaction.atomic():
            for cluster_data in clusters_data:
                cluster_id = cluster_data.get('cluster_id')
                pillar_keyword = cluster_data.get('pillar_keyword', '')
                member_keywords = cluster_data.get('keywords', [])

                if cluster_id is None:
                    continue

                defaults = {
                    'pillar_keyword': pillar_keyword,
                    'average_similarity': cluster_data.get('average_similarity', 0.0),
                    'total_volume': cluster_data.get('total_volume', 0),
                    'avg_difficulty': cluster_data.get('avg_difficulty', 0.0),
                    'avg_velocity': cluster_data.get('avg_velocity', 0.0),
                    'topic_tier': cluster_data.get('topic_tier', 'tier_4_discard'),
                }

                cluster_obj, created = SemanticCluster.objects.update_or_create(
                    organization=org,
                    cluster_id=cluster_id,
                    defaults=defaults
                )

                if created:
                    created_count += 1
                else:
                    updated_count += 1

                # Link member keywords to cluster
                for kw_text in member_keywords:
                    keyword_normalized = kw_text.lower().strip()
                    try:
                        keyword_obj = ResearchedKeyword.objects.get(
                            organization=org,
                            keyword_normalized=keyword_normalized
                        )
                        ClusterMembership.objects.update_or_create(
                            keyword=keyword_obj,
                            cluster=cluster_obj,
                            defaults={
                                'is_pillar': keyword_normalized == pillar_keyword.lower().strip(),
                            }
                        )
                    except ResearchedKeyword.DoesNotExist:
                        # Keyword not found, skip membership creation
                        pass

        return Response({
            'created': created_count,
            'updated': updated_count,
            'total': len(clusters_data)
        }, status=status.HTTP_200_OK)


class SEOWrittenArticleCreateView(APIView):
    """
    POST /api/seo/articles/

    Create a written article record and update keyword status.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        serializer = WrittenArticleCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        domain = serializer.validated_data['domain']
        primary_keyword = serializer.validated_data['primary_keyword']

        try:
            org = Organization.objects.get(domain=domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': f'Organization not found for domain: {domain}'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Get job reference if provided
        job = None
        job_id = serializer.validated_data.get('job_id')
        if job_id:
            from .models import ContentFactoryJob
            try:
                job = ContentFactoryJob.objects.get(job_id=job_id)
            except ContentFactoryJob.DoesNotExist:
                pass

        defaults = {
            'title': serializer.validated_data['title'],
            'category': serializer.validated_data['category'],
            'primary_keyword': primary_keyword,
            'article_url': serializer.validated_data.get('article_url'),
            'pr_url': serializer.validated_data.get('pr_url'),
            'job': job,
            'published_at': timezone.now(),
        }
        article, created = WrittenArticle.objects.update_or_create(
            organization=org,
            slug=serializer.validated_data['slug'],
            defaults=defaults,
        )

        # Update keyword status to written if it exists
        keyword_normalized = primary_keyword.lower().strip()
        try:
            keyword = ResearchedKeyword.objects.get(
                organization=org,
                keyword_normalized=keyword_normalized
            )
            keyword.status = KeywordStatus.WRITTEN
            keyword.written_article = article
            keyword.status_changed_at = timezone.now()
            keyword.save()
        except ResearchedKeyword.DoesNotExist:
            pass

        return Response({
            'id': str(article.id),
            'slug': article.slug,
            'status': 'created' if created else 'updated'
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class SEODashboardView(APIView):
    """
    GET /api/seo/dashboard/?domain=example.com

    Aggregate dashboard data for SEO research.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        domain = request.query_params.get('domain')
        if not domain:
            return Response(
                {'error': 'domain query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            org = Organization.objects.get(domain=domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        keywords = ResearchedKeyword.objects.filter(organization=org)

        data = {
            'domain': domain,
            'total_keywords': keywords.count(),
            'by_status': {
                'pending': keywords.filter(status='pending').count(),
                'approved': keywords.filter(status='approved').count(),
                'in_progress': keywords.filter(status='in_progress').count(),
                'written': keywords.filter(status='written').count(),
                'skipped': keywords.filter(status='skipped').count(),
            },
            'by_tier': {
                'blue_ocean': keywords.filter(tier='tier_1_blue_ocean').count(),
                'authority': keywords.filter(tier='tier_2_authority').count(),
                'long_tail': keywords.filter(tier='tier_3_long_tail').count(),
                'discard': keywords.filter(tier='tier_4_discard').count(),
            },
            'top_opportunities': ResearchedKeywordListSerializer(
                keywords.filter(status='pending').order_by('-opportunity_index')[:10],
                many=True
            ).data,
            'clusters': SemanticCluster.objects.filter(organization=org).count(),
            'articles_written': WrittenArticle.objects.filter(organization=org).count(),
        }

        return Response(data, status=status.HTTP_200_OK)


class ContentFactoryOrgDomainsView(APIView):
    """
    Return all known organization domains for fuzzy matching.

    GET /api/content-factory/orgs/domains
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        from django.core.cache import cache
        from integrations.utils import normalize_domain

        cache_key = "content_factory_org_domains"
        domains = cache.get(cache_key)

        if domains is None:
            raw_domains = Organization.objects.values_list('domain', flat=True).distinct()
            domains = sorted({normalize_domain(d) for d in raw_domains if d})
            cache.set(cache_key, domains, 300)  # 5-minute cache

        return Response(domains, status=status.HTTP_200_OK)


def _parse_optional_datetime(value):
    from django.utils.dateparse import parse_datetime

    if not value:
        return None
    if hasattr(value, "tzinfo"):
        return value
    return parse_datetime(value)


def _content_factory_run_result_payload(run: ContentFactoryRun) -> dict:
    payload = run.result or {}
    return payload if isinstance(payload, dict) else {}


def _content_factory_run_meta(run: ContentFactoryRun) -> dict:
    meta = _content_factory_run_result_payload(run).get(VALLEY_META_KEY) or {}
    return meta if isinstance(meta, dict) else {}


def _set_content_factory_run_meta(run: ContentFactoryRun, meta: dict) -> None:
    payload = dict(_content_factory_run_result_payload(run))
    payload[VALLEY_META_KEY] = dict(meta or {})
    run.result = payload


def _serialize_content_factory_run(run: ContentFactoryRun) -> dict:
    steps = {}
    for step in run.steps.order_by("display_order", "id"):
        attempts = []
        for attempt in step.attempt_history.order_by("attempt"):
            attempts.append(
                {
                    "attempt": attempt.attempt,
                    "status": attempt.status,
                    "message": attempt.message or None,
                    "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
                    "completed_at": attempt.completed_at.isoformat() if attempt.completed_at else None,
                    "artifacts": attempt.artifacts or [],
                    "error": attempt.error or None,
                    "input_path": attempt.input_path or None,
                    "output_path": attempt.output_path or None,
                    "notes_path": attempt.notes_path or None,
                    "status_path": attempt.status_path or None,
                }
            )
        steps[step.step_key] = {
            "name": step.step_key,
            "required": step.required,
            "status": step.status,
            "attempts": step.attempts,
            "message": step.message or None,
            "started_at": step.started_at.isoformat() if step.started_at else None,
            "completed_at": step.completed_at.isoformat() if step.completed_at else None,
            "artifacts": step.artifacts or [],
            "error": step.error or None,
            "latest_attempt_path": step.latest_attempt_path or None,
            "attempt_history": attempts,
        }
    return {
        "run_id": run.run_id,
        "workflow": run.workflow,
        "domain": run.domain,
        "github_repo": run.github_repo,
        "slack_user_id": run.slack_user_id,
        "status": run.status,
        "current_step": run.current_step,
        "artifact_root": run.artifact_root,
        "step_order": run.step_order or [],
        "acceptance_summary": run.acceptance_summary or {},
        "verification_summary": run.verification_summary or {},
        "approval_state": run.approval_state,
        "resume_available": run.resume_available,
        "error": run.error or None,
        "result": run.result or {},
        "run_request": run.run_request or {},
        "step_states": steps,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
    }


def _is_retryable_sqlite_lock(exc: Exception) -> bool:
    return connection.vendor == "sqlite" and "database is locked" in str(exc).lower()


def _sync_content_factory_run_snapshot(*, run_id: str, data: dict, step_states: dict):
    with transaction.atomic():
        run, created = ContentFactoryRun.objects.update_or_create(
            run_id=run_id,
            defaults={
                "workflow": data["workflow"],
                "domain": data.get("domain") or "",
                "github_repo": data.get("github_repo") or "",
                "slack_user_id": data.get("slack_user_id") or "",
                "status": data["status"],
                "current_step": data.get("current_step") or "",
                "approval_state": data.get("approval_state") or ContentFactoryApprovalState.NOT_REQUIRED,
                "artifact_root": data.get("artifact_root") or "",
                "step_order": data.get("step_order") or [],
                "acceptance_summary": data.get("acceptance_summary") or {},
                "verification_summary": data.get("verification_summary") or {},
                "run_request": data.get("run_request") or {},
                "result": data.get("result") or {},
                "error": data.get("error") or "",
                "resume_available": bool(data.get("resume_available")),
            },
        )

        seen_steps = set()
        ordered_steps = data.get("step_order") or list(step_states.keys())
        for index, step_key in enumerate(ordered_steps):
            state_payload = dict(step_states.get(step_key) or {"name": step_key})
            step, _ = ContentFactoryRunStep.objects.update_or_create(
                run=run,
                step_key=step_key,
                defaults={
                    "display_order": index,
                    "required": bool(state_payload.get("required", True)),
                    "status": state_payload.get("status", ContentFactoryStepStatus.PENDING),
                    "attempts": int(state_payload.get("attempts", 0)),
                    "message": state_payload.get("message") or "",
                    "started_at": _parse_optional_datetime(state_payload.get("started_at")),
                    "completed_at": _parse_optional_datetime(state_payload.get("completed_at")),
                    "error": state_payload.get("error") or "",
                    "latest_attempt_path": state_payload.get("latest_attempt_path") or "",
                    "artifacts": state_payload.get("artifacts") or [],
                },
            )
            seen_steps.add(step_key)

            for attempt_payload in state_payload.get("attempt_history", []):
                ContentFactoryRunStepAttempt.objects.update_or_create(
                    step=step,
                    attempt=int(attempt_payload.get("attempt", 0)),
                    defaults={
                        "status": attempt_payload.get("status", ContentFactoryStepStatus.PENDING),
                        "message": attempt_payload.get("message") or "",
                        "started_at": _parse_optional_datetime(attempt_payload.get("started_at")),
                        "completed_at": _parse_optional_datetime(attempt_payload.get("completed_at")),
                        "artifacts": attempt_payload.get("artifacts") or [],
                        "error": attempt_payload.get("error") or "",
                        "input_path": attempt_payload.get("input_path") or "",
                        "output_path": attempt_payload.get("output_path") or "",
                        "notes_path": attempt_payload.get("notes_path") or "",
                        "status_path": attempt_payload.get("status_path") or "",
                    },
                )

        if seen_steps:
            ContentFactoryRunStep.objects.filter(run=run).exclude(step_key__in=seen_steps).delete()

    return run, created


class ContentFactoryRunView(APIView):
    """
    GET/PUT durable Content Factory run snapshots.

    GET /api/content-factory/runs/<run_id>
    PUT /api/content-factory/runs/<run_id>
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        if not run:
            return Response({"error": "Run not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_content_factory_run(run), status=status.HTTP_200_OK)

    def put(self, request, run_id: str):
        existing_run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        payload = dict(request.data)
        payload["run_id"] = run_id
        serializer = ContentFactoryRunSyncSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        step_states = request.data.get("step_states", {}) or {}

        if (
            existing_run is not None
            and existing_run.status == ContentFactoryRunStatus.CANCELLED
            and data.get("status") != ContentFactoryRunStatus.CANCELLED
        ):
            return Response(
                {
                    "error": "run_cancelled",
                    "detail": "This run was cancelled and cannot accept more workflow updates.",
                    "run_id": run_id,
                    "status": existing_run.status,
                },
                status=status.HTTP_409_CONFLICT,
            )

        max_attempts = 3 if connection.vendor == "sqlite" else 1
        for attempt_number in range(1, max_attempts + 1):
            try:
                run, created = _sync_content_factory_run_snapshot(
                    run_id=run_id,
                    data=data,
                    step_states=step_states,
                )
                break
            except OperationalError as exc:
                if not _is_retryable_sqlite_lock(exc) or attempt_number == max_attempts:
                    raise
                logger.warning(
                    "Retrying Content Factory run sync for %s after SQLite lock (%s/%s).",
                    run_id,
                    attempt_number,
                    max_attempts,
                )
                time.sleep(0.25 * attempt_number)

        response_payload = _serialize_content_factory_run(run)
        response_payload["sync_status"] = "created" if created else "updated"
        return Response(
            response_payload,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ContentFactoryRunValleyJobView(APIView):
    """
    Track active Valley Celery jobs for a durable run.

    POST /api/content-factory/runs/<run_id>/valley-jobs
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        if not run:
            return Response({"error": "Run not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = ContentFactoryRunValleyJobSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        job_id = serializer.validated_data["job_id"]
        transition = serializer.validated_data["transition"]
        reason = serializer.validated_data.get("reason") or ""

        meta = _content_factory_run_meta(run)
        tracked_job_ids = [
            str(item).strip()
            for item in list(meta.get("tracked_job_ids") or [])
            if str(item).strip()
        ]
        if transition in {"queued", "started"}:
            if job_id not in tracked_job_ids:
                tracked_job_ids.append(job_id)
        elif transition == "finished":
            tracked_job_ids = [item for item in tracked_job_ids if item != job_id]

        meta["tracked_job_ids"] = tracked_job_ids
        meta["last_tracked_job_transition"] = {
            "job_id": job_id,
            "transition": transition,
            "reason": reason,
            "recorded_at": timezone.now().isoformat(),
        }
        _set_content_factory_run_meta(run, meta)
        run.save(update_fields=["result", "updated_at"])

        return Response(
            {
                "run_id": run_id,
                "job_id": job_id,
                "transition": transition,
                "tracked_job_ids": tracked_job_ids,
            },
            status=status.HTTP_200_OK,
        )


class ContentFactoryRunPreviewView(View):
    """
    Public signed preview for any run with a stored content package.

    GET /api/content-factory/runs/<run_id>/preview?sig=...
    """

    def get(self, request, run_id: str):
        signature = str(request.GET.get("sig") or "").strip()
        if not signature:
            return HttpResponse(
                render_content_preview_error_page(
                    title="Preview unavailable",
                    message="This preview link is missing its signature.",
                ),
                status=403,
                content_type="text/html; charset=utf-8",
            )

        try:
            validate_content_factory_preview_signature(run_id, signature)
        except signing.SignatureExpired:
            return HttpResponse(
                render_content_preview_error_page(
                    title="Preview link expired",
                    message="This content preview link has expired. Ask Roo to generate a fresh one from Slack.",
                ),
                status=410,
                content_type="text/html; charset=utf-8",
            )
        except signing.BadSignature:
            return HttpResponse(
                render_content_preview_error_page(
                    title="Preview unavailable",
                    message="This content preview link is invalid.",
                ),
                status=403,
                content_type="text/html; charset=utf-8",
            )

        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        if not run:
            return HttpResponse(
                render_content_preview_error_page(
                    title="Preview unavailable",
                    message="The requested content preview could not be found.",
                ),
                status=404,
                content_type="text/html; charset=utf-8",
            )

        content_package = _content_package_from_run(run)
        if not content_package:
            return HttpResponse(
                render_content_preview_error_page(
                    title="Preview unavailable",
                    message="This run does not have a stored content package yet.",
                ),
                status=404,
                content_type="text/html; charset=utf-8",
            )

        return HttpResponse(
            render_content_preview_page(
                domain=run.domain,
                content_package=content_package,
            ),
            content_type="text/html; charset=utf-8",
        )


class ContentFactoryRunArtifactsView(APIView):
    """
    GET artifact manifest for a durable run snapshot.

    GET /api/content-factory/runs/<run_id>/artifacts
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        if not run:
            return Response({"error": "Run not found"}, status=status.HTTP_404_NOT_FOUND)

        payload = _serialize_content_factory_run(run)
        return Response(
            {
                "run_id": payload["run_id"],
                "workflow": payload["workflow"],
                "artifact_root": payload["artifact_root"],
                "steps": payload["step_states"],
            },
            status=status.HTTP_200_OK,
        )


class ContentFactoryRunControlView(APIView):
    """
    Control approval and resume metadata for durable runs.

    POST /api/content-factory/runs/<run_id>/approve
    POST /api/content-factory/runs/<run_id>/deny
    POST /api/content-factory/runs/<run_id>/resume
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str, action: str):
        from integrations.services.article_generation import ArticleGenerationError, publish_article_as_pr
        from .models import ContentFactoryJob

        serializer = ContentFactoryRunControlSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        actor = serializer.validated_data.get("actor") or "content-factory"

        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        job = ContentFactoryJob.objects.filter(job_id=run_id).first()

        if action in {"promote-bundle", "publish-pr"}:
            if not run and not job:
                return Response({"error": "Run not found"}, status=status.HTTP_404_NOT_FOUND)

            try:
                result = publish_article_as_pr(
                    run_id,
                    slack_user_id=(job.slack_user_id if job else None),
                    requested_by_slack_user_id=(
                        str(((job.request_meta or {}) if job else {}).get("requested_by_slack_user_id") or "").strip()
                        or None
                    ),
                    domain=((job.domain if job else None) or (run.domain if run else None)),
                    slack_channel_id=(job.slack_channel_id if job else ""),
                    slack_thread_ts=(job.slack_thread_ts if job else ""),
                    slack_root_message_ts=(job.slack_root_message_ts if job else ""),
                )
                return Response(result, status=status.HTTP_200_OK)
            except ArticleGenerationError as exc:
                return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)

        if not run:
            return Response({"error": "Run not found"}, status=status.HTTP_404_NOT_FOUND)

        if action == "approve":
            run.approval_state = ContentFactoryApprovalState.APPROVED
            run.status = ContentFactoryRunStatus.RUNNING
        elif action == "deny":
            run.approval_state = ContentFactoryApprovalState.DENIED
            run.status = ContentFactoryRunStatus.DENIED
        elif action == "resume":
            run.resume_available = True
            if run.status in {
                ContentFactoryRunStatus.FAILED,
                ContentFactoryRunStatus.BLOCKED,
                ContentFactoryRunStatus.DENIED,
            }:
                run.status = ContentFactoryRunStatus.QUEUED
        else:
            return Response({"error": "Unsupported action"}, status=status.HTTP_400_BAD_REQUEST)

        run.save(update_fields=["approval_state", "status", "resume_available", "updated_at"])
        return Response(
            {
                "run_id": run_id,
                "action": action,
                "actor": actor,
                "status": run.status,
                "approval_state": run.approval_state,
                "resume_available": run.resume_available,
            },
            status=status.HTTP_200_OK,
        )
