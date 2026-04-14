from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework import exceptions
from django.conf import settings
import logging

logger = logging.getLogger(__name__)    

class CustomJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        # First try to get token from Authorization header
        header = self.get_header(request)
        if header:
            try:
                raw_token = self.get_raw_token(header)
                if raw_token:
                    validated_token = self.get_validated_token(raw_token)
                    user = self.get_user(validated_token)
                    logger.info(f"User authenticated via header: {user.email}")
                    return (user, validated_token)
            except Exception as e:
                logger.warning(f"Header authentication failed: {str(e)}")
                # Continue to try cookie authentication
        
        # Fall back to cookie authentication
        access_token = request.COOKIES.get('access_token')
        logger.info(f"Cookies in request: {list(request.COOKIES.keys())}")
        if access_token:
            try:
                validated_token = self.get_validated_token(access_token)
                user = self.get_user(validated_token)
                logger.info(f"User authenticated via cookie: {user.email}")
                return (user, validated_token)
            except exceptions.AuthenticationFailed as e:
                # Try to decode token to see user_id for debugging
                try:
                    unverified_token = self.get_unverified_token(access_token)
                    user_id = unverified_token.get('user_id')
                    logger.warning(f"Cookie authentication failed for user_id {user_id}: {str(e)}")
                except:
                    logger.warning(f"Cookie authentication failed: {str(e)}")
                return None
            except Exception as e:
                logger.error(f"Unexpected error during cookie authentication: {str(e)}")
                return None
        else:
            logger.info("No access_token cookie found in request")
        
        # No valid authentication found
        return None
