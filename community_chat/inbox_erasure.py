"""Verified relay cursor cleanup within the existing durable deletion ledger."""

import hashlib

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from . import adapter, deletion_tasks
from .inbox_accounts import account_key
from .models import AccountDeletionRequest, AccountDeletionTask, CommunityChatDevice, DeviceBindingStatus

TARGET = "relay_inbox_cursors"
PROTOCOL = "inbox_erasure_v1"


class ErasureVerificationError(RuntimeError):
    """Cleanup cannot be verified without another storage boundary or operator."""


def purge_relay_cursors(user_id, public_keys):
    """Purge grouped and pre-binding singleton positions; retain only counts.

    The caller holds the user's enrollment lock after access revocation. The
    relay independently rejects any account that still has active authority.
    """
    capabilities, _ = adapter._request("GET", "/v1/capabilities")
    protocols = capabilities.get("member_account_protocols")
    if not isinstance(protocols, list) or PROTOCOL not in protocols:
        raise adapter.MembershipAdapterUnavailable("adapter_protocol_unavailable")
    community = capabilities.get("community_id")
    grouped = account_key(community, user_id)
    accounts = {grouped}
    for public_key in public_keys:
        try:
            key = bytes.fromhex(public_key)
        except (ValueError, TypeError) as exc:
            raise ErasureVerificationError("invalid device authority") from exc
        if len(key) != 32:
            raise ErasureVerificationError("invalid device authority")
        accounts.add(hashlib.sha256(b"singleton:" + key).hexdigest())
    counts = {"deleted_rows": 0, "remaining_rows": 0, "verified_targets": 0}
    for key in sorted(accounts):
        result, _ = adapter._request("DELETE", f"/v2/inbox-accounts/{key}")
        if (result.get("status") != "erased" or result.get("account_key") != key
                or result.get("community_id") != community
                or any(type(result.get(name)) is not int or not 0 <= result[name] < 2**63
                       for name in ("deleted_rows", "remaining_rows"))
                or result["remaining_rows"] != 0):
            raise ErasureVerificationError("invalid relay deletion evidence")
        counts["deleted_rows"] += result["deleted_rows"]
        counts["verified_targets"] += 1
        if counts["deleted_rows"] >= 2**63:
            raise ErasureVerificationError("deletion evidence overflow")
    return counts


def execute_task(task_id):
    """Complete only this target after revoked access and read-after-delete proof.

    No flag-off call claims a lease, contacts the adapter, or opens a database.
    A lost completion ACK safely retries the idempotent relay deletion. A stale
    lease cannot mark cleanup complete or change another worker's retry state.
    """
    if not settings.COMMUNITY_CHAT_INBOX_ERASURE_ENABLED:
        return False
    task = deletion_tasks.claim_task(task_id)
    if task is None:
        return False
    try:
        if task.target != TARGET or task.request.user_id is None:
            raise ErasureVerificationError("cleanup authority unavailable")
        with transaction.atomic():
            user = get_user_model().objects.select_for_update().get(pk=task.request.user_id)
            record = AccountDeletionRequest.objects.select_for_update().get(pk=task.request_id)
            current = AccountDeletionTask.objects.select_for_update().get(pk=task.pk)
            if (current.status != AccountDeletionTask.Status.PROCESSING
                    or current.attempts != task.attempts):
                return False
            if (record.user_id != user.pk
                    or not record.tasks.filter(target="chat_access", status=AccountDeletionTask.Status.COMPLETED).exists()
                    or CommunityChatDevice.objects.filter(user_id=user.pk).exclude(status=DeviceBindingStatus.REVOKED).exists()):
                raise ErasureVerificationError("access revocation must complete first")
            keys = list(CommunityChatDevice.objects.filter(user_id=user.pk)
                        .order_by("public_key").values_list("public_key", flat=True).distinct())
            counts = purge_relay_cursors(user.pk, keys)
            return deletion_tasks.complete_task(task.pk, attempt=task.attempts, verified_counts=counts)
    except adapter.MembershipAdapterConflict:
        code = "operator_review_required"
    except adapter.MembershipAdapterUnavailable:
        code = "provider_unavailable"
    except (ErasureVerificationError, get_user_model().DoesNotExist):
        code = "verification_failed"
    deletion_tasks.fail_task(task.pk, attempt=task.attempts, error_code=code)
    return False
