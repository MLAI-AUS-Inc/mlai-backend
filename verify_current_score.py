import os
import django
import sys

sys.path.append('/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')
django.setup()

from esafety.models import Submission
from django.contrib.auth import get_user_model

User = get_user_model()

def verify():
    email = "eshinsharma1@gmail.com"
    try:
        user = User.objects.get(email=email)
        submission = Submission.objects.filter(user=user).order_by('-submitted_at').first()
        if submission:
            print(f"User: {email}")
            print(f"Submission ID: {submission.id}")
            print(f"Score: {submission.score}")
            print(f"Fine Score (Accuracy): {submission.fine_score}")
        else:
            print("No submission found.")
    except User.DoesNotExist:
        print("User not found.")

if __name__ == '__main__':
    verify()
