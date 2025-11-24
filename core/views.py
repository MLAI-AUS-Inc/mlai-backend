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

        full_name = data.get('fullName', '')
        role = data.get('role', 'participant')

        try:
            with transaction.atomic():
                # Check if user already exists
                user, created = User.objects.get_or_create(email=email)

                if created:
                    # New user, set additional fields
                    user.full_name = full_name
                    user.role = role
                    user.is_active = False  # User is inactive until they verify email
                    user.save()
                    logger.info(f"Created new user: {email}")
                else:
                    # Existing user, optionally update fields
                    updated = False
                    if full_name and user.full_name != full_name:
                        user.full_name = full_name
                        updated = True
                    if user.role != role:
                        user.role = role
                        updated = True
                    if updated:
                        user.save()
                        logger.info(f"Updated user information for: {email}")
                    else:
                        logger.info(f"No updates needed for existing user: {email}")

                # Generate magic link and send email
                magic_link = generate_magic_link(user)
                send_magic_link_email(user, magic_link)
                logger.info(f"Sent magic link to: {email}")

                return Response({"message": "Magic link sent to your email."}, status=status.HTTP_200_OK)
        except Exception as e:
            logger.exception(f"Error in SendMagicLinkView: {str(e)}")
            return Response({"error": "An error occurred while processing your request."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

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
                }

                response = Response(response_data, status=status.HTTP_200_OK)

                # Set cookies (optional, if you want to store tokens in cookies)
                response.set_cookie(
                    key='access_token',
                    value=access_token,
                    max_age=86400,  # 1 day
                    httponly=True,
                    secure=True,  # Set to True in production
                    samesite='None',
                    path='/',
                )
                response.set_cookie(
                    key='refresh_token',
                    value=refresh_token,
                    max_age=172800,  # 2 days
                    httponly=True,
                    secure=True,  # Set to True in production
                    samesite='None',
                    path='/',
                )

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

        response.set_cookie(
            key='access_token',
            value=access_token,
            httponly=True,
            secure=False,  # True in production with HTTPS
            samesite='None',  # 'Lax' is acceptable for same-origin requests
        )
        response.set_cookie(
            key='refresh_token',
            value=refresh_token,
            httponly=True,
            secure=False,  # True in production with HTTPS
            samesite='None',
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
        response.set_cookie(
            key='access_token',
            value=access_token,
            httponly=True,
            secure=True,  # Must be True when SameSite=None
            samesite='None',
            path='/',
        )
        return response

class CurrentUserView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if not user.is_authenticated:
            return Response({'error': 'Not authenticated'}, status=status.HTTP_401_UNAUTHORIZED)
        
        # Retrieve the user's team details (assuming one team)
        # Note: 'teams' related name might need to be checked if Team model is in another app
        team = user.teams.first() if hasattr(user, 'teams') else None
        team_data = None
        if team:
            members = team.members.all().values("full_name")
            team_data = {
                "team_name": team.team_name,
                "members": list(members)
            }
        
        data = {
            'full_name': user.full_name,
            'email': user.email,
            'role': user.role,
            'is_superuser': user.is_superuser,
            'team': team_data,  # team will be null if not set
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
