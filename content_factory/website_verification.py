"""Provider-backed CI and live-route receipts; supplied ready flags grant nothing."""

import base64
import json
import re
from urllib.parse import quote

from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey
from .website_connections import authority_guard, contract_for, require_unlocked_remote_call
from .website_contract import WebsiteAuthorityError, evidence_digest
from .website_models import WebsiteConnectionOperation


IDENTITY_FIELDS = ("website_connection_id", "connection_generation", "repository_id", "operation_id", "operation_attempt", "deletion_epoch",
    "github_repo", "source_sha", "target_id", "contract_digest", "evidence_digest")
BOOLEAN_PROOFS = ("content_loading", "listing", "detail", "unknown_slug_404", "canonical", "assets", "browser", "narrow_layout", "wide_layout")
CHECK_NAME = "MLAI articles native adapter verification"
MARKER = re.compile(r"MLAI_ARTICLES_ATTESTATION:([A-Za-z0-9+/=]+)")


def validated_ci_identity(data, provider):
    """Require provider-sealed exact scope and full build/browser evidence."""
    for field in IDENTITY_FIELDS:
        if field not in data or field not in provider or str(data[field]) != str(provider[field]):
            raise WebsiteAuthorityError("ci_attestation_scope_mismatch", "CI proof does not match this exact repository operation.")
    if any(provider.get(field) is not True for field in BOOLEAN_PROOFS) or provider.get("baseline_build") != "passed" or provider.get("patched_build") != "passed":
        raise WebsiteAuthorityError("ci_attestation_incomplete", "CI must verify builds and all article rendering checks.")
    if not provider.get("adapter_id") or not provider.get("adapter_version") or not provider.get("source_tree_sha") or not provider.get("environment_fingerprint") or not isinstance(provider.get("lockfile_digests"), dict):
        raise WebsiteAuthorityError("ci_attestation_incomplete", "CI proof needs adapter, tree, environment and dependency identities.")
    body = {key: value for key, value in provider.items() if key not in {"evidence_digest", "attestation_origin", "allowed"}}
    if provider["evidence_digest"] != evidence_digest(body):
        raise WebsiteAuthorityError("ci_attestation_digest_mismatch", "CI evidence digest does not match its sealed proof.")
    return provider


def read_ci_proof(connection, data):
    """Read only GitHub-owned successful checks at the exact source commit."""
    from integrations import http_client
    from .website_tokens import mint_ci_evidence_token, read_ci_provider_checks
    require_unlocked_remote_call()
    token = mint_ci_evidence_token(installation_id=connection.installation_id, repository=connection.github_repo,
        repository_id=connection.repository_id)
    headers = {"Authorization": f"Bearer {token.token}", "Accept": "application/vnd.github+json"}
    try:
        payload = read_ci_provider_checks(f"https://api.github.com/repos/{connection.github_repo}/commits/{quote(str(data['source_sha']), safe='')}/check-runs?per_page=100",
            headers=headers)
        for check in payload["check_runs"]:
            if (check.get("name") != CHECK_NAME or check.get("head_sha") != data["source_sha"] or check.get("status") != "completed"
                    or check.get("conclusion") != "success" or (check.get("app") or {}).get("slug") != "github-actions"):
                continue
            output = check.get("output") or {}
            match = MARKER.search(str(output.get("summary") or "") + "\n" + str(output.get("text") or ""))
            if match:
                try:
                    provider = json.loads(base64.b64decode(match.group(1), validate=True))
                    return validated_ci_identity(data, provider)
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
        raise WebsiteAuthorityError("ci_attestation_required", "Install the native adapter CI check and verify this exact source and operation.")
    finally:
        try:
            http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 8))
        except Exception:
            pass


def record_ci_attestation(data, *, owner_review=False):
    """Two-phase provider verification with a final cancellation/consent fence."""
    data = {**data, "expected_source_sha": data.get("source_sha")}
    with authority_guard(data, action="read") as website:
        target = website.targets.filter(generation=website.generation, target_key=data.get("target_id")).first()
        origin = website.operations.filter(pk=data.get("operation_id"), generation=website.generation).first()
        custom = origin.payload.get("contract") or origin.payload.get("custom_contract") if origin else None
        custom_verified = bool(custom and data.get("adapter_id") == "custom_contract_v1" and evidence_digest(custom) == data.get("contract_digest"))
        reviewed_target = (origin.payload.get("binding") or {}).get("target_id") if origin else None
        if custom_verified and reviewed_target and reviewed_target != data.get("target_id"):
            raise WebsiteAuthorityError("ci_attestation_scope_mismatch", "CI evidence differs from the reviewed custom target.")
        if not custom_verified and (target is None or target.contract.get("contract_digest") != data.get("contract_digest")):
            raise WebsiteAuthorityError("ci_attestation_contract_mismatch", "The CI check must match the reviewed target contract.")
        binding = {**data, **contract_for(website)}
        changed_source = website.verified_sha != data.get("source_sha")
        reviewed_heads = list(website.repository_mutations.filter(generation=website.generation, status="applied",
            run_id=data.get("run_id")).exclude(head_sha="").values_list("head_sha", flat=True))
        if changed_source and not owner_review and not reviewed_heads:
            raise WebsiteAuthorityError("website_source_review_required", "Review the changed source and its CI evidence before activating articles.")
    from .website_connections import verify_repository_head
    verify_repository_head(website, data["source_sha"])
    if changed_source and not owner_review:
        verify_reviewed_source_lineage(website, reviewed_heads, data["source_sha"])
    proof = read_ci_proof(website, binding)
    receipt = {**proof, "allowed": True, "attestation_origin": "authenticated_ci", "checked_at": timezone.now().isoformat()}
    with authority_guard(binding, action="read") as website:
        if custom_verified:
            origin.refresh_from_db()
            current_contract = origin.payload.get("contract") or origin.payload.get("custom_contract")
            if evidence_digest(current_contract) != data["contract_digest"]:
                raise WebsiteAuthorityError("website_configuration_changed", "The reviewed custom contract changed while CI was being verified.")
            from .website_support import promote_custom_target
            promote_custom_target(website, contract=custom, proof=proof)
        else:
            target.refresh_from_db()
            if target.contract.get("contract_digest") != data["contract_digest"]:
                raise WebsiteAuthorityError("website_configuration_changed", "The reviewed target contract changed while CI was being verified.")
            target.source_sha, target.verified_at = proof["source_sha"], timezone.now()
            target.contract = {**target.contract, "verification": {**proof, "status": "verified"}}
            target.capabilities = {**target.capabilities, "publishingReady": True}
            target.save(update_fields=["source_sha", "verified_at", "contract", "capabilities", "updated_at"])
            from .models import OrganizationContentConfig
            config = OrganizationContentConfig.objects.get(website_connection=website)
            if not config.default_publish_target_id:
                config.default_publish_target_id = target.target_key
            config.publish_targets = [row for row in config.publish_targets if row.get("target_id") != target.target_key] + [target.contract]
            config.save(update_fields=["default_publish_target_id", "publish_targets", "updated_at"])
        if website.verified_sha != proof["source_sha"]:
            website.configuration_version += 1
        website.verified_sha, website.last_verified_at = proof["source_sha"], timezone.now()
        website.blockers = [row for row in website.blockers if row.get("code") != "repository_source_changed"]
        website.capabilities = {**website.capabilities, "publishingReady": website.state == "connected", "previewSupported": True}
        website.save(update_fields=["verified_sha", "last_verified_at", "configuration_version", "blockers", "capabilities", "updated_at"])
        op, _ = WebsiteConnectionOperation.objects.update_or_create(idempotency_key=f"{website.pk}:ci:{data['evidence_digest']}", defaults={
            "connection": website, "generation": website.generation, "action": "ci-verify", "state": "completed", "payload": dict(data), "receipt": receipt})
        return op


def verify_reviewed_source_lineage(website, reviewed_heads, source_sha):
    """Require automatic CI advancement to contain an owned reviewed mutation."""
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token
    require_unlocked_remote_call()
    credential = create_installation_access_token(installation_id=website.installation_id, repository=website.github_repo,
        repository_id=website.repository_id, permission_mode="read", use_cache=False)
    headers = {"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}
    try:
        for head in reviewed_heads:
            response = http_client.get(f"https://api.github.com/repos/{website.github_repo}/compare/{head}...{source_sha}", headers=headers, timeout=(3, 15))
            if response.status_code == 200 and response.json().get("status") in {"ahead", "identical"}:
                return
        raise WebsiteAuthorityError("website_source_review_required", "The CI source does not contain this operation's reviewed integration commit.")
    finally:
        try:
            http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 8))
        except Exception:
            pass


def ci_attestation_for(connection, data):
    """Return a previously provider-validated receipt, never reflected input."""
    op = connection.operations.filter(generation=connection.generation, action="ci-verify", state="completed",
        payload__operation_id=data.get("operation_id"), payload__evidence_digest=data.get("evidence_digest")).order_by("-created_at").first()
    if op is None or any(str(op.receipt.get(key)) != str(data.get(key)) for key in IDENTITY_FIELDS):
        raise WebsiteAuthorityError("ci_attestation_required", "Authenticate and verify the exact CI evidence first.")
    return dict(op.receipt)


def verify_live_deployment(config, *, data):
    """Check selected source, provider CI receipt and same-site source marker."""
    from .website_live_fetch import fetch_live_route
    from .website_connections import verify_repository_head
    with authority_guard(data, action="read") as website:
        target = website.targets.filter(generation=website.generation, target_key=data.get("target_id"), source_sha=data.get("source_sha"), verified_at__isnull=False).first()
        if not target:
            raise WebsiteAuthorityError("verified_target_required", "Verify the articles target before live deployment.")
        ci_proof = ci_attestation_for(website, data)
        route = str(target.contract.get("route_path") or target.contract.get("public_path") or "")
        if not route.startswith("/"):
            raise WebsiteAuthorityError("public_route_required", "The target needs an explicit public route.")
        live_marker = target.contract.get("live_marker") or {}
        artifact_digest = live_marker.get("value") if live_marker.get("kind") == "artifact_digest" else target.contract.get("artifact_digest")
        if not re.fullmatch(r"[a-f0-9]{64}", str(artifact_digest or "")):
            raise WebsiteAuthorityError("deployment_marker_required", "Prepare a certified route with a reviewed artifact digest marker.")
        if ci_proof.get("artifact_digest") != artifact_digest:
            raise WebsiteAuthorityError("deployment_marker_unverified", "The repository CI proof must verify this exact reviewed live artifact marker.")
        routes = ci_proof.get("verified_routes")
        template = str(target.contract.get("route_template") or "")
        pattern = re.escape(template).replace(r"\{slug\}", r"[A-Za-z0-9][A-Za-z0-9_-]*")
        if (not isinstance(routes, dict) or routes.get("listing") != route or "{slug}" not in template
                or not all(re.fullmatch(pattern, str(routes.get(key) or "")) for key in ("detail", "unknown_slug"))
                or routes.get("detail") == routes.get("unknown_slug")):
            raise WebsiteAuthorityError("deployment_routes_required", "Repository CI must seal the exact listing, article detail and unknown-slug routes it tested.")
        url = f"https://{website.organization.domain}{route}"
        binding = {**data, **contract_for(website)}
    require_unlocked_remote_call()
    verify_repository_head(website, data["source_sha"])
    observations = []
    for kind in ("listing", "detail", "unknown_slug"):
        public_url = f"https://{website.organization.domain}{routes[kind]}"
        expected_status = 404 if kind == "unknown_slug" else 200
        body, headers = fetch_live_route(public_url, website.organization.domain, expected_status=expected_status)
        marker = headers.get("x-mlai-artifact-digest") or ""
        if expected_status == 200 and marker != artifact_digest and not re.search(rb'<meta\s+name=["\x27]mlai-artifact-digest["\x27]\s+content=["\x27]' + artifact_digest.encode() + rb'["\x27]', body):
            raise WebsiteAuthorityError("deployment_source_unverified", "The public listing or article detail does not contain this reviewed integration artifact.")
        observations.append({"kind": kind, "path": routes[kind], "status": expected_status})
    receipt = {"status": "passed", "source_sha": data["source_sha"], "connection_generation": website.generation,
        "target_id": data["target_id"], "public_url": url, "checked_at": timezone.now().isoformat(), "ci_evidence_digest": data["evidence_digest"],
        "artifact_digest": artifact_digest, "contract_digest": data["contract_digest"], "verified_routes": observations, "provider_verified": True}
    with authority_guard(binding, action="read") as website:
        op, _ = WebsiteConnectionOperation.objects.update_or_create(idempotency_key=f"{website.pk}:deployment:{evidence_digest(binding)}", defaults={
            "connection": website, "generation": website.generation, "action": "deployment-verify", "state": "completed", "payload": binding, "receipt": receipt})
        return op


class WebsiteCiAttestationView(APIView):
    """Authenticate exact provider CI evidence for internal worker consumers."""

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        try:
            operation = record_ci_attestation(dict(request.data))
            return Response(operation.receipt)
        except WebsiteAuthorityError as exc:
            return Response(exc.as_dict(), status=exc.status)
