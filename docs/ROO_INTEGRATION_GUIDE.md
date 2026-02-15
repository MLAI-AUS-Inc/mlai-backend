# Roo Slack Bot - User Management Integration Guide

## Overview

This guide explains how Roo's AI agent should integrate with the mlai-backend API to ensure users are registered before interacting with MedHack features.

---

## AI Agent System Prompt Addition

Add this to Roo's system prompt or context:

```markdown
## User Registration & Management

Before processing any MedHack game interactions (guesses, case queries, etc.), you MUST ensure the user is registered in the mlai-backend database.

### User Registration Flow

1. **On every user interaction**, extract the following from Slack:
   - `slack_id` (from event.user)
   - `email` (from Slack user profile)
   - `first_name` (optional, from Slack real_name or display_name)
   - `last_name` (optional, from Slack real_name)
   - `avatar_url` (optional, from Slack profile image)

2. **Call the user registration endpoint** before processing the user's request:
   ```
   POST https://api.mlai.au/api/v1/users/slack-user/
   ```

3. **Use the returned user_id or slack_id** for subsequent MedHack API calls

### When to Register Users

Register users at the start of these interactions:
- User asks about a sim patient case
- User submits a diagnosis guess
- User requests their game status
- User asks for hints
- Any MedHack-related query

### Error Handling

- If registration fails, inform the user: "I'm having trouble accessing your account. Please try again in a moment."
- Do NOT proceed with MedHack operations if registration fails
- Log the error for debugging

### Example Integration

When a user sends: "@Roo what's the patient's temperature?"

1. Extract: slack_id="U05QPB483K9", email="sam@mlai.au"
2. Call: POST /api/v1/users/slack-user/ with user data
3. Receive: {"user_id": 123, "created": false}
4. Proceed: Fetch case data using slack_id or user_id
5. Respond: "The patient's temperature is 38.5°C"
```

---

## Data Contract

### Endpoint: User Registration

**URL**: `POST /api/v1/users/slack-user/`

**Authentication**:
- Header: `X-API-Key: {ROO_API_KEY}`
- Or: `Authorization: Api-Key {ROO_API_KEY}`

**Request Schema**:
```typescript
{
  slack_id: string;        // REQUIRED - Slack user ID (e.g., "U05QPB483K9")
  email: string;           // REQUIRED - User's email from Slack profile
  first_name?: string;     // OPTIONAL - User's first name
  last_name?: string;      // OPTIONAL - User's last name
  avatar_url?: string;     // OPTIONAL - Slack profile image URL
}
```

**Response Schema**:
```typescript
// Success (200 OK - existing user found)
{
  user_id: number;         // Database user ID
  email: string;           // User's email (lowercased)
  slack_id: string;        // Slack user ID
  first_name: string;      // User's first name (may be empty)
  last_name: string;       // User's last name (may be empty)
  created: false;          // User already existed
  linked?: boolean;        // True if slack_id was just linked to existing email
}

// Success (201 Created - new user created)
{
  user_id: number;
  email: string;
  slack_id: string;
  first_name: string;
  last_name: string;
  created: true;           // New user was created
}

// Error (400 Bad Request - missing required fields)
{
  error: "slack_id and email are required"
}

// Error (500 Internal Server Error - creation failed)
{
  error: "Failed to create user"
}
```

---

## Python Integration Example

```python
import os
import requests
from typing import Optional, Dict, Any


class MedHackUserManager:
    """Manages user registration for MedHack interactions."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv('MLAI_API_KEY')
        self.base_url = "https://api.mlai.au/api/v1"

    def ensure_user_registered(
        self,
        slack_id: str,
        email: str,
        first_name: str = "",
        last_name: str = "",
        avatar_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Ensure a Slack user is registered in the database.

        Args:
            slack_id: Slack user ID (e.g., "U05QPB483K9")
            email: User's email address
            first_name: User's first name (optional)
            last_name: User's last name (optional)
            avatar_url: URL to user's avatar (optional)

        Returns:
            User data dict with keys: user_id, email, slack_id, created

        Raises:
            requests.HTTPError: If API request fails
            ValueError: If required fields are missing
        """
        if not slack_id or not email:
            raise ValueError("slack_id and email are required")

        payload = {
            "slack_id": slack_id,
            "email": email,
        }

        if first_name:
            payload["first_name"] = first_name
        if last_name:
            payload["last_name"] = last_name
        if avatar_url:
            payload["avatar_url"] = avatar_url

        response = requests.post(
            f"{self.base_url}/users/slack-user/",
            json=payload,
            headers={"X-API-Key": self.api_key},
            timeout=10
        )

        response.raise_for_status()
        return response.json()


# Usage in Roo's message handler
def handle_slack_message(event: dict, slack_client):
    """Process incoming Slack message."""
    user_id = event.get('user')

    # Get user info from Slack
    user_info = slack_client.users_info(user=user_id)['user']
    profile = user_info.get('profile', {})

    # Extract user data
    slack_id = user_id
    email = profile.get('email', f"{user_id}@slack.generated")  # Fallback if no email

    # Parse name
    real_name = user_info.get('real_name', '')
    name_parts = real_name.split(' ', 1)
    first_name = name_parts[0] if len(name_parts) > 0 else ''
    last_name = name_parts[1] if len(name_parts) > 1 else ''

    avatar_url = profile.get('image_192')  # 192x192 avatar

    # Ensure user is registered
    user_manager = MedHackUserManager()
    try:
        user_data = user_manager.ensure_user_registered(
            slack_id=slack_id,
            email=email,
            first_name=first_name,
            last_name=last_name,
            avatar_url=avatar_url
        )

        if user_data.get('created'):
            print(f"Created new user: {email} (Slack ID: {slack_id})")
        else:
            print(f"User already exists: {email}")

        # Now proceed with MedHack logic
        process_medhack_message(event, user_data)

    except requests.HTTPError as e:
        print(f"Failed to register user: {e}")
        slack_client.chat_postMessage(
            channel=event['channel'],
            text="I'm having trouble accessing your account. Please try again in a moment."
        )
```

---

## TypeScript/JavaScript Integration Example

```typescript
interface UserRegistrationRequest {
  slack_id: string;
  email: string;
  first_name?: string;
  last_name?: string;
  avatar_url?: string;
}

interface UserRegistrationResponse {
  user_id: number;
  email: string;
  slack_id: string;
  first_name: string;
  last_name: string;
  created: boolean;
  linked?: boolean;
}

class MedHackUserManager {
  private apiKey: string;
  private baseUrl: string = "https://api.mlai.au/api/v1";

  constructor(apiKey: string) {
    this.apiKey = apiKey;
  }

  async ensureUserRegistered(
    slackId: string,
    email: string,
    firstName?: string,
    lastName?: string,
    avatarUrl?: string
  ): Promise<UserRegistrationResponse> {
    if (!slackId || !email) {
      throw new Error("slack_id and email are required");
    }

    const payload: UserRegistrationRequest = {
      slack_id: slackId,
      email: email,
    };

    if (firstName) payload.first_name = firstName;
    if (lastName) payload.last_name = lastName;
    if (avatarUrl) payload.avatar_url = avatarUrl;

    const response = await fetch(`${this.baseUrl}/users/slack-user/`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": this.apiKey,
      },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      throw new Error(`Failed to register user: ${response.statusText}`);
    }

    return await response.json();
  }
}

// Usage in Slack Bot
async function handleSlackMessage(event: any, slackClient: any) {
  const userId = event.user;

  // Get user info from Slack
  const userInfo = await slackClient.users.info({ user: userId });
  const profile = userInfo.user.profile;

  // Extract user data
  const slackId = userId;
  const email = profile.email || `${userId}@slack.generated`;

  const realName = userInfo.user.real_name || "";
  const [firstName = "", lastName = ""] = realName.split(" ", 2);
  const avatarUrl = profile.image_192;

  // Ensure user is registered
  const userManager = new MedHackUserManager(process.env.MLAI_API_KEY!);

  try {
    const userData = await userManager.ensureUserRegistered(
      slackId,
      email,
      firstName,
      lastName,
      avatarUrl
    );

    if (userData.created) {
      console.log(`Created new user: ${email} (Slack ID: ${slackId})`);
    } else {
      console.log(`User already exists: ${email}`);
    }

    // Now proceed with MedHack logic
    await processMedHackMessage(event, userData);

  } catch (error) {
    console.error("Failed to register user:", error);
    await slackClient.chat.postMessage({
      channel: event.channel,
      text: "I'm having trouble accessing your account. Please try again in a moment.",
    });
  }
}
```

---

## Caching Recommendations

To avoid hitting the API on every message, consider caching user data:

```python
from functools import lru_cache
import time

class CachedUserManager(MedHackUserManager):
    """User manager with in-memory caching."""

    def __init__(self, *args, cache_ttl_seconds=3600, **kwargs):
        super().__init__(*args, **kwargs)
        self.cache = {}  # {slack_id: (user_data, timestamp)}
        self.cache_ttl = cache_ttl_seconds

    def ensure_user_registered(self, slack_id: str, email: str, **kwargs):
        # Check cache
        if slack_id in self.cache:
            user_data, timestamp = self.cache[slack_id]
            if time.time() - timestamp < self.cache_ttl:
                return user_data

        # Cache miss - call API
        user_data = super().ensure_user_registered(slack_id, email, **kwargs)

        # Update cache
        self.cache[slack_id] = (user_data, time.time())

        return user_data
```

---

## Testing

### Test the endpoint manually:

```bash
curl -X POST https://api.mlai.au/api/v1/users/slack-user/ \
  -H "X-API-Key: your-roo-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "slack_id": "U05QPB483K9",
    "email": "test@example.com",
    "first_name": "Test",
    "last_name": "User"
  }'
```

Expected response:
```json
{
  "user_id": 456,
  "email": "test@example.com",
  "slack_id": "U05QPB483K9",
  "first_name": "Test",
  "last_name": "User",
  "created": true
}
```

### Subsequent call (should return same user):
```bash
# Same request - should return created: false
curl -X POST https://api.mlai.au/api/v1/users/slack-user/ \
  -H "X-API-Key: your-roo-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "slack_id": "U05QPB483K9",
    "email": "test@example.com"
  }'
```

Expected response:
```json
{
  "user_id": 456,
  "email": "test@example.com",
  "slack_id": "U05QPB483K9",
  "first_name": "Test",
  "last_name": "User",
  "created": false
}
```

---

## Checklist for Integration

- [ ] Add user registration to Roo's system prompt
- [ ] Extract user data from Slack events (slack_id, email, name)
- [ ] Call `/users/slack-user/` before MedHack operations
- [ ] Handle registration errors gracefully
- [ ] Use returned `slack_id` in MedHack API calls
- [ ] Implement caching to reduce API calls
- [ ] Test with new user (should create)
- [ ] Test with existing user (should return existing)
- [ ] Test error cases (missing email, network failure)
- [ ] Monitor logs for registration success/failure

---

## Questions?

Contact: sam@mlai.au
API Docs: https://api.mlai.au/api/schema/swagger-ui/
