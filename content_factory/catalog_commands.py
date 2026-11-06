"""Durable, bounded catalogue commands for the explicitly admitted owner path.

Receipts contain hashes and revisions, never whole requests or old responses.
The owning view holds the organisation/config/profile/company locks through the
catalogue change and receipt. Reads and after-commit offer checks remain outside
this command's effect boundary.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from uuid import UUID
from types import SimpleNamespace

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .editorial_catalog import CatalogConflict, review_payload

KEY = "editorial_catalog_commands_v1"
MAX_COMMANDS = 256
_HEX = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = frozenset({"schema_version", "organization_id", "company_id", "user_id", "action",
    "body_sha256", "expected_version", "catalog_version", "catalog_sha256", "accepted_at"})


class CatalogCommandConflict(CatalogConflict):
    """A retained command or the current catalogue cannot be verified."""


def _digest(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise CatalogCommandConflict("This catalogue request cannot be verified.") from None
    return hashlib.sha256(encoded).hexdigest()


def _nonnegative(value):
    return type(value) is int and value >= 0


def _valid_receipt(value):
    if isinstance(value, dict) and value.get("action") == "brief_suggestion":
        from .catalog_brief_commands import valid_suggestion_receipt
        return valid_suggestion_receipt(value)
    fields = _FIELDS | {"selection"} if isinstance(value, dict) and value.get("action") == "first_audience" else _FIELDS
    if not isinstance(value, dict) or set(value) != fields:
        return False
    at = value.get("accepted_at")
    try:
        parsed = parse_datetime(at) if isinstance(at, str) else None
        company = str(UUID(value["company_id"]))
    except (ValueError, TypeError, AttributeError):
        return False
    return bool(type(value["schema_version"]) is int and value["schema_version"] == 1
        and type(value["organization_id"]) is int and value["organization_id"] > 0
        and company == value["company_id"]
        and type(value["user_id"]) is int and value["user_id"] > 0
        and isinstance(value["action"], str) and value["action"] in {"edit", "approve", "first_audience"}
        and (value["action"] != "first_audience" or _valid_selection(value["selection"]))
        and all(isinstance(value[key], str) and _HEX.fullmatch(value[key])
            for key in ("body_sha256", "catalog_sha256"))
        and _nonnegative(value["expected_version"]) and _nonnegative(value["catalog_version"])
        and value["catalog_version"] in {value["expected_version"], value["expected_version"] + 1}
        and parsed is not None and timezone.is_aware(parsed))


def _valid_selection(value):
    return bool(isinstance(value, dict) and set(value) == {"id", "version", "content_sha256"}
        and isinstance(value["id"], str) and 0 < len(value["id"]) <= 200
        and type(value["version"]) is int and value["version"] > 0
        and isinstance(value["content_sha256"], str) and _HEX.fullmatch(value["content_sha256"]))


def _receipts(strategy):
    value = strategy.get(KEY, {})
    if (not isinstance(value, dict) or len(value) > MAX_COMMANDS
            or any(not isinstance(key, str) or not _HEX.fullmatch(key) or not _valid_receipt(receipt)
                for key, receipt in value.items())):
        raise CatalogCommandConflict("Saved catalogue requests need an ownership review.")
    return value


def _require_current_owner(*, organization, context, user_id, strategy):
    from django.contrib.auth import get_user_model
    from founder_tools import startup_team
    if (type(user_id) is not int or not get_user_model().objects.filter(pk=user_id, is_active=True).exists()
            or context.organization.pk != organization.pk
            or context.company.organization_id != organization.pk
            or startup_team._owner(organization) != user_id):
        raise PermissionError("Only the current active startup owner can change this catalogue.")
    roster = startup_team._roster(SimpleNamespace(pillar_strategy=strategy))
    if startup_team.KEY in strategy and (roster is None or roster.get("frozen") is not False):
        raise PermissionError("This startup is read only until its ownership review is complete.")


@dataclass(frozen=True)
class CatalogCommand:
    """One validated owner intent, admitted while the owning SQL locks are held."""
    key_sha256: str
    identity: dict
    previous: dict | None

    @property
    def replay(self):
        """Whether this exact command already committed with its catalogue."""
        return self.previous is not None


def prepare_command(*, strategy, organization, context, user_id, key, body, action):
    """Bind a keyed v2 owner request, or leave the existing no-key/service path alone.

    A retained command continues to govern its replay if new admissions are later
    disabled. The namespace is organisation-local; independently authorised
    organisations may use the same raw caller UUID as different intents.
    """
    from .p2_foundation import p2_applies
    if context is None or user_id is None or key is None:
        return None
    admitted = p2_applies(organization)
    if not isinstance(strategy, dict):
        raise CatalogCommandConflict("The stored catalogue strategy cannot be verified.")
    if not admitted and KEY not in strategy:
        return None
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key):
        raise ValueError("A valid catalogue request key is required.")
    if action not in {"edit", "approve", "first_audience"} or not isinstance(body, dict):
        raise ValueError("A catalogue owner command is required.")
    _require_current_owner(organization=organization, context=context, user_id=user_id, strategy=strategy)
    records = _receipts(strategy)
    key_sha = hashlib.sha256(key.encode("utf-8")).hexdigest()
    identity = {"schema_version": 1, "organization_id": organization.pk,
        "company_id": str(context.company.pk), "user_id": user_id, "action": action,
        "body_sha256": _digest(body)}
    previous = records.get(key_sha)
    if previous is not None:
        if previous["user_id"] != user_id:
            raise PermissionError("That catalogue command belongs to a different owner.")
        if any(previous[field] != value for field, value in identity.items()):
            raise CatalogCommandConflict("This catalogue request key already records different details.")
        current = review_payload(strategy)
        version = current["editorial_catalog_version"]
        if (version < previous["catalog_version"]
                or version == previous["catalog_version"] and _digest(current) != previous["catalog_sha256"]):
            raise CatalogCommandConflict("The saved catalogue receipt does not match the current revision.")
        return CatalogCommand(key_sha, identity, deepcopy(previous))
    if not admitted:
        raise CatalogCommandConflict("New catalogue commands are currently unavailable.")
    if len(records) >= MAX_COMMANDS:
        raise CatalogCommandConflict("This catalogue has reached its saved request limit.")
    return CatalogCommand(key_sha, identity, None)


def record_command(*, original, updated, body, command, response, selection=None):
    """Save minimal accepted evidence in the same transaction as the mutation.

    Do not evict old keys: capacity refuses a new command instead of making an
    old unknown acknowledgement eligible for another effect.
    """
    if command is None or command.replay:
        return updated
    expected = body.get("expected_editorial_catalog_version")
    version = response.get("editorial_catalog_version")
    if (not _nonnegative(expected) or not _nonnegative(version)
            or version not in {expected, expected + 1}):
        raise CatalogCommandConflict("The accepted catalogue revision cannot be verified.")
    receipt = {**command.identity, "expected_version": expected, "catalog_version": version,
        "catalog_sha256": _digest(response), "accepted_at": timezone.now().isoformat()}
    if command.identity["action"] == "first_audience":
        receipt["selection"] = deepcopy(selection)
    if not _valid_receipt(receipt):
        raise CatalogCommandConflict("The accepted catalogue command cannot be verified.")
    result = deepcopy(updated)
    result[KEY] = {**deepcopy(_receipts(original)), command.key_sha256: receipt}
    return result
