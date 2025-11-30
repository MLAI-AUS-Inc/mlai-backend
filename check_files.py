import os
import django
import sys

sys.path.append('/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')
django.setup()

from esafety.models import Submission

def check_files():
    submissions = Submission.objects.all()
    print(f"Total Submissions: {submissions.count()}")
    for s in submissions:
        print(f"ID: {s.id}, User: {s.user.email}, File URL: {s.file_url}")

if __name__ == '__main__':
    check_files()
