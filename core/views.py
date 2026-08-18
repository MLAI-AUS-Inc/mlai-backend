import logging
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlsplit
from django.contrib.auth import get_user_model, login as auth_login, logout as auth_logout
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.views import APIView
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from django.views.decorators.csrf import csrf_exempt

from .auth_cookies import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    clear_auth_cookie,
    clear_auth_cookies,
    clear_django_session_cookies,
    cookie_kwargs,
    set_auth_cookies,
)
from .email_utils import (
    MAGIC_LINK_KIND_USER,
    generate_magic_link,
    send_magic_link_email,
    verify_magic_link,
)
from .refresh_sessions import (
    RefreshRevocationUnavailable,
    issue_refresh_token,
    revoke_refresh_credential,
)
from .models import Hackathon, SlackFounderAccountLink
from .serializers import (
    HackathonSerializer,
    MyTokenObtainPairSerializer,
    RevocableTokenRefreshSerializer,
    UserSerializer,
)
from rest_framework.generics import ListAPIView, RetrieveAPIView, RetrieveUpdateAPIView
from .permissions import (
    HasAPIKey,
    HasRooApiKey,
    HasStrictRooApiKey,
    IsOwnerOrTeammateOrSuperuser,
)
from .slack_founder_links import (
    SlackFounderLinkError,
    complete_slack_founder_link,
    create_slack_founder_link_request,
    founder_account_connection_status,
    preview_slack_founder_link,
)
from .throttles import AuthEndpointRateThrottle, MagicLinkSendRateThrottle
from .user_compat import DEFAULT_USER_ROLE, get_compat_user_role, user_has_team
from .slack_users import resolve_existing_user_from_profile

User = get_user_model()
logger = logging.getLogger(__name__)

ALLOWED_PERSONAS = {"hacker", "hustler", "hipster", "healer"}
MEDHACK_TEAM_MIN_MEMBERS = 2
MEDHACK_TEAM_MAX_MEMBERS = 6
HEALTHHACK_ADMIN_ONLY_MESSAGE = "HealthHack has closed. Administrator access only."
OPERATIONS_ADMIN_ONLY_MESSAGE = "MLAI Operations administrator access only."
OPERATIONS_FRONTEND_ORIGIN = "https://ops.mlai.au"

# Reject encoded slash and backslash variants, including repeatedly encoded
# values such as ``%252f``. A browser or frontend router may decode these at a
# later hop and reinterpret the result as a scheme-relative URL.
ENCODED_PATH_SEPARATOR_RE = re.compile(r"%(?:25)*(?:2f|5c)", re.IGNORECASE)

APP_CONTEXT_ALIASES = {
    "medhack": "hospital",
    "hospital": "hospital",
    "esafety": "esafety",
    "e-safety": "esafety",
    "watt-the-hack": "watt-the-hack",
    "watt_the_hack": "watt-the-hack",
    "wattthehack": "watt-the-hack",
    "founder-tools": "founder-tools",
    "founder_tools": "founder-tools",
    "foundertools": "founder-tools",
    "vibe-marketing": "founder-tools",
    "vibe_marketing": "founder-tools",
    "vibe-raising": "founder-tools",
    "vibe_raising": "founder-tools",
    "viberaising": "founder-tools",
    "content-factory": "content-factory",
    "content_factory": "content-factory",
    "contentfactory": "content-factory",
    "community-chat": "community-chat",
    "community_chat": "community-chat",
    "chat": "community-chat",
    "admin": "admin",
}


def _team_member_payload_from_values(member):
    return {
        "full_name": f"{member['first_name']} {member['last_name']}".strip(),
        "avatar_url": member["avatar_url"],
        "role": DEFAULT_USER_ROLE,
    }


def _active_hospital_team(user):
    manager = getattr(user, 'hospital_teams', None)
    if manager is None:
        return None
    return manager.filter(round__status='active').first()


def _normalize_app_context(app_value, default='hospital'):
    app = str(app_value or '').strip().lower()
    if not app:
        return default
    return APP_CONTEXT_ALIASES.get(app)


def _unsupported_app_response():
    return Response({"error": "Unsupported app."}, status=status.HTTP_400_BAD_REQUEST)


def _healthhack_admin_only_requested(data, app):
    raw_value = data.get('healthhack_admin_only')
    requested = raw_value is True or str(raw_value or '').strip().lower() in {
        '1', 'true', 'yes', 'on',
    }
    return app == 'hospital' and requested


def _healthhack_admin_only_response():
    return Response(
        {"detail": HEALTHHACK_ADMIN_ONLY_MESSAGE},
        status=status.HTTP_403_FORBIDDEN,
    )


def _is_operations_admin(user):
    if user is None or not getattr(user, 'is_active', False):
        return False

    # Keep the operations-login entitlement identical to the flag returned by
    # /auth/me/. The local import avoids coupling core module initialisation to
    # the Roo app's model imports.
    from roo.permissions import is_points_admin_user

    return is_points_admin_user(user)


def _operations_admin_only_response():
    return Response(
        {"detail": OPERATIONS_ADMIN_ONLY_MESSAGE},
        status=status.HTTP_403_FORBIDDEN,
    )


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


def _normalize_operations_next_path(next_path):
    """Return an operations-app path/query or ``None`` when it is unsafe.

    The operations callback origin is fixed separately. This validator only
    accepts an absolute-path reference beginning with exactly one slash, with
    an optional query string. It intentionally rejects fragments, raw or
    encoded backslashes, scheme-relative paths, controls, and encoded path
    separators so downstream URL decoding cannot turn a relative target into
    a cross-origin redirect.
    """
    if next_path is None:
        return None

    raw_value = str(next_path)
    if not raw_value or raw_value != raw_value.strip():
        return None
    if not raw_value.startswith('/') or raw_value.startswith('//'):
        return None
    if '\\' in raw_value or ENCODED_PATH_SEPARATOR_RE.search(raw_value):
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in raw_value):
        return None

    decoded_value = raw_value
    for _ in range(5):
        parsed = urlsplit(decoded_value)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or not parsed.path.startswith('/')
            or parsed.path.startswith('//')
            or '\\' in decoded_value
            or any(ord(character) < 32 or ord(character) == 127 for character in decoded_value)
        ):
            return None

        next_decoded_value = unquote(decoded_value)
        if next_decoded_value == decoded_value:
            break
        decoded_value = next_decoded_value
    else:
        # Do not accept a value that still changes after several decoding
        # passes. Legitimate route paths have no reason to be nested this way.
        return None

    return raw_value


def _normalize_next_path_for_app(next_path, app):
    if app == 'admin':
        return _normalize_operations_next_path(next_path)
    return _normalize_next_path(next_path)


def _invalid_operations_next_path(next_path, normalized_next_path, app):
    return app == 'admin' and next_path not in (None, '') and normalized_next_path is None


def _invalid_next_path_response():
    return Response({"error": "Invalid next path."}, status=status.HTTP_400_BAD_REQUEST)


def _append_auth_query_params(magic_link, app, next_path=None):
    parsed = urlparse(str(magic_link))
    query_params = parse_qsl(parsed.query, keep_blank_values=True)
    query_params.append(("app", app))
    if next_path:
        query_params.append(("next", next_path))
    return parsed._replace(query=urlencode(query_params, safe="/")).geturl()


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
    if app_context == 'watt-the-hack':
        return _origin_from_url(getattr(settings, 'WATT_THE_HACK_URL', None), default_origin)
    if app_context == 'founder-tools':
        return _origin_from_url(
            getattr(settings, 'FOUNDER_TOOLS_URL', None)
            or getattr(settings, 'VIBE_RAISING_URL', None),
            default_origin,
        )
    if app_context == 'content-factory':
        return _origin_from_url(getattr(settings, 'CONTENT_FACTORY_FRONTEND_URL', None), default_origin)
    if app_context == 'community-chat':
        return _origin_from_url(getattr(settings, 'COMMUNITY_CHAT_FRONTEND_URL', None), default_origin)
    if app_context == 'admin':
        # ``admin`` is the stable legacy app-context identifier. Its browser
        # destination is deliberately not configurable from request data or an
        # environment variable: the operations application lives at this exact
        # origin during the Plane domain transition.
        return OPERATIONS_FRONTEND_ORIGIN
    return default_origin


class CheckUserView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    # check-user is an intentional existence oracle the passwordless login flow
    # depends on (branch to signup vs magic link). It cannot be flattened without
    # breaking that UX, so the mitigation is rate limiting the enumeration.
    throttle_classes = [AuthEndpointRateThrottle]

    def post(self, request):
        data = request.data
        email = data.get('email')
        app = _normalize_app_context(data.get('app'), default='hospital')
        if app is None:
            return _unsupported_app_response()

        if not email:
            return Response({"error": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(email__iexact=email).first()
        if app == 'admin' and not _is_operations_admin(user):
            return _operations_admin_only_response()
        if _healthhack_admin_only_requested(data, app) and not (
            user and user.is_active and user.is_superuser
        ):
            return _healthhack_admin_only_response()

        return Response(
            {"user_exists": user is not None},
            status=status.HTTP_200_OK,
        )

class SendMagicLinkView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [MagicLinkSendRateThrottle]

    # Generic response returned regardless of whether the account exists, so this
    # endpoint is not an enumeration oracle. The login frontend only reads
    # `magic_link_sent`/`message` here (it branches to signup off the separate
    # check-user endpoint), so a constant response is safe for that flow.
    _GENERIC_RESPONSE = {
        "magic_link_sent": True,
        "message": "If an account exists for this email, a magic link has been sent.",
    }

    def post(self, request):
        data = request.data
        email = data.get('email')
        app = _normalize_app_context(data.get('app'), default='hospital')
        if app is None:
            return _unsupported_app_response()
        requested_next_path = data.get('next')
        next_path = _normalize_next_path_for_app(requested_next_path, app)
        if _invalid_operations_next_path(requested_next_path, next_path, app):
            return _invalid_next_path_response()

        if not email:
            return Response({"error": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.filter(email__iexact=email).first()

            # Admin-surface gates are preserved. They gate on privilege, not on
            # existence, and return an identical response for every non-admin
            # (existent or not), so they remain non-enumerating.
            if app == 'admin' and not _is_operations_admin(user):
                return _operations_admin_only_response()

            if _healthhack_admin_only_requested(data, app) and not (
                user and user.is_active and user.is_superuser
            ):
                return _healthhack_admin_only_response()

            # Only send when the account exists, but return an identical generic
            # response either way so callers cannot distinguish the two cases.
            if user:
                base_url = _frontend_base_url(app)
                magic_link = generate_magic_link(user, base_url=base_url)
                magic_link = _append_auth_query_params(magic_link, app, next_path=next_path)
                # A magic link is a bearer credential. Never emit the link or its
                # signed token—or the destination email—into application logs.
                logger.info("Generated magic link for user_id=%s app=%s", user.id, app)
                send_magic_link_email(user, magic_link, message_id="2")
                logger.info("Sent magic link to existing user_id=%s app=%s", user.id, app)
            else:
                logger.info("send-magic-link requested for non-existent email (suppressed) app=%s", app)

            return Response(self._GENERIC_RESPONSE, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception(f"Error in SendMagicLinkView: {str(e)}")
            return Response({"error": "An error occurred while processing your request."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CreateUserView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [AuthEndpointRateThrottle]

    def post(self, request):
        data = request.data
        email = data.get('email')
        first_name = data.get('firstName') or data.get('first_name') or ''
        last_name = data.get('lastName') or data.get('last_name') or ''
        phone = data.get('phone')
        app = _normalize_app_context(data.get('app'), default='hospital')
        if app is None:
            return _unsupported_app_response()
        if app == 'admin':
            # Operations access is provisioned out of band. Never let the
            # public signup endpoint manufacture an operations identity.
            return _operations_admin_only_response()
        next_path = _normalize_next_path(data.get('next'))

        if not email:
            return Response({"error": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)

        if _healthhack_admin_only_requested(data, app):
            return _healthhack_admin_only_response()

        try:
            with transaction.atomic():
                if User.objects.filter(email__iexact=email).exists():
                    return Response(
                        {"error": "User with this email already exists."}, 
                        status=status.HTTP_400_BAD_REQUEST
                    )

                user = User.objects.create_user(
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    phone=phone
                )
                user.is_active = False # Require verification
                user.save()
                
                logger.info("Created new user_id=%s", user.id)

            # Generate magic link and send email OUTSIDE the transaction
            # so if email fails, user is still created.
            
            base_url = _frontend_base_url(app)

            magic_link = generate_magic_link(user, base_url=base_url)
            magic_link = _append_auth_query_params(magic_link, app, next_path=next_path)
            magic_link_sent = False
            try:
                send_magic_link_email(user, magic_link, message_id="2")
                logger.info("Sent magic link to new user_id=%s app=%s", user.id, app)
                message = "Account created. Check your email for the magic link to sign in."
                magic_link_sent = True
            except Exception as e:
                logger.error(
                    "Failed to send magic link for user_id=%s error_type=%s",
                    user.id,
                    e.__class__.__name__,
                )
                # The magic link is intentionally NOT returned to the client; the user
                # must use the link emailed to them. Surface a failure so the frontend
                # can prompt a resend.
                message = "Account created, but we couldn't send the email. Please use Resend to try again."

            return Response(
                {
                    "id": user.id,
                    "email": user.email,
                    "full_name": user.full_name,
                    "message": message,
                    "magic_link_sent": magic_link_sent,
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
    throttle_classes = [AuthEndpointRateThrottle]

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

                app_param = _normalize_app_context(request.query_params.get('app'), default='hospital')
                if app_param is None:
                    return _unsupported_app_response()
                requested_next_path = request.query_params.get('next')
                next_param = _normalize_next_path_for_app(requested_next_path, app_param)
                if _invalid_operations_next_path(requested_next_path, next_param, app_param):
                    return _invalid_next_path_response()

                user = User.objects.get(email__iexact=email)
                logger.info("Verified magic link for existing user_id=%s", user.id)

                if app_param == 'admin' and not _is_operations_admin(user):
                    logger.warning(
                        "Rejected MLAI Operations login for non-admin user: %s",
                        email,
                    )
                    return _operations_admin_only_response()

                if app_param == 'hospital' and not (
                    user.is_active and user.is_superuser
                ):
                    logger.warning(
                        "Rejected closed HealthHack login for non-admin user_id=%s",
                        user.id,
                    )
                    return _healthhack_admin_only_response()

                if not user.is_active:
                    user.is_active = True
                    user.save()
                    logger.info("Activated user account user_id=%s", user.id)

                auth_login(
                    request._request,
                    user,
                    backend="django.contrib.auth.backends.ModelBackend",
                )

                # Generate JWT tokens
                refresh = issue_refresh_token(user)
                access_token = str(refresh.access_token)
                refresh_token = str(refresh)

                # Build the response payload
                # Determine next_url based on app context
                base_url = _frontend_base_url(app_param)

                # Construct app-aware redirect path
                if next_param:
                    redirect_path = next_param
                elif app_param == 'esafety':
                    redirect_path = "/esafety/dashboard"
                elif app_param == 'founder-tools':
                    redirect_path = "/founder-tools"
                elif app_param == 'watt-the-hack':
                    redirect_path = "/watt-the-hack/dashboard"
                elif app_param == 'content-factory':
                    redirect_path = "/content-factory"
                elif app_param == 'admin':
                    redirect_path = "/"
                elif app_param == 'community-chat':
                    redirect_path = "/"
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
                        'role': get_compat_user_role(user),
                        'is_superuser': user.is_superuser,
                        'is_active': user.is_active,
                        'has_team': user_has_team(user),
                        'avatar_url': user.avatar_url,
                    },
                    'redirect': redirect_path,
                    'next_url': next_url, 
                }

                response = Response(response_data, status=status.HTTP_200_OK)

                # Cookie lifetimes track the SIMPLE_JWT token lifetimes (see core.auth_cookies),
                # so the refresh cookie survives as long as the refresh token itself does.
                set_auth_cookies(
                    response,
                    access_token=access_token,
                    refresh_token=refresh_token,
                )

                logger.info("Set authentication cookies for user_id=%s", user.id)
                return response

            except User.DoesNotExist:
                logger.warning("Magic link referenced a user that no longer exists")
                return Response({"error": "User does not exist."}, status=status.HTTP_400_BAD_REQUEST)
        else:
            logger.warning("Invalid or expired magic link token.")
            return Response({"error": "Invalid or expired token."}, status=status.HTTP_400_BAD_REQUEST)

class MyTokenObtainPairView(TokenObtainPairView):
    serializer_class = MyTokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        set_auth_cookies(
            response,
            access_token=response.data.get('access'),
            refresh_token=response.data.get('refresh'),
        )
        # Remove tokens from response body
        response.data = {}
        return response

class CookieTokenRefreshView(TokenRefreshView):
    """Mint a fresh access token from the refresh cookie, with no user interaction.

    This is what keeps a login alive across days: callers (the website worker's
    session-refresh middleware, or the browser keepalive) POST here with the auth
    cookies attached and get back a refreshed pair. With ROTATE_REFRESH_TOKENS on,
    the serializer also returns a new refresh token, which slides the long window
    forward — so an active user never has to request a new magic link.
    """

    serializer_class = RevocableTokenRefreshSerializer

    def post(self, request, *args, **kwargs):
        refresh_token = request.COOKIES.get(REFRESH_COOKIE)
        if not refresh_token:
            # 401 (not 400) so callers can treat "no session" and "dead session"
            # identically: both mean "fall through to the login flow".
            return Response(
                {'error': 'Refresh token not found in cookies'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        serializer = self.get_serializer(data={'refresh': refresh_token})
        try:
            serializer.is_valid(raise_exception=True)
        except Exception:
            logger.info("Refresh rejected: expired or invalid refresh cookie")
            # Clear both cookies so an expired session stops re-attempting the
            # refresh on every single page load.
            response = Response(
                {'error': 'Invalid or expired refresh token'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
            return clear_auth_cookies(response)

        data = serializer.validated_data
        response = Response({'refreshed': True}, status=status.HTTP_200_OK)
        return set_auth_cookies(
            response,
            access_token=data.get('access'),
            # Present only when ROTATE_REFRESH_TOKENS is enabled.
            refresh_token=data.get('refresh'),
        )

class CurrentUserView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        user = request.user
        if not user.is_authenticated:
            response = Response({'error': 'Not authenticated'}, status=status.HTTP_401_UNAUTHORIZED)
            response._has_been_logged = True
            return response
        
        # Retrieve hospital team
        hospital_team = _active_hospital_team(user)
        hospital_team_data = None
        if hospital_team:
            members = hospital_team.members.all().values("first_name", "last_name", "avatar_url")
            hospital_team_data = {
                "team_name": hospital_team.team_name,
                "team_id": hospital_team.team_id,
                "avatar_url": hospital_team.avatar_url,
                "member_count": hospital_team.members.count(),
                "is_valid_team_size": MEDHACK_TEAM_MIN_MEMBERS <= hospital_team.members.count() <= MEDHACK_TEAM_MAX_MEMBERS,
                "members": [_team_member_payload_from_values(m) for m in members]
            }

        # Retrieve esafety team
        esafety_team = user.esafety_teams.first() if hasattr(user, 'esafety_teams') else None
        esafety_team_data = None
        if esafety_team:
            members = esafety_team.members.all().values("first_name", "last_name", "avatar_url")
            esafety_team_data = {
                "team_name": esafety_team.team_name,
                "team_id": esafety_team.team_id,
                "avatar_url": esafety_team.avatar_url,
                "members": [_team_member_payload_from_values(m) for m in members]
            }
        
        # Determine primary team for backward compatibility (prefer hospital)
        primary_team_data = hospital_team_data or esafety_team_data

        # PointsAdmin-based admin flag, consumed by the founder-tools / Vibe
        # Raising frontend to gate the admin dashboard. Resolves from
        # PointsAdmin.user (Django superusers always count). Local import keeps
        # core from depending on the roo feature app at module load.
        from roo.permissions import is_points_admin_user

        data = {
            'id': user.id,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'full_name': user.full_name,
            'email': user.email,
            'phone': user.phone,
            'about': user.about,
            'role': get_compat_user_role(user),
            'is_superuser': user.is_superuser,
            'is_vibe_raising_admin': is_points_admin_user(user),
            'has_team': user_has_team(user),
            'team': primary_team_data,  # Backward compatibility
            'hospital_team': hospital_team_data,
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
        app_context = _normalize_app_context(request.data.get('app'), default='')
        if request.data.get('app') and app_context is None:
            return _unsupported_app_response()

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
            if app_context == 'hospital':
                team = _active_hospital_team(user)
            elif app_context == 'esafety':
                team = user.esafety_teams.first()
            elif app_context == 'watt-the-hack':
                try:
                    from generic_hackathons.models import GenericHackathonTeam

                    team = GenericHackathonTeam.objects.filter(
                        hackathon__slug='watt-the-hack',
                        members=user,
                    ).first()
                except Exception as e:
                    logger.error(f"Error resolving Watt The Hack team avatar target: {e}")
                    team = None
            else:
                team = (
                    _active_hospital_team(user)
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
        hospital_team = _active_hospital_team(user)
        hospital_team_data = None
        if hospital_team:
            members = hospital_team.members.all().values("first_name", "last_name", "avatar_url")
            hospital_team_data = {
                "team_name": hospital_team.team_name,
                "team_id": hospital_team.team_id,
                "avatar_url": hospital_team.avatar_url,
                "member_count": hospital_team.members.count(),
                "is_valid_team_size": MEDHACK_TEAM_MIN_MEMBERS <= hospital_team.members.count() <= MEDHACK_TEAM_MAX_MEMBERS,
                "members": [_team_member_payload_from_values(m) for m in members]
            }

        esafety_team = user.esafety_teams.first()
        esafety_team_data = None
        if esafety_team:
            members = esafety_team.members.all().values("first_name", "last_name", "avatar_url")
            esafety_team_data = {
                "team_name": esafety_team.team_name,
                "team_id": esafety_team.team_id,
                "avatar_url": esafety_team.avatar_url,
                "members": [_team_member_payload_from_values(m) for m in members]
            }

        primary_team_data = hospital_team_data or esafety_team_data

        data = {
            'id': user.id,
            'full_name': user.full_name,
            'email': user.email,
            'phone': user.phone,
            'about': user.about,
            'role': get_compat_user_role(user),
            'is_superuser': user.is_superuser,
            'team': primary_team_data,
            'hospital_team': hospital_team_data,
            'esafety_team': esafety_team_data,
            'has_team': user_has_team(user),
            'avatar_url': user.avatar_url,
            'personas': user.personas,
        }

        return Response(data, status=status.HTTP_200_OK)

@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def logout_view(request):
    """Revoke the refresh family and invalidate the Django/browser session.

    Logout authenticates with the refresh cookie instead of the access token so
    it remains usable after the short-lived access credential expires. Browser
    state is cleared even for missing/invalid credentials, but only a confirmed
    shared-cache revocation receives a success response.
    """
    raw_refresh_token = request.COOKIES.get(REFRESH_COOKIE)
    preserve_refresh_for_retry = False

    if not raw_refresh_token:
        response = Response(
            {'error': 'Valid refresh credential required.'},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    else:
        try:
            revoke_refresh_credential(raw_refresh_token)
        except TokenError:
            logger.info("Logout rejected an invalid or expired refresh credential")
            response = Response(
                {'error': 'Valid refresh credential required.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except RefreshRevocationUnavailable:
            logger.exception("Logout could not persist refresh-session revocation")
            preserve_refresh_for_retry = True
            response = Response(
                {'error': 'Logout revocation is temporarily unavailable.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        else:
            response = Response(
                {
                    'message': 'Logged out successfully',
                    'refresh_revoked': True,
                },
                status=status.HTTP_200_OK,
            )

    try:
        # django.contrib.auth.logout flushes the server-side session row and
        # rotates the request session key, rather than only deleting a cookie.
        auth_logout(request._request)
    except Exception:
        logger.exception("Logout could not invalidate the Django session")
        if not preserve_refresh_for_retry:
            response = Response(
                {'error': 'Logout session invalidation failed.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    # A 503 means the refresh family may still be live in another browser or a
    # copied token. Preserve only that refresh credential so this browser can
    # retry revocation after Redis/Valkey recovers. Access and Django session
    # state are still cleared immediately. Terminal 200/401 outcomes clear both
    # JWT cookies.
    if preserve_refresh_for_retry:
        clear_auth_cookie(response, ACCESS_COOKIE)
    else:
        clear_auth_cookies(response)
    clear_django_session_cookies(response)
    return response

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


class LinkSlackView(APIView):
    """
    Link a Slack ID to an existing user found by email.
    Path: POST /api/v1/users/link-slack/
    """
    permission_classes = [HasStrictRooApiKey]

    def post(self, request):
        slack_id = str(request.data.get('slack_id') or '').strip()
        email = User.objects.normalize_email(request.data.get('email'))

        if not slack_id or not email:
            return Response(
                {
                    "code": "invalid_slack_link_request",
                    "error": "slack_id and email are required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # An existing Slack link is authoritative and idempotent. Never move
        # that Slack identity to a different account based on a later email.
        user = User.objects.filter(slack_id=slack_id).first()
        if user:
            return Response(
                {"user_id": user.id, "linked": True, "already_linked": True},
                status=status.HTTP_200_OK,
            )

        user = resolve_existing_user_from_profile(
            slack_user_id=slack_id,
            profile={"slack_id": slack_id, "email": email},
        )
        if user:
            return Response(
                {"user_id": user.id, "linked": True},
                status=status.HTTP_200_OK,
            )

        if not User.objects.filter(email__iexact=email).exists():
            return Response(
                {
                    "code": "slack_account_not_found",
                    "error": "User not found by email",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                "code": "slack_identity_conflict",
                "error": "That MLAI account is already linked to another Slack identity",
            },
            status=status.HTTP_409_CONFLICT,
        )


def _slack_founder_link_error_response(exc):
    logger.info("slack_founder_link_rejected code=%s", exc.code)
    return Response(
        {"code": exc.code, "error": str(exc)},
        status=exc.status_code,
    )


class SlackFounderLinkStartView(APIView):
    authentication_classes = []
    permission_classes = [HasStrictRooApiKey]

    def post(self, request):
        slack_user_id = str(request.data.get("slack_user_id") or "").strip()
        if not slack_user_id:
            return Response(
                {
                    "code": "invalid_request",
                    "error": "slack_user_id is required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        slack_user = User.objects.filter(slack_id=slack_user_id).first()
        if slack_user is None:
            return Response(
                {
                    "code": "slack_user_not_found",
                    "error": "Ask Roo to register your Slack account before linking.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        if SlackFounderAccountLink.objects.filter(slack_user=slack_user).exists():
            return Response(
                {"status": "already_linked"},
                status=status.HTTP_200_OK,
            )

        link_request, raw_token = create_slack_founder_link_request(slack_user)
        base_url = _frontend_base_url("founder-tools").rstrip("/")
        link_url = (
            f"{base_url}/founder-tools/link-roo?"
            f"{urlencode({'token': raw_token})}"
        )
        return Response(
            {
                "status": "link_required",
                "link_url": link_url,
                "expires_at": link_request.expires_at.isoformat(),
            },
            status=status.HTTP_201_CREATED,
        )


class SlackFounderLinkStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            founder_account_connection_status(request.user),
            status=status.HTTP_200_OK,
        )


class SlackFounderLinkPreviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            preview = preview_slack_founder_link(
                request.data.get("token"),
                founder_user=request.user,
            )
        except SlackFounderLinkError as exc:
            return _slack_founder_link_error_response(exc)

        slack_display_name = (
            preview.request.slack_user.full_name
            or "Your Roo Slack account"
        )
        return Response(
            {
                "status": preview.status,
                "slack_display_name": slack_display_name,
                "expires_at": preview.request.expires_at.isoformat(),
            },
            status=status.HTTP_200_OK,
        )


class SlackFounderLinkCompleteView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            _, created = complete_slack_founder_link(
                request.data.get("token"),
                founder_user=request.user,
            )
        except SlackFounderLinkError as exc:
            return _slack_founder_link_error_response(exc)

        return Response(
            {"status": "linked" if created else "already_linked"},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


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
# Compatibility re-exports for callers that still import Content Factory views
# from core.views during the app split transition.
from content_factory.service_views import (
    ContentFactoryCallbackView,
    ContentFactoryComponentDetailView,
    ContentFactoryComponentsView,
    ContentFactoryConnectGitHubView,
    ContentFactoryGitHubReconnectView,
    ContentFactoryGitHubStatusView,
    ContentFactoryHealingRecordView,
    ContentFactoryOAuthInitiateView,
    ContentFactoryOrgConfigView,
    ContentFactoryOrgDomainsView,
    ContentFactoryRunArtifactsView,
    ContentFactoryRunControlView,
    ContentFactoryRunPreviewView,
    ContentFactoryRunValleyJobView,
    ContentFactoryRunView,
    ContentFactoryTokenView,
    ScheduledDiscoveryReplayView,
    SEOClusterBulkUpsertView,
    SEOClusterListView,
    SEODashboardView,
    SEOKeywordBulkUpsertView,
    SEOKeywordDetailView,
    SEOKeywordListView,
    SEOKeywordResearchFeedbackView,
    SEOKeywordStatusUpdateView,
    SEOWrittenArticleCreateView,
)
from workflow_runs.models import ContentFactoryRun
