# Retire founder blog hosting

Founders host their own articles. Delivery is a repository pull request or a copy-ready draft. Temporary article previews and hosted build verification remain supported.

The retirement policy is permanent: rollout flags cannot offer new hosted publishing, registration or activation. Existing site/publication service reads and in-flight reporting remain available during migration. A saved hosted strategy pin falls back to repository setup. Historical live publications remain history.

## Operations order

1. Explicitly disable automatic registration and clear the hosted rung allowlist; disable Content Factory proxy setup. Keep `ARTICLES_HOSTED_SITES_ENABLED` enabled while in-flight callbacks need it.
2. Inventory every `HostedArticleSite` and its publications in the private migration record. Do not infer article counts from a screenshot or a listing page.
3. Deploy the coordinated backend, Content Factory and Chat retirement changes. Apply `content_factory.0050_retire_hosted_article_sites` (state choices only).
4. Review `python manage.py retire_hosted_article_sites --domain pawshareclub.com --dry-run`. It performs no writes or provider calls.
5. Run the same command without `--dry-run`. It first pauses the site, then records a durable 30-day deadline and per-article delivery receipts in `profile.retirement`.
6. Repeat after founder action. Repository articles retain their slug and require the founder to merge; the backend merge worker never merges retirement publications. Missing setup or publish consent remains a founder action. Copy-ready deliveries retain the draft and queue an ops alert with the private site receipt identifier. The existing alert transport sends to ops, not directly to founders: ops must communicate the receipt's URLs and any manual removal instructions to the founder.
7. Once every article is independently served or has merged publication evidence, request the proxy-removal PR. The founder merges `mlai/remove-blog-proxy`. Edited proxies require manual review; exact generated removal instructions are retained in the private receipt.
8. Recheck the article URLs and index without `x-mlai-hosted: 1` before deleting the managed Worker. At the durable deadline, the approved 30-day grace policy permits deletion even if founder action remains incomplete; queue the expiry alert. Content Factory independently rechecks live routes or that deadline and refuses to delete an untagged Worker.
9. A retired site has an empty Worker URL and withdrawn publications; `WrittenArticle` history is preserved. Repeated runs are no-ops.

## Later cleanup gate

Do not remove the service endpoints, model tables, legacy source/domain ownership references, or Worker credentials until every site is retired. Export both hosted tables to protected cold storage before proposing their drop migration. Keep snapshot storage, article sanitization, preview infrastructure, preview quality checks, and hosted build verification. Review Cloudflare token use by previews before revoking any scope.
