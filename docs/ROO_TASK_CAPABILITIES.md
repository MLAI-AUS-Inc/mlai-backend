# Roo Structured Task Capabilities

This document explains the Roo task system in plain English.

It is for:

- volunteers
- reviewers
- points admins
- anyone trying to understand what Roo can and cannot do with structured work

## What The Task System Is For

Use the structured task system when MLAI has a defined piece of work with:

- a clear outcome
- a known reviewer
- a point value or bounded point range
- a clear completion path

Examples:

- coding tasks
- docs tasks
- event operations shifts
- content or communications deliverables
- design work

Do **not** use structured tasks for every contribution. Small or ad hoc work can still use `PointsRequest`.

## Core Objects

The system is split into three parts:

1. `Task`
   The opportunity or work definition.
2. `TaskAssignment`
   The active ownership record for the person doing the work.
3. `TaskSubmission`
   The attempt or deliverable that gets reviewed.

That split matters because one task can have retries and revisions without rewriting the original work definition.

## What Roo Can Do

### For Volunteers

Roo can:

- list open claimable tasks with `tasks` or `tasks open`
- list assigned tasks with `tasks mine`
- list review queue tasks with `tasks review`
- list all tasks with `tasks all`
- claim a task
- submit work for review
- release a task back to the queue before any submission exists
- show task codes like `ROO-0001`

### For Reviewers And Admins

Roo can:

- create tasks
- review submitted work
- approve a task and award points
- reject a submission and send it back for another pass
- directly award a task in legacy/admin flows

## Task Types

The task system separates the kind of work from the review method.

### Work Domains

Current domains include:

- `tech`
- `event_ops`
- `content_comms`
- `community`
- `governance`
- `partnerships`
- `grants`
- `finance`
- `design`
- `ops`

### Review Flows

Current review flows are:

- `pr_review`
- `deliverable_review`
- `attendance_confirmation`

Examples:

- a code fix is usually `tech` + `pr_review`
- a newsletter draft is usually `content_comms` + `deliverable_review`
- a registration-desk shift is usually `event_ops` + `attendance_confirmation`

## Volunteer Journey

### 1. Discover

Volunteer sees:

- `tasks`
- `tasks open`
- `tasks mine`
- `tasks review`
- `tasks all`
- a specific task code like `ROO-0042`

### 2. Claim

Volunteer claims the task.

Important rules:

- Roo links or creates the local user record immediately from Slack
- only one active assignment can exist for a task at a time

### 3. Submit

Volunteer submits work.

What happens depends on the review flow:

- PR review: usually a PR URL plus notes
- deliverable review: usually a link, file, or text summary
- attendance confirmation: reviewer can confirm completion directly

### 4. Review

Reviewer approves or requests changes.

If rejected:

- the submission is rejected
- the task returns to `claimed`
- the same volunteer can resubmit

### 5. Reward

Points are awarded once, on final approval.

## Task Statuses

The volunteer-facing task status stays simple:

- `open`
- `claimed`
- `submitted`
- `approved`
- `cancelled`

These are projected from the underlying assignment state so old clients can still work.

## Important Rules

### One Active Assignment Per Task

Only one active assignment can exist at a time.

In v1, active means:

- `claimed`
- `submitted`

### Unclaim Rules

`unclaim` is allowed only when:

- the assignment is still `claimed`
- there are no submissions on that assignment

Once a submission exists, the assignment stays tied to that volunteer for review and resubmission.

### Approval Rules

Approval truth lives on the assignment:

- `TaskAssignment.approved_at` means the work was accepted

Submission review fields are still kept for audit on each attempt.

### Group Work

Group work is modeled as sibling tasks sharing a `group_key`, not one task with many claimants.

That keeps ownership simple while still allowing grouped reporting.

## When To Use Structured Tasks vs Points Requests

Use structured tasks when:

- work is scoped in advance
- a reviewer is known
- completion can be checked

Use `PointsRequest` when:

- work was ad hoc
- no task existed before the work happened
- the contribution is more like participation or support than a defined deliverable

## Example Commands

- `@Roo tasks`
- `@Roo tasks open`
- `@Roo tasks mine`
- `@Roo tasks review`
- `@Roo tasks all`
- `@Roo claim ROO-0042`
- `@Roo submit ROO-0042 fixed the docs and opened PR #123`
- `@Roo unclaim ROO-0042`
- `@Roo approve ROO-0042`
- `@Roo reject ROO-0042 please add a test`

## Current Limits

This version intentionally stays conservative.

It does not yet try to do:

- deep recommendation ranking
- many-to-many group assignments on one row
- rich analytics over task history
- fancy Slack workflow UI

The priority is reliable execution, review, and rewards.
