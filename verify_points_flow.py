import requests
import json
import uuid

BASE_URL = "http://localhost:8000/api/v1/points"

# Test Data
MINTER_ID = "U_MINTER_" + str(uuid.uuid4())[:8]
VOLUNTEER_ID = "U_VOLUNTEER_" + str(uuid.uuid4())[:8]

def run_test():
    print("🚀 Starting Points System Verification Flow")
    print(f"Minter: {MINTER_ID}, Volunteer: {VOLUNTEER_ID}")

    # 1. Create a Task (Mint)
    print("\n[1] Creating Task...")
    task_payload = {
        "title": "Verify Points System",
        "description": "Automated test task",
        "portfolio": "tech",
        "points": 5,
        "created_by_user_id": MINTER_ID,
        "slack_channel_id": "C12345",
        "slack_thread_ts": "12345.678"
    }
    response = requests.post(f"{BASE_URL}/tasks/", json=task_payload)
    if response.status_code != 201:
        print(f"❌ Failed to create task: {response.text}")
        return
    task = response.json()
    task_id = task['id']
    print(f"✅ Task #{task_id} created: {task['title']} ({task['points']} pts)")

    # 2. List Tasks
    print("\n[2] Listing Open Tasks...")
    response = requests.get(f"{BASE_URL}/tasks/?status=open")
    tasks = response.json()
    found = any(t['id'] == task_id for t in tasks)
    if found:
        print(f"✅ verified task #{task_id} is in open list")
    else:
        print(f"❌ Task #{task_id} not found in open list")

    # 3. Claim Task
    print("\n[3] Claiming Task...")
    claim_payload = {"slack_user_id": VOLUNTEER_ID}
    response = requests.post(f"{BASE_URL}/tasks/{task_id}/claim/", json=claim_payload)
    if response.status_code == 200:
        task = response.json()
        print(f"✅ Task #{task_id} claimed by {task['assigned_to_user_id']}")
    else:
        print(f"❌ Failed to claim task: {response.text}")

    # 4. Request Completion
    print("\n[4] Requesting Completion...")
    complete_payload = {"slack_user_id": VOLUNTEER_ID}
    response = requests.post(f"{BASE_URL}/tasks/{task_id}/request-complete/", json=complete_payload)
    if response.status_code == 200:
        task = response.json()
        print(f"✅ Task #{task_id} status: {task['status']}")
    else:
        print(f"❌ Failed to request complete: {response.text}")

    # 5. Approve Task
    print("\n[5] Approving Task...")
    approve_payload = {"slack_user_id": MINTER_ID}
    # Using 'approve' action (or 'close' depending on my implementation naming) -> I used 'approve'
    response = requests.post(f"{BASE_URL}/tasks/{task_id}/approve/", json=approve_payload)
    if response.status_code == 200:
        task = response.json()
        print(f"✅ Task #{task_id} approved. Status: {task['status']}")
    else:
        print(f"❌ Failed to approve task: {response.text}")

    # 6. Verify Ledger/Balance
    print("\n[6] Checking Balance...")
    response = requests.get(f"{BASE_URL}/users/{VOLUNTEER_ID}/balance/")
    if response.status_code == 200:
        balance = response.json()
        print(f"✅ Balance for {VOLUNTEER_ID}: {balance['lifetime_balance']} (Expected: 5)")
        if balance['lifetime_balance'] == 5:
            print("🎉 SUCCESS: Full flow verified.")
        else:
            print("❌ FAILURE: Balance mismatch.")
    else:
        print(f"❌ Failed to get balance: {response.text}")

if __name__ == "__main__":
    try:
        run_test()
    except Exception as e:
        print(f"❌ Error running test: {e}")
