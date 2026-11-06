# D3 — one publication policy

15 October 2026. The shared policy requires three explicit validated facts:
current article integration, repository write authority and live deployment.
Missing facts remain false. Activation previously defaulted a missing deployment
claim to true; the journey allowed publishing after deployment with no write
proof. Legacy prepare/status readers also trusted a bare derived publish label.

`article_publication_policy.can_publish_article` now owns that decision.
Activation's generation/publishing gate and the journey call it with their
validated evidence. Legacy prepare and public status use its journey reader,
which checks the underlying facts and current write policy. Existing enrolled
goal completion still requires the recorded milestone proofs, separate from
the capability to publish another article.

Generation readiness continues to describe writing a draft; it does not grant
publication without the deployment fact. Existing repository preparation paths
remain unchanged. This step has no models, migrations or customer operations.

The actual capability/journey/status/prepare truth table includes missing
deployment, read-only access, paused writes and a forged bare publish label.
Those cases failed before the change. The focused policy and neighboring
activation/journey/status/goal suite passes 114 tests. Full CI remains required.
