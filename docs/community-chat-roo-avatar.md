# Roo avatar account contract

`GET` and `PATCH /api/v1/community-chat/roo-avatar/` read and save the signed-in
member's kangaroo outfit across devices. This is the backend source contract,
not deployment evidence. The schema requires the specifically approved
[`0016_roo_avatar` migration](roo-avatar-migration-proposal.md), the [`0019_roo_avatar_fur` migration](roo-avatar-fur-migration-proposal.md), and `0020_roo_avatar_owned_items` for ownership.

## Identity and authority

Use the existing Chat account bearer session (native) or account cookie (web).
Cookie writes require the exact trusted origin bound to that session. Bootstrap
and usage-reporter credentials cannot access this endpoint. The server rechecks
active account, admission, session expiry/revocation, auth version and token
rotation after locking the canonical account and session.

The outfit belongs to (`core.User`, trusted Volunteer community). The response
`account_id` is the canonical **public UUID** `user.community_chat_profile_id`,
matching account `profile.public_id`; it is never the internal numeric User ID.
`community_id` comes from trusted deployment settings, and `relay_url` is the
configured relay. Clients must reject responses belonging to a different active
account/relay, and reset drafts and cached snapshots on an account or community
switch. Body identity, rank, eligibility and community overrides are rejected.

Outfits are private account preferences. They do not replace `avatar_url`,
publish public profile metadata, award contribution Roo, or mint badges. Confirmed purchases spend wallet Roo.

## Read and write snapshot

GET and a successful PATCH return the same shape:

```json
{
  "account_id": "00000000-0000-4000-8000-000000000001",
  "community_id": "chat.example.test",
  "relay_url": "wss://chat.example.test",
  "catalog_version": "roo-avatar-v1",
  "version": 0,
  "available_points": 12,
  "current_level": null,
  "rank_name": null,
  "history_reconciled": false,
  "outfit": {"fur": null, "hair": null, "glasses": null, "jacket": null,
    "shoes": null},
  "effective_outfit": {"fur": null, "hair": null, "glasses": null,
    "jacket": null, "shoes": null},
  "updated_at": null,
  "catalog": {
    "fur": [{"id": "golden_sand", "name": "Golden sand", "required_level": 1,
      "unlocked": false, "unavailable_reason": "history_unreconciled", "price_points": 1, "owned": false}],
    "hair": [], "glasses": [], "jacket": [], "shoes": []
  }
}
```

The example abbreviates the catalog; actual responses always include all five
items in each slot, and `outfit`, `effective_outfit` and `catalog` always
include `fur`. `unavailable_reason` is null, `level_required`, or
`history_unreconciled`. None is always available and represented by JSON null,
not an extra catalog object. No saved row yields version 0, null `updated_at`
and five null selections. A saved row's timestamp is an ISO-8601 datetime.

The wardrobe and the private journey use the same 200-level contribution
ladder as published member profiles. The verified contribution total determines
`current_level` and `rank_name` independently of wallet balance and profile
publication. When the total is unknown, both fields are null and
`history_reconciled` is false.

`outfit` is the saved preference. Render and initialize the editor from
`effective_outfit`, which includes owned items even after a level correction.

## Stable catalog

| Slot | Stable ID | Name | Required level | Roo Points |
| --- | --- | --- | ---: | ---: |
| fur | golden_sand | Golden sand | 1 | 1 |
| hair | honey_tuft | Tuft | 2 | 1 |
| glasses | classic_black | Classic black | 3 | 2 |
| shoes | red_sneakers | Red sneakers | 4 | 2 |
| fur | blue_flier | Blue flier | 5 | 3 |
| hair | cocoa_curls | Curls | 6 | 3 |
| glasses | honey_rounds | Honey rounds | 7 | 4 |
| shoes | cream_high_tops | Cream high tops | 8 | 5 |
| fur | ironbark | Ironbark | 9 | 6 |
| hair | peach_fringe | Swept fringe | 10 | 7 |
| glasses | rose_hearts | Rose hearts | 12 | 8 |
| shoes | bush_boots | Bush boots | 14 | 10 |
| fur | snow_gum | Snow gum | 16 | 12 |
| hair | lilac_bob | Bob | 18 | 14 |
| glasses | ocean_visor | Ocean visor | 19 | 15 |
| shoes | pink_roller_skates | Pink roller skates | 20 | 16 |
| fur | midnight_wallaroo | Midnight wallaroo | 22 | 18 |
| hair | mint_mohawk | Mohawk | 24 | 21 |
| glasses | lilac_stars | Lilac stars | 25 | 23 |
| shoes | teal_runners | Teal runners | 26 | 24 |
| jacket | sage_bomber | Sage bomber | 28 | 27 |
| jacket | blue_denim | Blue denim | 30 | 30 |
| jacket | berry_varsity | Berry varsity | 32 | 33 |
| jacket | sunflower_raincoat | Sunflower raincoat | 34 | 36 |
| jacket | lilac_puffer | Lilac puffer | 36 | 40 |

The default Outback red coat and empty accessory slots are free at every level.
Each catalog item has a one-time price. `owned` includes both purchases and
previously equipped legacy items. `unlocked` means the member can equip the
item, either through ownership or by reaching its purchase level.
`available_points` is the whole-point digital-service balance after reservations.

## Update

PATCH accepts the saved version, outfit, and an optional confirmed purchase total in whole Roo Points.

```json
{"version": 0, "purchase_points": 3, "outfit": {
  "fur": null, "hair": "honey_tuft", "glasses": "classic_black",
  "jacket": null, "shoes": null
}}
```

`hair`, `glasses`, `jacket` and `shoes` are all required. `fur` is optional so
that clients released before the fur slot keep working: when `fur` is absent,
the saved fur is left unchanged (null for a first save). When present, `fur` is
validated like any other slot, and null selects the default fur. Any other
missing or extra slot is rejected.

`version` is the last server version, an integer from 0 through 2147483646;
booleans, strings and floating-point versions are rejected. The first save
creates version 1. Every successful replacement advances it by one. Locks
serialize writes across devices, including concurrent first saves. Both the
version and current eligibility are checked within the transaction. The server sums the catalog price of each eligible unowned selection.
The supplied `purchase_points` must equal this sum. Omission consents to a
zero-cost equip only. The digital-first wallet debit, saved outfit, ownership
and version commit together. A repeated version cannot charge twice.

| Status | `error` and `outcome` | Meaning |
| --- | --- | --- |
| 400 | invalid_roo_avatar | Wrong fields, missing slot, invalid version or unknown/cross-category ID |
| 402 | roo_avatar_insufficient_points | The wallet cannot cover the confirmed total |
| 409 | roo_avatar_price_changed | Reload the catalog and explicitly confirm the new total |
| 403 | roo_avatar_item_locked | At least one selected item is unavailable at the freshly verified rank |
| 409 | roo_avatar_version_conflict | Another device saved after the client's read |
| 503 | roo_avatar_scope_unavailable | Missing trusted community/relay configuration |

Validation errors may include `slots`; locked errors include all unavailable
slots. Authentication/admission errors retain the existing account API format.
On a conflict, retain the local draft for review and reload; never silently
retry with a fresh version. On a locked response, reload eligibility before
saving again. All responses have `Cache-Control: private, no-store`.

`catalog_version` stays `roo-avatar-v1`: released iOS builds refuse any other
value. Clients can omit `fur` on updates to retain the saved coat. Native
builds that validate item or member levels against the old maximum of six
require a client update to read this catalog.
