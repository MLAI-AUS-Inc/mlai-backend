# Community event ticket: 32 Roo Points

The approved EVENT_TICKET price is 32 Roo Points for one fully covered ticket whose regular price is strictly below A$100. Tickets at A$100 or above are excluded. Already-free tickets do not need points. The server catalogue and ticket policy determine displayed prices, affordability and the charged quote.

The account-authenticated **Member perks** modal also derives a 16-point, 50%
ticket discount from that same configured full-ticket offer. Both tiers use the
same live ticket, stock and one-per-member event limit. The backend freezes the
chosen tier and exact points cost in the quote and charges that saved cost only
after explicit confirmation and successful coupon issuance. No separate half-price
catalogue entry or schema change is required.

After deploying the command, preview against the intended environment:

```sh
python manage.py set_community_event_ticket_price
```

Apply the approved catalogue update:

```sh
python manage.py set_community_event_ticket_price --apply
```

This locks and changes only EVENT_TICKET.cost_points. It does not create a migration, change balances or stock, or create a redemption. Repeating the command is safe. A missing reward fails without creating a replacement. Verify the rewards API returns 32 before publishing matching public website pricing.

New ticket redemptions must use an event-specific quote, so the generic reward path cannot bypass the live price and eligibility checks. Already completed ledger entries are unchanged.

Configure each eligible event with the preview-first command:

```sh
python manage.py configure_event_ticket_offer --event-id evt-EXAMPLE --ticket-type-id ttype-EXAMPLE
# Add --apply only after reviewing the preview.
```

This validates the exact managed event and ticket, saves a 100% single-use offer at 32 points, and expires it no later than event start or ticket sales close. It does not issue a coupon or spend any points. It rejects changes while an event has processing redemptions. Hidden, flexible, non-AUD, unavailable and group-registration tickets remain excluded. Issuance rechecks the live ticket price before creating a coupon, including a price increase to A$100 after the quote.

Approval-required tickets are allowed under the user's 30 October approval, including Stranger Builds. The quote explicitly states that organiser approval is not guaranteed and points are spent when the coupon is issued, before approval. A change to approval requirements after the quote prevents issuance and requires a new quote. Roo never changes Luma's admission settings.

## Automatic member perks priced from the ticket (November 2026)

`ROO_EVENT_PERKS_AUTO` replaces hand-made offers for Member perks:

- `off` (default): only offers made with `configure_event_ticket_offer`, at the fixed 16/32 prices above.
- `test`: automatic offers only for event ids in `ROO_EVENT_PERKS_TEST_EVENT_IDS`, only for staff accounts. Listed test events may be private on Luma.
- `on`: any event the MLAI calendar manages on Luma (`access: manage`, `platform: luma`; events only listed on the calendar are excluded) gets an offer when a member opens Member perks.

The offer covers the most expensive visible, fixed-price, paid AUD ticket under A$100 that is on sale now, and expires at the earlier of event start and ticket sales close. Its reward code starts `EVENT_PERK_`. With the switch `on`, every Member perks quote is priced from the ticket, 1 Roo Point = A$1, rounded up: half price costs 50% of the ticket, free costs 90% (A$40: 20 and 36). Issuance re-reads the ticket and refuses the quote if the price changed.

After issuance the code is emailed once (`event_perk_code`, only when that key is in `SYSTEM_EMAIL_RESEND_MESSAGES`); it is always in the app.

Cancel an unused code and refund its exact points (preview first):

```sh
python manage.py cancel_event_perk --redemption <id>
python manage.py cancel_event_perk --redemption <id> --apply
```

It refuses a used or already disabled code, disables the code in Luma and verifies it before refunding.
