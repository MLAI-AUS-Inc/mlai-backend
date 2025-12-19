import logging
from django.db import transaction
from django.contrib.auth import get_user_model
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
from .email_utils import generate_magic_link, send_magic_link_email, verify_magic_link
from .models import Hackathon
from esafety.models import Team as EsafetyTeam
from .serializers import MyTokenObtainPairSerializer, HackathonSerializer, UserSerializer
from rest_framework.generics import ListAPIView, RetrieveAPIView, RetrieveUpdateAPIView
from .permissions import IsOwnerOrTeammateOrSuperuser, HasAPIKey, HasRooApiKey
from .models import Organization, OrganizationContentConfig

logger = logging.getLogger(__name__)
User = get_user_model()

class SendMagicLinkView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        data = request.data
        email = data.get('email')
        
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

            # Determine app context
            app = data.get('app', 'hospital')
            from django.conf import settings
            
            # Determine base_url based on environment
            # We now use a single domain for both apps
            if settings.DEBUG:
                base_url = "http://localhost:5173"
            else:
                base_url = "https://mlai.au" 

            # Use message_id "2" for both apps since it's configured in Customer.io
            # We append app and next params to the magic link so the verify page knows where to go
            magic_link = generate_magic_link(user, base_url=base_url)
            
            # Append query params to the magic link
            # generate_magic_link returns base_url + path + ?token=...
            # We want to add &app=...&next=...
            if '?' in magic_link:
                magic_link += f"&app={app}"
            else:
                magic_link += f"?app={app}"
            
            # We don't have 'next' in the request body usually, but if we did:
            next_path = data.get('next')
            if next_path:
                magic_link += f"&next={next_path}"

            logger.info(f"Generated magic link for {email}: {magic_link}")
            send_magic_link_email(user, magic_link, message_id="2")
            logger.info(f"Sent magic link to existing user: {email} for app {app}")

            return Response(
                {"user_exists": True, "message": "Magic link sent to your email."}, 
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
        first_name = data.get('firstName', '')
        last_name = data.get('lastName', '')
        full_name = f"{first_name} {last_name}".strip()
        phone = data.get('phone')
        role = data.get('role', 'participant')

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
            
            # Determine app context
            app = data.get('app', 'hospital')
            from django.conf import settings
            
            # Determine base_url based on environment
            if settings.DEBUG:
                base_url = "http://localhost:5173"
            else:
                base_url = "https://mlai.au"

            # Use message_id "2" for both apps since it's configured in Customer.io
            magic_link = generate_magic_link(user, base_url=base_url)
            
            if '?' in magic_link:
                magic_link += f"&app={app}"
            else:
                magic_link += f"?app={app}"
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
                {"message": message, "magic_link": magic_link}, # Return link for dev convenience
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
        email = verify_magic_link(token)
        if email:
            try:
                user = User.objects.get(email=email)

                if not user.is_active:
                    user.is_active = True
                    user.save()
                    logger.info(f"Activated user account for {email}")

                # Generate JWT tokens
                refresh = RefreshToken.for_user(user)
                access_token = str(refresh.access_token)
                refresh_token = str(refresh)

                # Build the response payload
                # Determine next_url based on app context
                
                app_param = request.query_params.get('app')
                next_param = request.query_params.get('next')
                
                from django.conf import settings
                
                # Determine base_url based on environment
                if settings.DEBUG:
                    base_url = "http://localhost:5173"
                else:
                    base_url = "https://mlai.au"

                # Construct the full next_url
                if next_param:
                    if not next_param.startswith('/'):
                        next_param = '/' + next_param
                    next_url = f"{base_url}{next_param}"
                else:
                    # Default landing pages
                    if app_param == 'esafety':
                        next_url = f"{base_url}/esafety/dashboard" 
                    else:
                        next_url = f"{base_url}/dashboard"

                response_data = {
                    'message': 'Login successful',
                    'user': {
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
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if not user.is_authenticated:
            return Response({'error': 'Not authenticated'}, status=status.HTTP_401_UNAUTHORIZED)
        
        # Retrieve hospital team
        hospital_team = user.hospital_teams.first() if hasattr(user, 'hospital_teams') else None
        hospital_team_data = None
        if hospital_team:
            members = hospital_team.members.all().values("first_name", "last_name", "avatar_url", "role")
            hospital_team_data = {
                "team_name": hospital_team.team_name,
                "team_id": hospital_team.team_id,
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
        
        # Determine primary team for backward compatibility (prefer hospital)
        primary_team_data = hospital_team_data if hospital_team_data else esafety_team_data

        data = {
            'first_name': user.first_name,
            'last_name': user.last_name,
            'full_name': user.full_name,
            'email': user.email,
            'phone': user.phone,
            'about': user.about,
            'role': user.role,
            'is_superuser': user.is_superuser,
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
        team_name = request.data.get("team")
        
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
            # Ensure it's a list
            if isinstance(personas, str):
                personas = [personas]
            user.personas = personas

        # Only update the email if it's changed.
        if email and email != user.email:
            # TODO: Consider marking the email as unverified and sending a verification email.
            user.email = email

        user.save()

        # Handle team update if provided
        if team_name:
            # Remove user from any existing esafety teams
            # Assuming a user can only be in one team for esafety
            current_teams = user.esafety_teams.all()
            for t in current_teams:
                t.members.remove(user)
            
            # Find or create the team
            # We use filter().first() to avoid errors if multiple teams somehow have same name (though unlikely with unique constraints if any)
            # But Team model in esafety doesn't enforce unique name, only unique team_id. 
            # Let's assume we want to match by name.
            team = EsafetyTeam.objects.filter(team_name__iexact=team_name).first()
            
            if not team:
                # Create new team
                team = EsafetyTeam.objects.create(team_name=team_name)
            
            # Add user to the team
            team.members.add(user)

        # Handle avatar upload
        avatar_file = request.FILES.get('avatar')
        if avatar_file:
            try:
                from PIL import Image
                from io import BytesIO
                from .firebase_utils import upload_file_to_storage
                
                # Open image
                img = Image.open(avatar_file)
                
                # Resize/Crop to square (optional but good for avatars)
                # Simple resize to max 200x200 while maintaining aspect ratio, then center crop?
                # Or just resize to 200x200 thumbnail
                img.thumbnail((300, 300)) 
                
                # Save to buffer
                output_buffer = BytesIO()
                # Convert to RGB if RGBA (e.g. PNG) and saving as JPEG, 
                # but let's keep original format or default to PNG for transparency
                img_format = img.format if img.format else 'PNG'
                img.save(output_buffer, format=img_format)
                output_buffer.seek(0)
                
                # Upload
                # Use user ID in filename to avoid collisions/overwrite
                filename = f"avatars/{user.id}_{int(timezone.now().timestamp())}.{img_format.lower()}"
                avatar_url = upload_file_to_storage(output_buffer, filename, content_type=f'image/{img_format.lower()}')
                
                if avatar_url:
                    user.avatar_url = avatar_url
                    user.save()
                    
            except Exception as e:
                logger.error(f"Error uploading avatar: {e}")
                # Don't fail the whole request, just log it

        # Handle team avatar upload
        # Check both FILES and data (in case of different parsing)
        team_avatar_file = request.FILES.get('team_avatar') or request.data.get('team_avatar')
        
        if team_avatar_file:
            logger.info(f"Processing team_avatar: {team_avatar_file}")
            # Get the user's current team (esafety)
            # We assume the user is in a team if they are uploading a team avatar, 
            # or they just joined/created one above.
            team = user.esafety_teams.first()
            if team:
                try:
                    from PIL import Image
                    from io import BytesIO
                    from .firebase_utils import upload_file_to_storage
                    
                    # Open image
                    img = Image.open(team_avatar_file)
                    
                    # Resize/Crop
                    img.thumbnail((300, 300)) 
                    
                    # Save to buffer
                    output_buffer = BytesIO()
                    img_format = img.format if img.format else 'PNG'
                    img.save(output_buffer, format=img_format)
                    output_buffer.seek(0)
                    
                    # Upload
                    filename = f"team-avatars/{team.team_id}_{int(timezone.now().timestamp())}.{img_format.lower()}"
                    team_avatar_url = upload_file_to_storage(output_buffer, filename, content_type=f'image/{img_format.lower()}')
                    
                    if team_avatar_url:
                        team.avatar_url = team_avatar_url
                        team.save()
                        
                except Exception as e:
                    logger.error(f"Error uploading team avatar: {e}")


        # Return the updated profile
        # Re-use CurrentUserView logic or similar structure
        
        # Retrieve esafety team (freshly updated)
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

        data = {
            'full_name': user.full_name,
            'email': user.email,
            'phone': user.phone,
            'about': user.about,
            'role': user.role,
            'is_superuser': user.is_superuser,
            'team': esafety_team_data, 
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
    permission_classes = [HasAPIKey]

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
        Lookup org config by domain query param.
        Returns 404 if organization not found.
        """
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
                {'error': 'Organization not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get config if exists
        config = getattr(org, 'content_config', None)
        
        response_data = {
            'org_id': org.id,
            'org_name': org.name,
            'domain': org.domain,
            'article_template': config.article_template if config else None,
            'design_guide': config.design_guide if config else None,
            'github_repo': config.github_repo if config else None,
            'brand_name': config.brand_name if config else None,
        }
        
        return Response(response_data, status=status.HTTP_200_OK)

    def put(self, request):
        """
        Create org if not exists, then upsert config.
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
        
        # Update org name if provided and org already existed
        if not org_created and name and org.name != name:
            org.name = name
            org.save()
        
        # Upsert config
        config, config_created = OrganizationContentConfig.objects.update_or_create(
            organization=org,
            defaults={
                'article_template': data.get('article_template'),
                'design_guide': data.get('design_guide'),
                'github_repo': data.get('github_repo'),
                'brand_name': data.get('brand_name'),
            }
        )
        
        status_text = 'created' if org_created else 'updated'
        
        return Response({
            'status': status_text,
            'org_id': org.id,
            'org_name': org.name,
            'domain': org.domain,
        }, status=status.HTTP_201_CREATED if org_created else status.HTTP_200_OK)


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
