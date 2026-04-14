"""
WSGI config for medhack_backend project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.1/howto/deployment/wsgi/
"""

import os
import logging

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')

application = get_wsgi_application()

from django.conf import settings  # noqa: E402

logger = logging.getLogger(__name__)
logger.info(
    "MLAI backend WSGI boot app_env=%s release=%s",
    getattr(settings, "APP_ENV", "unknown"),
    getattr(settings, "APP_RELEASE", "unknown"),
)
