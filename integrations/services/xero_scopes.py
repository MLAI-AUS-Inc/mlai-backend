from __future__ import annotations

from typing import Any, Iterable

XERO_REPORT_SCOPE = "accounting.reports.read"
XERO_LEGACY_REPORT_SCOPE = "accounting.reports"
XERO_PROFIT_AND_LOSS_REPORT_SCOPE = "accounting.reports.profitandloss.read"
XERO_BALANCE_SHEET_REPORT_SCOPE = "accounting.reports.balancesheet.read"
XERO_PAYMENT_WRITE_SCOPE = "accounting.payments"
XERO_REQUIRED_OPERATIONAL_SCOPES = (
    "offline_access",
    "accounting.invoices.read",
    "accounting.payments.read",
    "accounting.settings.read",
    "accounting.contacts.read",
)
XERO_REQUIRED_REPORT_SCOPES = (
    XERO_PROFIT_AND_LOSS_REPORT_SCOPE,
    XERO_BALANCE_SHEET_REPORT_SCOPE,
)
XERO_REPORT_SCOPE_WARNING = "Reconnect Xero to allow Profit and Loss and Balance Sheet report metrics."
XERO_REPORT_SCOPE_CONFIGURATION_WARNING = (
    "Xero report metrics are disabled until Profit and Loss and Balance Sheet report scopes are configured for OAuth."
)


def normalize_xero_scopes(value: Any) -> set[str]:
    if not value:
        return set()
    if isinstance(value, str):
        return {item.strip() for item in value.replace(",", " ").split() if item.strip()}
    if isinstance(value, Iterable):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


def xero_has_report_scope(value: Any) -> bool:
    scopes = normalize_xero_scopes(value)
    return (
        XERO_REPORT_SCOPE in scopes
        or XERO_LEGACY_REPORT_SCOPE in scopes
        or all(scope in scopes for scope in XERO_REQUIRED_REPORT_SCOPES)
    )


def xero_missing_report_scopes(value: Any) -> tuple[str, ...]:
    scopes = normalize_xero_scopes(value)
    if XERO_REPORT_SCOPE in scopes or XERO_LEGACY_REPORT_SCOPE in scopes:
        return ()
    return tuple(scope for scope in XERO_REQUIRED_REPORT_SCOPES if scope not in scopes)


def xero_can_request_report_scopes(value: Any) -> bool:
    return not xero_missing_report_scopes(value)


def xero_needs_report_reconnect(value: Any) -> bool:
    return not xero_has_report_scope(value)


def xero_has_payment_write_scope(value: Any) -> bool:
    return XERO_PAYMENT_WRITE_SCOPE in normalize_xero_scopes(value)
