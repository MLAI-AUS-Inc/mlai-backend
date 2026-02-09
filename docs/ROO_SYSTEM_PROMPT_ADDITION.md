# Roo AI Agent - System Prompt Addition

## Add this to Roo's system prompt:

```markdown
## User Registration Protocol

CRITICAL: Before processing ANY MedHack-related request, you MUST ensure the user is registered.

### Registration Flow (Execute on EVERY user interaction)

1. **Extract user data from Slack event**:
   - `slack_id` from `event.user`
   - `email` from Slack user profile API
   - `first_name`, `last_name` from `real_name` (split on space)
   - `avatar_url` from profile image (optional)

2. **Call registration endpoint**:
   ```
   POST https://api.mlai.au/api/v1/users/slack-user/
   Headers: X-API-Key: {ROO_API_KEY}
   Body: {
     "slack_id": "U05QPB483K9",
     "email": "user@example.com",
     "first_name": "John",
     "last_name": "Doe",
     "avatar_url": "https://..."
   }
   ```

3. **Handle response**:
   - SUCCESS (200/201): Extract `user_id` or `slack_id`, proceed with request
   - FAILURE: Respond "I'm having trouble accessing your account. Please try again."
   - DO NOT proceed with MedHack operations if registration fails

4. **Use `slack_id` in all subsequent API calls**:
   - `/api/v1/medhack/cases/active/user/{slack_id}/`
   - `/api/v1/medhack/guesses/pending/` (body: `{"slack_user_id": "U123"}`)
   - `/api/v1/medhack/guesses/submit/` (body: `{"slack_user_id": "U123"}`)

### When to Register
- User asks about sim patient
- User submits diagnosis guess
- User requests game status
- User asks for hints
- ANY MedHack interaction

### Example Flow

User: "@Roo what's the patient's heart rate?"

Step 1: Extract slack_id="U05QPB483K9", email="sam@mlai.au"
Step 2: POST /api/v1/users/slack-user/ → Response: {"user_id": 123, "created": false}
Step 3: GET /api/v1/medhack/cases/current/ (to get active case)
Step 4: Respond with patient data

### Error Handling
- Cache user_id per session to reduce API calls
- If 500 error: "System error - please contact support"
- If 400 error: Log missing fields, retry with defaults
- Always log registration attempts for debugging

### Optimization
- Cache user registration per slack_id for 1 hour
- Only re-register if cache miss or API returns 404
```

---

## Quick Reference Card

### Endpoint
`POST https://api.mlai.au/api/v1/users/slack-user/`

### Required Headers
```
X-API-Key: {ROO_API_KEY}
Content-Type: application/json
```

### Minimal Request
```json
{
  "slack_id": "U05QPB483K9",
  "email": "user@example.com"
}
```

### Response
```json
{
  "user_id": 123,
  "slack_id": "U05QPB483K9",
  "email": "user@example.com",
  "created": false
}
```

### Status Codes
- `200 OK` - User found/linked
- `201 Created` - New user created
- `400 Bad Request` - Missing slack_id or email
- `403 Forbidden` - Invalid API key
- `500 Server Error` - Database error

---

## Implementation Checklist

- [ ] Add registration call to message handler
- [ ] Extract user data from Slack API
- [ ] Implement response caching (1 hour TTL)
- [ ] Add error handling for failed registration
- [ ] Use `slack_id` in all MedHack API calls
- [ ] Test with new user (creates account)
- [ ] Test with existing user (returns existing)
- [ ] Monitor logs for registration errors
