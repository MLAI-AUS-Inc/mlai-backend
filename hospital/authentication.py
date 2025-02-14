from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework import exceptions
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)    


class CustomJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        access_token = request.COOKIES.get('access_token')
        if not access_token:
            # No token provided, allow unauthenticated access
            return None

        try:
            validated_token = self.get_validated_token(access_token)
            user = self.get_user(validated_token)
            return (user, validated_token)
        except exceptions.AuthenticationFailed as e:
            # Log and allow unauthenticated access
            logger.warning(f"Authentication failed: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during authentication: {str(e)}")
            return None