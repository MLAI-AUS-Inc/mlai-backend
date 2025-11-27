# verify_avatar_upload.py
import os
import django
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from io import BytesIO

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')
django.setup()

from django.contrib.auth import get_user_model
from rest_framework.test import APIRequestFactory, force_authenticate
from core.views import UpdateProfileView

User = get_user_model()

def verify_avatar_upload():
    print("Verifying avatar upload...")
    
    # Create a test user
    email = "test_avatar@example.com"
    user, created = User.objects.get_or_create(email=email)
    if created:
        user.set_password("password")
        user.save()
        print(f"Created test user: {email}")
    else:
        print(f"Using existing test user: {email}")

    # Create a dummy image
    img = Image.new('RGB', (400, 400), color='red')
    img_io = BytesIO()
    img.save(img_io, format='JPEG')
    img_io.seek(0)
    
    avatar_file = SimpleUploadedFile("avatar.jpg", img_io.getvalue(), content_type="image/jpeg")

    # Create request
    factory = APIRequestFactory()
    url = '/api/v1/auth/update-profile/'
    data = {
        'full_name': 'Avatar Tester',
        'avatar': avatar_file
    }
    
    # Use 'multipart/form-data' for file upload
    request = factory.patch(url, data, format='multipart')
    force_authenticate(request, user=user)
    
    view = UpdateProfileView.as_view()
    
    try:
        print("Sending request to update profile with avatar...")
        response = view(request)
        
        if response.status_code == 200:
            print("Response 200 OK")
            print("Response data:", response.data)
            
            updated_user = User.objects.get(email=email)
            print(f"User avatar_url: {updated_user.avatar_url}")
            
            if updated_user.avatar_url:
                print("SUCCESS: Avatar URL is set!")
            else:
                print("FAILURE: Avatar URL is NOT set. Check logs for upload errors (likely missing credentials).")
        else:
            print(f"FAILURE: Response status {response.status_code}")
            print(response.data)
            
    except Exception as e:
        print(f"EXCEPTION: {e}")

if __name__ == "__main__":
    verify_avatar_upload()
