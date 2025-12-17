import requests
import os
import sys

# Configuration
BASE_URL = "http://localhost:8000/api/v1/integrations"
API_KEY = os.environ.get("INTERNAL_API_KEY")
SLACK_USER_ID = "U12345678"

if not API_KEY:
    print("Error: INTERNAL_API_KEY env var not set.")
    sys.exit(1)

headers = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json"
}

def step(name, func):
    print(f"--- {name} ---")
    try:
        func()
        print("PASS")
    except Exception as e:
        print(f"FAIL: {e}")
        # Continue?

def test_upsert_token():
    url = f"{BASE_URL}/github/token"
    payload = {
        "slack_user_id": SLACK_USER_ID,
        "token": "ghp_EXAMPLE_TOKEN",
        "user_name": "test_user",
        "scopes": ["repo", "user"]
    }
    resp = requests.post(url, json=payload, headers=headers)
    if resp.status_code != 200:
        raise Exception(f"Status {resp.status_code}: {resp.text}")
    print(resp.json())

def test_get_token():
    url = f"{BASE_URL}/github/token"
    resp = requests.get(url, params={"slack_user_id": SLACK_USER_ID}, headers=headers)
    if resp.status_code != 200:
        raise Exception(f"Status {resp.status_code}: {resp.text}")
    data = resp.json()
    if data['token'] != "ghp_EXAMPLE_TOKEN":
        raise Exception("Token mismatch")
    print(data)

def test_intent():
    url = f"{BASE_URL}/intent"
    payload = {"slack_user_id": SLACK_USER_ID, "intent": {"action": "create_repo"}}
    resp = requests.post(url, json=payload, headers=headers)
    if resp.status_code != 200:
        raise Exception(f"Status {resp.status_code}: {resp.text}")

def test_status():
    url = f"{BASE_URL}/status"
    payload = {"slack_user_id": SLACK_USER_ID, "project_scanned": True}
    resp = requests.patch(url, json=payload, headers=headers)
    if resp.status_code != 200:
        raise Exception(f"Status {resp.status_code}: {resp.text}")

def test_delete_intent():
    url = f"{BASE_URL}/intent"
    payload = {"slack_user_id": SLACK_USER_ID}
    resp = requests.delete(url, json=payload, headers=headers)
    if resp.status_code != 200:
        raise Exception(f"Status {resp.status_code}: {resp.text}")

def main():
    step("Upsert Token", test_upsert_token)
    step("Get Token", test_get_token)
    step("Update Intent", test_intent)
    step("Update Status", test_status)
    step("Delete Intent", test_delete_intent)

if __name__ == "__main__":
    main()
