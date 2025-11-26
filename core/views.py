import logging
from django.db import transaction
from django.contrib.auth import get_user_model
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
            
            if app == 'esafety':
                base_url = settings.ESAFETY_URL
            else:
                base_url = settings.MEDHACK_URL

            # Use message_id "2" for both apps since it's configured in Customer.io
            magic_link = generate_magic_link(user, base_url=base_url)
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
        full_name = data.get('fullName', '')
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
                    full_name=full_name
                )
                user.is_active = False # Require verification
                user.save()
                
                logger.info(f"Created new user: {email}")

            # Generate magic link and send email OUTSIDE the transaction
            # so if email fails, user is still created.
            
            # Determine app context
            app = data.get('app', 'hospital')
            from django.conf import settings
            
            if app == 'esafety':
                base_url = settings.ESAFETY_URL
            else:
                base_url = settings.MEDHACK_URL

            # Use message_id "2" for both apps since it's configured in Customer.io
            magic_link = generate_magic_link(user, base_url=base_url)
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
                response_data = {
                    'message': 'Login successful',
                    'user': {
                        'email': user.email,
                        'full_name': user.full_name,
                        'role': user.role,
                        'is_superuser': user.is_superuser,
                        'is_active': user.is_active,
                        'has_team': user.has_team,
                    },
                    'next_url': 'http://esafety.localhost:5173/platform/app/dashboard', 
                }

                response = Response(response_data, status=status.HTTP_200_OK)

                # Set cookies
                # Use settings for secure flag to support local dev (HTTP) vs prod (HTTPS)
                from django.conf import settings
                
                # For local development, set domain to .localhost to allow sharing between subdomains
                # In production, set domain to .yourdomain.com with Secure=True and SameSite=None
                is_production = not settings.DEBUG
                
                cookie_domain = None if not is_production else '.med-hack.com' # Replace with actual prod domain
                
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
            members = hospital_team.members.all().values("full_name")
            hospital_team_data = {
                "team_name": hospital_team.team_name,
                "team_id": hospital_team.team_id,
                "members": list(members)
            }

        # Retrieve esafety team
        esafety_team = user.esafety_teams.first() if hasattr(user, 'esafety_teams') else None
        esafety_team_data = None
        if esafety_team:
            members = esafety_team.members.all().values("full_name")
            esafety_team_data = {
                "team_name": esafety_team.team_name,
                "team_id": esafety_team.team_id,
                "members": list(members)
            }
        
        # Determine primary team for backward compatibility (prefer hospital)
        primary_team_data = hospital_team_data if hospital_team_data else esafety_team_data

        data = {
            'full_name': user.full_name,
            'email': user.email,
            'role': user.role,
            'is_superuser': user.is_superuser,
            'team': primary_team_data,  # Backward compatibility
            'hospital_team': hospital_team_data,
            'esafety_team': esafety_team_data,
        }

        return Response(data, status=status.HTTP_200_OK)

class UpdateProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        user = request.user
        full_name = request.data.get("full_name")
        email = request.data.get("email")

        # Require all fields to be provided.
        if not full_name or not email:
            return Response(
                {"error": "full_name and email are required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Update the user's profile information.
        user.full_name = full_name

        # Only update the email if it's changed.
        if email and email != user.email:
            # TODO: Consider marking the email as unverified and sending a verification email.
            user.email = email

        user.save()

        return Response({"message": "Profile updated successfully."}, status=status.HTTP_200_OK)

@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout_view(request):
    try:
        # Create response object
        response = Response({'message': 'Logged out successfully'}, status=status.HTTP_200_OK)
        
        # Delete authentication cookies
        response.delete_cookie('access_token', path='/')
        response.delete_cookie('refresh_token', path='/')
        
        # Delete any session-related cookies
        response.delete_cookie('sessionid', path='/')
        response.delete_cookie('csrftoken', path='/')
        
        return response
    except Exception as e:
        return Response({'error': 'Logout failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
