# Roo avatar account contract

`GET` and `PATCH /api/v1/community-chat/roo-avatar/` read and save the signed-in
member's kangaroo outfit across devices. This is the backend source contract,
not deployment evidence. The new schema requires the specifically approved
[`0016_roo_avatar` migration](roo-avatar-migration-proposal.md).

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
publish public profile metadata, award contribution Roo, spend wallet Roo, or
mint permanent badge entitlements.

## Read and write snapshot

GET and a successful PATCH return the same shape:

```json
{
  "account_id": "00000000-0000-4000-8000-000000000001",
  "community_id": "chat.example.test",
  "relay_url": "wss://chat.example.test",
  "catalog_version": "roo-avatar-v1",
  "version": 0,
  "current_level": null,
  "rank_name": null,
  "history_reconciled": false,
  "outfit": {"hair": null, "glasses": null, "jacket": null, "shoes": null},
  "effective_outfit": {"hair": null, "glasses": null, "jacket": null, "shoes": null},
  "updated_at": null,
  "catalog": {
    "hair": [{"id": "honey_tuft", "name": "Honey tuft", "required_level": 0,
      "unlocked": true, "unavailable_reason": null}],
    "glasses": [], "jacket": [], "shoes": []
  }
}
```

The example abbreviates the catalog; actual responses always include all five
items in each slot. `unavailable_reason` is null, `level_required`, or
`history_unreconciled`. None is always available and represented by JSON null,
not an extra catalog object. No saved row yields version 0, null `updated_at`
and four null selections. A saved row's timestamp is an ISO-8601 datetime.

`current_level` and `rank_name` derive from the existing verified Volunteer
contribution total and rank policy. Spendable balance, purchased Roo, unverified client activity and
unknown legacy earnings do not unlock cosmetics. Unreconciled or unclassified
history yields null rank/level and `history_reconciled: false`; starter items
with required level 0 remain available without fabricating a Curious rank.

`outfit` is the raw saved preference. `effective_outfit` suppresses choices that
are currently unavailable, preserving the saved preference if an audited rank
correction later restores eligibility. Render and initialize editing from
`effective_outfit`, while explaining temporarily unavailable saved choices.
This is current-rank eligibility, not a permanent unlock entitlement.

## Stable catalog

| Slot | Stable ID | Name | Required level |
| --- | --- | --- | ---: |
| hair | honey_tuft | Honey tuft | 0 |
| hair | cocoa_curls | Cocoa curls | 1 |
| hair | peach_fringe | Peach fringe | 2 |
| hair | lilac_bob | Lilac bob | 4 |
| hair | mint_mohawk | Mint mohawk | 6 |
| glasses | classic_black | Classic black | 0 |
| glasses | honey_rounds | Honey rounds | 1 |
| glasses | rose_hearts | Rose hearts | 3 |
| glasses | ocean_visor | Ocean visor | 4 |
| glasses | lilac_stars | Lilac stars | 6 |
| jacket | sage_bomber | Sage bomber | 1 |
| jacket | blue_denim | Blue denim | 2 |
| jacket | berry_varsity | Berry varsity | 3 |
| jacket | sunflower_raincoat | Sunflower raincoat | 5 |
| jacket | lilac_puffer | Lilac puffer | 6 |
| shoes | red_sneakers | Red sneakers | 0 |
| shoes | cream_high_tops | Cream high tops | 2 |
| shoes | bush_boots | Bush boots | 3 |
| shoes | pink_roller_skates | Pink roller skates | 4 |
| shoes | teal_runners | Teal runners | 5 |

Existing level names are Curious (0), Connected (1), Contributor (2), Regular
(3), Collaborator (4), Community Builder (5), and Steward (6). Their thresholds
remain owned by `community_chat.volunteer.policy`; this catalog adds no second
rank or badge engine. Renderers map stable IDs to assets and must not persist
array positions.

## Update

PATCH accepts exactly these fields, including all four outfit slots:

```json
{"version": 0, "outfit": {
  "hair": "honey_tuft", "glasses": "classic_black", "jacket": null, "shoes": null
}}
```

`version` is the last server version, an integer from 0 through 2147483646;
booleans, strings and floating-point versions are rejected. The first save
creates version 1. Every successful replacement advances it by one. Locks
serialize writes across devices, including concurrent first saves. Both the
version and current eligibility are checked within the transaction. A failed
write leaves the stored outfit unchanged.

| Status | `error` and `outcome` | Meaning |
| --- | --- | --- |
| 400 | invalid_roo_avatar | Wrong fields, missing slot, invalid version or unknown/cross-category ID |
| 403 | roo_avatar_item_locked | At least one selected item is unavailable at the freshly verified rank |
| 409 | roo_avatar_version_conflict | Another device saved after the client's read |
| 503 | roo_avatar_scope_unavailable | Missing trusted community/relay configuration |

Validation errors may include `slots`; locked errors include all unavailable
slots. Authentication/admission errors retain the existing account API format.
On a conflict, retain the local draft for review and reload; never silently
retry with a fresh version. On a locked response, reload eligibility before
saving again. All responses have `Cache-Control: private, no-store`.
