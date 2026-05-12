# Roo Task Operations Runbook

This runbook is for:

- points admins
- reviewers
- tech leads
- event leads

It explains how to operate the structured task system safely.

## What Counts As A Volunteer-Ready Task

A task is volunteer-ready only when the work can move without back-and-forth to figure out basic expectations.

Minimum standard:

- clear title
- clear outcome
- correct `work_domain`
- correct `review_flow`
- points set
- reviewer assigned
- completion criteria written down

For `pr_review` tasks, volunteer-ready means all of these are present:

- `repo`
- `reviewer_slack_id` or `fallback_reviewer_slack_id`
- `acceptance_criteria`
- `how_to_test`

If any of those are missing, the backend should reject `volunteer_ready=true`.

## Create Flow

When creating a structured task:

1. Choose the right work domain.
2. Choose the right review flow.
3. Set points.
4. Set the named reviewer.
5. Add the context needed to complete it.
6. Mark it volunteer-ready only when it is actually ready.

Recommended fields:

- `title`
- `description`
- `work_domain`
- `review_flow`
- `points_estimate`
- `points_min`
- `points_max`
- `difficulty`
- `estimate_minutes`
- `reviewer_slack_id`
- `fallback_reviewer_slack_id`
- `acceptance_criteria`
- `how_to_test`
- `repo`
- `source_system`
- `source_ref`
- `source_url`

## Review Flows

### PR Review

Use for:

- code changes
- infrastructure changes
- technical docs that should ship with a PR

Reviewer checks:

- PR exists
- acceptance criteria are met
- tests or validation steps are covered

### Deliverable Review

Use for:

- content drafts
- design assets
- outreach materials
- operational documents

Reviewer checks:

- deliverable exists
- expected quality bar is met
- requested changes are resolved before approval

### Attendance Confirmation

Use for:

- event shifts
- registration desks
- setup/pack-down
- physical attendance or lead-confirmed completion

Reviewer confirms completion directly. A formal submission is optional.

## Review Rules

### Who Can Approve

Approval is allowed for:

- the named reviewer
- the fallback reviewer
- an admin override

### How Reject Works

Reject always applies to a submission attempt, not to the task definition itself.

If Slack does not pass a `submission_id`, the backend resolves the latest submitted attempt and rejects that.

### How Approval Works

Approval writes final completion on the assignment.

That means:

- the assignment becomes `approved`
- points are awarded once
- the approved submission gets review metadata

## Unclaim Rules

Volunteer can unclaim only when:

- assignment status is `claimed`
- no submission exists on that assignment

If any submission exists, do not reassign the work casually. Review or resolve it explicitly.

## Structured Tasks vs Manual Points Requests

Use structured tasks for planned work.

Use `PointsRequest` for:

- ad hoc help
- event participation not scoped ahead of time
- manual recognition
- edge cases that do not fit the structured flow yet

Do not create fake tasks just to avoid using `PointsRequest`.

## Slack Operating Commands

Common commands:

- `@Roo tasks`
- `@Roo tasks open`
- `@Roo tasks mine`
- `@Roo tasks review`
- `@Roo tasks all`
- `@Roo claim ROO-0042`
- `@Roo submit ROO-0042 ...`
- `@Roo unclaim ROO-0042`
- `@Roo approve ROO-0042`
- `@Roo reject ROO-0042 ...`

## Deployment And Migration Notes

Backend deploy on `main` already runs:

- `migrate --plan`
- `migrate --noinput`
- `migrate --check --noinput`

That behavior lives in [deploy.sh](../deploy.sh).

Roo deploy is separate and does not run Django migrations.

Safe deploy order for task-engine changes:

1. deploy `mlai-backend`
2. verify migrations and health
3. deploy `roo`

## Post-Deploy Smoke Checks

After deploy, verify:

1. task list still works
2. `tasks` and `tasks open` return the same claimable queue
3. claim creates an assignment and links the user
4. submit moves the task to review
5. reject returns it to claimed
6. approve awards points once
7. Roo can resolve a `ROO-xxxx` task code

## Failure Modes To Watch

### Claim Works But Submit Fails

This should no longer happen because claim auto-links the Slack user.

If it does, inspect:

- Slack profile lookup
- local user creation
- assignment row creation

### Task Stuck In Submitted

Check:

- reviewer routing
- whether the latest submission is still `submitted`
- whether approval or rejection hit the correct task code/id

### Duplicate Claims

Check:

- active assignment uniqueness
- any manual DB edits
- race conditions around concurrent claim attempts

## Guidance For Leads

Keep most volunteer tasks small.

Recommended sizes:

- `3-6` points for tiny work
- `6-18` points for normal volunteer tasks
- split tasks above `24` points unless there is a strong reason not to

The system can support bigger work, but small clear tasks usually get better volunteer follow-through.
