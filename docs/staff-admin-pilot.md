# Staff Admin pilot

22-10-26. Existing database only, no tables or migrations. No provider calls.
STAFF_ADMIN_PANEL_ENABLED defaults false and is exposed as
feature_flags.staff_admin_panel by Community Home. The isolated Chat staging
profile enables it beside mentoring, following 6a7c7d5. The client also requires
a successful access response. Disabled returns 404; denied roles return 403.

## Exact mapping

An active authenticated account receives the first matching role below. There
is no permission union. Only active PointsAdmin records linked by user FK count;
Slack-only appointments grant nothing. Portfolio grants require appointment role
committee, portfolio_lead or admin; partner and unknown roles grant nothing. Inactive accounts receive no role.

| Priority | Panel role and committee title | Existing model condition |
| --- | --- | --- |
| 1 | Superadmin / President | Verified-session ChatRole owner, or Django is_superuser |
| 2 | Admin / Vice President | Verified-session ChatRole admin |
| 3 | Community / Community Manager | Active Moderator, or approved staff PointsAdmin portfolio ops |
| 4 | Marketing and Partnerships | PointsAdmin role portfolio_lead, portfolio marketing or sales |
| 5 | Tech / Tech Lead | Approved staff PointsAdmin portfolio tech |
| 6 | Finance / Treasurer | Unassigned until an explicit operator-approved mapping exists |
| 7 | Tech / Tech Lead | Remaining Django is_staff accounts |

Open operator question: which explicit existing appointment mapping identifies
the Treasurer? No existing field distinguishes that office from ordinary
committee appointments, including the default created by Points Admin. Empty
or null portfolios grant no Finance access. Add an approved mapping to
compute_role and the member-query role filter together before assigning Finance.
The Finance permission matrix remains defined for that future mapping.
Member lists display stored ChatRole appointments directly. Viewer owner/admin
authority still requires the session's verified device, as in staff check
03088c6. Moderator and PointsAdmin grants are account based. Relay permissions
and Django staff flags are unchanged.

The fixed permission matrix matches the approved mockup. All new pages are read
only, even where future policy says Edit. Mentoring writes require Superadmin,
Admin or Community; Marketing and Tech can read only. Member search for invites
requires mentoring edit access. Read-only mentoring responses use explicit
allowlists with no person emails, private calendar email, space notes or provider
error text. Editors retain the existing private mentoring contract. The original mentoring
staff policy remains when this panel flag is false. Its old tab and endpoints
continue under MENTOR_BOOKING_ENABLED.

## API

All new routes are GET beneath /api/v1/community-chat/admin/, with existing Chat
account auth, private no-store responses and a 30/minute scoped throttle.

| Route | Response and filters |
| --- | --- |
| access/ | Viewer role, label, committee title and view/edit section grants |
| overview/ | Permitted counts, attention groups, badges and snapshot time |
| members/ | Masked accounts; q, role, active=true/false, joined=7d/30d/month |
| members/<id>/ | Masked account details, Chat role and computed panel role |
| roo/ | tab=balances/ledger/requests/redemptions; status on the latter two |
| startups/ | Monthly update/reminder statuses and connector counts; q, updated=true/false |
| roles/ | Read-only six-role matrix, for Superadmin and Admin |

Lists accept page and page_size, default 25, maximum 100. Invalid filters or
pages return 400. Filtering precedes pagination. Member relations are selected
and prefetched; startups use Exists/Subquery; Roo uses selected relations.
Existing mentoring lists retain their bounded 200-row contract.

## Data limits

Members use core.User, ChatRole, Moderator and PointsAdmin. This is the backend
account directory, without relay-only members or live presence. Active means
is_active. Emails remain masked on detail too; no reveal flow is introduced.

Roo uses PointsAccount, Ledger, PointsRequest, RewardRedemption and catalog
names. Balances sort by initialized microroo or legacy balance converted to microroo
before pagination, matching the client display. Microroo takes precedence when initialized; legacy ledger deltas remain
supported. Requests have Slack identities, so rows identify request numbers.
Approvals stay in Slack. No purchases, checkout URLs, acceptance details,
request reasons, redemption notes or tokens are serialized.

Startup Pulse uses StartupProfile/Organization, ready MonthlyUpdateDraft rows
for this reporting month, and the latest MonthlyUpdateReminderDelivery to any
bound founder through UserStartupBinding. A reminder may cover several startups.
ExternalServiceConnection provider/status counts are stored state, not live
health, Gmail sync status or worker readiness. No raw artifacts, update prose
or financial figures are read. The inspected Vibe Raising staff API uses these
models, but exposes private update detail and a broader PointsAdmin policy; the
pilot therefore uses a narrower projection.

Overview queries only permitted sections. Mentoring counts, attention entries
and badges are omitted without querying mentoring when MENTOR_BOOKING_ENABLED
is false. It counts each problematic mentoring
session once, pending mentor applications, pending Roo requests, unfulfilled
redemptions and startups lacking a ready update. Other sections are role-filtered
coming-soon descriptions. Moderation stays at the private admin host; no relay
/api/admin/v1 route or ingress boundary changes.

Database-free tests cover permissions, redaction, filters, pagination and SQL
compilation against model metadata. They do not execute SQL or test query plans.
