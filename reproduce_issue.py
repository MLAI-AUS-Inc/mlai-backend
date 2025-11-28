import os
import django
from django.conf import settings
from unittest.mock import MagicMock, patch

# Configure Django settings
if not settings.configured:
    settings.configure(
        DEBUG=True,
        SECRET_KEY='test',
        ROOT_URLCONF=__name__,
        INSTALLED_APPS=[
            'django.contrib.auth',
            'django.contrib.contenttypes',
            'rest_framework',
            'esafety',
            'core',
            'hospital',
        ],
        DATABASES={
            'default': {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': ':memory:',
            }
        },
        REST_FRAMEWORK={
            'DEFAULT_PERMISSION_CLASSES': [
                'rest_framework.permissions.IsAuthenticated',
            ],
            'DEFAULT_AUTHENTICATION_CLASSES': [
                'hospital.authentication.CustomJWTAuthentication',
            ],
        },
        AUTH_USER_MODEL='core.User',
    )
    django.setup()
    from django.core.management import call_command
    call_command('migrate', verbosity=0)

from rest_framework.test import APIRequestFactory, force_authenticate
from esafety.views import AnnouncementListView
from esafety.models import Announcement
from django.contrib.auth import get_user_model

User = get_user_model()

def test_announcement_view():
    print("Testing AnnouncementListView permissions...")
    
    # Mock the queryset to avoid DB access if possible, or just let it use sqlite
    # Since we configured sqlite, we can use it.
    
    # Create a user
    user = User.objects.create(email='test@example.com', role='participant')
    print(f"Created user: {user.email}, is_active={user.is_active}, is_authenticated={user.is_authenticated}")

    # Create a request
    factory = APIRequestFactory()
    request = factory.get('/api/v1/hackathons/esafety/announcements/')
    
    # Force authenticate
    force_authenticate(request, user=user)
    
    # Instantiate view
    view = AnnouncementListView.as_view()
    
    # Call view
    try:
        response = view(request)
        print(f"Response status: {response.status_code}")
        if response.status_code == 403:
            print("Reproduced 403 Forbidden!")
        elif response.status_code == 200:
            print("Success: 200 OK")
        else:
            print(f"Unexpected status: {response.status_code}")
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    test_announcement_view()
