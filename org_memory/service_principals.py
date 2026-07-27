from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional
from uuid import UUID

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone

from .models import (
    ServicePrincipal,
    ServicePrincipalAuditEvent,
    ServicePrincipalCredential,
)


TOKEN_PREFIX = "mlai_sp_"
TOKEN_PATTERN = re.compile(
    rf"^{TOKEN_PREFIX}(?P<credential_id>[0-9a-f]{{32}})\.(?P<secret>[A-Za-z0-9_-]{{32,128}})$"
)
SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{1,127}$")
VALID_SURFACES = frozenset({"public_roo", "admin_roo", "worker", "operator"})
_DUMMY_SECRET_HASH = make_password("invalid-service-principal-secret")


class ServicePrincipalCredentialError(ValueError):
    pass


@dataclass(frozen=True)
class ServicePrincipalAuthContext:
    principal: ServicePrincipal
    credential: ServicePrincipalCredential
    token: str = field(repr=False)


@dataclass(frozen=True)
class ServicePrincipalUser:
    principal: ServicePrincipal

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False

    @property
    def pk(self) -> str:
        return f"service-principal:{self.principal_id}"

    @property
    def principal_id(self):
        return self.principal.pk

    def __str__(self) -> str:
        return f"service-principal:{self.principal.name}"


def normalize_scopes(values: Iterable[str]) -> list[str]:
    scopes = sorted({str(value).strip() for value in values if str(value).strip()})
    invalid = [value for value in scopes if not SCOPE_PATTERN.fullmatch(value)]
    if invalid:
        raise ServicePrincipalCredentialError(
            "Invalid service-principal scopes: " + ", ".join(invalid)
        )
    return scopes


def normalize_surfaces(values: Iterable[str]) -> list[str]:
    surfaces = sorted({str(value).strip() for value in values if str(value).strip()})
    invalid = [value for value in surfaces if value not in VALID_SURFACES]
    if invalid:
        raise ServicePrincipalCredentialError(
            "Invalid service-principal surfaces: " + ", ".join(invalid)
        )
    return surfaces


def record_service_principal_audit(
    event_type: str,
    *,
    principal: Optional[ServicePrincipal] = None,
    credential: Optional[ServicePrincipalCredential] = None,
    request_id: str = "",
    remote_address: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> ServicePrincipalAuditEvent:
    safe_metadata = {
        str(key): value
        for key, value in (metadata or {}).items()
        if str(key) not in {"token", "secret", "assertion", "authorization"}
    }
    return ServicePrincipalAuditEvent.objects.create(
        principal=principal,
        credential=credential,
        event_type=event_type,
        request_id=str(request_id or "")[:128],
        remote_address=remote_address or None,
        metadata=safe_metadata,
    )


@transaction.atomic
def issue_service_principal_credential(
    principal: ServicePrincipal,
    *,
    expires_at: Optional[datetime] = None,
    rotated_from: Optional[ServicePrincipalCredential] = None,
    created_by=None,
) -> tuple[ServicePrincipalCredential, str]:
    if not principal.is_active:
        raise ServicePrincipalCredentialError("Cannot issue a credential for an inactive principal")
    if rotated_from and rotated_from.principal_id != principal.pk:
        raise ServicePrincipalCredentialError("Rotation source belongs to another principal")
    if expires_at and expires_at <= timezone.now():
        raise ServicePrincipalCredentialError("Credential expiry must be in the future")

    secret = secrets.token_urlsafe(48)
    credential = ServicePrincipalCredential.objects.create(
        principal=principal,
        secret_hash=make_password(secret),
        token_hint="pending",
        expires_at=expires_at,
        rotated_from=rotated_from,
        created_by=created_by,
    )
    token = f"{TOKEN_PREFIX}{credential.pk.hex}.{secret}"
    credential.token_hint = f"{TOKEN_PREFIX}{credential.pk.hex[:12]}…"
    credential.save(update_fields=("token_hint",))
    record_service_principal_audit(
        "credential_rotated" if rotated_from else "credential_issued",
        principal=principal,
        credential=credential,
        metadata={"rotated_from_id": str(rotated_from.pk) if rotated_from else ""},
    )
    return credential, token


@transaction.atomic
def revoke_service_principal_credential(
    credential: ServicePrincipalCredential,
    *,
    reason: str = "",
) -> None:
    if credential.revoked_at:
        return
    credential.revoked_at = timezone.now()
    credential.save(update_fields=("revoked_at",))
    record_service_principal_audit(
        "credential_revoked",
        principal=credential.principal,
        credential=credential,
        metadata={"reason": str(reason or "")[:500]},
    )


def parse_service_principal_token(token: str) -> tuple[UUID, str]:
    match = TOKEN_PATTERN.fullmatch(str(token or "").strip())
    if not match:
        raise ServicePrincipalCredentialError("Malformed service-principal credential")
    return UUID(hex=match.group("credential_id")), match.group("secret")


def authenticate_service_principal_token(token: str) -> ServicePrincipalAuthContext:
    credential_id, secret = parse_service_principal_token(token)
    try:
        credential = (
            ServicePrincipalCredential.objects.select_related("principal", "principal__organization")
            .get(pk=credential_id)
        )
    except ServicePrincipalCredential.DoesNotExist as exc:
        check_password(secret, _DUMMY_SECRET_HASH)
        raise ServicePrincipalCredentialError("Invalid service-principal credential") from exc

    if not check_password(secret, credential.secret_hash):
        record_service_principal_audit(
            "authentication_failed",
            principal=credential.principal,
            credential=credential,
            metadata={"reason": "secret_mismatch"},
        )
        raise ServicePrincipalCredentialError("Invalid service-principal credential")

    now = timezone.now()
    if not credential.principal.is_active:
        reason = "principal_inactive"
    elif credential.revoked_at:
        reason = "credential_revoked"
    elif credential.expires_at and credential.expires_at <= now:
        reason = "credential_expired"
    else:
        reason = ""
    if reason:
        record_service_principal_audit(
            "authentication_failed",
            principal=credential.principal,
            credential=credential,
            metadata={"reason": reason},
        )
        raise ServicePrincipalCredentialError("Inactive service-principal credential")

    ServicePrincipalCredential.objects.filter(pk=credential.pk).update(last_used_at=now)
    credential.last_used_at = now
    return ServicePrincipalAuthContext(
        principal=credential.principal,
        credential=credential,
        token=token,
    )
