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
from .models import Organization, OrganizationContentConfig, GeneratedComponent, ComponentMapping
from .serializers import GeneratedComponentSerializer, GeneratedComponentListSerializer

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

        # 0. Try lookup via slack_user_id -> UserIntegration -> github_repo
        if slack_user_id and not github_repo:
            try:
                from integrations.models import UserIntegration
                integration = UserIntegration.objects.filter(slack_user_id=slack_user_id).first()
                if integration and integration.github_repo:
                    github_repo = integration.github_repo
                    logger.info(f"Resolved github_repo={github_repo} from slack_user_id={slack_user_id}")
            except Exception as e:
                logger.warning(f"Error looking up integration by slack_user_id {slack_user_id}: {e}")

        # 1. Try lookup by github_repo if provided
        if github_repo:
            try:
                # Find the config that matches this repo
                config = OrganizationContentConfig.objects.filter(github_repo=github_repo).first()
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
            'article_template': config.article_template if config else None,
            'design_guide': config.design_guide if config else None,
            'resource_prompt': config.resource_prompt if config else None,
            'company_context': config.company_context if config else None,
            'github_repo': config.github_repo if config else None,
            'brand_name': config.brand_name if config else None,
            'scan_summary': config.scan_summary if config else None,
            'tech_stack': config.tech_stack if config else {},
            'installed_packages': config.installed_packages if config else {},
            'pillar_strategy': config.pillar_strategy if config else {},
            'article_path_pattern': config.article_path_pattern if config else None,
            'registry_path': config.registry_path if config else None,
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
        
        # Prepare defaults dynamically to allow partial updates
        # Only include fields that are present in the request data
        defaults = {}
        target_fields = [
            'article_template',
            'design_guide',
            'resource_prompt',
            'company_context',
            'github_repo',
            'brand_name',
            'scan_summary',
            'tech_stack',
            'installed_packages',
            'pillar_strategy',
            'article_path_pattern',
            'registry_path',
        ]
        
        for field in target_fields:
            if field in data:
                defaults[field] = data[field]

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

class ContentFactoryTokenView(APIView):
    """
    On-demand token refresh endpoint for content-factory.
    
    GET /api/content-factory/token?slack_user_id=U12345
    
    Content-factory can call this endpoint mid-job to get a fresh GitHub token
    without needing to restart the entire pipeline.
    
    Returns:
        {
            "github_token": "ghu_xxxx...",
            "github_repo": "owner/repo",
            "expires_at": "2024-01-16T12:00:00Z" (optional)
        }
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        from integrations.services.github import ensure_valid_token, TokenRefreshError
        from integrations.models import UserIntegration
        
        slack_user_id = request.query_params.get('slack_user_id')
        
        if not slack_user_id:
            return Response(
                {'error': 'slack_user_id query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            # Get fresh token (auto-refreshes if expired)
            fresh_token = ensure_valid_token(slack_user_id)
            
            # Fetch integration for additional context
            integration = UserIntegration.objects.get(slack_user_id=slack_user_id)
            
            response_data = {
                'github_token': fresh_token,
                'github_repo': integration.github_repo,
                'slack_user_id': slack_user_id,
            }
            
            # Include expiry if available
            if integration.github_token_expires_at:
                response_data['expires_at'] = integration.github_token_expires_at.isoformat()
            
            logger.info(f"Provided fresh GitHub token for {slack_user_id}")
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
        except Exception as e:
            logger.exception(f"Error fetching token for {slack_user_id}: {e}")
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class ContentFactoryCallbackView(APIView):
    """
    Receives callbacks from content-factory for various pipeline events.
    
    POST /api/content-factory/callback
    
    Event types:
    - topic_selection: Research complete, topic selected, awaiting confirmation
    - article_complete: Article generated and published successfully
    - error: Pipeline failed with error
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        from .models import ContentFactoryJob
        
        data = request.data
        event_type = data.get('event_type')
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
        error_message = data.get('error_message')

        logger.info(f"Received auth_required callback for job {job_id} (user {slack_user_id})")

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
                         slack_user_id=slack_user_id
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
             self._send_auth_required_notification(slack_user_id, domain, error_message, job_id)
        except Exception as e:
             logger.error(f"Failed to send auth_required notification: {e}")
             # Return success anyway to avoid crashing the caller (Content Factory)
             # The job status is already updated in DB so we can track it.
             return Response({'status': 'processed_with_error', 'error': str(e)}, status=status.HTTP_200_OK)

        return Response({'status': 'processed', 'job_id': job_id}, status=status.HTTP_200_OK)

    def _send_auth_required_notification(self, slack_user_id, domain, error_message, job_id):
        from integrations.services.slack import SlackService
        from django.urls import reverse
        from django.conf import settings

        try:
            # Construct Re-Auth URL with job_id in state
            # We point to our backend's connect endpoint, passing job_id as a query param
            # The backend view will embed it into the OAuth state
            base_url = getattr(settings, 'MEDHACK_URL', 'https://mlai.au').rstrip('/')
            
            try:
                connect_path = reverse('github_connect')
            except Exception:
                # Fallback path if reverse fails or namespace issue
                connect_path = '/integrations/connect/github'
                
            auth_url = f"{base_url}{connect_path}?slack_user_id={slack_user_id}&job_id={job_id}"

            text = f"⚠️ GitHub Authentication Failed for {domain}"
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
                        "text": f"The content pipeline could not access your repository for *{domain}*.\n\n*Error:* {error_message}"
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

            SlackService.send_dm(slack_user_id, text, blocks=blocks)
            
        except Exception as e:
            logger.error(f"Error constructing/sending Slack notification: {e}")
            raise

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
        from .models import ContentFactoryJob, Organization
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
        
        logger.info(f"Topic selection recorded for job {job_id}: {len(options)} options found")
        
        if slack_user_id and options:
            # Fetch organization context for explanations
            company_context = None
            competitors = []
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
                        "text": f"*{idx + 1}. {keyword}*\n"
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
            
            SlackService.send_dm(slack_user_id, "Topic selection ready for review", blocks=blocks)
        
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
        
        logger.info(f"Article complete for job {job_id}: pr_url={pr_url}")
        
        if slack_user_id:
             blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"✅ *Article Published!* for {domain}"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"The article has been generated and a Pull Request is ready.\n\n📄 *< {article_url} | View Article >*\n🔗 *< {pr_url} | View Pull Request >*"
                    }
                }
            ]
             SlackService.send_dm(slack_user_id, "Article generation complete!", blocks=blocks)
        
        return Response({
            'status': 'received',
            'message': 'Article complete callback processed',
            'job_id': job_id,
        }, status=status.HTTP_200_OK)

    def _handle_error(self, data):
        """Handle error event from content-factory."""
        from .models import ContentFactoryJob

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
    KeywordStatusUpdateSerializer, SEODashboardSerializer
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
        qs = qs[:limit]

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
                velocity = kw_data.get('velocity_data')
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

        # Create written article
        article = WrittenArticle.objects.create(
            organization=org,
            title=serializer.validated_data['title'],
            slug=serializer.validated_data['slug'],
            category=serializer.validated_data['category'],
            primary_keyword=primary_keyword,
            article_url=serializer.validated_data.get('article_url'),
            pr_url=serializer.validated_data.get('pr_url'),
            job=job,
            published_at=timezone.now(),
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
            'status': 'created'
        }, status=status.HTTP_201_CREATED)


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
