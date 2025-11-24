
# Security settings for Cloudflare
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = False  # Handled by Cloudflare
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
