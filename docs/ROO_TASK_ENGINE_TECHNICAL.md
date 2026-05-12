# Roo Task Engine Technical Notes

This document describes the structured task engine as implemented across `mlai-backend` and Roo.

It is for engineers working on:

- task workflows
- reviewer flows
- Roo Slack commands
- point-award integrity
- migration and backfill logic

## Design Goal

Split the old overloaded task model into three concerns without breaking the existing API surface.

The three concerns are:

1. work definition
2. execution ownership
3. reviewable attempts

## Core Models

### `Task`

`Task` is the opportunity or work definition.

It holds:

- identity (`id`, `task_code`)
- copy (`title`, `description`)
- routing (`work_domain`, `review_flow`, reviewer fields)
- pricing (`points_estimate`, `points_min`, `points_max`)
- readiness (`volunteer_ready`, `difficulty`, `estimate_minutes`)
- grouping (`group_key`, `group_capacity`, `slot_label`)
- legacy compatibility fields (`status`, `assigned_to_user_id`, `assigned_user`, `closed_by_user_id`)

### `TaskAssignment`

`TaskAssignment` is the execution source of truth.

It holds:

- current assignee
- Slack assignee id
- assignment status
- claim/submission/approval timestamps
- final awarded points

Statuses:

- `claimed`
- `submitted`
- `approved`
- `released`
- `cancelled`

Active assignment is defined strictly as:

- `claimed`
- `submitted`

There is a partial unique constraint enforcing one active assignment per task.

### `TaskSubmission`

`TaskSubmission` is a review attempt on an assignment.

It holds:

- submission text
- submission URL
- evidence metadata
- review metadata
- approval/rejection state
- optional ledger entry link

Multiple submissions can belong to one assignment.

### `TaskActivity`

`TaskActivity` is append-only audit history.

In v1 it is primarily for integrity and debugging, not rich end-user analytics.

## Approval Source Of Truth

Final completion truth lives on:

- `TaskAssignment.approved_at`

Submission approval fields are attempt metadata, not the authoritative completion flag for the work item itself.

## Task Status Projection

`Task.status` remains as a compatibility projection for old clients.

Projection rules:

1. if task is cancelled -> `cancelled`
2. else if there is no non-released/non-cancelled assignment -> `open`
3. else if current assignment is `claimed` -> `claimed`
4. else if current assignment is `submitted` -> `submitted`
5. else if current assignment is `approved` -> `approved`

`released` assignments are historical and should not drive projected status.

## Review Permissions

Review is allowed for:

- `reviewer_slack_id`
- `fallback_reviewer_slack_id`
- admin override

This is enforced in the service layer through `TaskService.can_review(...)`.

## Backward Compatibility

The implementation keeps the existing REST actions and Slack command patterns alive:

- `POST /tasks/{id}/claim/`
- `POST /tasks/{id}/submit/`
- `POST /tasks/{id}/approve/`
- `POST /tasks/{id}/reject/`
- `POST /tasks/{id}/request-complete/`
- `POST /tasks/{id}/award/`

New compatibility surface:

- `GET /tasks/by-code/{task_code}/`
- `POST /tasks/{id}/unclaim/`
- `GET /tasks/?claimable=true`

Roo can still use numeric ids, but now also resolves `ROO-xxxx` codes.

## Service-Layer Behavior

Important methods live in `roo/services.py`.

Key responsibilities:

- assign `ROO-xxxx` task codes
- create and sync active assignments
- auto-link/create local users from Slack on claim
- submit work against assignments
- reject the latest pending submission when no `submission_id` is provided
- approve exactly once via ledger idempotency
- mirror projected task status for old clients

## Migration Strategy

The migration:

1. adds the new task metadata fields
2. creates `TaskAssignment`
3. extends `TaskSubmission`
4. creates `TaskActivity`
5. backfills existing tasks/submissions into assignments
6. enforces one active assignment per task

Backfill rules:

- existing task rows get `ROO-xxxx`
- existing `points` become estimate/min/max
- work domain and review flow are inferred from legacy portfolio where possible
- historical submissions are attached to the backfilled assignment

## Structured Tasks vs Points Requests

Structured tasks:

- planned work
- reviewer-driven approval
- auto-award on approval

`PointsRequest`:

- ad hoc/manual contributions
- separate approval path

Both remain valid because MLAI does more than planned coding tasks.

## Test Coverage

Current added API tests cover:

- claim auto-linking and assignment creation
- reject latest pending submission without `submission_id`
- unclaim forbidden after any submission exists
- multi-submission reject/resubmit/approve flow
- task-code lookup
- optimistic-locking conflicts on edit
- task cancel/archive behavior
- frozen claimed-points behavior during later task edits

The backend GitHub Action should run `roo.tests_api.TaskViewSetTests`.

## Known Limits In V1

- one active assignment per task
- group work uses sibling tasks with `group_key`, not many-to-many assignment
- minimal activity UX
- no advanced recommendation engine yet
- no dedicated web UI yet in `mlai-au`

Those are deliberate constraints to keep the workflow predictable.
