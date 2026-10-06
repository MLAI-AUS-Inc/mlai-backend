"""Finite account liveness proofs without treating optional user APIs as access."""

from django.core.cache import cache
from django.utils import timezone

from .website_contract import evidence_digest


def verify_account_access(user, *, installations=(), connection=None, force=False):
    """Probe owned installation health independently of repository selection."""
    from integrations.services.github_app import probe_installation_liveness, INSTALLATION_LIVE
    ids = {str(row.installation_id) for row in installations}
    if connection is not None and connection.authorized_by_id == user.pk and connection.installation_id and connection.state != "revoked":
        ids.add(str(connection.installation_id))
    key = "website-account-access-v2:" + evidence_digest({"user": user.pk, "installations": sorted(ids)})
    existing = None if force else cache.get(key)
    if existing is not None:
        return existing
    # A bounded lease prevents concurrent page reads from creating probe storms.
    lease = key + ":lease"
    if not cache.add(lease, True, timeout=15):
        return {"verified": False, "reasonCode": "github_probe_in_progress", "status": "checking", "leaseExpiresIn": 15}
    try:
        verified = any(probe_installation_liveness(identifier) == INSTALLATION_LIVE for identifier in sorted(ids))
        proof = {"verified": verified, "reasonCode": "" if verified else "github_access_required", "checkedAt": timezone.now().isoformat()}
        cache.set(key, proof, 60 if verified else 15)
        return proof
    finally:
        cache.delete(lease)
