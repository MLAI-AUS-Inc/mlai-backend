# Community event ticket: 32 Roo Points

The approved EVENT_TICKET price is 32 Roo Points for one fully covered ticket whose regular price is strictly below A$100. Tickets at A$100 or above are excluded. Already-free tickets do not need points. The catalogue model remains the single source for the Home API, rewards API, affordability and redemption charge.

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

This validates the exact managed event and ticket, saves a 100% single-use offer at 32 points, and expires it no later than event start or ticket sales close. It does not issue a coupon or spend any points. It rejects changes while an event has processing redemptions. Approval-required, hidden, flexible, non-AUD, unavailable and group-registration tickets remain excluded. Issuance rechecks the live ticket price before creating a coupon, including a price increase to A$100 after the quote.
