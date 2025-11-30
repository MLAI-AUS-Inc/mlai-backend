import os
import django
import sys
from django.core.files.uploadedfile import SimpleUploadedFile

sys.path.append('/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')
django.setup()

from esafety.views import submit_predictions
from django.contrib.auth import get_user_model
from rest_framework.test import APIRequestFactory, force_authenticate

User = get_user_model()

def debug_submission():
    user = User.objects.first()
    if not user:
        print("No user found")
        return

    print(f"Submitting as {user.email}")
    
    # Read sample submission
    with open('esafety/sample_submission.csv', 'rb') as f:
        file_content = f.read()
        
    csv_file = SimpleUploadedFile("predictions.csv", file_content, content_type="text/csv")
    
    factory = APIRequestFactory()
    request = factory.post('/api/v1/hackathons/esafety/submissions/', {'predictions_csv': csv_file}, format='multipart')
    force_authenticate(request, user=user)
    
    response = submit_predictions(request)
    print(f"Response status: {response.status_code}")
    print(f"Response data: {response.data}")

if __name__ == '__main__':
    debug_submission()
