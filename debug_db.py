import os
import dj_database_url
from dotenv import load_dotenv

load_dotenv()

url = os.environ.get('DATABASE_URL')
print(f"Raw DATABASE_URL length: {len(url) if url else 'None'}")
if url:
    # Mask password
    safe_url = url.split('@')[-1] if '@' in url else '...no-auth...'
    print(f"Safe URL end: {safe_url}")

config = dj_database_url.config(default=url)
print("Parsed Config:")
for k, v in config.items():
    if k == 'PASSWORD':
        print(f"{k}: *****")
    else:
        print(f"{k}: {v}")
