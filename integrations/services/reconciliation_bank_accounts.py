"""Xero-verified active bank-account catalogue for statement reconciliation."""

from __future__ import annotations

from typing import Any

from integrations import http_client
from integrations.models import ReconciliationProfile
from integrations.services.xero_reconciliation import (
    XERO_API_URL,
    ReconciliationValidationError,
    _xero_headers,
)


def _normalized_id(value: Any) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def _normalise_accounts(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    accounts: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for row in rows:
        if str(row.get("Type") or "").strip().upper() != "BANK":
            continue
        if str(row.get("Status") or "").strip().upper() != "ACTIVE":
            continue
        account_id = str(row.get("AccountID") or "").strip()
        name = str(row.get("Name") or "").strip()
        normalized_id = _normalized_id(account_id)
        normalized_name = " ".join(name.casefold().split())
        if not normalized_id or not normalized_name:
            raise ReconciliationValidationError(
                "Xero returned an active bank account without a stable ID and name."
            )
        if normalized_id in seen_ids:
            raise ReconciliationValidationError(
                "Xero returned duplicate active bank-account IDs."
            )
        seen_ids.add(normalized_id)
        accounts.append({"bank_account_id": account_id, "name": name})
    accounts.sort(key=lambda item: (item["name"].casefold(), item["bank_account_id"].casefold()))
    if not accounts:
        raise ReconciliationValidationError("Xero returned no active BANK accounts.")
    return accounts


def fetch_active_xero_bank_accounts(profile: ReconciliationProfile) -> list[dict[str, str]]:
    """Read the exact active BANK account catalogue directly from Xero."""

    connection = profile.xero_connection
    if connection is None:
        raise ReconciliationValidationError("A Xero connection must be selected.")
    try:
        response = http_client.get(
            f"{XERO_API_URL}/Accounts",
            headers=_xero_headers(connection),
            timeout=(3, 30),
        )
        response.raise_for_status()
        payload = response.json()
    except (http_client.RequestException, ValueError) as exc:
        raise ReconciliationValidationError(
            "The active Xero bank-account catalogue could not be refreshed."
        ) from exc
    rows = payload.get("Accounts") if isinstance(payload, dict) else []
    return _normalise_accounts(
        [item for item in rows or [] if isinstance(item, dict)]
    )


def active_bank_account(
    profile: ReconciliationProfile,
    bank_account_id: str,
    *,
    accounts: list[dict[str, str]] | None = None,
    allow_legacy_profile_account: bool = True,
) -> dict[str, str] | None:
    expected = _normalized_id(bank_account_id)
    legacy_account_id = str(profile.xero_bank_account_id or "").strip()
    if (
        accounts is None
        and allow_legacy_profile_account
        and legacy_account_id
        and _normalized_id(legacy_account_id) == expected
    ):
        return {
            "bank_account_id": legacy_account_id,
            "name": str(profile.xero_bank_account_name or legacy_account_id).strip(),
        }
    return next(
        (
            item
            for item in (accounts if accounts is not None else fetch_active_xero_bank_accounts(profile))
            if _normalized_id(item["bank_account_id"]) == expected
        ),
        None,
    )
