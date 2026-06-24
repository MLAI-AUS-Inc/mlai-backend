import logging
from urllib.parse import parse_qsl, urlencode, urlparse
from django.contrib.auth import get_user_model, login as auth_login
from django.db import transaction
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

from .serializers import MyTokenObtainPairSerializer
from .email_utils import (
    MAGIC_LINK_KIND_USER,
    generate_magic_link,
    send_magic_link_email,
    verify_magic_link,
)
from .models import Hackathon
from .serializers import MyTokenObtainPairSerializer, HackathonSerializer, UserSerializer
from rest_framework.generics import ListAPIView, RetrieveAPIView, RetrieveUpdateAPIView
from .permissions import IsOwnerOrTeammateOrSuperuser, HasAPIKey, HasRooApiKey
from .user_compat import DEFAULT_USER_ROLE, get_compat_user_role, user_has_team

User = get_user_model()
logger = logging.getLogger(__name__)

ALLOWED_PERSONAS = {"hacker", "hustler", "hipster", "healer"}
MEDHACK_TEAM_MIN_MEMBERS = 2
MEDHACK_TEAM_MAX_MEMBERS = 6

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
    "admin": "admin",
}


def _team_member_payload_from_values(member):
    return {
        "full_name": f"{member['first_name']} {member['last_name']}".strip(),
        "avatar_url": member["avatar_url"],
        "role": DEFAULT_USER_ROLE,
    }


def _normalize_app_context(app_value, default='hospital'):
    app = str(app_value or '').strip().lower()
    if not app:
        return default
    return APP_CONTEXT_ALIASES.get(app)


def _unsupported_app_response():
    return Response({"error": "Unsupported app."}, status=status.HTTP_400_BAD_REQUEST)


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
    if app_context == 'admin':
        return _origin_from_url(getattr(settings, 'ADMIN_FRONTEND_URL', None), default_origin)
    return default_origin


class CheckUserView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        data = request.data
        email = data.get('email')
        app = _normalize_app_context(data.get('app'), default='hospital')
        if app is None:
            return _unsupported_app_response()

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
        if app is None:
            return _unsupported_app_response()
        
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
        app = _normalize_app_context(data.get('app'), default='hospital')
        next_path = _normalize_next_path(data.get('next'))
        if app is None:
            return _unsupported_app_response()

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
            magic_link_sent = False
            try:
                send_magic_link_email(user, magic_link, message_id="2")
                logger.info(f"Sent magic link to new user: {email} for app {app}")
                message = "Account created. Check your email for the magic link to sign in."
                magic_link_sent = True
            except Exception as e:
                logger.error(f"Failed to send magic link to {email}: {e}")
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
                next_param = _normalize_next_path(request.query_params.get('next'))
                
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
        cookie_domain = None if not is_production else '.mlai.au'
        response.set_cookie(
            key='access_token',
            value=access_token,
            httponly=True,
            domain=cookie_domain,
            secure=is_production,
            samesite='None' if is_production else 'Lax',
        )
        response.set_cookie(
            key='refresh_token',
            value=refresh_token,
            httponly=True,
            domain=cookie_domain,
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
        cookie_domain = None if not is_production else '.mlai.au'
        response.set_cookie(
            key='access_token',
            value=access_token,
            httponly=True,
            domain=cookie_domain,
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
                team = user.hospital_teams.first()
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
                    user.hospital_teams.first()
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
