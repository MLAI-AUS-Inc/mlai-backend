"""Provider-backed CI and live-route receipts; supplied ready flags grant nothing."""

import base64
import json
import re
from datetime import timedelta
from urllib.parse import quote

from django.db import transaction
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey
from .website_connections import authority_guard, contract_for, require_unlocked_remote_call
from .website_contract import WebsiteAuthorityError, evidence_digest
from .website_models import WebsiteConnectionOperation
from .website_publishing_inputs import has_candidate


IDENTITY_FIELDS = ("website_connection_id", "connection_generation", "repository_id", "operation_id", "operation_attempt", "deletion_epoch",
    "github_repo", "source_sha", "target_id", "contract_digest", "evidence_digest")
BOOLEAN_PROOFS = ("content_loading", "listing", "detail", "unknown_slug_404", "canonical", "assets", "browser", "narrow_layout", "wide_layout")
CHECK_NAME = "MLAI articles native adapter verification"
MARKER = re.compile(r"MLAI_ARTICLES_ATTESTATION:([A-Za-z0-9+/=]+)")


def require_legacy_proof_inputs(*, target=None, proof=None, query=False):
    """Candidate checksums require the future executing-verifier proof protocol."""
    version = proof.get("schema_version", 2) if isinstance(proof, dict) else 2
    legacy_version = type(version) is int and version == 2
    if query and type(version) is str and version == "2":
        legacy_version = True
    if has_candidate(target) or (isinstance(proof, dict) and (
            not legacy_version
            or any(key in proof for key in ("publishing_inputs_version", "publishing_inputs_digest", "baseline_source_sha", "verifier_sha256")))):
        raise WebsiteAuthorityError("ci_attestation_contract_mismatch",
            "Publishing input candidates require compatible execution verification before activation.")


def validated_ci_identity(data, provider):
    """Require provider-sealed exact scope and full build/browser evidence."""
    require_legacy_proof_inputs(proof=data)
    require_legacy_proof_inputs(proof=provider)
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


def record_ci_attestation(data, *, owner_review=False, origin="authenticated_ci"):
    """Two-phase provider verification with a final cancellation/consent fence."""
    attestation_origin = origin
    if attestation_origin not in {"authenticated_ci", "authenticated_hosted"}:
        raise ValueError("Unknown native attestation origin")
    sealed_v3 = type(data.get("schema_version")) is int and data["schema_version"] == 3
    sealed_pending = None
    sealed_replay = None
    if sealed_v3 and attestation_origin != "authenticated_hosted":
        raise WebsiteAuthorityError("ci_attestation_contract_mismatch", "This execution proof requires authenticated hosted verification.")
    if attestation_origin == "authenticated_hosted":
        from .website_hosted_verification import enabled
        from .website_models import WebsiteConnection
        if not enabled(WebsiteConnection.objects.get(pk=data.get("website_connection_id"))):
            raise WebsiteAuthorityError("ci_attestation_required", "Hosted native attestation is disabled.")
    data = {**data, "expected_source_sha": data.get("source_sha")}
    with authority_guard(data, action="read") as website:
        target = website.targets.filter(generation=website.generation, target_key=data.get("target_id")).first()
        if sealed_v3:
            from . import website_sealed_workflow as sealed
            if target is None or not sealed.enabled(website):
                raise WebsiteAuthorityError("ci_attestation_required", "Compatible hosted execution verification is disabled.")
            sealed_replay = sealed.replay_operation(website, target, data)
            if sealed_replay is None:
                _, _, sealed_pending = sealed.child_inputs(website, data)
                sealed.fence_pending(website, target, sealed_pending)
            else:
                replay_target_digest = sealed.digest(target.contract)
                replay_receipt_digest = sealed.digest(sealed_replay.receipt)
        else:
            require_legacy_proof_inputs(target=target.contract if target else None, proof=data)
            from .website_sealed_workflow import required
            if required(website, target):
                raise WebsiteAuthorityError("ci_attestation_contract_mismatch", "This target now requires execution proof version three.")
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
            run_id=data.get("source_run_id") if attestation_origin == "authenticated_hosted" else data.get("run_id")).exclude(head_sha="").values_list("head_sha", flat=True))
        # Recorded setup merges prove lineage for squash-merged reviewed heads.
        owned_merge = False
        if changed_source and not owner_review and not reviewed_heads:
            owned_merge = _setup_merged_at(website, data.get("run_id"), data.get("source_sha"))
            if not owned_merge:
                raise WebsiteAuthorityError("website_source_review_required", "Review the changed source and its CI evidence before activating articles.")
    from .website_connections import verify_repository_head
    verify_repository_head(website, data["source_sha"])
    if sealed_replay is not None:
        with authority_guard(binding, action="read") as current:
            target.refresh_from_db()
            replay = sealed.replay_operation(current, target, data)
            if (replay is None or sealed.digest(target.contract) != replay_target_digest
                    or sealed.digest(replay.receipt) != replay_receipt_digest):
                raise WebsiteAuthorityError("website_configuration_changed", "The accepted execution proof changed during replay.")
            return replay
    if changed_source and not owner_review and not owned_merge:
        try:
            verify_reviewed_source_lineage(website, reviewed_heads, data["source_sha"])
        except WebsiteAuthorityError as exc:
            if exc.code != "website_source_review_required" or not _setup_merged_at(website, data.get("run_id"), data.get("source_sha")):
                raise
    if attestation_origin == "authenticated_hosted":
        from .website_hosted_verification import read_completed_proof
        proof = read_completed_proof(website, binding)
        if sealed_v3:
            from .website_sealed_verification import PROOF_FIELDS
            if any(type(data.get(key)) is not type(proof[key]) or data.get(key) != proof[key] for key in PROOF_FIELDS):
                raise WebsiteAuthorityError("ci_attestation_scope_mismatch", "The submitted proof differs from its authenticated service result.")
        else:
            proof = validated_ci_identity(binding, proof)
    else:
        proof = read_ci_proof(website, binding)
    receipt = {**proof, "allowed": True, "attestation_origin": attestation_origin, "checked_at": timezone.now().isoformat()}
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
            if sealed_v3:
                # Repeat the original parent, selection, source and consent
                # fence after every provider read and before atomic promotion.
                _, _, current_pending = sealed.child_inputs(website, data)
                if current_pending != sealed_pending:
                    raise WebsiteAuthorityError("website_configuration_changed", "The original execution proposal changed.")
                promoted = sealed.fence_pending(website, target, current_pending)
            else:
                require_legacy_proof_inputs(target=target.contract, proof=proof)
                if required(website, target):
                    raise WebsiteAuthorityError("ci_attestation_contract_mismatch", "This target now requires execution proof version three.")
            if target.contract.get("contract_digest") != data["contract_digest"]:
                raise WebsiteAuthorityError("website_configuration_changed", "The reviewed target contract changed while CI was being verified.")
            target.source_sha, target.verified_at = proof["source_sha"], timezone.now()
            target.contract = {**(promoted if sealed_v3 else target.contract),
                "verification": {**(receipt if sealed_v3 else proof), "status": "verified"}}
            target.capabilities = {**target.capabilities, "publishingReady": True, "previewSupported": True}
            if sealed_v3:
                target.capabilities[sealed.REQUIRED_VERSION] = 3
            target.save(update_fields=["source_sha", "verified_at", "contract", "capabilities", "updated_at"])
            from .models import OrganizationContentConfig
            config = OrganizationContentConfig.objects.get(website_connection=website)
            if not config.default_publish_target_id:
                config.default_publish_target_id = target.target_key
            config.publish_targets = [row for row in config.publish_targets if row.get("target_id") != target.target_key] + [target.contract]
            config.save(update_fields=["default_publish_target_id", "publish_targets", "updated_at"])
        if website.verified_sha != proof["source_sha"]:
            previous_revision = website.configuration_version
            website.configuration_version += 1
            from .website_goal_scope import record_source_transition
            record_source_transition(website, previous_revision=previous_revision, source_sha=proof["source_sha"])
        website.verified_sha, website.last_verified_at = proof["source_sha"], timezone.now()
        website.blockers = [row for row in website.blockers if row.get("code") != "repository_source_changed"]
        website.capabilities = {**website.capabilities, "publishingReady": website.state == "connected" and not custom_verified,
            "generationReady": website.state == "connected" and not custom_verified, "previewSupported": True}
        website.save(update_fields=["verified_sha", "last_verified_at", "configuration_version", "blockers", "capabilities", "updated_at"])
        op, _ = WebsiteConnectionOperation.objects.update_or_create(idempotency_key=f"{website.pk}:ci:{data['evidence_digest']}", defaults={
            "connection": website, "generation": website.generation, "action": "ci-verify", "state": "completed", "payload": dict(data), "receipt": receipt})
        from .website_prepare import wake_prepare_after_commit
        wake_prepare_after_commit(website.pk)
        if changed_source:
            from .website_wakeups import enabled as event_wakes_enabled
            if event_wakes_enabled():
                from .website_prepare import wake_prepare_for_source
                transaction.on_commit(lambda: wake_prepare_for_source(website.pk, organization_id=website.organization_id), robust=True)
        return op


def _setup_merged_at(website, run_id, source_sha):
    """The current generation's setup run whose own recorded merge produced source_sha."""
    from workflow_runs.models import ContentFactoryRun
    from .website_connections import setup_settled_at
    if not run_id or not source_sha:
        return False
    run = ContentFactoryRun.objects.filter(run_id=run_id, organization_id=website.organization_id,
        workflow="article_system_setup", run_request__website_connection_id=str(website.pk),
        run_request__connection_generation=website.generation).first()
    result = run.result if run and isinstance(run.result, dict) else {}
    setup = result.get("article_system_setup") if isinstance(result.get("article_system_setup"), dict) else {}
    return bool(run and str(run.github_repo or "").casefold() == str(website.github_repo or "").casefold()
        and "merged" in {result.get("merge_status"), setup.get("merge_status")} and setup_settled_at(run, source_sha))


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


def ci_attestation_for(connection, data, *, query=False):
    """Return a previously provider-validated receipt, never reflected input."""
    target = connection.targets.filter(generation=connection.generation, target_key=data.get("target_id")).first()
    from . import website_sealed_workflow as sealed
    needs_sealed = sealed.required(connection, target)
    if not needs_sealed:
        require_legacy_proof_inputs(proof=data, query=query)
        require_legacy_proof_inputs(target=target.contract if target else None)
    op = connection.operations.filter(generation=connection.generation, action="ci-verify", state="completed",
        payload__operation_id=data.get("operation_id"), payload__evidence_digest=data.get("evidence_digest")).order_by("-created_at").first()
    if op is None or any(str(op.receipt.get(key)) != str(data.get(key)) for key in IDENTITY_FIELDS):
        raise WebsiteAuthorityError("ci_attestation_required", "Authenticate and verify the exact CI evidence first.")
    if needs_sealed:
        # Query strings are selectors only. The original typed saved receipt
        # and child binding supply authority; the caller cannot choose origin.
        for key in ("publishing_inputs_digest", "baseline_source_sha", "verifier_sha256"):
            if data.get(key) != op.receipt.get(key):
                raise WebsiteAuthorityError("ci_attestation_scope_mismatch", "The execution proof identity changed.")
        return sealed.saved_receipt(connection, target, op.receipt)
    require_legacy_proof_inputs(proof=op.receipt)
    return dict(op.receipt)


def current_ci_attestation(connection, target, *, capabilities=None):
    """Reuse only fresh, exact-target provider proof in its original operation."""
    from .activation import mapping, VERIFICATION_MAX_AGE
    from .incident_guards import proof_stamp
    from .website_journey import build_proof_fresh
    from .website_operations import deletion_epoch, validate_operation
    if (target is None or getattr(target, "connection_id", None) != connection.pk or getattr(target, "generation", None) != connection.generation
            or not target.verified_at or not build_proof_fresh(connection, target, capabilities or {})):
        return None
    proof = mapping(mapping(target.contract).get("verification"))
    if proof.get("status") not in ("passed", "verified") or any(field not in proof for field in IDENTITY_FIELDS):
        return None
    expected = {**{key: value for key, value in contract_for(connection).items() if key in IDENTITY_FIELDS}, "source_sha": connection.verified_sha,
        "target_id": target.target_key, "contract_digest": target.contract.get("contract_digest"), "deletion_epoch": deletion_epoch(connection)}
    if any(str(proof.get(field)) != str(value) for field, value in expected.items()):
        return None
    try:
        receipt = ci_attestation_for(connection, proof)
        now = timezone.now()
        stamp = proof_stamp(receipt, now=now)
        if (receipt.get("allowed") is not True or receipt.get("attestation_origin") not in {"authenticated_ci", "authenticated_hosted"}
                or not stamp or not now - VERIFICATION_MAX_AGE <= stamp <= now + timedelta(minutes=5)):
            return None
        operation = validate_operation(connection, {field: receipt[field] for field in IDENTITY_FIELDS})
        if operation is None:
            return None
    except (WebsiteAuthorityError, TypeError, ValueError, AttributeError):
        return None
    return receipt


KIT_CONFIG_PATH = ".mlai/articles-verification.json"


def kit_marker_routes(website, source_sha, artifact_digest):
    """Return the public routes the merged verification kit sealed with the live marker.

    The CI proof seals the listing, detail and unknown-slug routes it tested. An
    adopted integration marks only the listing it reviewed, because the article
    detail is the site's own renderer (``artifact_marker_routes`` in the committed
    kit config). Vyavos, 17 Oct: its listing served the reviewed marker, but its
    detail served the original setup's marker, so the live check never passed.
    Returns ``None`` when the exact commit's config is unreadable or names
    another marker, so every rendered route keeps requiring the marker.
    """
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token
    # The same read-only repository token as verify_repository_head: live checks
    # never need the optional CI evidence grants.
    try:
        credential = create_installation_access_token(installation_id=website.installation_id,
            repository=website.github_repo, repository_id=website.repository_id, permission_mode="read")
        response = http_client.get(f"https://api.github.com/repos/{website.github_repo}/contents/{KIT_CONFIG_PATH}?ref={quote(str(source_sha), safe='')}",
            headers={"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}, timeout=(3, 15))
        if response.status_code != 200:
            return None
        config = json.loads(base64.b64decode(response.json()["content"], validate=False))
    except Exception:
        # Reading the kit only narrows the check; without it every route stays marked.
        return None
    if not isinstance(config, dict):
        return None
    routes = config.get("artifact_marker_routes")
    if (config.get("artifact_digest") != artifact_digest or not isinstance(routes, list) or not routes
            or not all(isinstance(path, str) and path.startswith("/") for path in routes)):
        return None
    return {path.rstrip("/") or "/" for path in routes}


def route_requires_marker(kind, path, marker_routes):
    """The listing always carries the reviewed marker; other routes only when the kit sealed them."""
    return kind == "listing" or marker_routes is None or (str(path).rstrip("/") or "/") in marker_routes


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
            from .website_github_failure import taxonomy_active
            raise WebsiteAuthorityError("ci_marker_identity_mismatch" if taxonomy_active() else "deployment_marker_unverified", "The repository CI proof must verify this exact reviewed live artifact marker.")
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
    from .website_deployment import ensure_fingerprint, observe_headers
    ensure_fingerprint(website, binding)
    marker_routes = kit_marker_routes(website, data["source_sha"], artifact_digest)
    observations = []
    for kind in ("listing", "detail", "unknown_slug"):
        public_url = f"https://{website.organization.domain}{routes[kind]}"
        expected_status = 404 if kind == "unknown_slug" else 200
        body, headers = fetch_live_route(public_url, website.organization.domain, expected_status=expected_status)
        if kind == "listing":observe_headers(website, binding, headers)
        marker = headers.get("x-mlai-artifact-digest") or ""
        if (expected_status == 200 and route_requires_marker(kind, routes[kind], marker_routes)
                and marker != artifact_digest and not re.search(rb'<meta\s+name=["\x27]mlai-artifact-digest["\x27]\s+content=["\x27]' + artifact_digest.encode() + rb'["\x27]', body)):
            raise WebsiteAuthorityError("live_route_unavailable", "The public listing or article detail has not deployed this reviewed integration artifact yet.")
        observations.append({"kind": kind, "path": routes[kind], "status": expected_status})
    receipt = {"status": "passed", "source_sha": data["source_sha"], "connection_generation": website.generation,
        "target_id": data["target_id"], "public_url": url, "checked_at": timezone.now().isoformat(), "ci_evidence_digest": data["evidence_digest"],
        "artifact_digest": artifact_digest, "contract_digest": data["contract_digest"], "verified_routes": observations, "provider_verified": True}
    with authority_guard(binding, action="read") as website:
        current_target = website.targets.filter(generation=website.generation, target_key=data.get("target_id")).first()
        current_ci = ci_attestation_for(website, data)
        if current_target is None or current_target.contract != target.contract or current_ci != ci_proof:
            raise WebsiteAuthorityError("website_configuration_changed", "The verified target changed during the live check.")
        op, _ = WebsiteConnectionOperation.objects.update_or_create(idempotency_key=f"{website.pk}:deployment:{evidence_digest(binding)}", defaults={
            "connection": website, "generation": website.generation, "action": "deployment-verify", "state": "completed", "payload": binding, "receipt": receipt})
        from .website_prepare import wake_prepare_after_commit
        wake_prepare_after_commit(website.pk)
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


def discover_source_attestation(website, target, source_sha):
    """Read a sealed provider proof for an automatically observed source change."""
    require_legacy_proof_inputs(target=target.contract)
    from integrations import http_client
    from .website_tokens import mint_ci_evidence_token, read_ci_provider_checks
    from .website_operations import deletion_epoch
    require_unlocked_remote_call()
    credential = mint_ci_evidence_token(installation_id=website.installation_id,
        repository=website.github_repo, repository_id=website.repository_id)
    headers = {"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}
    try:
        payload = read_ci_provider_checks(f"https://api.github.com/repos/{website.github_repo}/commits/{quote(source_sha, safe='')}/check-runs?per_page=100", headers=headers)
        for check in payload["check_runs"]:
            if (check.get("name") != CHECK_NAME or check.get("head_sha") != source_sha
                    or check.get("status") != "completed" or check.get("conclusion") != "success"
                    or (check.get("app") or {}).get("slug") != "github-actions"):
                continue
            output = check.get("output") or {}
            match = MARKER.search(str(output.get("summary") or "") + "\n" + str(output.get("text") or ""))
            if not match:
                continue
            try:
                provider = json.loads(base64.b64decode(match.group(1), validate=True))
            except (ValueError, TypeError):
                continue
            expected = {**provider, **contract_for(website), "github_repo": website.github_repo,
                "source_sha": source_sha, "target_id": target.target_key,
                "contract_digest": target.contract.get("contract_digest"), "deletion_epoch": deletion_epoch(website)}
            validated_ci_identity(expected, provider)
            origin = website.operations.filter(pk=provider["operation_id"], generation=website.generation).first()
            if origin is None or provider["operation_attempt"] != origin.payload.get("attempt", 1):
                raise WebsiteAuthorityError("website_operation_changed", "The source proof belongs to an older attempt.")
            return {**provider, "run_id": origin.payload.get("run_id") or ""}
        raise WebsiteAuthorityError("ci_attestation_required", "Waiting for native CI proof at the observed source.", retryable=True)
    finally:
        try:
            http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 8))
        except Exception:
            pass
