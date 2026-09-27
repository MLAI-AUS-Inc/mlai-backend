# Monthly startup benefits

The Founder Tools and MLAI Chat approval APIs share the same reward and
coworking services. An ABR-verified active Australian organisation qualifies;
an eligible nonprofit such as an incorporated association does not need an
ACN. A claimed registration, valid checksum alone, or old synthetic ABR
verification does not qualify.

Saving or generating a draft does not complete the update. A founder must
review and approve its exact revision and audience. Private approvals qualify
as well as community publications. Future reporting months cannot be approved.

Each startup earns 20 points once per reporting month. Approval and credit
commit together. A failed credit returns a retryable 503 and rolls back that
approval; repeating the request cannot credit the same update twice. A second
company record pointing at the same startup organisation/month cannot claim
another payment. Volunteer ranking retains its shared personal monthly cap,
but that cap does not suppress another startup's completion payment.
New payments use an organisation/month ledger key and also recognise earlier
company/month keys. Deleting derived report data and recreating the same month
does not mint more points or restart the paid benefit window.

The qualifying founder/director pays 4 points rather than 8 for coworking
during the 30 days following first approval. Editing or approving another
revision of the same month does not restart the window or remove an existing
benefit. The exact expiry instant is excluded. Bookings use Melbourne dates;
advance bookings must also begin before expiry. Eligibility is checked again
when calculating the authoritative booking price, including ABR verification
and the founder binding's `coworking_discount_eligible` override.
Existing revision approval records and the first publication timestamp take
precedence over older generation-time `ready_at` stamps; no database backfill is required.

Runtime pricing remains governed by the existing catalogue and settings.
Before rollout, confirm the active `COWORKING_DAY` catalogue item is 8,
`COWORKING_DAY_COST_POINTS=8`, `COWORKING_DAY_DISCOUNT_COST_POINTS=4`, and
`ROO_POINTS_MONTHLY_UPDATE_REWARD=20`. This change does not alter production
catalogue rows or environment variables.

The Roo reminder scheduler is described in
[monthly-update-reminders.md](monthly-update-reminders.md).

## Validation boundary

`roo.tests_startup_update_benefits_unit` exercises the benefits without a
database or network, including expiry, DST, nonprofits, approval clocks,
idempotency keys, scoped reward history and failure propagation. Database
integration tests require a specific disposable migration approval under
[AGENTS.md](../AGENTS.md). No migrations or live operational actions were run
while preparing this change.
