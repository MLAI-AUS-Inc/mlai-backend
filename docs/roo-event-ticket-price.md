# Community event ticket: 15 Roo Points

The approved EVENT_TICKET price is 15 Roo Points. The catalogue model remains the single source for the Home API, rewards API, affordability and redemption charge.

After deploying the command, preview against the intended environment:

```sh
python manage.py set_community_event_ticket_price
```

Apply the approved catalogue update:

```sh
python manage.py set_community_event_ticket_price --apply
```

This locks and changes only EVENT_TICKET.cost_points. It does not create a migration, change balances or stock, or create a redemption. Repeating the command is safe. A missing reward fails without creating a replacement. Verify the rewards API returns 15 before publishing matching public website pricing.

Manual redemptions use the catalogue price at approval time; already completed ledger entries are unchanged.
