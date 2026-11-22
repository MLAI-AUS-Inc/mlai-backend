"""Which code a missing or unusable website connection is refused with (plan B12).

``website_connection_required`` ("Reconnect this website") used to cover three
different things. It is now split:

* ``github_access_required``: MLAI lost GitHub access (the connection was
  revoked). The only code whose founder copy offers Reconnect.
* ``website_not_connected``: no website is connected, or it was disconnected
  ("Connect your website").
* ``callback_contract_missing``: a worker update for a bound run arrived without
  its connection identity. A protocol fault: retried, alerted, never shown to
  the founder. Retryable only when the run has a saved contract and that
  connection is still connected at the saved generation; otherwise the real
  state of the connection decides the code.

A paused connection is ``website_publishing_paused`` (Chat offers Resume).
``website_connection_required`` stays registered as the legacy alias: the
registry's ``legacy`` view of both new codes is that code, and refusals carry
it as ``legacy_code`` for old clients.
"""

from __future__ import annotations

from .website_contract import WebsiteAuthorityError, connection_contract

LEGACY_CODE = "website_connection_required"
ACCESS_LOST = "github_access_required"
NOT_CONNECTED = "website_not_connected"
CONTRACT_MISSING = "callback_contract_missing"
PAUSED = "website_publishing_paused"


def not_connected(message: str = "Connect your website to continue.") -> WebsiteAuthorityError:
    """No website connection is selected for this organisation."""
    return WebsiteAuthorityError(NOT_CONNECTED, message)


def for_connection_state(connection, message: str | None = None) -> WebsiteAuthorityError | None:
    """The refusal a connection's own state calls for, or None while connected."""
    if connection is None:
        return not_connected(message or "Connect your website to continue.")
    state = str(getattr(connection, "state", "") or "")
    if state == "revoked":
        return WebsiteAuthorityError(ACCESS_LOST, "MLAI can't read your website on GitHub any more. Reconnect it to continue.")
    if state == "paused":
        return WebsiteAuthorityError(PAUSED, "Publishing is paused for this website. Resume it to continue.")
    if state != "connected":
        return not_connected(message or "Connect your website to continue.")
    return None


def _saved_run(payload):
    run_id = str(payload.get("run_id") or payload.get("job_id") or "").strip()
    if not run_id:
        return None
    from workflow_runs.models import ContentFactoryRun
    return ContentFactoryRun.objects.filter(run_id=run_id).first()


def _domain_connection(domain):
    from .models import OrganizationContentConfig
    domain = str(domain or "").strip().lower()
    if not domain:
        return None, False
    config = (OrganizationContentConfig.objects.select_related("website_connection")
              .filter(organization__domain__iexact=domain).first())
    return (config.website_connection if config else None), config is not None


def missing_contract_error(payload) -> WebsiteAuthorityError:
    """The refusal for a request that carries no connection identity.

    Decided from the saved run, never from the request's event name: a bound
    run whose connection is still connected at the saved generation gets the
    retryable protocol fault; otherwise the connection's real state answers.
    """
    from .website_connections import scoped_run_contract
    from .website_models import WebsiteConnection
    payload = payload if isinstance(payload, dict) else {}
    run = _saved_run(payload)
    if run is not None:
        try:
            saved = connection_contract(scoped_run_contract(run))
        except WebsiteAuthorityError:
            saved = {}
        if saved:
            for key, expected in (("domain", run.domain), ("github_repo", run.github_repo)):
                claimed = str(payload.get(key) or "").strip()
                if claimed and expected and claimed.casefold() != str(expected).strip().casefold():
                    return WebsiteAuthorityError("website_scope_mismatch",
                                                 "This update names a different website from its run.")
            connection = WebsiteConnection.objects.filter(pk=saved["website_connection_id"]).first()
            refusal = for_connection_state(connection)
            if refusal is not None:
                return refusal
            if connection.generation != saved["connection_generation"]:
                return WebsiteAuthorityError("website_connection_changed",
                                             "The website connection changed. Refresh before continuing.")
            return WebsiteAuthorityError(CONTRACT_MISSING,
                                         "A worker update arrived without its website identity. MLAI will retry it.",
                                         retryable=True)
        connection, _ = _domain_connection(getattr(run, "domain", "") or payload.get("domain"))
        if connection is None:
            return not_connected()
        # A run without saved consent on a connected site: retrying cannot help
        # and the founder has nothing to reconnect (B24 owns restarting it).
        return WebsiteAuthorityError("invalid_connection_contract",
                                     "This run has no saved website consent.")
    connection, _ = _domain_connection(payload.get("domain"))
    if connection is None:
        return not_connected()
    refusal = for_connection_state(connection)
    if refusal is not None:
        return refusal
    # A caller (an old web app) that did not send the reviewed identity.
    return WebsiteAuthorityError(LEGACY_CODE, "Reload the web app or update MLAI, then reconnect this website.")


def legacy_code(code: str | None) -> str | None:
    """The registry's legacy code for ``code`` when it differs, for old clients."""
    if not isinstance(code, str):
        return None
    from .registry_policy import load_registry
    entry = load_registry()["by_code_v2"].get(code)
    legacy = (entry or {}).get("legacy") if isinstance(entry, dict) else None
    old = legacy.get("code") if isinstance(legacy, dict) else None
    return old if isinstance(old, str) and old != code else None
