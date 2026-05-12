# MLAI Platform Operating Model

This document is the long-term operating model for building MLAI Tech with volunteers, Roo, Linear, GitHub, Slack, and the MLAI web platform.

It is intentionally simple:

- Linear is the planning and tracking system.
- GitHub is the code and review system.
- Roo is the community-facing orchestrator.
- Slack is the conversational workspace.
- `mlai-backend` is the platform source of truth.
- `mlai-au` is the public website and web application surface.

## Why This Lives Here

This document lives in `mlai-backend/docs` because `mlai-backend` is the platform core. It owns the durable state and business rules for users, points, tasks, programs, integrations, and future opportunity services.

Linear can mirror this as a planning document, but the repo copy is the version-controlled source of truth.

Related task-system docs:

- [Roo Structured Task Capabilities](./ROO_TASK_CAPABILITIES.md)
- [Roo Task Operations Runbook](./ROO_TASK_OPERATIONS_RUNBOOK.md)
- [Roo Task Engine Technical Notes](./ROO_TASK_ENGINE_TECHNICAL.md)

## North Star

MLAI Tech should become a community operating system.

Members and volunteers should be able to:

- discover useful work
- claim a task
- understand what good completion means
- ship the work through GitHub
- have the work reviewed
- earn Roo Points
- build reputation and trust over time

Roo should make this feel lightweight in Slack. The platform should make it reliable.

## One-Line Architecture

Roo talks to people. The website shows structured experiences. The backend owns truth. Workers do slow work. GitHub stores code. Linear tracks plans.

```text
Slack / Discord / Email
        |
        v
      Roo
conversation, commands, approvals, reminders
        |
        v
+------------------+        +-------------------+
|  mlai-au         | -----> |  mlai-backend     |
|  public website  |        |  platform core    |
|  member app      |        |  auth/users       |
|  admin app       |        |  points/tasks     |
+------------------+        |  jobs/startups    |
                            |  events/programs  |
                            |  integrations     |
                            +----+---------+----+
                                 |         |
                                 v         v
                            Postgres   Workers/Queues
                                          |
                     +--------------------+--------------------+
                     |                    |                    |
              Jobs Worker          Content Factory       Bridge/Gmail
              scrape/rank          AI publish/PR         async sync
```

## System Roles

### Linear

Linear is the planning and accountability layer.

Use Linear for:

- initiatives
- projects
- issues
- ownership
- priority
- status
- due dates
- delivery planning

Do not use Linear as the place where technical implementation detail disappears. Technical detail should also live in GitHub issues, PRs, or repo docs when needed.

### GitHub

GitHub is the execution and code review layer.

Use GitHub for:

- branches
- pull requests
- code review
- CI checks
- release history
- technical discussion attached to code

Every code task should end in either a PR or a clear reason why no PR was needed.

### Roo

Roo is the community-facing command layer.

Roo should:

- list open volunteer tasks
- help people claim tasks
- post task reminders
- connect Slack users to Linear/GitHub records
- summarize task status
- manage points requests and approvals
- notify users when reviews are needed
- post scheduled outputs, such as daily jobs

Roo should not be the source of truth for points, tasks, or project state. Roo should call `mlai-backend`.

### Slack

Slack is the human workspace.

Use Slack for:

- asking questions
- posting calls for help
- celebrating shipped work
- lightweight approvals
- Roo commands
- community feedback

Do not rely on Slack history as the only record of work. If it matters, it should be in Linear, GitHub, or `mlai-backend`.

### `mlai-backend`

`mlai-backend` is the platform source of truth.

It should own:

- users and identities
- Slack/GitHub/Gmail links
- Roo Points ledger
- tasks and submissions
- rewards and redemptions
- jobs/opportunities data
- startup/founder data
- program and hackathon state
- integration state
- workflow runs and audit logs

### `mlai-au`

`mlai-au` is the experience layer.

It should own:

- public website
- member web app
- admin views
- jobs pages
- founder/startup UI
- program/hackathon UI

It should not own platform business rules.

## Core Rule

If it is durable state or business logic, it belongs in `mlai-backend`.

If it is conversation, triggering, status updates, or approval prompts, it belongs in Roo.

If it is UI, it belongs in `mlai-au`.

If it is slow, scheduled, unreliable, scrape-heavy, or AI-heavy, it belongs in a worker with persisted run state.

## Team Structure

Start with two Linear teams:

- `MLAI`: broad community, events, operations, non-technical projects.
- `Tech`: software, data, platform, automation, and infrastructure work.

Over time, Tech can split into domain ownership groups without creating too much process:

- Platform Core
- Roo
- Web Experience
- Opportunities
- Startup Tools
- Programs/Hackathons
- Content/AI
- Integrations/Ops

## Linear Hierarchy

Use this hierarchy:

```text
Initiative
  Strategic outcome, 3-12 months

Project
  Deliverable with owner, scope, and target date

Issue
  Unit of work assignable to one person or pair

Sub-issue/checklist
  Optional breakdown for larger issues
```

Example:

```text
Initiative: Make MLAI Tech volunteer-powered
Project: Roo Tasks and Points Workflow
Issue: Add backend task claim endpoint
Issue: Add Roo command for task claiming
Issue: Add member dashboard task list
Issue: Add points approval audit trail
```

## Issue Template

Every volunteer-ready issue should include:

```text
Title:
Outcome:
Repo:
Domain:
Owner:
Reviewer:
Points:
Difficulty:
Context:
Acceptance criteria:
Likely files:
How to test:
Definition of done:
```

Required fields:

- `Outcome`
- `Repo`
- `Owner`
- `Reviewer`
- `Points`
- `Acceptance criteria`
- `How to test`

If these are missing, the task is not ready for volunteers.

## Issue Status Flow

Use the existing Linear statuses:

```text
Backlog -> Todo -> In Progress -> In Review -> Done
```

Status rules:

- `Backlog`: idea exists, not ready.
- `Todo`: ready for someone to claim.
- `In Progress`: assigned and actively worked.
- `In Review`: PR opened or deliverable submitted.
- `Done`: merged, accepted, deployed if needed, points handled.
- `Canceled` or `Duplicate`: explicitly closed with reason.

## Job Posting Flow

A job means a scoped piece of work, not employment.

Job creation flow:

1. A lead creates a Linear issue.
2. The issue uses the volunteer-ready template.
3. The lead assigns points and reviewer.
4. Roo posts selected ready tasks into Slack.
5. Volunteers claim tasks through Roo or Linear.
6. Roo records claim state in `mlai-backend`.
7. Work happens in GitHub.
8. PR review decides completion.
9. Roo triggers or assists points approval.

Slack post format:

```text
New MLAI Tech task

Title: Add jobs daily run model
Points: 5
Difficulty: Medium
Repo: mlai-backend
Owner: @lead
Reviewer: @reviewer

Acceptance criteria:
- stores daily job run
- records source counts
- exposes status endpoint

Claim: @Roo claim MLA-123
```

## Allocation Flow

There are two allocation modes.

### Open Claim

Use for normal volunteer work.

```text
@Roo tasks open
@Roo claim MLA-123
```

Roo should:

- check the issue is claimable
- assign or comment in Linear
- record the claim in `mlai-backend`
- post a Slack confirmation
- explain next steps

### Direct Assignment

Use for trusted contributors or urgent work.

```text
@Roo assign MLA-123 to @alex
```

Roo should:

- check requester permission
- update Linear assignment
- notify the assignee
- record assignment in `mlai-backend`

## Tracking Flow

Tracking should be automatic where possible.

Minimum tracking records:

- Linear issue ID
- GitHub repo
- GitHub PR URL
- Slack user ID
- assigned points
- status
- reviewer
- submitted at
- approved at
- points ledger entry

Roo should be able to answer:

```text
@Roo what am I working on?
@Roo status MLA-123
@Roo what needs review?
@Roo what tasks are blocked?
@Roo what points are pending approval?
```

## GitHub Workflow

Every code task should use:

```text
branch: linear-id-short-description
PR title: [MLA-123] Add jobs daily run model
PR body:
- Linear issue
- What changed
- How tested
- Points requested
```

Rules:

- No direct commits to `main`.
- CI must pass before merge.
- Reviewer is accountable for acceptance criteria, not just code style.
- PR merge is not enough for points if the work does not meet the agreed outcome.

## Points Flow

Points are not money. Points are community trust, contribution memory, and reward access.

Points should be awarded only when work is accepted.

Standard flow:

1. Issue is created with proposed points.
2. Volunteer claims issue.
3. Volunteer submits PR or deliverable.
4. Reviewer approves work.
5. Roo creates or confirms points request.
6. Points admin approves.
7. `mlai-backend` writes immutable ledger entry.
8. Roo notifies the volunteer.

Example:

```text
@Roo submit MLA-123 PR https://github.com/MLAI-AUS-Inc/mlai-backend/pull/234
@Roo request 5 points for MLA-123
@Roo approve points request 456
```

## Points Bands

Use simple bands.

```text
1 point
Tiny contribution: typo, docs cleanup, small QA, helpful issue reproduction.

2-3 points
Small task: small UI fix, simple backend change, small test, content update.

5 points
Standard task: complete feature slice in one repo, clear review needed.

8 points
Larger task: backend + frontend, non-trivial integration, production risk.

13 points
Project slice: multiple files/modules, strong ownership, testing, deployment support.

20+ points
Lead work: architecture, incident handling, project leadership, mentoring, complex delivery.
```

Avoid custom point values unless there is a clear reason.

## Reviewer Responsibilities

Reviewers must check:

- acceptance criteria
- tests or verification
- security implications
- user impact
- operational risk
- whether points are fair

Reviewers should not approve vague work.

## Lead Responsibilities

Leads must:

- keep tasks small
- define acceptance criteria
- keep Linear current
- review or assign reviewers
- protect volunteers from ambiguous scope
- close stale tasks
- approve points fairly

## Roo Command Roadmap

Phase 1 commands:

```text
@Roo tasks open
@Roo task MLA-123
@Roo claim MLA-123
@Roo unclaim MLA-123
@Roo submit MLA-123 <url>
@Roo what am I working on?
@Roo points
@Roo request points for MLA-123
```

Phase 2 commands:

```text
@Roo assign MLA-123 to @user
@Roo tasks needing review
@Roo stale tasks
@Roo weekly tech summary
@Roo post ready tasks to #bounty-jobs
@Roo approve points request <id>
```

Phase 3 commands:

```text
@Roo recommend a task for me
@Roo onboard new contributor
@Roo show contributor profile
@Roo create Linear task from this thread
@Roo create GitHub issue for this task
```

## MVP Implementation Plan

### Phase 1: Operating Discipline

Goal: make work visible and claimable.

Deliverables:

- This document exists and is linked from core repos.
- Linear issue template exists.
- GitHub PR template exists.
- Every new tech task has points and acceptance criteria.
- Roo can list open tasks manually or through a basic backend endpoint.
- Points approvals are consistently recorded.

### Phase 2: Roo Task Workflow

Goal: Roo becomes the front door for volunteer work.

Deliverables:

- `mlai-backend` stores task bindings to Linear/GitHub/Slack.
- Roo can claim, submit, and report task status.
- Roo can post ready tasks into `#bounty-jobs` or `#tech`.
- Roo can show "my tasks".
- Points request can be tied to a task and PR.

### Phase 3: Web Dashboard

Goal: members can see and manage contribution work outside Slack.

Deliverables:

- member dashboard shows open tasks
- member dashboard shows claimed tasks
- member dashboard shows points history
- admin dashboard shows pending reviews and points requests
- project pages show public contribution opportunities

### Phase 4: Automation and Scale

Goal: the system becomes reliable with many volunteers.

Deliverables:

- stale task reminders
- review reminders
- weekly Tech digest
- contributor profiles
- points leaderboards by month/project
- task recommendations
- source-health dashboards for jobs/content workers

## 12-Month Roadmap

### Months 1-2: Foundations

- Adopt this operating model.
- Make `mlai-backend`, `mlai-au`, and `roo` docs consistent.
- Create Linear templates and labels.
- Define point bands.
- Require acceptance criteria for volunteer tasks.
- Create basic GitHub PR template.

### Months 3-4: Roo Work Coordination

- Add Roo commands for task list, claim, submit, and status.
- Add backend task tracking model if Linear alone is insufficient.
- Connect Roo task claims to Linear issue IDs.
- Start posting curated tasks weekly.

### Months 5-6: Jobs and Opportunities

- Build Roo Jobs Daily as the first `opportunities` domain.
- Store job runs and ranked jobs in `mlai-backend`.
- Expose full jobs list in `mlai-au`.
- Use Roo to post the daily top 7.
- Add admin source health and failure alerts.

### Months 7-9: Contributor Experience

- Add member contribution dashboard.
- Add "recommended tasks for me".
- Add contributor profile and contribution history.
- Formalize reviewer and domain-lead roles.
- Start onboarding volunteers through Roo.

### Months 10-12: Scale and Governance

- Add domain ownership.
- Add monthly architecture review.
- Add ADR process for major decisions.
- Add stronger CI/test requirements.
- Archive or classify non-core repos.
- Publish a public "Build with MLAI" contributor page.

## Simple Operating Rhythm

Weekly:

- leads groom tasks
- Roo posts ready tasks
- contributors claim work
- reviewers clear review queue
- points requests are approved

Monthly:

- review points economy
- review stale projects
- review architecture decisions
- publish contributor highlights

Quarterly:

- revisit repo structure
- review domain ownership
- review what should be archived
- review which services need stronger infrastructure

## Success Metrics

Track:

- number of ready tasks
- number of claimed tasks
- average time from claim to PR
- average time from PR to review
- average time from approval to points award
- active contributors per month
- repeat contributors per month
- volunteer tasks completed per project
- abandoned task rate
- production incidents caused by volunteer changes

## Anti-Patterns To Avoid

Avoid:

- vague tasks
- points awarded without accepted work
- work tracked only in Slack
- business logic duplicated in Roo and backend
- volunteer work with no reviewer
- large tasks given to new contributors
- private context needed to complete public tasks
- side projects pretending to be core platform

## Final Rule

Make the path from curiosity to contribution obvious:

```text
See task -> claim task -> do work -> submit PR -> get review -> earn points -> build trust.
```

Everything else should support that loop.
