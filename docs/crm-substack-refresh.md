# Daily Substack activity export

The owner authorized daily CRM ratings from Substack, Luma, public Slack and
account logins, preserving consent and unsubscribe preferences and sending no
email. The server computes ratings after 06:00 Australia/Melbourne. A local
scheduled Codex heartbeat supplies the Substack export at 05:30.

## Browser source

Use only the official ChatGPT Chrome extension through Computer Use. Do not use
Kimi, another browser driver, copied cookies or an undocumented HTTP endpoint.
The publication is **MLAI Aus**, at
`https://mlaiaus.substack.com/publish/subscribers`.
The tested Chrome profile is `mlai.au`, extension instance
`be96f920-c475-4a2b-bf70-7070b78f08bd`; discover current browser IDs rather than
assuming a saved numeric ID is still valid.

Verify the publication and signed-in dashboard. If sign-in, CAPTCHA or new
permissions are required, stop and report the specific action needed. Do not
request a login email or change any account/security settings.

1. Open Audience → Subscribers. Leave the list unfiltered and do not select or
   modify individual subscribers.
2. Open the table's more menu and choose Export.
3. Choose Export all columns, then Export. Generation may finish by changing
   the control to Download last export; click that control if needed.
4. Verify a **new** `subscriber-export-YYYY-MM-DD-HH-MM-SS.csv` under
   `/Users/samdonegan/Downloads` (also exposed in iCloud Downloads). Browser
   download events can time out even when the file successfully lands. Inspect
   the visible export state and filesystem before repeating a download.
5. The timestamp in this filename is UTC. Parse it explicitly, require that
   its Melbourne date is the current day and that it is not in the future.
   Reject an old cached export. Do not use download time to disguise old data.
6. Validate required `Email` and `Last clicked at` columns and compute SHA-256.
   Do not log rows, addresses, names, or session data.

This is a download, not a newsletter send. Never use Compose/Create, subscriber
add/remove, subscriptions, notification controls or other messaging actions.

## Private source ingestion

Use established SSH alias `mlai-droplet`. Create a private mode-700 directory
`/root/mlai-crm-substack-activity/` and copy the validated CSV there; use mode600
on the file. Quote paths safely. Copy it into the stable
`mlai-backend-web-1` container under `/tmp/` with `docker cp`.

Run this source-only management command in that container, using the verified
file checksum and the UTC filename timestamp:

```text
python manage.py crm_import_newsletter_activity /tmp/FILE.csv
  --expected-sha256 SHA --exported-at YYYY-MM-DDTHH:MM:SSZ
  --actor-email sam@mlai.au
```

The default is rollback-only. Inspect the aggregate result, then repeat with
`--apply`. A completed import or `newer_or_equal_skipped` is successful; record a
small local receipt with source timestamp, checksum and counts. No row data.
Verify that the result is from this command, not a sending command.

Never invoke warm-up, blast dispatch, source-consent sync, a full scheduler tick,
provider test sends, migrations, service restarts or deployments from this task.
Never modify mailboxes, sender caps, enabled flags, roster, suppressions or
consent. Missing subscribers are not opt-outs. New subscribers absent from CRM
are skipped. Do not infer or grant permission from subscriber type or activity.

The normal server job performs the rating sweep. If export access or ingestion
fails, retain the previous source and report the blocker; never claim fresh
coverage, clear a hold, or manufacture dates. The source's successful export
cursor, not local download time, determines freshness.
