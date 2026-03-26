# Content Factory GitHub Reconnect Contract

## Overview

`mlai-backend` exposes a Roo-facing reconnect endpoint for the existing GitHub auth flow and normalizes how repo-backed Content Factory actions report missing or expired GitHub access.

This contract covers:

- Manual Roo prompts like `reconnect to github`
- Preflight auth gating before repo-backed work is queued
- Structured `AUTH_REQUIRED` failures from Content Factory
- Structured `auth_required` callbacks for in-run failures

## Reconnect Endpoint

Roo should call:

`POST /api/content-factory/github/reconnect`

Request body:

```json
{
  "domain": "mlai.au",
  "slack_user_id": "U12345678",
  "github_repo": "MLAI-AUS-Inc/mlai-au",
  "trigger": "manual",
  "pending_action": "publish_pr"
}
```

Fields:

- `domain`: preferred lookup key for the GitHub connection
- `slack_user_id`: required so the auth URL can return to the correct Roo user
- `github_repo`: optional supporting context
- `trigger`: `manual`, `preflight`, or another caller-defined source
- `pending_action`: optional hint describing what Roo intends to do after auth

Responses:

```json
{
  "status": "already_connected",
  "connection_state": "connected",
  "domain": "mlai.au",
  "github_repo": "MLAI-AUS-Inc/mlai-au",
  "trigger": "manual",
  "pending_action": "publish_pr",
  "message": "GitHub is already connected for mlai.au."
}
```

```json
{
  "status": "auth_started",
  "connection_state": "auth_required",
  "domain": "mlai.au",
  "github_repo": "MLAI-AUS-Inc/mlai-au",
  "trigger": "preflight",
  "pending_action": "publish_pr",
  "auth_url": "https://api.mlai.au/api/content-factory/github/auth?domain=mlai.au&slack_user_id=U12345678",
  "message": "Reconnect GitHub for mlai.au before continuing."
}
```

`connection_state` may also be `repo_selection_required` when a valid token exists but no repo has been selected for the domain.

## Preflight Gate

Roo/mlai-backend should preflight the reconnect endpoint before queueing these repo-backed actions:

1. Repo scan
2. Scaffold
3. Article generation when the resolved delivery mode is `publish_code`
4. Bundle promotion / publish-as-PR

If reconnect returns `auth_started`, do not queue the downstream Content Factory job. Post the reconnect CTA instead and preserve the pending action for an explicit retry.

`content_only` article runs are not gated by GitHub auth.

## Content Factory 412 Contract

If Content Factory catches the auth problem before queueing, it returns `412 Precondition Failed` with this payload shape:

```json
{
  "status": "precondition_failed",
  "error_code": "AUTH_REQUIRED",
  "missing_step": "github_auth",
  "next_action": "reconnect_github",
  "requires_user_action": true,
  "resume_hint": "reconnect_github_then_retry",
  "domain": "mlai.au",
  "github_repo": "MLAI-AUS-Inc/mlai-au",
  "reason_code": "missing_credentials",
  "message": "Reconnect GitHub for mlai.au before continuing."
}
```

`mlai-backend` should convert this into a Roo-facing reconnect response rather than a generic prerequisite or transport failure.

## In-Run Callback Contract

If a queued scan, scaffold, or publish path later discovers missing or expired GitHub auth, Content Factory emits:

```json
{
  "event": "auth_required",
  "event_type": "auth_required",
  "status": "auth_required",
  "error_code": "AUTH_REQUIRED",
  "missing_step": "github_auth",
  "next_action": "reconnect_github",
  "requires_user_action": true,
  "resume_hint": "reconnect_github_then_retry",
  "domain": "mlai.au",
  "github_repo": "MLAI-AUS-Inc/mlai-au",
  "reason_code": "expired_credentials",
  "workflow": "publish_code",
  "job_id": "job_123",
  "run_id": "run_456",
  "slack_user_id": "U12345678",
  "message": "Reconnect GitHub for mlai.au before continuing."
}
```

Roo should present the reconnect CTA using the same handling path as the preflight blocker.

## Retry Model

V1 does not auto-resume blocked work after GitHub auth succeeds. Roo should:

1. Start the auth flow
2. Preserve the pending action
3. Ask the user to retry once the reconnect is complete
