# Founder blog hosting removal

Founders host their own articles. Delivery is a repository pull request or a copy-ready draft. The cleanup removes the legacy hosted-site models, routes, scheduler, publishing adapters and operational commands. A saved hosted strategy uses the repository ladder, and a saved hosted publication request cannot enter repository dispatch. `WrittenArticle` and existing Run history remain available.

Temporary article previews, snapshot storage, preview quality review, exact-site approval, retry and hosted build verification remain supported. The `ARTICLES_HOSTED_VERIFICATION_ENABLED`, `ARTICLES_HOSTED_ATTESTATION_ENABLED` and `ARTICLES_HOSTED_ENVIRONMENT_RECOVERY_ENABLED` switches describe build verification and remain in use. They do not offer founder blog hosting.

## Retirement before cleanup

The migration window is implemented in [backend #1283](https://github.com/MLAI-AUS-Inc/mlai-backend/pull/1283), [Content Factory #1223](https://github.com/drsamdonegan/content-factory/pull/1223) and [Chat #642](https://github.com/MLAI-AUS-Inc/mlai-chat/pull/642). Deploy those changes in that order before retiring any site. The choices-only `content_factory.0050_retire_hosted_article_sites` migration adds the retired state without deleting data.

Run the retirement command from that migration-window release, not this cleanup checkout. Review its `--dry-run` output first. The command pauses the site and records a durable 30-day deadline and per-article delivery receipts in its profile. Repository deliveries preserve slugs and require the founder's normal approval and merge. Copy-ready deliveries retain the draft and queue an ops alert; the transport does not send directly to the founder.

For each live article, verify the founder's own route or merged publication evidence before requesting proxy removal. A founder merges any removal PR. A missing hosting header alone cannot bypass an outstanding removal PR: Content Factory must confirm the managed proxy is absent, or the original 30-day deadline must have expired. Edited proxy files require the instructions recorded in the receipt.

Delete only the exact tagged managed Worker after the independent route and proxy checks, or the approved grace expiry. A completed receipt has site state `retired`, an empty Worker URL and withdrawn publications. A repeat command must be a no-op. Inventory every site before proceeding to table removal; a Worker can contain native repository articles even when the publication table is empty.

## Protected archive and table removal

Export both legacy tables only after every site has completed retirement. Keep the export on the backend host. The archive contains an independent Django JSON export, a PostgreSQL custom-format dump of exactly the two tables, the dump's section list and restore instructions. Decode the dump fully with `pg_restore --file=/dev/null` before uploading, and re-export the current rows to detect changes during backup.

Store the archive and its manifest in a dedicated private COLDLINE bucket with uniform access, public access prevention and deletion holds. Verify an exact-generation download with CRC32C and SHA-256, and confirm anonymous access is refused. The operator's private manifest records the object URI, generation, digest, source release and row counts and fingerprints. Do not put raw table records or credentials in the repository or pull request.

The table-drop migration must pin that verified archive and refuse any changed or unretired rows. Its PostgreSQL guard locks both tables through the atomic migration, rechecks the protected archive's exact generation and data, then deletes the publication model before the site model. Fresh empty installations do not need an archive or a storage request.

Use the normal deployment workflow with the exact reviewed migration-plan digest. It stops all runtime writers and host watchdogs before applying an approved schema change. An incomplete migration transition leaves writers stopped for schema repair. After the complete graph is applied, recovery can use the compatible new runtime with features staged off; it must not roll an old binary back onto the new schema.

Reversing the schema migration creates empty legacy tables. It does not restore archived rows. A data restore requires a separately reviewed plan: verify the archive digest, restore first into an isolated PostgreSQL database containing the referenced organisation, user and article tables, and validate the result before considering production recovery.

## Configuration and final checks

Remove only obsolete blog settings: backend `ARTICLES_HOSTED_SITES_ENABLED`, `HOSTED_PUBLISH_REQUIRES_RENDERED_PREVIEW`, `ARTICLES_HOSTED_AUTO_REGISTER_ENABLED` and `ARTICLES_HOSTED_RUNG_ORGANIZATIONS`; Content Factory `CONTENT_FACTORY_HOSTED_PROXY_SETUP_ENABLED` and `CONTENT_FACTORY_HOSTED_WORKERS_SUBDOMAIN`. Preserve preview and build-verification settings, all unrelated environment values, notification gates, caps and shared credentials.

Review credential use before revocation. A shared preview token or the operator's personal Wrangler login is not a dedicated founder-blog credential. Do not revoke either as a substitute for removing the managed blog Worker.

After cleanup, verify both tables and legacy service routes are absent, normal repository/copy-ready delivery remains available, temporary preview callbacks still authorize the original build nonce, and preview quality and build-verification checks still pass. Recheck every recorded PawShare article URL and the blog index on the founder's domain, and confirm the deleted Worker remains absent. Preserve unrelated configuration and existing operational receipts across both deployments.

## Execution record

Backend #1283, Content Factory #1223 and Chat #642 deployed successfully in order at revisions `5a9a3f9e69f95e88dda86030e84e924ca96163be`, `32775b288986c1622143ce99423650c6a99c278f` and `7589103357128889592799648d13ec54c68b194d`.

PawShare's retirement receipt completed at `2026-11-05T02:22:06.625227+00:00` with `proxy_absent`, no hosted publications and no article-delivery jobs. All 25 articles already existed on the founder's native site; their canonical routes, titles and headings passed checks before and after removal. The exact tagged Worker `mlai-blog-pawshareclub-com-70681cee` is absent from Cloudflare's API and its public route returns 404. Repeating retirement retains the same original receipt.

The protected archive was verified at `2026-11-05T02:26:54.032810+00:00`: one retired site and zero publications, COLDLINE storage in `AUSTRALIA-SOUTHEAST2`, generation `1793845488886957`, SHA-256 `c293aa206addda0885ad0fe282e4185bdd7f72462dacac5db1094f45a72cd2dd`. The archive and its manifest have verified deletion holds; uniform access and public access prevention are enforced, and an anonymous request returns 403. Raw records and database credentials remained on the backend host. The guarded `0051_remove_retired_blog_records` migration pins the exact object, generation and record fingerprints; the migration itself repeats the required archive and row checks before removing either table.

The cleanup PR still requires green CI, the exact reviewed migration-plan approval and the normal ordered backend-then-Content-Factory deployments. Final operational evidence is retained in the release ticket; this runbook records the already completed retirement and archive prerequisites.
