from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


PUBLIC_URL_SETTINGS = (
    "DEFAULT_BACKEND_URL",
    "DEFAULT_FRONTEND_URL",
    "MEDHACK_URL",
    "ESAFETY_URL",
    "VIBE_RAISING_URL",
    "FOUNDER_TOOLS_URL",
)

OAUTH_REDIRECT_URI_SETTINGS = (
    "GOOGLE_OAUTH_REDIRECT_URI",
    "GITHUB_OAUTH_REDIRECT_URI",
    "STRIPE_OAUTH_REDIRECT_URI",
    "XERO_OAUTH_REDIRECT_URI",
    "NOTION_OAUTH_REDIRECT_URI",
    "GOOGLE_DRIVE_OAUTH_REDIRECT_URI",
    "SLACK_OAUTH_REDIRECT_URI",
)

SERVICE_URL_SETTINGS = (
    "CONTENT_FACTORY_URL",
    "VALLEY_HARNESS_URL",
)

SERVICE_API_KEY_SETTINGS = (
    "VALLEY_HARNESS_API_KEY",
    "INTERNAL_API_KEY",
    "ROO_API_KEY",
    "MLAI_API_KEY",
)

DOCKER_ONLY_SERVICE_HOSTS = {
    "CONTENT_FACTORY_URL": {"content-factory-web"},
    "VALLEY_HARNESS_URL": {"valley-api"},
}

REQUIRED_CORS_ORIGINS = {
    "https://mlai.au",
    "https://www.mlai.au",
    # victorai.win registration form posts to /api/v1/victor-ai/ cross-origin.
    "https://victorai.win",
    "https://www.victorai.win",
}

REQUIRED_CSRF_ORIGINS = {
    "https://api.mlai.au",
    "https://mlai.au",
    "https://www.mlai.au",
}

REQUIRED_ALLOWED_HOSTS = {
    "api.mlai.au",
    "10.126.0.2",
}


def _as_clean_string(value) -> str:
    return str(value or "").strip()


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.replace(",", " ").split() if item.strip()]
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_production_settings() -> bool:
    app_env = _as_clean_string(getattr(settings, "APP_ENV", "")).lower()
    if app_env in {"local", "development", "dev", "test"}:
        return False
    if app_env in {"production", "prod"}:
        return True
    return not bool(getattr(settings, "DEBUG", False))


def _parse_http_url(setting_name: str, value: str, errors: list[str]) -> urllib.parse.ParseResult | None:
    if not value:
        errors.append(f"{setting_name} is required in production.")
        return None

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        errors.append(f"{setting_name} must be an absolute http or https URL.")
        return None
    return parsed


def _is_raw_ip(hostname: str | None) -> bool:
    if not hostname:
        return False
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def _validate_https_url(setting_name: str, errors: list[str]) -> None:
    value = _as_clean_string(getattr(settings, setting_name, ""))
    parsed = _parse_http_url(setting_name, value, errors)
    if parsed and parsed.scheme != "https":
        errors.append(f"{setting_name} must use https in production.")


def _validate_service_url(setting_name: str, errors: list[str]) -> None:
    value = _as_clean_string(getattr(settings, setting_name, ""))
    parsed = _parse_http_url(setting_name, value, errors)
    if not parsed:
        return
    if parsed.scheme == "https" and _is_raw_ip(parsed.hostname):
        errors.append(
            f"{setting_name} uses https with a raw IP address. Use http for the current service endpoint "
            "or put the service behind DNS with a valid TLS certificate."
        )
    blocked_hosts = DOCKER_ONLY_SERVICE_HOSTS.get(setting_name, set())
    allow_docker_aliases = _as_bool(getattr(settings, "ALLOW_DOCKER_SERVICE_ALIASES", False))
    hostname = (parsed.hostname or "").lower()
    if hostname in blocked_hosts and not allow_docker_aliases:
        errors.append(
            f"{setting_name} uses Docker-only service host '{hostname}', which only works on the same host. "
            "Set it to the Valley private/VPC URL for cross-droplet production deploys, or set "
            "ALLOW_DOCKER_SERVICE_ALIASES=true for same-host deployments."
        )


def _validate_required_values(
    setting_name: str,
    required_values: set[str],
    errors: list[str],
    *,
    value_label: str,
) -> None:
    configured = set(_as_list(getattr(settings, setting_name, [])))
    missing = sorted(required_values - configured)
    if missing:
        errors.append(f"{setting_name} is missing required {value_label}(s): {', '.join(missing)}.")


def _service_api_key_with_source() -> tuple[str, str]:
    for setting_name in SERVICE_API_KEY_SETTINGS:
        value = _as_clean_string(getattr(settings, setting_name, ""))
        if value:
            return value, setting_name
    return "", ""


def validate_prod_url_settings() -> list[str]:
    errors: list[str] = []
    if not _is_production_settings():
        return errors

    for setting_name in PUBLIC_URL_SETTINGS:
        _validate_https_url(setting_name, errors)

    for setting_name in OAUTH_REDIRECT_URI_SETTINGS:
        _validate_https_url(setting_name, errors)

    for setting_name in SERVICE_URL_SETTINGS:
        _validate_service_url(setting_name, errors)

    _validate_required_values("CORS_ALLOWED_ORIGINS", REQUIRED_CORS_ORIGINS, errors, value_label="origin")
    _validate_required_values("CSRF_TRUSTED_ORIGINS", REQUIRED_CSRF_ORIGINS, errors, value_label="origin")
    _validate_required_values("ALLOWED_HOSTS", REQUIRED_ALLOWED_HOSTS, errors, value_label="host")

    return errors


def _connectivity_url_for_service(setting_name: str, value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    base_path = (parsed.path or "").rstrip("/")
    if setting_name == "CONTENT_FACTORY_URL":
        health_path = f"{base_path}/healthz/ready" if base_path else "/healthz/ready"
        return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, health_path, "", "", ""))
    if setting_name == "VALLEY_HARNESS_URL":
        health_path = f"{base_path}/internal/healthz" if base_path else "/internal/healthz"
        return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, health_path, "", "", ""))
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


def _service_url_connectivity_error(setting_name: str, *, timeout: float) -> str | None:
    value = _as_clean_string(getattr(settings, setting_name, ""))
    url = _connectivity_url_for_service(setting_name, value)
    headers = {
        "Connection": "close",
        "User-Agent": "mlai-prod-url-check/1.0",
    }
    if setting_name == "VALLEY_HARNESS_URL":
        api_key, api_key_source = _service_api_key_with_source()
        if not api_key:
            return "VALLEY_HARNESS_URL connectivity check requires VALLEY_HARNESS_API_KEY or another service API key."
        headers["X-API-Key"] = api_key
        headers["X-API-Key-Source"] = api_key_source
    request = urllib.request.Request(
        url,
        method="GET",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1)
    except urllib.error.HTTPError as exc:
        if setting_name == "VALLEY_HARNESS_URL":
            return f"{setting_name} health check failed at {url}: HTTP {exc.code}"
        return None
    except (TimeoutError, socket.timeout, OSError, urllib.error.URLError) as exc:
        return f"{setting_name} is not reachable at {url}: {exc}"
    return None


def validate_service_url_connectivity(*, timeout: float) -> list[str]:
    errors: list[str] = []
    for setting_name in SERVICE_URL_SETTINGS:
        error = _service_url_connectivity_error(setting_name, timeout=timeout)
        if error:
            errors.append(error)
    return errors


class Command(BaseCommand):
    help = "Validate production URL settings without printing secret values."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            "--check-connectivity",
            action="store_true",
            help="Also verify configured service URLs accept an HTTP connection.",
        )
        parser.add_argument(
            "--warn-connectivity",
            action="store_true",
            help="Print service connectivity failures as warnings instead of failing the command.",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=5.0,
            help="Per-service connectivity timeout in seconds.",
        )

    def handle(self, *args, **options):
        errors = validate_prod_url_settings()
        if options["check_connectivity"] and not errors:
            connectivity_errors = validate_service_url_connectivity(timeout=options["timeout"])
            if options["warn_connectivity"]:
                for error in connectivity_errors:
                    self.stderr.write(self.style.WARNING(f"- WARNING: {error}"))
            else:
                errors.extend(connectivity_errors)

        if errors:
            for error in errors:
                self.stderr.write(f"- {error}")
            raise CommandError(f"Production URL validation failed with {len(errors)} issue(s).")

        if _is_production_settings():
            self.stdout.write(self.style.SUCCESS("Production URL settings are valid."))
        else:
            self.stdout.write("Production URL validation skipped outside production settings.")
