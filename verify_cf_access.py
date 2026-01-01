
import os
import requests
import sys

# Mock settings
CONTENT_FACTORY_URL = os.environ.get('CONTENT_FACTORY_URL', 'http://localhost:8001')
API_KEY = os.environ.get('CONTENT_FACTORY_API_KEY')

def check_publish_endpoint():
    endpoint = f"{CONTENT_FACTORY_URL.rstrip('/')}/api/pipeline/publish/test-job-id" # Random job id
    print(f"Checking endpoint: {endpoint}")
    
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-API-KEY"] = "FOUND"
    
    try:
        # We expect 404 or 422 or 500, but NOT a connection error
        # Use a small timeout to see if it even connects
        response = requests.post(endpoint, json={"github_token": "test", "github_repo": "test"}, timeout=5)
        print(f"Response Status: {response.status_code}")
        print(f"Response Text: {response.text}")
        return True
    except requests.exceptions.ReadTimeout:
        print("Timeout! Connection established but no response.")
        return False
    except Exception as e:
        print(f"Error: {e}")
        return False

if __name__ == "__main__":
    check_publish_endpoint()
