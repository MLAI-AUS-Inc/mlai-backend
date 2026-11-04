"""Finite account liveness proofs without treating optional user APIs as access."""

from django.core.cache import cache
from django.utils import timezone

from .website_contract import evidence_digest


# How long a probe result stays readable by observation-only projections (the
# bootstrap and website journey never probe). A verified result has to outlive
# the article form, or a ready website reads as "not set up" mid-form; article
# admission always re-probes (force=True), and any local revocation, reconnect
# or reinstall changes the observation key, so a longer positive window cannot
# admit work on revoked access. Failures stay short so a fix shows quickly.
VERIFIED_ACCESS_OBSERVATION_SECONDS = 15 * 60
FAILED_ACCESS_OBSERVATION_SECONDS = 15


def access_observation_seconds(verified) -> int:
    return VERIFIED_ACCESS_OBSERVATION_SECONDS if verified else FAILED_ACCESS_OBSERVATION_SECONDS


def verify_account_access(user, *, installations=(), connection=None, force=False, allow_probe=True):
    """Read a finite observation, probing only at an explicitly allowed boundary."""
    from integrations.services.github_app import probe_installation_liveness, INSTALLATION_LIVE
    ids = {str(row.installation_id) for row in installations}
    if connection is not None and connection.authorized_by_id == user.pk and connection.installation_id and connection.state != "revoked":
        ids.add(str(connection.installation_id))
    key = "website-account-access-v2:" + evidence_digest({"user": user.pk, "installations": sorted(ids)})
    existing = None if force and allow_probe else cache.get(key)
    if existing is not None:
        return existing
    if not allow_probe:
        return {"verified": False, "reasonCode": "github_verification_required", "status": "checking"}
    # A bounded lease prevents concurrent page reads from creating probe storms.
    lease = key + ":lease"
    if not cache.add(lease, True, timeout=15):
        return {"verified": False, "reasonCode": "github_probe_in_progress", "status": "checking", "leaseExpiresIn": 15}
    try:
        verified = any(probe_installation_liveness(identifier) == INSTALLATION_LIVE for identifier in sorted(ids))
        proof = {"verified": verified, "reasonCode": "" if verified else "github_access_required", "checkedAt": timezone.now().isoformat()}
        cache.set(key, proof, access_observation_seconds(verified))
        return proof
    finally:
        cache.delete(lease)
