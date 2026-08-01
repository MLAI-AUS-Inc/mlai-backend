
import os


# Security settings for Cloudflare
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'true').strip().lower() in {
    '1',
    'true',
    'yes',
    'on',
}
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_DOMAIN = ".mlai.au"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_SAMESITE = "Lax"
