"""Durable GitHub revocation and reference-only outbox processing."""

from datetime import timedelta
import base64
import hashlib
import logging
from types import SimpleNamespace
from urllib.parse import quote

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import OrganizationContentConfig
from .website_connections import contract_for, transition_connection
from .website_contract import cleanup_plan, safe_repository_path, evidence_digest
from .website_models import WebsiteConnection, WebsiteConnectionOperation, WebsiteScanSnapshot
from . import website_preview_cleanup_budget as preview_budget

logger = logging.getLogger(__name__)


def _merge_connection_identity(website):
    """Fence authenticated merge observations to the currently selected source."""
    return (str(website.pk), website.generation, website.repository_id,
        website.github_repo.casefold(), website.branch, website.state,
        website.installation_id, website.configuration_version)


def _merge_intent_matches(website, run, pull, source_sha):
    """Accept only a merged GitHub PR matching our saved pre-merge intent."""
    intent = (run.result or {}).get("publish_merge_intent") or {}
    request = run.run_request or {}
    head, base = pull.get("head") or {}, pull.get("base") or {}
    if not (intent.get("website_connection_id") == str(website.pk)
            and intent.get("connection_generation") == website.generation
            and intent.get("repository_id") == website.repository_id
            and intent.get("run_id") == run.run_id
            and str(intent.get("github_repo") or "").casefold() == website.github_repo.casefold()
            and intent.get("source_sha") == website.verified_sha
            and intent.get("base_branch") == website.branch
            and request.get("website_connection_id") == str(website.pk)
            and request.get("connection_generation") == website.generation
            and run.github_repo.casefold() == website.github_repo.casefold()
            and pull.get("merged") is True and pull.get("merge_commit_sha") == source_sha
            and pull.get("number") == intent.get("pr_number")
            and base.get("ref") == website.branch
            and head.get("sha") == intent.get("head_sha")
            and head.get("ref") == intent.get("head_branch")):
        return False
    return _pull_from_selected_repository(website, head, base)


def _pull_from_selected_repository(website, head, base):
    """Both sides of a pull request are the selected repository itself, never a fork."""
    return all((side.get("repo") or {}).get("id") == website.repository_id
        and str((side.get("repo") or {}).get("full_name") or "").casefold() == website.github_repo.casefold()
        for side in (head, base))


def _owner_merge_vouch(website, run):
    """The pull request number and previewed head an open, exactly verified setup vouches for."""
    from .vibe_marketing_views import _pull_request_number_from_run
    from .website_connections import _verified_setup_run_base, scoped_run_contract, setup_pr_open
    from .website_contract import SHA_PATTERN, connection_contract
    saved = connection_contract(scoped_run_contract(run))
    head_sha = str(run.result.get("branch_commit_sha") or "") if isinstance(run.result, dict) else ""
    number = _pull_request_number_from_run(run) if setup_pr_open(run) else None
    eligible = bool(number and number > 0 and SHA_PATTERN.fullmatch(head_sha)
        and saved.get("website_connection_id") == str(website.pk)
        and saved.get("connection_generation") == website.generation
        and saved.get("repository_id") == website.repository_id
        and str(run.github_repo or "").casefold() == website.github_repo.casefold()
        and _verified_setup_run_base(run, head_sha, website))
    return (number, head_sha) if eligible else None


def _owner_merge_setup_candidates(website, rows):
    """Approved setups whose own verified pull request MLAI has not merged.

    Stopping preparation keeps such a pull request open to be merged, and its
    owner may merge it on GitHub instead of with Publish. No merge intent exists
    then, so only the setup's exact preview proof can vouch for its head.
    """
    from .website_contract import WebsiteAuthorityError
    found = []
    for run in rows.filter(workflow="article_system_setup", status="completed",
            approval_state="approved").order_by("-updated_at")[:10]:
        try:
            vouch = _owner_merge_vouch(website, run)
        except WebsiteAuthorityError:
            continue
        except Exception as exc:
            # Unreadable saved results cannot vouch for a merge, and must not
            # stop the push from being handled as an external change.
            logger.warning("website_owner_setup_merge_unreadable connection=%s run_id=%s error_type=%s",
                website.pk, run.run_id, type(exc).__name__)
            continue
        if vouch:
            found.append((run, *vouch))
    return found


def _github_merged_at(pull):
    """GitHub's aware merge time for a merged pull request, or "" when unusable."""
    from django.utils.dateparse import parse_datetime
    try:
        merged_at = parse_datetime(pull["merged_at"]) if isinstance(pull.get("merged_at"), str) else None
    except ValueError:
        merged_at = None
    return merged_at.isoformat() if merged_at and merged_at.tzinfo else ""


def _setup_pull_merged_as(website, pull, *, number, head_sha, source_sha):
    """GitHub merged this setup pull request, at its previewed head, as source_sha."""
    head, base = pull.get("head") or {}, pull.get("base") or {}
    return bool(pull.get("merged") is True and pull.get("merge_commit_sha") == source_sha
        and pull.get("number") == number and base.get("ref") == website.branch
        and head.get("sha") == head_sha and _pull_from_selected_repository(website, head, base))


def _commit_tree_sha(website, sha, headers):
    """Read the tree of one exact commit in the selected repository."""
    from integrations import http_client
    from .website_contract import SHA_PATTERN
    response = http_client.get(f"https://api.github.com/repos/{website.github_repo}/git/commits/{sha}",
        headers=headers, timeout=(3, 15))
    response.raise_for_status()
    commit = response.json()
    tree = commit.get("tree") if isinstance(commit, dict) else None
    tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(tree_sha, str) or not SHA_PATTERN.fullmatch(tree_sha) or commit.get("sha") != sha:
        raise ValueError("GitHub returned an unexpected commit identity.")
    return tree_sha


def _find_owner_merged_setup(website, rows, source_sha, identity):
    """Recognize the owner's GitHub merge of a verified setup pull request.

    GitHub must report the setup's recorded pull request merged into the
    selected branch as source_sha, from this repository and at the exact head
    the setup previewed, and source_sha must have that head's tree. The new
    source is then exactly what was verified, whichever merge method was used.
    Unavailable or ambiguous evidence remains an external source change.
    A match carries that evidence for record_owner_setup_merge.
    """
    from .website_connections import authority_guard, require_unlocked_remote_call
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token
    candidates = _owner_merge_setup_candidates(website, rows)
    if not candidates:
        return None
    binding = {**contract_for(website), "domain": website.organization.domain, "github_repo": website.github_repo}
    credential, headers, trees = None, {}, {}
    try:
        with authority_guard(binding, action="read"):
            pass
        require_unlocked_remote_call()
        credential = create_installation_access_token(installation_id=website.installation_id,
            repository=website.github_repo, repository_id=website.repository_id, permission_mode="read", use_cache=False)
        headers = {"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}
        for run, number, head_sha in candidates:
            try:
                response = http_client.get(f"https://api.github.com/repos/{website.github_repo}/pulls/{number}",
                    headers=headers, timeout=(3, 15))
                response.raise_for_status()
                pull = response.json()
                if not isinstance(pull, dict) or not _setup_pull_merged_as(website, pull, number=number,
                        head_sha=head_sha, source_sha=source_sha):
                    continue
                for sha in (source_sha, head_sha):
                    if sha not in trees:
                        trees[sha] = _commit_tree_sha(website, sha, headers)
                if trees[source_sha] != trees[head_sha]:
                    # Merged with other changes (a moved base or an edited merge):
                    # that source was never previewed, so it is scanned as external.
                    logger.info("website_owner_setup_merge_changed connection=%s run_id=%s pr_number=%s source_sha=%s",
                        website.pk, run.run_id, number, source_sha)
                    continue
                evidence = {"pr_number": number, "head_sha": head_sha, "merge_commit_sha": source_sha,
                    "merged_at": _github_merged_at(pull), "request_digest": evidence_digest(run.run_request or {})}
                with authority_guard(binding, action="read") as current:
                    if _merge_connection_identity(current) == identity:
                        logger.info("website_owner_setup_merge_matched connection=%s run_id=%s pr_number=%s source_sha=%s",
                            website.pk, run.run_id, number, source_sha)
                        return {"run_id": run.run_id, "connection_identity": identity, "owner_merge": evidence}
            except Exception as exc:
                logger.warning("website_owner_setup_merge_unverified connection=%s run_id=%s error_type=%s",
                    website.pk, run.run_id, type(exc).__name__)
    except Exception as exc:
        logger.warning("website_owner_setup_merge_unavailable connection=%s error_type=%s", website.pk, type(exc).__name__)
    finally:
        if credential:
            try:
                http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 10))
            except Exception:
                pass
    return None


def _find_owned_merge(website, organization_id, source_sha):
    """Resolve webhook races with read-only authenticated PR evidence, outside locks.

    A push is owned when a run recorded it as its merge commit, when a saved
    pre-merge intent matches the merged pull request, or when the owner merged
    a verified setup pull request on GitHub with exactly its previewed source.
    Anything else is an external source change.
    """
    from workflow_runs.models import ContentFactoryRun
    from .website_connections import authority_guard, require_unlocked_remote_call
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token
    rows = ContentFactoryRun.objects.filter(organization_id=organization_id,
        github_repo__iexact=website.github_repo,
        run_request__website_connection_id=str(website.pk), run_request__connection_generation=website.generation)
    saved = rows.filter(result__merge_status="merged", result__merge_sha=source_sha).first()
    if saved is None:
        saved = rows.filter(result__merge_status="merged", result__merge_response__sha=source_sha).first()
    identity = _merge_connection_identity(website)
    if saved:
        return {"run_id": saved.run_id, "connection_identity": identity}
    candidates = rows.filter(result__publish_merge_intent__website_connection_id=str(website.pk),
        result__publish_merge_intent__connection_generation=website.generation,
        result__publish_merge_intent__repository_id=website.repository_id,
        result__publish_merge_intent__source_sha=website.verified_sha).order_by("-updated_at")[:10]
    for run in candidates:
        intent = (run.result or {}).get("publish_merge_intent") or {}
        number = intent.get("pr_number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            continue
        credential = None
        headers = {}
        try:
            binding = {**contract_for(website), "domain": website.organization.domain, "github_repo": website.github_repo}
            with authority_guard(binding, action="read"):
                pass
            require_unlocked_remote_call()
            credential = create_installation_access_token(installation_id=website.installation_id,
                repository=website.github_repo, repository_id=website.repository_id, permission_mode="read", use_cache=False)
            headers = {"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}
            response = http_client.get(f"https://api.github.com/repos/{website.github_repo}/pulls/{number}",
                headers=headers, timeout=(3, 15))
            response.raise_for_status()
            pull = response.json()
            if not isinstance(pull, dict) or not _merge_intent_matches(website, run, pull, source_sha):
                continue
            with authority_guard(binding, action="read") as current:
                if _merge_connection_identity(current) == identity:
                    return {"run_id": run.run_id, "connection_identity": identity}
        except Exception:
            # Unavailable or ambiguous PR evidence remains an external source change.
            continue
        finally:
            if credential:
                try:
                    http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 10))
                except Exception:
                    pass
    return _find_owner_merged_setup(website, rows, source_sha, identity)


OWNER_SETUP_MERGE_SOURCE = "github_owner_merge"


def _owner_merge_recordable(website, run, evidence):
    """The run still vouches for the matched merge and records no merge commit.

    It is either still the open setup whose exact proof vouched for the match,
    or the same setup since marked merged without a commit. A recorded merge
    commit is never replaced.
    """
    from .vibe_marketing_views import _pull_request_number_from_run
    result = run.result if isinstance(run.result, dict) else {}
    setup = result.get("article_system_setup") if isinstance(result.get("article_system_setup"), dict) else {}
    response = result.get("merge_response") if isinstance(result.get("merge_response"), dict) else {}
    if result.get("merge_sha") or setup.get("merge_sha") or response.get("sha"):
        return False
    number, head_sha = evidence["pr_number"], evidence["head_sha"]
    if "merged" not in {result.get("merge_status"), setup.get("merge_status")}:
        return _owner_merge_vouch(website, run) == (number, head_sha)
    return bool(run.status == "completed" and run.approval_state == "approved"
        and setup.get("setup_run_id") == run.run_id
        and str(run.github_repo or "").casefold() == website.github_repo.casefold()
        and evidence_digest(run.run_request or {}) == evidence["request_digest"]
        and _pull_request_number_from_run(run) == number
        and result.get("branch_commit_sha") == head_sha and setup.get("branch_commit_sha") == head_sha)


def record_owner_setup_merge(website, owned_merge):
    """Save the owner's tree-verified GitHub merge as its setup run's own merge.

    Callers hold the organization and connection locks and have rechecked the
    connection identity. Only _find_owner_merged_setup's match carries this
    evidence; a recorded merge or a publish intent already has its own. The
    run is locked before the saved pending setup, as MLAI's own merge does.
    Settle and CI-lineage checks then accept it like MLAI's Publish merge,
    while goal evidence still needs a publish intent to call it a backend
    merge. Recording never fails the push: a refusal leaves the run as it was.
    """
    from workflow_runs.models import ContentFactoryRun
    from .vibe_marketing_views import _mark_pending_article_system_setup_merged, _save_setup_merge_result
    evidence = (owned_merge or {}).get("owner_merge")
    if not isinstance(evidence, dict):
        return False
    run_id, source_sha = owned_merge.get("run_id"), evidence.get("merge_commit_sha")
    try:
        with transaction.atomic():
            run = ContentFactoryRun.objects.select_for_update().filter(run_id=run_id,
                organization_id=website.organization_id, workflow="article_system_setup",
                run_request__website_connection_id=str(website.pk),
                run_request__connection_generation=website.generation).first()
            if run is None or not _owner_merge_recordable(website, run, evidence):
                logger.info("website_owner_setup_merge_unrecorded connection=%s run_id=%s source_sha=%s reason=run_changed",
                    website.pk, run_id, source_sha)
                return False
            config = OrganizationContentConfig.objects.select_for_update().filter(website_connection=website).first()
            _save_setup_merge_result(run, checks_status="merged", merged_at=evidence["merged_at"] or None,
                merge_response={"source": OWNER_SETUP_MERGE_SOURCE, "sha": source_sha, "pull": {
                    "number": evidence["pr_number"], "merge_commit_sha": source_sha, "head_sha": evidence["head_sha"]}})
            _mark_pending_article_system_setup_merged(config, run=run, result=run.result)
    except Exception as exc:
        logger.warning("website_owner_setup_merge_unrecorded connection=%s run_id=%s source_sha=%s error_type=%s",
            website.pk, run_id, source_sha, type(exc).__name__)
        return False
    logger.info("website_owner_setup_merge_recorded connection=%s run_id=%s pr_number=%s source_sha=%s",
        website.pk, run_id, evidence["pr_number"], source_sha)
    return True


def revoke_installation(installation_id, *, repository_ids=None, reason="github_authorization_revoked"):
    """Revoke exact GitHub identities; an install/reinstall event never reconnects."""
    rows = OrganizationContentConfig.objects.filter(website_connection__installation_id=str(installation_id),
        website_connection__state__in=["connected", "paused"]).select_related("website_connection__organization")
    if repository_ids is not None:
        rows = rows.filter(website_connection__repository_id__in=repository_ids)
    count = 0
    for config in rows:
        connection = config.website_connection
        transition_connection(config, action="revoke", expected=contract_for(connection),
            idempotency_key=f"{reason}:{connection.generation}")
        count += 1
    return count


def handle_website_github_event(event_type, payload):
    """Process verified lifecycle events using immutable IDs, fail closed on loss."""
    action = str(payload.get("action") or "")
    installation_id = (payload.get("installation") or {}).get("id")
    result = {"event": event_type, "revoked": 0}
    if event_type == "installation" and action in {"deleted", "suspend"} and installation_id:
        result["revoked"] = revoke_installation(installation_id, reason=f"github_installation_{action}")
    elif event_type == "installation_repositories" and action == "removed" and installation_id:
        ids = [r["id"] for r in payload.get("repositories_removed", []) if isinstance(r, dict) and isinstance(r.get("id"), int)]
        result["revoked"] = revoke_installation(installation_id, repository_ids=ids, reason="github_repository_removed")
    elif event_type == "repository" and action in {"deleted", "transferred", "renamed", "archived", "privatized"}:
        repository_id = (payload.get("repository") or {}).get("id")
        if repository_id:
            for inst in WebsiteConnection.objects.filter(repository_id=repository_id).values_list("installation_id", flat=True).distinct():
                result["revoked"] += revoke_installation(inst, repository_ids=[repository_id], reason=f"github_repository_{action}")
    elif event_type == "push":
        repo = payload.get("repository") or {}
        source_sha = str(payload.get("after") or "")
        from .website_contract import SHA_PATTERN
        if not isinstance(repo.get("id"), int) or not SHA_PATTERN.fullmatch(source_sha) or set(source_sha) == {"0"}:
            result["ignored"] = True
            return result
        result["invalidated"] = 0
        prepare_wakes = []
        for config in OrganizationContentConfig.objects.filter(website_connection__repository_id=repo["id"], website_connection__state__in=["connected", "paused"]).select_related("website_connection__organization"):
            website = config.website_connection
            if payload.get("ref") != f"refs/heads/{website.branch}" or website.verified_sha == source_sha:
                continue
            owned_merge = _find_owned_merge(website, config.organization_id, source_sha)
            from organizations.models import Organization
            with transaction.atomic():
                Organization.objects.select_for_update().get(pk=config.organization_id)
                website = WebsiteConnection.objects.select_for_update().get(pk=website.pk)
                if (website.state not in {"connected", "paused"} or website.repository_id != repo["id"]
                        or payload.get("ref") != f"refs/heads/{website.branch}" or website.verified_sha == source_sha):
                    continue
                if owned_merge and owned_merge["connection_identity"] != _merge_connection_identity(website):
                    owned_merge = None
                if owned_merge:
                    record_owner_setup_merge(website, owned_merge)
                _, created = WebsiteScanSnapshot.objects.get_or_create(connection=website, generation=website.generation,
                    run_id=f"github-head:{source_sha}", fingerprint=evidence_digest({"source_sha": source_sha}),
                    defaults={"source_sha": source_sha, "detector_version": "github_head", "evidence": {"source": "github_push", "branch": website.branch}})
                if created:
                    previous_revision = website.configuration_version
                    website.configuration_version += 1
                    from .website_goal_scope import record_source_transition
                    record_source_transition(website, previous_revision=previous_revision, source_sha=source_sha)
                    if not owned_merge:
                        website.capabilities = {**website.capabilities, "publishingReady": False, "previewSupported": False}
                    WebsiteConnectionOperation.objects.get_or_create(
                        idempotency_key=f"{website.pk}:source-reverify:{website.generation}:{source_sha}", defaults={
                            "connection": website, "generation": website.generation, "action": "source-reverify",
                            "payload": {"source_sha": source_sha, "target_id": config.default_publish_target_id,
                                "previous_configuration_revision": previous_revision, "configuration_revision": website.configuration_version,
                                "owned_merge_run_id": owned_merge["run_id"] if owned_merge else "", "scan_required": not bool(owned_merge)},
                            "receipt": {"status": "verification_queued", "repository_modified": False}})
                    if not owned_merge:
                        website.blockers = [item for item in website.blockers if item.get("code") != "repository_source_changed"] + [{"code": "repository_source_changed", "message": "The repository changed. Scan and verify the current source before publishing.", "source_sha": source_sha}]
                    website.save(update_fields=["configuration_version", "capabilities", "blockers", "updated_at"])
                    result["invalidated"] += 1
                    prepare_wakes.append((website.pk, config.organization_id, owned_merge["run_id"] if owned_merge else ""))
        # Run no provider calls while the webhook transaction owns authority
        # locks. Both MLAI merges and customer pushes resume the same journey.
        for identifier, organization_id, owned_merge_run_id in prepare_wakes:
            from .website_prepare import wake_prepare_for_source
            wake_prepare_for_source(identifier, organization_id=organization_id, owned_merge_run_id=owned_merge_run_id)
    else:
        result["ignored"] = True
    return result


def _cleanup_proposal(operation):
    """Read current files and propose changes only; never mutate a repository here."""
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token
    connection = operation.connection
    token = create_installation_access_token(installation_id=connection.installation_id,
        repository=connection.github_repo, repository_id=connection.repository_id, permission_mode="read", use_cache=False)
    headers = {"Authorization": f"Bearer {token.token}", "Accept": "application/vnd.github+json"}
    try:
        return _read_cleanup_proposal(operation, headers=headers)
    finally:
        try:
            http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 10))
        except Exception:
            pass


def _read_cleanup_proposal(operation, *, headers):
    """Read a bounded ledger manifest while the scoped temporary token is live."""
    from integrations import http_client
    connection = operation.connection
    repo_url = f"https://api.github.com/repos/{connection.github_repo}"
    head = http_client.get(f"{repo_url}/commits/{quote(connection.branch, safe='')}", headers=headers, timeout=(3, 15))
    head.raise_for_status()
    sha = head.json()["sha"]
    files = []
    unproven_paths = []
    for mutation in connection.repository_mutations.filter(pk__in=operation.payload.get("mutation_ids", [])):
        applied = mutation.status == "applied" and bool(mutation.head_sha)
        if applied and mutation.head_sha != sha:
            comparison = http_client.get(f"{repo_url}/compare/{mutation.head_sha}...{sha}", headers=headers, timeout=(3, 15))
            comparison.raise_for_status()
            applied = comparison.json().get("status") in {"ahead", "identical"}
        if applied:
            files.extend(mutation.files)
        else:
            unproven_paths.extend(row.get("path", "") for row in mutation.files)
    # Multiple ledger entries for one file mean shared ownership; retain by default.
    seen = set()
    for entry in files:
        if entry["path"] in seen:
            entry["ownership"] = "shared"
        seen.add(entry["path"])
    current = {}
    for entry in files:
        path = safe_repository_path(entry["path"])
        response = http_client.get(f"{repo_url}/contents/{quote(path, safe='/')}", params={"ref": sha}, headers=headers, timeout=(3, 15))
        if response.status_code == 404:
            current[path] = None
            continue
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or value.get("type") != "file" or value.get("encoding") != "base64":
            current[path] = "unverified"
        else:
            current[path] = hashlib.sha256(base64.b64decode(value["content"])).hexdigest()
            if entry.get("after_blob_sha") and entry["after_blob_sha"] != value.get("sha"):
                current[path] = "modified_blob"
    from .models import WrittenArticle
    retained_paths = unproven_paths + list(WrittenArticle.objects.filter(organization=connection.organization).exclude(content_path="").values_list("content_path", flat=True))
    plan = cleanup_plan(files, current, retained_paths=retained_paths)
    plan["retained"] = sorted(set(plan["retained"]) | set(retained_paths))
    ambiguous = {row["path"] for row in plan["conflicts"]}
    plan["deletions"] = sorted(set(plan["deletions"]) - ambiguous)
    receipt = {"status": "review_required", "source_sha": sha, "repository_modified": False,
        "requires_review": True, **plan}
    receipt["proposal_digest"] = evidence_digest(receipt)
    return receipt


def disable_pending_native_auto_merge(connection, urls):
    """Withdraw previously queued GitHub merges while retaining the pull requests."""
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token
    from urllib.parse import urlparse
    if not urls:
        return []
    pending = []
    token = create_installation_access_token(installation_id=connection.installation_id,
        repository=connection.github_repo, repository_id=connection.repository_id, permission_mode="write", use_cache=False)
    headers = {"Authorization": f"Bearer {token.token}", "Accept": "application/vnd.github+json"}
    try:
        for url in urls:
            parsed = urlparse(url)
            prefix = f"/{connection.github_repo}/pull/"
            number = parsed.path.removeprefix(prefix)
            if parsed.hostname != "github.com" or not parsed.path.startswith(prefix) or not number.isdigit():
                continue
            try:
                response = http_client.get(f"https://api.github.com/repos/{connection.github_repo}/pulls/{number}", headers=headers, timeout=(3, 15))
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                pull = response.json()
                if not pull.get("auto_merge"):
                    continue
                response = http_client.post("https://api.github.com/graphql", headers=headers,
                    json={"query": "mutation($id: ID!) { disablePullRequestAutoMerge(input: {pullRequestId: $id}) { clientMutationId } }", "variables": {"id": pull["node_id"]}}, timeout=(3, 15))
                response.raise_for_status()
                if response.json().get("errors"):
                    pending.append(url)
            except Exception:
                pending.append(url)
    finally:
        try:
            http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 10))
        except Exception:
            pass
    return pending


def _process_worker_followup(identifier, now):
    """Claim a durable follow-up, release all DB locks, then contact the worker."""
    from .website_connections import authority_guard, owner_write_guard
    from .website_contract import WebsiteAuthorityError
    from integrations.services import article_generation
    with transaction.atomic():
        operation = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, state="pending").first()
        if operation is None or (operation.next_attempt_at and operation.next_attempt_at > now):
            return None
        operation.attempts += 1
        attempt = operation.attempts
        operation.next_attempt_at = now + timedelta(minutes=5)
        operation.save(update_fields=["attempts", "next_attempt_at", "updated_at"])
        payload = dict(operation.payload)
    binding = payload.get("binding") or {}
    kind = payload.get("kind")
    state, receipt = "pending", {"status": "retry_pending", "repository_modified": False}
    remote = {}
    try:
        action = "publish" if kind == "publish_article" else "read"
        with authority_guard(binding, action=action):
            pass
        functions = {"publish_article": article_generation.publish_article,
            "trigger_article_generation": article_generation.trigger_article_generation,
            "confirm_topic": article_generation.confirm_topic}
        remote = functions[kind](**payload["arguments"])
        with owner_write_guard(binding):
            state, receipt = "completed", {"status": "dispatched", "repository_modified": False,
                "run_id": str((remote or {}).get("run_id") or (remote or {}).get("job_id") or payload.get("source_run_id") or "")}
            if kind == "publish_article":
                from .models import ContentFactoryJob
                job = ContentFactoryJob.objects.filter(job_id=payload.get("source_run_id")).first()
                if job:
                    job.status = "generating"
                    metadata = dict(job.request_meta or {})
                    actions = dict(metadata.get("callback_actions") or {})
                    markers = list(actions.get("preview_ready_auto_approve") or [])
                    if payload.get("callback_dedupe_key") and payload["callback_dedupe_key"] not in markers:
                        markers.append(payload["callback_dedupe_key"])
                    actions["preview_ready_auto_approve"] = markers
                    job.request_meta = {**metadata, "publish_stage": "auto_approved", "callback_actions": actions}
                    job.save(update_fields=["status", "request_meta", "updated_at"])
    except WebsiteAuthorityError as exc:
        state, receipt = "cancelled", {"status": "authority_revoked", "code": exc.code, "repository_modified": False}
        if isinstance(remote, dict) and (remote.get("run_id") or remote.get("job_id")):
            from .vibe_marketing_views import _create_local_run
            _create_local_run(workflow="publish_article" if kind == "publish_article" else "article_generation",
                domain=binding.get("domain", ""), payload=binding, remote_data=remote)
    except Exception:
        receipt["last_error"] = "worker_followup_unavailable"
    # Compare-and-set the claim: a duplicate reconciler cannot replace a newer receipt.
    WebsiteConnectionOperation.objects.filter(pk=identifier, attempts=attempt, state="pending").update(
        state=state, receipt=receipt, next_attempt_at=now + timedelta(seconds=min(3600, 15 * 2 ** min(attempt, 8))), updated_at=timezone.now())
    return state


SUPERSEDED_SETUP_CLEANUP_DEADLINE = timedelta(hours=24)


def _superseded_setup_refs(body):
    """Keep only bounded ref identities and outcomes from the worker's receipt."""
    rows = body.get("refs") if isinstance(body, dict) and isinstance(body.get("refs"), list) else []
    return [{key: str(row[key])[:255] for key in ("status", "reason", "github_repo", "branch_name") if isinstance(row.get(key), str)}
        for row in rows[:20] if isinstance(row, dict)]


def _process_superseded_setup_cleanup(identifier, now):
    """Ask Content Factory to remove a replaced setup's own review branch.

    CF fences the run, then deletes only a ledger-owned head that never had a
    pull request. It retains everything else with a reason. An unknown outcome
    retries until the deadline, then settles as timed out instead of retrying
    forever.
    """
    from integrations import http_client
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers
    from .website_operations import deletion_epoch
    with transaction.atomic():
        operation = WebsiteConnectionOperation.objects.select_for_update().select_related("connection").filter(
            pk=identifier, state="pending").first()
        if operation is None or (operation.next_attempt_at and operation.next_attempt_at > now):
            return None
        operation.attempts += 1
        attempt = operation.attempts
        operation.next_attempt_at = now + timedelta(minutes=5)
        operation.save(update_fields=["attempts", "next_attempt_at", "updated_at"])
        payload, connection = dict(operation.payload), operation.connection
        generation, created_at = operation.generation, operation.created_at
    binding = payload.get("binding") if isinstance(payload.get("binding"), dict) else {}
    run_id = str(payload.get("run_id") or "")
    state, receipt = "pending", {"status": "cleanup_pending", "run_id": run_id, "repository_modified": None}
    try:
        if (not binding or binding.get("run_id") != run_id or connection.state != "connected" or connection.generation != generation
                or str(binding.get("deletion_epoch")) != str(deletion_epoch(connection))):
            # The worker would retain the ref for a changed connection anyway.
            state, receipt = "cancelled", {"status": "authority_changed", "run_id": run_id, "repository_modified": False}
        else:
            remote = _content_factory_remote_config()
            if not remote["enabled"]:
                receipt["last_error"] = "content_factory_unavailable"
            else:
                response = http_client.post(f"{remote['base_url']}/api/connections/{connection.pk}/superseded-setup-cleanup",
                    headers=_content_factory_headers(), json=binding, timeout=(3, 30))
                try:
                    body = response.json()
                except (ValueError, AttributeError):
                    body = {}
                refs = _superseded_setup_refs(body)
                if (response.status_code == 404 and isinstance(body, dict) and body.get("status") == "run_missing"
                        and body.get("run_id") == run_id):
                    # Any other 404 is a worker without this route yet; it retries until the deadline.
                    state, receipt = "completed", {"status": "run_missing", "run_id": run_id, "repository_modified": False}
                elif response.status_code == 409:
                    # Identity mismatch, a live run, or a fenced operation: never retried.
                    state, receipt = "cancelled", {"status": "refused", "run_id": run_id, "repository_modified": False}
                elif (response.status_code == 200 and isinstance(body, dict) and body.get("run_id") == run_id
                        and body.get("status") == "completed" and body.get("cleanup_pending") is False):
                    state, receipt = "completed", {"status": "completed", "run_id": run_id, "refs": refs,
                        "reason": str(body.get("reason") or "")[:64],
                        "repository_modified": any(row.get("status") == "deleted" for row in refs)}
                else:
                    receipt.update(refs=refs, last_error="superseded_setup_cleanup_pending")
    except Exception:
        logging.getLogger(__name__).warning("superseded_setup_cleanup_failed operation=%s", identifier, exc_info=True)
        receipt["last_error"] = "superseded_setup_cleanup_unavailable"
    if state == "pending" and now - created_at >= SUPERSEDED_SETUP_CLEANUP_DEADLINE:
        state, receipt = "completed", {**receipt, "status": "timed_out", "branch_cleanup_unconfirmed": True}
    # Compare-and-set the claim: a duplicate reconciler cannot replace a newer receipt.
    WebsiteConnectionOperation.objects.filter(pk=identifier, attempts=attempt, state="pending").update(
        state=state, receipt=receipt, updated_at=timezone.now(),
        next_attempt_at=now + timedelta(seconds=min(3600, 15 * 2 ** min(attempt, 8))) if state == "pending" else None)
    return state


SETUP_PR_MERGE_TIMEOUT = timedelta(minutes=60)


def _process_setup_pr_merge(identifier, now):
    """Merge an owner-published setup PR once Content Factory verified its changed head."""
    import logging
    from django.contrib.auth import get_user_model
    from django.utils.dateparse import parse_datetime
    from workflow_runs.models import ContentFactoryRun
    from .website_contract import WebsiteAuthorityError
    from .vibe_marketing_views import (_attempt_setup_publish_merge, _call_content_factory_run_status, _get_config,
        _mark_setup_merge_blocked, _setup_pr_reverification_record, _sync_local_run_from_remote,
        get_founder_company_context)
    with transaction.atomic():
        operation = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, state="pending").first()
        if operation is None or (operation.next_attempt_at and operation.next_attempt_at > now):
            return None
        operation.attempts += 1
        attempt = operation.attempts
        operation.next_attempt_at = now + timedelta(seconds=30)
        operation.save(update_fields=["attempts", "next_attempt_at", "updated_at"])
        payload = dict(operation.payload)
    state, receipt = "pending", {"status": "checking", "repository_modified": False}
    try:
        run = ContentFactoryRun.objects.filter(run_id=payload.get("run_id")).first()
        if run is None:
            state, receipt = "cancelled", {"status": "run_missing", "repository_modified": False}
        else:
            user = get_user_model().objects.get(pk=payload["requested_by_user_id"], is_active=True)
            context = get_founder_company_context(user, company_id=payload["company_id"])
            config = _get_config(context.organization)
            remote = _call_content_factory_run_status(run.run_id, workflow=run.workflow)
            if isinstance(remote, dict) and remote.get("run_id") == run.run_id:
                run = _sync_local_run_from_remote(run, remote)
            record = _setup_pr_reverification_record(run)
            requested_at = parse_datetime(str(payload.get("requested_at") or "")) or now
            if (run.result or {}).get("merge_status") == "merged":
                state, receipt = "completed", {"status": "merged", "repository_modified": False}
            elif record.get("head_sha") != payload.get("head_sha"):
                state, receipt = "cancelled", {"status": "superseded", "repository_modified": False}
            elif record.get("status") == "passed":
                outcome = _attempt_setup_publish_merge(run=run, context=context, automatic=False)
                kind = outcome.get("outcome")
                detail = str(outcome.get("detail") or "")[:300]
                if kind == "waiting" or (kind == "error" and outcome.get("retryable")
                                         and now - requested_at < SETUP_PR_MERGE_TIMEOUT):
                    receipt = {"status": "merge_waiting", "detail": detail, "repository_modified": False}
                else:
                    if kind == "error":
                        _mark_setup_merge_blocked(run=outcome.get("run") or run, config=config,
                            reason=detail or "The setup pull request could not be merged. Publish again to retry.")
                    state, receipt = "completed", {"status": kind, "detail": detail,
                        "repository_modified": kind in {"merged", "auto_merge_pending"}}
            elif record.get("status") == "failed":
                reason = str(record.get("error") or "Your changes to the setup pull request did not pass verification.")
                _mark_setup_merge_blocked(run=run, config=config, status_value="setup_changes_failed",
                    reason=f"{reason} Fix it on GitHub, then publish again.")
                state, receipt = "completed", {"status": "verification_failed", "code": record.get("error_code") or "",
                    "repository_modified": False}
            elif now - requested_at >= SETUP_PR_MERGE_TIMEOUT:
                _mark_setup_merge_blocked(run=run, config=config, status_value="setup_changes_failed",
                    reason="Checking your changes to the setup pull request took too long. Publish again to retry.")
                state, receipt = "completed", {"status": "timed_out", "repository_modified": False}
    except WebsiteAuthorityError as exc:
        state, receipt = "cancelled", {"status": "authority_revoked", "code": exc.code, "repository_modified": False}
    except Exception:
        logging.getLogger(__name__).warning("setup_pr_merge_followup_failed operation=%s", identifier, exc_info=True)
        receipt["last_error"] = "setup_pr_merge_unavailable"
    # Compare-and-set the claim: a duplicate reconciler cannot replace a newer receipt.
    WebsiteConnectionOperation.objects.filter(pk=identifier, attempts=attempt, state="pending").update(
        state=state, receipt=receipt, updated_at=timezone.now(),
        next_attempt_at=now + timedelta(seconds=30) if state == "pending" else None)
    return state


# A stop follow-up whose runs are cancelled waits this long for Content Factory
# to confirm its preview teardown: the TTL of CF's preview worker reaper.
CANCEL_PREVIEW_STOP_DEADLINE = timedelta(days=7)


def process_website_connection_operations(*, limit=20, now=None, connection_id=None):
    """Retry cancellation/token revocation, and build bounded cleanup proposals."""
    from integrations import http_client
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers
    from .website_tokens import revoke_generation_tokens
    now = now or timezone.now()
    from django.conf import settings
    from .website_notification_outbox import ACTION as notice_action
    from .website_first_article_refunds import ACTION as refund_action
    from .website_cost_recovery import ACTION as usage_action, advance as advance_usage
    from .website_cost_ledger import enabled as accounting_enabled
    from .website_operational_alert_delivery import ACTION as alert_action, advance as advance_alert, channel as alert_channel
    notices_enabled=getattr(settings,"ARTICLES_GOAL_NOTIFICATIONS_ENABLED",False)
    excluded = ["workflow", "cms-artifact", "cms-stage", "cms-policy"] if notices_enabled else ["workflow", "cms-artifact", "cms-stage", "cms-policy", notice_action]
    from .cms_connection_authority import enabled as cms_channel_enabled
    cms_enabled = cms_channel_enabled()
    from .channel_artifacts import enabled as artifacts_enabled
    from .cms_artifact_review import ensure_reviews, advance_review
    from .cms_owned_media import ensure_media, advance_media
    from .cms_staged_review import ensure_stages
    from .cms_article_approval import ensure_approvals, advance_approval
    from .cms_article_publication import ensure_publications, advance_publication
    artifact_reviews_enabled = artifacts_enabled()
    if artifact_reviews_enabled:
        ensure_reviews(limit=limit, connection_id=connection_id)
        ensure_media(limit=limit, connection_id=connection_id)
        ensure_stages(limit=limit, connection_id=connection_id)
        ensure_approvals(limit=limit, connection_id=connection_id)
        ensure_publications(limit=limit, connection_id=connection_id)
    else:
        excluded.extend(["cms-review", "cms-media", "cms-approval", "cms-publish"])
    if not cms_enabled:
        # Frozen provider jobs must never fall through to Git token cleanup.
        excluded.extend(["cms-capture", "cms-publish", "cms-unpublish"])
    if not accounting_enabled():
        excluded.append(usage_action)
    if not getattr(settings, "ARTICLES_OPERATIONAL_ALERTS_ENABLED", False):
        excluded.append(alert_action)
    unconfigured_alerts = Q(action=alert_action, state="needs_attention", receipt__status="unconfigured") if alert_channel() else Q(pk__in=[])
    due = WebsiteConnectionOperation.objects.filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now),
        Q(state="pending") | Q(action="prepare", state="running") | Q(action=notice_action,state="running") | Q(action=refund_action,state="running") |
        Q(action=usage_action, state="running") |
        Q(action=alert_action, state="running") |
        Q(action="cms-capture", state="running") |
        Q(action="cms-review", state="running") |
        Q(action="cms-media", state="running") |
        Q(action="cms-approval", state="running") |
        Q(action="cms-publish", state="running") |
        unconfigured_alerts |
        Q(action="prepare", state="needs_user", receipt__user_action__id__in=["deploy_site","merge_pr","review_draft"])).exclude(action__in=excluded)
    if connection_id:
        due = due.filter(connection_id=connection_id)
    ids = list(due.order_by("next_attempt_at").values_list("id", flat=True)[:limit])
    result = {"status": "completed", "processed": 0, "completed": 0, "pending": 0, "operations": []}
    from .website_support import reconcile_support_verifications
    reconcile_support_verifications(limit=limit, connection_id=connection_id)
    monitor_cleanup_pull_requests(limit=limit, connection_id=connection_id)
    from .website_wakeups import enabled as event_wakes_enabled
    for identifier in ids:
        if WebsiteConnectionOperation.objects.filter(pk=identifier, action="cms-publish").exists():
            operation = advance_publication(identifier, now=now) if artifact_reviews_enabled else None
            if operation is not None:
                result["processed"] += 1
                result["pending" if operation.state in {"pending", "running"} else "completed"] += 1
                if len(result["operations"]) < 20:
                    result["operations"].append({"id": str(identifier), "action": "cms-publish", "outcome": operation.state})
            continue
        if WebsiteConnectionOperation.objects.filter(pk=identifier, action="cms-approval").exists():
            operation = advance_approval(identifier, now=now) if artifact_reviews_enabled else None
            if operation is not None:
                result["processed"] += 1
                result["pending" if operation.state in {"pending", "running"} else "completed"] += 1
                if len(result["operations"]) < 20:
                    result["operations"].append({"id": str(identifier), "action": "cms-approval", "outcome": operation.state})
            continue
        if WebsiteConnectionOperation.objects.filter(pk=identifier, action="cms-media").exists():
            operation = advance_media(identifier, now=now) if artifact_reviews_enabled else None
            if operation is not None:
                result["processed"] += 1
                result["pending" if operation.state in {"pending", "running"} else "completed"] += 1
                if len(result["operations"]) < 20:
                    result["operations"].append({"id": str(identifier), "action": "cms-media", "outcome": operation.state})
            continue
        if WebsiteConnectionOperation.objects.filter(pk=identifier, action="cms-review").exists():
            # A frozen/changed CMS action must never fall through to Git cleanup.
            operation = advance_review(identifier, now=now) if artifact_reviews_enabled else None
            if operation is not None:
                result["processed"] += 1
                result["pending" if operation.state in {"pending", "running"} else "completed"] += 1
                if len(result["operations"]) < 20:
                    result["operations"].append({"id": str(identifier), "action": "cms-review", "outcome": operation.state})
            continue
        if cms_enabled and WebsiteConnectionOperation.objects.filter(pk=identifier, action="cms-capture").exists():
            from .cms_capture import advance_capture
            operation = advance_capture(identifier, now=now)
            if operation is not None:
                result["processed"] += 1
                result["pending" if operation.state in {"pending", "running"} else "completed"] += 1
                if len(result["operations"]) < 20:
                    result["operations"].append({"id": str(identifier), "action": "cms-capture", "outcome": operation.state})
            continue
        if getattr(settings, "ARTICLES_OPERATIONAL_ALERTS_ENABLED", False) and WebsiteConnectionOperation.objects.filter(pk=identifier, action=alert_action).exists():
            outcome = advance_alert(identifier, now=now)
            if outcome is not None:
                result["processed"] += 1
                result["pending" if outcome == "pending" else "completed"] += 1
                if len(result["operations"]) < 20:
                    result["operations"].append({"id": str(identifier), "action": alert_action, "outcome": outcome})
            continue
        if accounting_enabled() and WebsiteConnectionOperation.objects.filter(pk=identifier, action=usage_action).exists():
            outcome = advance_usage(identifier, now=now)
            if outcome is not None:
                result["processed"] += 1
                result["pending" if outcome == "pending" else "completed"] += 1
                if len(result["operations"]) < 20:
                    result["operations"].append({"id": str(identifier), "action": usage_action, "outcome": outcome})
            continue
        if WebsiteConnectionOperation.objects.filter(pk=identifier,action=refund_action).exists():
            from .website_first_article_refunds import advance_refund
            outcome=advance_refund(identifier,now=now)
            if outcome is not None:
                result["processed"]+=1
                result["pending" if outcome=="pending" else "completed"]+=1
                if len(result["operations"])<20:result["operations"].append({"id":str(identifier),"action":refund_action,"outcome":outcome})
                if outcome=="completed":
                    row=WebsiteConnectionOperation.objects.filter(pk=identifier,action=refund_action).first()
                    if row and row.payload.get("purge_after_reconciliation"):
                        from .website_first_article_refunds import finish_purged_connection
                        finish_purged_connection(row.connection_id)
            continue
        if notices_enabled and WebsiteConnectionOperation.objects.filter(pk=identifier,action=notice_action).exists():
            from .website_notification_outbox import advance_notice
            outcome=advance_notice(identifier,now=now)
            if outcome is not None:
                result["processed"]+=1
                result["pending" if outcome=="pending" else "completed"]+=1
                if len(result["operations"])<20:result["operations"].append({"id":str(identifier),"action":notice_action,"outcome":outcome})
            continue
        if event_wakes_enabled() and WebsiteConnectionOperation.objects.filter(pk=identifier, action="goal-wake").exists():
            from .website_wakeups import advance_wake
            outcome = advance_wake(identifier, now=now)
            if outcome is not None:
                result["processed"] += 1
                result["pending" if outcome == "pending" else "completed"] += 1
                if len(result["operations"]) < 20:
                    result["operations"].append({"id": str(identifier), "action": "goal-wake", "outcome": outcome})
            continue
        if WebsiteConnectionOperation.objects.filter(pk=identifier, action="prepare").exists():
            from .website_prepare import advance_prepare
            operation = advance_prepare(identifier, now=now)
            if operation is not None:
                result["processed"] += 1
                if len(result["operations"]) < 20:
                    result["operations"].append({"id": str(identifier), "action": "prepare", "outcome": operation.state})
                result["completed" if operation.state == "completed" else "pending"] += 1
            continue
        if WebsiteConnectionOperation.objects.filter(pk=identifier, action="source-reverify").exists():
            outcome = _process_source_reverification(identifier, now)
            if outcome is not None:
                result["processed"] += 1
                if len(result["operations"]) < 20:
                    result["operations"].append({"id": str(identifier), "action": "source-reverify", "outcome": outcome})
                result["pending" if outcome == "pending" else "completed"] += 1
            continue
        if WebsiteConnectionOperation.objects.filter(pk=identifier, action="setup-pr-merge").exists():
            outcome = _process_setup_pr_merge(identifier, now)
            if outcome is not None:
                result["processed"] += 1
                if len(result["operations"]) < 20:
                    result["operations"].append({"id": str(identifier), "action": "setup-pr-merge", "outcome": outcome})
                result["pending" if outcome == "pending" else "completed"] += 1
            continue
        if WebsiteConnectionOperation.objects.filter(pk=identifier, action="superseded-setup-cleanup").exists():
            outcome = _process_superseded_setup_cleanup(identifier, now)
            if outcome is not None:
                result["processed"] += 1
                if len(result["operations"]) < 20:
                    result["operations"].append({"id": str(identifier), "action": "superseded-setup-cleanup", "outcome": outcome})
                result["pending" if outcome == "pending" else "completed"] += 1
            continue
        if WebsiteConnectionOperation.objects.filter(pk=identifier, action="worker_followup").exists():
            outcome = _process_worker_followup(identifier, now)
            if outcome is not None:
                result["processed"] += 1
                if len(result["operations"]) < 20:
                    result["operations"].append({"id": str(identifier), "action": "worker_followup", "outcome": outcome})
                result["pending" if outcome == "pending" else "completed"] += 1
            continue
        purge_connection_id = None
        with transaction.atomic():
            # Lease this operation briefly. Neither operation nor authority rows
            # may stay locked across worker HTTP (offboarding locks both).
            op = WebsiteConnectionOperation.objects.select_for_update(of=("self",)).select_related("connection").filter(pk=identifier).first()
            if op is None:
                continue
            if op.state != "pending" or (op.next_attempt_at and op.next_attempt_at > now):
                continue
            result["processed"] += 1
            op.attempts += 1
            preview_budget.start(op, now=now)
            op.next_attempt_at = now + timedelta(minutes=5)
            op.save(update_fields=["attempts", "receipt", "next_attempt_at", "updated_at"])
            claimed_at = op.updated_at
        from .website_connections import require_unlocked_remote_call
        require_unlocked_remote_call()
        before_cleanup = evidence_digest({"payload": op.payload, "completed_steps": op.receipt.get("completed_steps", []),
            "refunded_run_ids": op.receipt.get("refunded_run_ids", []), "closed_mutation_ids": op.receipt.get("closed_mutation_ids", []),
            "tokens": op.receipt.get("tokens", {})})
        try:
            preview_budget.begin(op, now=now)
            if op.action == "cleanup":
                from .website_restoration import worker_restoration
                op.receipt = worker_restoration(op) or _cleanup_proposal(op)
                op.state = "review_required"
            else:
                if op.action == "disconnect" and "remove_setup" in op.payload:
                    from .website_disconnect import advance_disconnect_steps
                    advance_disconnect_steps(op)
                token_scope = f"operation:{op.payload['cancelled_operation_id']}" if op.action == "cancel-operation" else op.payload.get("previous_generation")
                tokens = revoke_generation_tokens(op.connection_id, token_scope)
                for child_id in op.payload.get("cancelled_child_operation_ids", []):
                    child_tokens = revoke_generation_tokens(op.connection_id, f"operation:{child_id}")
                    for key in ("revoked", "pending"):
                        tokens[key] = tokens.get(key, 0) + child_tokens.get(key, 0)
                cancel_ids = op.payload.get("cancel_run_ids", [])
                preview_ids = op.payload.get("stop_preview_run_ids", [])
                pending = list(cancel_ids[20:])
                preview_pending = list(preview_ids[20:])
                remote = _content_factory_remote_config()
                for run_id in cancel_ids[:20]:
                    if not remote["enabled"]:
                        pending.append(run_id)
                        continue
                    if op.action == "cancel-operation" and op.payload.get("cancelled_action") != "prepare":
                        cancelled = op.connection.operations.get(pk=op.payload["cancelled_operation_id"])
                        from .website_operations import deletion_epoch
                        response = http_client.post(f"{remote['base_url']}/api/connections/{op.connection_id}/cancel-operation",
                            headers=_content_factory_headers(), json={**contract_for(op.connection), "run_id": run_id,
                                "operation_id": str(cancelled.pk), "operation_attempt": cancelled.payload.get("attempt", 1),
                                "deletion_epoch": deletion_epoch(op.connection)}, timeout=(3, 15))
                    else:
                        response = http_client.post(f"{remote['base_url']}/api/runs/{run_id}/cancel",
                            headers=_content_factory_headers(), json={"reason": "website_connection_changed"}, timeout=(3, 15))
                    try:
                        body = response.json()
                    except (ValueError, AttributeError):
                        body = {}
                    cleanup_pending = isinstance(body, dict) and (body.get("cleanup_pending") is True or body.get("cleanupPending") is True)
                    if response.status_code not in {200, 204, 404} or cleanup_pending:
                        pending.append(run_id)
                    if op.action == "cancel-operation" and isinstance(body, dict):
                        op.receipt["remote_outcomes"] = body.get("remote_outcomes", [])
                for run_id in preview_ids[:20]:
                    if not remote["enabled"]:
                        preview_pending.append(run_id)
                        continue
                    response = http_client.post(f"{remote['base_url']}/api/runs/{run_id}/preview/stop",
                        headers=_content_factory_headers(), json={"reason": "website_connection_changed"}, timeout=(3, 15))
                    try:
                        body = response.json()
                    except (ValueError, AttributeError):
                        body = {}
                    confirmed = response.status_code in {204, 404} or (response.status_code == 200 and isinstance(body, dict)
                        and body.get("cleanup_success") is True and body.get("cleanup_pending") is not True and body.get("cleanupPending") is not True)
                    if response.status_code == 404:
                        op.receipt.setdefault("preview_absent_run_ids", []).append(run_id)
                    if not confirmed:
                        preview_pending.append(run_id)
                pending_merges = disable_pending_native_auto_merge(op.connection, op.payload.get("disable_auto_merge_prs", []))
                op.payload = {**op.payload, "cancel_run_ids": pending, "stop_preview_run_ids": preview_pending, "disable_auto_merge_prs": pending_merges,
                    "purge_run_ids": op.payload.get("purge_run_ids", preview_ids)}
                op.receipt = {**op.receipt, "tokens": tokens, "preview_cleanup_pending": bool(preview_pending),
                    "remote_cleanup_pending": bool(pending or preview_pending or pending_merges or tokens["pending"])}
                if (op.action == "cancel-operation" and remote["enabled"] and preview_pending
                        and not (pending or pending_merges or tokens["pending"])
                        and not op.payload.get("purge_after_reconciliation")
                        and now - op.created_at >= CANCEL_PREVIEW_STOP_DEADLINE):
                    # The stopped runs are cancelled and their tokens revoked. Only
                    # Content Factory's own preview teardown is unconfirmed, and it
                    # can stay so indefinitely (a missing provider credential), so
                    # stop retrying it hourly. The receipt keeps the run ids.
                    op.state = "completed"
                    op.receipt.update(status="timed_out", unconfirmed_preview_run_ids=preview_pending,
                        message="Stopped. Content Factory did not confirm its preview was removed.")
                if op.action == "purge":
                    from .website_operations import deletion_epoch
                    manifest = {"phase": "apply", "approved": True, "connection_generation": op.payload["previous_generation"],
                        "operation_id": str(op.pk), "operation_attempt": op.payload.get("attempt", 1), "deletion_epoch": op.payload["deletion_epoch"],
                        "run_ids": op.payload.get("purge_run_ids", op.payload.get("stop_preview_run_ids", [])),
                        "domain": op.connection.organization.domain, "github_repo": op.connection.github_repo,
                        "idempotency_key": op.idempotency_key}
                    if remote["enabled"]:
                        manifest["repository_id"] = op.connection.repository_id
                        plan_response = http_client.post(f"{remote['base_url']}/api/connections/{op.connection_id}/worker-cleanup",
                            headers=_content_factory_headers(), json={**manifest, "phase": "plan"}, timeout=(3, 20))
                        plan_response.raise_for_status()
                        plan = plan_response.json()
                        if not plan.get("plan_digest") or plan.get("conflicts"):
                            raise ValueError("Unreviewable worker cleanup scope")
                        manifest["plan_digest"] = plan["plan_digest"]
                        response = http_client.post(f"{remote['base_url']}/api/connections/{op.connection_id}/worker-cleanup",
                            headers=_content_factory_headers(), json=manifest, timeout=(3, 20))
                        response.raise_for_status()
                        cleanup = response.json()
                        op.receipt["artifact_cleanup"] = cleanup
                        worker_pending = cleanup.get("status") != "completed" or bool(cleanup.get("cleanup_pending") or cleanup.get("external_pending"))
                    else:
                        worker_pending = True
                    op.receipt["remote_cleanup_pending"] |= worker_pending
                    op.receipt["deletion_epoch"] = op.payload["deletion_epoch"]
                if op.action == "disconnect":
                    op.receipt["remote_cleanup_pending"] |= bool(op.receipt.get("disconnect_steps_pending"))
                if not op.receipt["remote_cleanup_pending"]:
                    op.state = "completed"
                    op.receipt["status"] = "completed"
        except preview_budget.PreviewCleanupBudgetReached:
            pass
        except Exception:
            # No transport messages can accidentally include credentials or source bodies.
            op.receipt = {**op.receipt, "last_error": "remote_reconciliation_unavailable"}
        preview_budget.completed(op, now=now)
        op.next_attempt_at = None if op.state == "needs_attention" else now + timedelta(seconds=min(3600, 15 * 2 ** min(op.attempts, 8)))
        if op.action == "disconnect" and "remove_setup" in op.payload:
            from .website_disconnect import disconnect_retry_policy
            after_cleanup = evidence_digest({"payload": op.payload, "completed_steps": op.receipt.get("completed_steps", []),
                "refunded_run_ids": op.receipt.get("refunded_run_ids", []), "closed_mutation_ids": op.receipt.get("closed_mutation_ids", []),
                "tokens": op.receipt.get("tokens", {})})
            disconnect_retry_policy(op, now=now, progressed=before_cleanup != after_cleanup)
        applied = preview_budget.persist(op, claimed_at=claimed_at, now=now)
        # Offboarding may redact the payload while transport runs. Never replace
        # that newer retention/erasure receipt with our pre-request snapshot.
        result["pending" if not applied or op.state == "pending" else "completed"] += 1
        if len(result["operations"]) < 20:
            result["operations"].append({"id": str(op.pk), "action": op.action, "outcome": op.state if applied else "lease_changed"})
        if applied and op.state == "completed" and op.payload.get("purge_after_reconciliation"):
            purge_connection_id = op.connection_id
        if purge_connection_id:
            # Follow the same org -> connection lock order as owner lifecycle
            # writes, after releasing the operation lock to avoid inversion.
            from .website_first_article_refunds import finish_purged_connection
            finish_purged_connection(purge_connection_id)
    return result


def monitor_cleanup_pull_requests(*, limit=20, connection_id=None):
    """Observe merge/deploy stages; a removal PR is never completed removal."""
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token
    from .website_connections import require_unlocked_remote_call
    from urllib.parse import urlparse
    require_unlocked_remote_call()
    rows = WebsiteConnectionOperation.objects.filter(action="cleanup", state__in=["awaiting_merge", "awaiting_deployment"])
    if connection_id:
        rows = rows.filter(connection_id=connection_id)
    for op in rows.select_related("connection__organization").order_by("updated_at")[:limit]:
        parsed = urlparse(str(op.receipt.get("pr_url") or ""))
        prefix = f"/{op.connection.github_repo}/pull/"
        number = parsed.path.removeprefix(prefix)
        if parsed.hostname != "github.com" or not parsed.path.startswith(prefix) or not number.isdigit():
            continue
        headers = {}
        try:
            credential = create_installation_access_token(installation_id=op.connection.installation_id,
                repository=op.connection.github_repo, repository_id=op.connection.repository_id, permission_mode="read", use_cache=False)
            headers = {"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}
            response = http_client.get(f"https://api.github.com/repos/{op.connection.github_repo}/pulls/{number}", headers=headers, timeout=(3, 15))
            response.raise_for_status()
            pull = response.json()
            if ((pull.get("base") or {}).get("repo") or {}).get("id") != op.connection.repository_id or (pull.get("base") or {}).get("ref") != op.connection.branch:
                continue
            receipt, state = dict(op.receipt), op.state
            if pull.get("merged"):
                receipt.update(status="deployment_pending", default_branch_modified=True, merge_sha=pull.get("merge_commit_sha"),
                    cleanup_complete=False, deployment_verification_required=True)
                state = "awaiting_deployment"
            elif pull.get("state") == "closed":
                receipt.update(status="pull_request_closed_without_merge", default_branch_modified=False, cleanup_complete=False)
                state = "attention_required"
            WebsiteConnectionOperation.objects.filter(pk=op.pk, state=op.state, updated_at=op.updated_at).update(state=state, receipt=receipt, updated_at=timezone.now())
        except Exception:
            WebsiteConnectionOperation.objects.filter(pk=op.pk, updated_at=op.updated_at).update(
                receipt={**op.receipt, "last_error": "cleanup_merge_status_unavailable"}, updated_at=timezone.now())
        finally:
            if headers:
                try:
                    http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 8))
                except Exception:
                    pass


def approve_cleanup_proposal(config, *, user, data):
    """Open a reviewed, exact-SHA removal PR without restoring ordinary access."""
    from .website_restoration import approve_worker_restoration
    worker = approve_worker_restoration(config, user=user, data=data)
    if worker is not None:
        return worker
    from organizations.models import Organization
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token
    from .website_connections import verify_repository_access
    from .website_contract import WebsiteAuthorityError, connection_contract, evidence_digest
    binding = connection_contract(data)
    metadata = verify_repository_access(user=user, repo=config.github_repo)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=config.organization_id)
        config.refresh_from_db()
        website = WebsiteConnection.objects.select_for_update().get(pk=config.website_connection_id)
        if not binding or binding['website_connection_id'] != str(website.pk) or binding['connection_generation'] != website.generation:
            raise WebsiteAuthorityError('website_connection_changed', 'Refresh the cleanup proposal for the current connection.')
        if metadata['repository_id'] != website.repository_id:
            raise WebsiteAuthorityError('website_repository_changed', 'Repository identity changed.')
        from uuid import UUID
        try:
            operation_id = UUID(str(data.get('operation_id')))
        except (ValueError, TypeError):
            raise WebsiteAuthorityError('cleanup_proposal_required', 'Select a valid cleanup proposal.')
        op = WebsiteConnectionOperation.objects.select_for_update().filter(pk=operation_id, connection=website, action='cleanup', generation=website.generation).first()
        if op is None:
            raise WebsiteAuthorityError('cleanup_proposal_required', 'Prepare a current cleanup proposal first.')
        if op.state in {'awaiting_merge', 'awaiting_deployment', 'completed'} and op.receipt.get('pr_url'):
            return op
        if op.state not in {'review_required', 'applying'} or data.get('source_sha') != op.receipt.get('source_sha') or data.get('proposal_digest') != op.receipt.get('proposal_digest'):
            raise WebsiteAuthorityError('cleanup_proposal_changed', 'Review the current cleanup proposal before creating a pull request.')
        if op.state == 'applying' and op.next_attempt_at and op.next_attempt_at > timezone.now():
            raise WebsiteAuthorityError('cleanup_in_progress', 'Cleanup is already being reconciled.', status=409, retryable=True)
        op.state = 'applying'
        op.attempts += 1
        claim_attempt = op.attempts
        op.next_attempt_at = timezone.now() + timedelta(minutes=5)
        op.payload = {**op.payload, 'approved_cleanup': {
            'source_sha': op.receipt['source_sha'], 'proposal_digest': op.receipt['proposal_digest'],
            'deletions': list(op.receipt.get('deletions') or []), 'approved_by_user_id': str(user.pk)}}
        op.save(update_fields=['state', 'attempts', 'next_attempt_at', 'payload', 'updated_at'])
    # All GitHub reads/writes below happen after the approval transaction exits.
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    fresh = _cleanup_proposal(op)
    if fresh['proposal_digest'] != op.receipt['proposal_digest']:
        op.receipt = fresh
        op.state = 'review_required'
        op.save(update_fields=['state', 'receipt', 'updated_at'])
        raise WebsiteAuthorityError('cleanup_source_changed', 'Website files changed. Prepare and review a fresh cleanup proposal.')
    deletions = fresh['deletions']
    if not deletions:
        raise WebsiteAuthorityError('cleanup_no_owned_files', 'No unchanged, exclusively owned files can be removed automatically.')
    token = create_installation_access_token(installation_id=metadata['installation_id'], repository=website.github_repo, repository_id=website.repository_id,
        permission_mode='write', permission_profile='workflow_files' if '.github/workflows/mlai-articles-verification.yml' in deletions else 'repository', use_cache=False)
    headers = {'Authorization': f'Bearer {token.token}', 'Accept': 'application/vnd.github+json'}
    base = f'https://api.github.com/repos/{website.github_repo}'
    def request(method, path, body=None):
        response = http_client.request(method, base + path, headers=headers, json=body, timeout=(3, 20))
        response.raise_for_status()
        return response.json()
    branch = f'codex/website-cleanup-{op.pk.hex}'
    try:
        base_commit = request('GET', f"/git/commits/{fresh['source_sha']}")
        tree = request('POST', '/git/trees', {'base_tree': base_commit['tree']['sha'],
            'tree': [{'path': path, 'mode': '100644', 'type': 'blob', 'sha': None} for path in deletions]})
        existing = http_client.get(base + f'/git/ref/heads/{branch}', headers=headers, timeout=(3, 15))
        if existing.status_code == 404:
            commit = request('POST', '/git/commits', {'message': 'Remove reviewed MLAI integration files',
                'tree': tree['sha'], 'parents': [fresh['source_sha']]})
            request('POST', '/git/refs', {'ref': 'refs/heads/' + branch, 'sha': commit['sha']})
        else:
            existing.raise_for_status()
            commit = request('GET', f"/git/commits/{existing.json()['object']['sha']}")
            if commit['tree']['sha'] != tree['sha']:
                raise WebsiteAuthorityError('cleanup_branch_changed', 'The cleanup branch changed. Review it before continuing.')
        existing_prs = http_client.get(base + '/pulls', params={'head': website.github_repo.split('/')[0] + ':' + branch, 'state': 'all'}, headers=headers, timeout=(3, 15))
        existing_prs.raise_for_status()
        prs = existing_prs.json()
        skipped = [f"- `{row['path']}`: {row['reason'].replace('_', ' ')}" for row in fresh.get('conflicts', [])]
        retained = [f"- `{path}`" for path in fresh.get('retained', [])]
        body = 'Removes only unchanged files recorded as exclusively created by MLAI. The default branch is changed only if you review and merge this pull request.'
        if skipped:
            body += '\n\nSkipped customer-edited or shared files:\n' + '\n'.join(skipped)
        if retained:
            body += '\n\nRetained article content and dependencies:\n' + '\n'.join(retained)
        pr = prs[0] if prs else request('POST', '/pulls', {'title': 'Remove reviewed MLAI website integration files',
            'head': branch, 'base': website.branch,
            'body': body})
        op.state = 'awaiting_merge'
        op.receipt = {**fresh, 'status': 'pull_request_opened', 'pr_url': pr['html_url'], 'branch': branch,
            'head_sha': commit['sha'], 'repository_modified': True, 'default_branch_modified': False, 'approved_by_user_id': str(user.pk)}
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=config.organization_id)
            current = WebsiteConnection.objects.select_for_update().get(pk=website.pk)
            saved = WebsiteConnectionOperation.objects.select_for_update().get(pk=op.pk)
            if current.generation != op.generation or saved.state != 'applying' or saved.attempts != claim_attempt:
                op.state = 'attention_required'
                op.receipt['status'] = 'authority_changed_after_pr_creation'
            saved.state, saved.receipt = op.state, op.receipt
            saved.save(update_fields=['state', 'receipt', 'updated_at'])
            return saved
    except WebsiteAuthorityError:
        raise
    except Exception as exc:
        raise WebsiteAuthorityError('cleanup_pull_request_unavailable', 'Cleanup pull request could not be confirmed. Retry the same proposal to reconcile its branch.') from exc
    finally:
        try:
            http_client.delete('https://api.github.com/installation/token', headers=headers, timeout=(3, 10))
        except Exception:
            pass


# A rescan reserved in one of these states can never replay under its key.
SETTLED_SOURCE_RESCAN_STATES = frozenset({"failed", "cancelled", "denied", "deleted"})


def _source_rescan_identity(op):
    """Return this source check's rescan dispatch count and request identity."""
    count = op.payload.get("rescan_dispatch", 0)
    count = count if type(count) is int and count >= 0 else 0
    return count, f"source-rescan:{op.pk}:{count}" if count else f"source-rescan:{op.pk}"


def _source_rescan_finished(op, website):
    """Whether the rescan this check dispatched can no longer record targets."""
    from .website_operations import TERMINAL_WORKFLOW_STATES
    _, request_id = _source_rescan_identity(op)
    scan = WebsiteConnectionOperation.objects.filter(
        idempotency_key=f"{website.pk}:workflow:{request_id}").first()
    return scan is not None and scan.state in TERMINAL_WORKFLOW_STATES | SETTLED_SOURCE_RESCAN_STATES


def _source_rescan_request(op, website, attempt):
    """Name this source's rescan dispatch, moving past a settled failed one.

    The first dispatch keeps ``source-rescan:<op>`` so an in-flight, completed
    or running rescan still replays idempotently. Once its reserved operation
    failed or was cancelled, that key is spent; retrying it only conflicts
    forever. Count up instead and persist the count under this claim.
    Returns the request identity and its existing reserved operation, if any.
    """
    count, request_id = _source_rescan_identity(op)
    prior = WebsiteConnectionOperation.objects.filter(
        idempotency_key=f"{website.pk}:workflow:{request_id}").first()
    if prior is None or prior.state not in SETTLED_SOURCE_RESCAN_STATES:
        return request_id, prior
    from .website_contract import WebsiteAuthorityError
    updated = {**op.payload, "rescan_dispatch": count + 1}
    if WebsiteConnectionOperation.objects.filter(pk=op.pk, attempts=attempt, state="pending").update(
            payload=updated, updated_at=timezone.now()) != 1:
        raise WebsiteAuthorityError("website_operation_cancelled", "The source verification operation changed.")
    op.payload = updated
    return f"source-rescan:{op.pk}:{count + 1}", None


def _process_source_reverification(identifier, now):
    """Re-scan external changes and then authenticate current CI/live proof."""
    from .website_connections import authority_guard, require_unlocked_remote_call
    from .website_contract import WebsiteAuthorityError
    from .website_verification import discover_source_attestation, record_ci_attestation, verify_live_deployment
    from .vibe_marketing_views import (_content_factory_remote_config, _content_factory_headers, _create_local_run,
        _require_roo_points_for_ai_agent, _mark_roo_points_gate_authorized, founder_actor_id_for_user)
    from .contract import CONTENT_FACTORY_REQUEST_SOURCE
    from .website_operations import reserve_workflow_operation, bind_operation_run
    from integrations import http_client
    with transaction.atomic():
        op = WebsiteConnectionOperation.objects.select_for_update().select_related("connection__organization").filter(pk=identifier, state="pending").first()
        if op is None or op.next_attempt_at and op.next_attempt_at > now:
            return None
        op.attempts += 1
        attempt = op.attempts
        op.next_attempt_at = now + timedelta(minutes=5)
        op.save(update_fields=["attempts", "next_attempt_at", "updated_at"])
    if op.payload.get("site_recipe_reconnect") is True and now - op.created_at >= timedelta(hours=24):
        receipt = {**op.receipt, "status": "needs_attention", "code": "ci_attestation_required", "retryable": False,
            "message": "Reconnect verification did not complete within its verification window."}
        WebsiteConnectionOperation.objects.filter(pk=identifier, attempts=attempt, state="pending").update(
            state="needs_attention", receipt=receipt, next_attempt_at=None, updated_at=timezone.now())
        return "needs_attention"
    binding = {**contract_for(op.connection), "domain": op.connection.organization.domain,
        "connection_generation": getattr(op, "generation", op.connection.generation),
        "github_repo": op.connection.github_repo, "expected_source_sha": op.payload["source_sha"]}
    state, receipt = "pending", dict(op.receipt)
    ci_receipt = None
    recipe_reconnect = op.payload.get("site_recipe_reconnect") is True
    try:
        with authority_guard(binding, action="read") as website:
            pinned = op.payload.get("target_id")
            target = website.targets.filter(generation=website.generation, target_key=pinned).first()
            if target is None and receipt.get("scan_run_id"):
                # The push pinned the default it saw. That id can name a target this
                # generation never recorded, or one no scan re-derives, so waiting
                # for it never succeeds. After the rescan, verify the target selected now.
                config = OrganizationContentConfig.objects.get(website_connection=website)
                if config.default_publish_target_id and config.default_publish_target_id != pinned:
                    target = website.targets.filter(generation=website.generation,
                        target_key=config.default_publish_target_id).first()
        # First-time scaffold merges have no publishing target yet. Their owned
        # merge receipt proves provenance, but cannot replace the inventory scan
        # that discovers the newly committed integration.
        scan_required = bool(op.payload.get("scan_required") or target is None)
        if recipe_reconnect:
            from .website_site_recipe_reconnect import enabled as recipe_reconnect_enabled
            from .website_verification import current_ci_attestation
            from .website_hosted_verification import advance as advance_hosted
            from .website_site_recipe import dispatch_selection
            if (not recipe_reconnect_enabled(website) or target is None
                    or dispatch_selection(website, target, op.payload["source_sha"]) is None
                    or target.contract.get("contract_digest") != op.payload.get("contract_digest")
                    or target.contract.get("site_contract_digest") != op.payload.get("site_contract_digest")):
                raise WebsiteAuthorityError("verified_target_required", "The enrolled reconnect recipe is unavailable.")
            ci_receipt = current_ci_attestation(website, target)
            # New-generation inventory must run after a fresh certificate exists:
            # the existing certified-config guard then preserves this selection.
            scan_required = bool(op.payload.get("scan_required") and ci_receipt is not None)
        if scan_required and target is not None and not receipt.get("scan_run_id") and not recipe_reconnect:
            owned_merge = _find_owned_merge(website, website.organization_id, op.payload["source_sha"])
            if owned_merge:
                with authority_guard(binding, action="read") as current:
                    if owned_merge["connection_identity"] != _merge_connection_identity(current):
                        raise WebsiteAuthorityError("website_connection_changed", "Website authority changed during merge verification.", retryable=True)
                    updated_payload = {**op.payload, "owned_merge_run_id": owned_merge["run_id"], "scan_required": False}
                    changed = WebsiteConnectionOperation.objects.filter(pk=identifier, attempts=attempt,
                        state="pending", generation=current.generation).update(payload=updated_payload, updated_at=timezone.now())
                    if changed != 1:
                        raise WebsiteAuthorityError("website_operation_cancelled", "The source verification operation changed.")
                    op.payload = updated_payload
                    scan_required = False
                    record_owner_setup_merge(current, owned_merge)
        if scan_required and not receipt.get("scan_run_id"):
            require_unlocked_remote_call()
            remote = _content_factory_remote_config()
            if not remote["enabled"]:
                raise WebsiteAuthorityError("source_reverification_unavailable", "Repository scanning is unavailable.", retryable=True)
            owner = website.authorized_by
            if owner is None:
                raise WebsiteAuthorityError("website_owner_review_required", "The website owner must verify the current integration.", retryable=True)
            points_error, balance = _require_roo_points_for_ai_agent(owner,
                domain=website.organization.domain, action="repo_scan")
            if points_error is not None:
                raise WebsiteAuthorityError("roo_points_required", "The website owner needs sufficient Roo points to verify this integration.", retryable=True)
            request_id, prior = _source_rescan_request(op, website, attempt)
            scan_payload = {**binding, "client_request_id": request_id, "force_refresh": True,
                "request_source": CONTENT_FACTORY_REQUEST_SOURCE, "slack_user_id": founder_actor_id_for_user(owner),
                "scan_purpose": "setup", "scaffold_if_missing": False,
                "auto_setup_preview": False, "generate_components": recipe_reconnect}
            _mark_roo_points_gate_authorized(scan_payload, domain=website.organization.domain,
                action="repo_scan", current_balance=balance)
            try:
                scan_op = reserve_workflow_operation(website, workflow="repo_scan", payload=scan_payload)
            except WebsiteAuthorityError as exc:
                if exc.authority_code == "operation_key_conflict" and prior is not None and prior.state in {"running", "pending"}:
                    # Different, still-active work owns this key. It completes,
                    # or settles and the next attempt takes a fresh identity.
                    raise WebsiteAuthorityError("source_reverification_unavailable",
                        "Waiting for the earlier repository scan to settle.", retryable=True) from exc
                raise
            response = http_client.post(f"{remote['base_url']}/api/runs/scan", json=scan_payload,
                headers=_content_factory_headers(), timeout=(3, 30))
            response.raise_for_status()
            remote_run = response.json()
            if not remote_run.get("run_id"):
                raise WebsiteAuthorityError("source_reverification_unavailable", "The scan dispatch could not be confirmed.", retryable=True)
            run = _create_local_run(workflow="repo_scan", domain=website.organization.domain,
                github_repo=website.github_repo, payload=scan_payload, remote_data=remote_run)
            bind_operation_run(scan_op, run)
            receipt["scan_run_id"] = run.run_id
        if target is None and op.receipt.get("scan_run_id") and _source_rescan_finished(op, website):
            # An earlier attempt's rescan finished without recording the pinned
            # or the selected target, so no row this check could verify will
            # appear. Only a setup registers and verifies one, so it owns this
            # source now.
            state = "cancelled"
            receipt.update(status="superseded", code="verified_target_required", retryable=False,
                message="No publishing target exists at this source. Setting up articles verifies it.")
        elif target is None:
            raise WebsiteAuthorityError("verified_target_required", "Waiting for a current verified publishing target.", retryable=True)
        else:
            ci_id = None
            if recipe_reconnect:
                config = OrganizationContentConfig.objects.get(website_connection=website)
                if ci_receipt is None:
                    outcome = advance_hosted(op, SimpleNamespace(organization=website.organization), config, target, op.payload["source_sha"])
                    receipt.update(op.receipt)
                    if outcome["status"] == "completed":
                        website.refresh_from_db()
                        target.refresh_from_db()
                        ci_receipt = current_ci_attestation(website, target)
                        ci_id = outcome["operation_id"]
                        if ci_receipt is None:
                            raise WebsiteAuthorityError("ci_attestation_required", "Waiting for saved reconnect build proof.", retryable=True)
                if ci_receipt is not None and ci_id is None:
                    ci_id = receipt.get("ci_operation_id")
                if ci_id:
                    receipt["ci_operation_id"] = ci_id
            else:
                proof = discover_source_attestation(website, target, op.payload["source_sha"])
                ci = record_ci_attestation(proof)
                ci_receipt, ci_id = ci.receipt, str(ci.pk)
                config = OrganizationContentConfig.objects.get(website_connection=website)
            if ci_receipt is not None:
                inventory_ready = True
                if recipe_reconnect:
                    if not receipt.get("scan_run_id"):
                        updated = {**op.payload, "scan_required": True}
                        if WebsiteConnectionOperation.objects.filter(pk=identifier, attempts=attempt,
                                state="pending", generation=website.generation).update(payload=updated, updated_at=timezone.now()) != 1:
                            raise WebsiteAuthorityError("website_operation_cancelled", "Reconnect changed while its scan was reserved.")
                        op.payload = updated
                        inventory_ready = False
                    else:
                        from .website_site_recipe_reconnect import scan_complete
                        with authority_guard(binding, action="read") as current:
                            inventory_ready = scan_complete(current, config, op, receipt)
                    if not inventory_ready:
                        receipt.update(status="inventory_pending", message="Refreshing article templates and component inventory at the verified source.")
                if inventory_ready:
                    deployment = verify_live_deployment(config, data=ci_receipt)
                    state, receipt = "completed", {**receipt, "status": "verified", "source_sha": op.payload["source_sha"],
                        "target_id": target.target_key, "ci_operation_id": ci_id, "deployment_operation_id": str(deployment.pk),
                        "repository_modified": False}
    except WebsiteAuthorityError as exc:
        receipt.update(status="verification_pending", code=exc.code, retryable=exc.retryable)
        if exc.code in {"website_disconnected", "website_connection_changed", "website_operation_cancelled"}:
            state = "cancelled"
        elif exc.authority_code == "website_source_changed":
            # GitHub's branch has moved past this source, and that newer push
            # queued its own verification. Retrying this one can never succeed.
            state = "cancelled"
            receipt.update(status="superseded", retryable=False)
        elif exc.authority_code == "operation_key_conflict":
            # A settled rescan already moved to a fresh identity, so this key
            # belongs to finished or different work: retrying cannot repair it.
            # Stop; the source blocker stays until a new scan re-verifies.
            state = "needs_attention"
            receipt.update(status="needs_attention", code="operation_key_conflict", retryable=False,
                message="The repository scan for this change could not be restarted. Scan the repository again.")
        elif op.payload.get("site_recipe_reconnect") is True:
            from .website_prepare import DEPLOYMENT_WAIT_CODES
            if ci_receipt is not None and exc.authority_code in DEPLOYMENT_WAIT_CODES:
                receipt.update(retryable=True)
            elif not exc.retryable:
                state = "needs_attention"
                receipt.update(status="needs_attention", retryable=False)
    except Exception:
        receipt.update(status="verification_pending", code="source_reverification_unavailable", retryable=True)
    delay = timedelta(seconds=min(3600, 15 * 2 ** min(attempt, 8)))
    if recipe_reconnect and receipt.get("status") == "inventory_pending":
        # Scan callbacks update local records; this wait performs no repeated
        # model dispatch, provider build or deployment request.
        delay = timedelta(seconds=30)
    elif recipe_reconnect and ci_receipt is not None:
        from .website_prepare import DEPLOYMENT_RETRY_MINUTES
        delay = timedelta(minutes=DEPLOYMENT_RETRY_MINUTES[min(attempt - 1, len(DEPLOYMENT_RETRY_MINUTES) - 1)])
    WebsiteConnectionOperation.objects.filter(pk=identifier, attempts=attempt, state="pending").update(
        state=state, receipt=receipt, next_attempt_at=now + delay if state == "pending" else None, updated_at=timezone.now())
    return state
