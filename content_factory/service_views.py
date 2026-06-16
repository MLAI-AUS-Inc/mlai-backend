import logging
import os
import time
from datetime import date as calendar_date, datetime, timezone as datetime_timezone
from typing import Any, Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import OperationalError, connection, transaction
from django.db.models import Q
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from content_factory.article_system import (
    article_system_ready,
    best_registry_driven_publish_target,
    merge_article_system,
    normalize_article_system,
    registry_target_publish_ready,
    resolve_article_system,
)
from content_factory.auth import content_factory_github_connection_state
from content_factory.delivery import (
    build_content_factory_preview_url,
    build_content_ready_blocks,
    build_draft_pr_created_blocks,
    build_progress_update_blocks,
    build_preview_ready_blocks,
    build_content_thread_messages,
    render_content_preview_error_page,
    render_content_preview_page,
    validate_content_factory_preview_signature,
)
from content_factory.article_publish_status import advance_publish_status
from content_factory.models import (
    ArticlePublishStatus,
    ComponentMapping,
    ContentFactoryHealingRecord,
    GeneratedComponent,
    OrganizationContentConfig,
    WebsiteBaselineSnapshot,
)
from content_factory.progress import (
    live_card_summary_for_job,
    maybe_send_still_working_ping,
    upsert_live_progress_card,
)
from content_factory.serializers import (
    ContentFactoryHealingRecordSerializer,
    GeneratedComponentListSerializer,
    GeneratedComponentSerializer,
)
from core.permissions import HasRooApiKey
from integrations.services.github_connections import get_owned_org_configs
from organizations.models import Organization
from workflow_runs.models import (
    ContentFactoryApprovalState,
    ContentFactoryRun,
    ContentFactoryRunStatus,
    ContentFactoryRunStep,
    ContentFactoryRunStepAttempt,
    ContentFactoryStepStatus,
)
from workflow_runs.serializers import (
    ContentFactoryRunControlSerializer,
    ContentFactoryRunSyncSerializer,
    ContentFactoryRunValleyJobSerializer,
)
from workflow_runs.sanitization import sanitize_json_for_postgres

logger = logging.getLogger(__name__)
User = get_user_model()
VALLEY_META_KEY = "_valley_meta"


def _normalize_discovery_diagnostics(value):
    if not isinstance(value, dict):
        return {}

    diagnostics = {}
    for key, raw_value in value.items():
        try:
            diagnostics[str(key)] = int(raw_value)
        except (TypeError, ValueError):
            continue
    return diagnostics


def _format_discovery_diagnostics(diagnostics):
    normalized = _normalize_discovery_diagnostics(diagnostics)
    if not normalized:
        return ""

    seed_count = normalized.get("seed_count", 0)
    competitor_count = normalized.get("competitor_count", 0)
    seed_results = (
        normalized.get("keyword_ideas_count", 0)
        + normalized.get("keyword_suggestions_count", 0)
        + normalized.get("related_keywords_count", 0)
        + normalized.get("ai_question_count", 0)
    )
    competitor_candidates = normalized.get("competitor_candidate_count", 0)
    relevance_rejections = (
        normalized.get("keyword_relevance_rejected_count", 0)
        + normalized.get("competitor_relevance_rejected_count", 0)
    )
    already_used = (
        normalized.get("written_exclusion_count", 0)
        + normalized.get("already_used_exclusion_count", 0)
        + normalized.get("semantic_dedup_exclusion_count", 0)
    )
    remaining = normalized.get("remaining_opportunity_count", normalized.get("deduplicated_count", 0))

    lines = []
    if seed_count or competitor_count:
        lines.append(
            f"Checked {seed_count} seed keywords and {competitor_count} competitors."
        )
    if seed_results or competitor_candidates:
        lines.append(
            f"Found {seed_results} seed-derived candidates and {competitor_candidates} competitor candidates."
        )
    if relevance_rejections:
        lines.append(f"Filtered out {relevance_rejections} candidates as irrelevant.")
    if already_used:
        lines.append(f"Excluded {already_used} candidates that were already used or too close to existing topics.")
    lines.append(f"{remaining} viable topics remained.")
    return "\n".join(lines)


def _scan_destination_summary(article_system, publish_targets):
    resolved = normalize_article_system(article_system)
    location = resolved.get("directory_path") or resolved.get("directory_name") or "your content directory"
    targets = [item for item in (publish_targets or []) if isinstance(item, dict)]

    registry_target = best_registry_driven_publish_target(targets, resolved)
    if registry_target:
        strategy = registry_target.get("registration_strategy") if isinstance(registry_target.get("registration_strategy"), dict) else {}
        registry_path = (
            strategy.get("registry_path")
            or registry_target.get("registry_path")
            or registry_target.get("content_source")
            or location
        )
        readiness = registry_target.get("readiness") if isinstance(registry_target.get("readiness"), dict) else {}
        if registry_target_publish_ready(registry_target):
            return (
                f"I found a registry-driven SEO system at `{registry_path}`.\n\n"
                f"Roo can publish new SEO pages by adding typed registry entries through the existing route, metadata, sitemap, and schema structure."
            )

        issues = registry_target.get("diagnostics") or strategy.get("diagnostics") or readiness.get("diagnostics") or {}
        if isinstance(issues, dict):
            issue_items = issues.get("issues") or issues.get("blocking_issues") or []
        else:
            issue_items = []
        issue_text = ""
        if issue_items:
            issue_text = "\n\nCurrent blockers: " + "; ".join(str(item).strip() for item in issue_items[:3] if str(item).strip())
        return (
            f"I found a registry-driven SEO system at `{registry_path}`, but it is not safe to patch automatically yet.{issue_text}\n\n"
            f"Roo can draft content now, or direct publish can be enabled with a resolved registry target or `.content-factory/target.yml`."
        )

    if any(
        str(item.get("kind") or "").strip() == "bundle_only_article_directory"
        or str(item.get("publish_capability") or "").strip() == "bundle_only"
        for item in targets
    ):
        return (
            f"I found a content directory at `{location}`.\n\n"
            f"Roo can draft content for it now, and direct publish can be added later with a supported target or `.content-factory/target.yml`."
        )

    if any(str(item.get("kind") or "").strip() == "hook_publish" for item in targets):
        return (
            f"I found a configured content target at `{location}`.\n\n"
            f"This repo can publish through the existing Content Factory hook configuration."
        )

    if article_system_ready(resolved):
        return (
            f"I found a ready article system at "
            f"`{location}`."
        )

    return ""


def _normalize_content_factory_domain(domain: str) -> str:
    if not domain:
        return ""
    domain = str(domain).lower().strip()
    if domain.startswith('https://'):
        domain = domain[8:]
    elif domain.startswith('http://'):
        domain = domain[7:]
    if domain.startswith('www.'):
        domain = domain[4:]
    if '/' in domain:
        domain = domain.split('/')[0]
    return domain


def _content_factory_github_connection_state(config) -> str:
    return content_factory_github_connection_state(config)


def _content_factory_github_auth_url(*, slack_user_id: str, domain: Optional[str] = None) -> str:
    from integrations.services.github import build_github_auth_url

    normalized_domain = _normalize_content_factory_domain(domain or "")
    return build_github_auth_url(slack_user_id or "", domain=normalized_domain or None)


class ContentFactoryOrgConfigView(APIView):
    """
    GET/PUT org config for Content Factory service.
    Used by external Content Factory service to read/write organization templates.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def _normalize_domain(self, domain: str) -> str:
        """
        Strip www., https://, http://, and trailing paths from domain.
        Examples:
            https://www.mlai.au/about → mlai.au
            http://mlai.au → mlai.au
            www.mlai.au → mlai.au
        """
        if not domain:
            return domain
        
        # Remove protocol
        domain = domain.lower().strip()
        if domain.startswith('https://'):
            domain = domain[8:]
        elif domain.startswith('http://'):
            domain = domain[7:]
        
        # Remove www.
        if domain.startswith('www.'):
            domain = domain[4:]
        
        # Remove trailing path (everything after first /)
        if '/' in domain:
            domain = domain.split('/')[0]
        
        return domain

    def get(self, request):
        """
        Lookup org config by domain, github_repo, or slack_user_id query param.
        Returns 404 if organization not found.
        """
        domain = request.query_params.get('domain')
        github_repo = request.query_params.get('github_repo')
        slack_user_id = request.query_params.get('slack_user_id')
        
        org = None

        # 0. Try lookup via explicit owned-domain mapping when only slack_user_id is provided
        if slack_user_id and not github_repo and not domain:
            try:
                owned_configs = list(get_owned_org_configs(slack_user_id))
                if len(owned_configs) == 1:
                    org = owned_configs[0].organization
                elif len(owned_configs) > 1:
                    return Response(
                        {
                            'error': 'Multiple domains found for this Slack user. Please provide a domain.',
                            'requires_domain_selection': True,
                            'connected_domains': [
                                {
                                    'domain': cfg.organization.domain,
                                    'github_repo': cfg.github_repo,
                                }
                                for cfg in owned_configs
                                if cfg.organization
                            ],
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            except Exception as e:
                logger.warning(f"Error looking up owned domains for slack_user_id {slack_user_id}: {e}")

        # 1. Try lookup by github_repo if provided
        if github_repo and not org:
            try:
                # Find the config that matches this repo
                config_qs = OrganizationContentConfig.objects.filter(github_repo=github_repo)
                if slack_user_id:
                    config_qs = config_qs.filter(
                        Q(connected_slack_user_id=slack_user_id)
                        | Q(connected_slack_user_id__isnull=True)
                    )
                config = config_qs.first()
                if config:
                    org = config.organization
            except Exception as e:
                logger.warning(f"Error looking up org by repo {github_repo}: {e}")

        # 2. Try lookup by domain if no org found yet
        if not org and domain:
            normalized_domain = self._normalize_domain(domain)
            try:
                org = Organization.objects.get(domain=normalized_domain)
            except Organization.DoesNotExist:
                pass

        if not org:
            return Response(
                {'error': 'Organization not found. Please provide valid domain or github_repo.'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get config if exists (might have already fetched it, but get fresh ref)
        config = getattr(org, 'content_config', None)
        
        response_data = {
            'org_id': org.id,
            'org_name': org.name,
            'domain': org.domain,
            'competitors': org.competitors,
            'seed_keywords': org.seed_keywords,
            'connected_slack_user_id': config.connected_slack_user_id if config else None,
            'default_timezone': config.default_timezone if config else "",
            'daily_discovery_enabled': config.daily_discovery_enabled if config else False,
            'daily_discovery_priority': config.daily_discovery_priority if config else 0,
            'article_template': config.article_template if config else None,
            'design_guide': config.design_guide if config else None,
            'resource_prompt': config.resource_prompt if config else None,
            'company_context': config.company_context if config else None,
            'github_repo': config.github_repo if config else None,
            'article_delivery_mode': config.article_delivery_mode if config else None,
            'brand_name': config.brand_name if config else None,
            'scan_summary': config.scan_summary if config else None,
            'tech_stack': config.tech_stack if config else {},
            'installed_packages': config.installed_packages if config else {},
            'pillar_strategy': config.pillar_strategy if config else {},
            'build_healing_hints': config.build_healing_hints if config else [],
            'repo_execution_contract': config.repo_execution_contract if config else {},
            'article_path_pattern': config.article_path_pattern if config else None,
            'registry_path': config.registry_path if config else None,
            'publish_targets': config.publish_targets if config else [],
            'default_publish_target_id': config.default_publish_target_id if config else None,
            'article_system': resolve_article_system(config),
            # Component-reuse round-trip: feed content-factory's _hydrate_existing_artifacts so
            # the scanner's SHA short-circuit (_maybe_reuse_unchanged_scan) and component reuse
            # decision (_build_article_artifact_reuse_decision) can skip regenerating article
            # components. Lightweight by design: the setup cache carries the inventory
            # (names/paths/fingerprint), not the full component code (which lives in the repo).
            'repo_head_sha': (config.last_scanned_sha if config else None),
            'commit_sha': (config.last_scanned_sha if config else None),
            'scan_completed_at': (
                config.last_scanned_at.isoformat() if config and config.last_scanned_at else None
            ),
            'scan_request_fingerprint': (config.scan_request_fingerprint if config else ''),
            'article_system_setup_cache': (config.article_system_setup_cache if config else {}),
            'framework_component_specs': (config.framework_component_specs if config else {}),
        }

        return Response(response_data, status=status.HTTP_200_OK)

    def put(self, request):
        """
        Create org if not exists, then upsert config.
        Supports partial updates (only fields present in request are updated).
        Also handles component generation data:
        - generated_components: array of component objects to upsert
        - component_generation: summary of generation pipeline result
        - component_mapping: dict of component name -> match result
        """
        data = sanitize_json_for_postgres(request.data if isinstance(request.data, dict) else dict(request.data))
        domain = data.get('domain')
        name = data.get('name')
        
        if not domain:
            return Response(
                {'error': 'domain is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        normalized_domain = self._normalize_domain(domain)
        
        # Get or create organization
        org, org_created = Organization.objects.get_or_create(
            domain=normalized_domain,
            defaults={'name': name or normalized_domain}
        )
        
        # Update org fields if provided
        org_updated = False
        competitors = data.get('competitors')
        seed_keywords = data.get('seed_keywords')

        if not org_created and name and org.name != name:
            org.name = name
            org_updated = True

        if competitors is not None:
            org.competitors = competitors
            org_updated = True

        if seed_keywords is not None:
            org.seed_keywords = seed_keywords
            org_updated = True

        if org_updated:
            org.save()

        existing_config = getattr(org, 'content_config', None)
        current_enabled = bool(getattr(existing_config, 'daily_discovery_enabled', False))
        current_priority = int(getattr(existing_config, 'daily_discovery_priority', 0) or 0)
        current_owner = str(getattr(existing_config, 'connected_slack_user_id', '') or '').strip()
        current_github_repo = str(getattr(existing_config, 'github_repo', '') or '').strip()

        if 'daily_discovery_priority' in data:
            try:
                resulting_priority = int(data.get('daily_discovery_priority'))
            except (TypeError, ValueError):
                return Response(
                    {'error': 'daily_discovery_priority must be an integer'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if resulting_priority < 0:
                return Response(
                    {'error': 'daily_discovery_priority must be 0 or greater'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            resulting_priority = current_priority

        resulting_enabled = bool(data.get('daily_discovery_enabled')) if 'daily_discovery_enabled' in data else current_enabled
        raw_owner = data.get('connected_slack_user_id') if 'connected_slack_user_id' in data else current_owner
        resulting_owner = str(raw_owner or '').strip()
        resulting_github_repo = (
            str(data.get('github_repo') or '').strip()
            if 'github_repo' in data
            else current_github_repo
        )

        from integrations.services.daily_discovery import (
            count_enabled_daily_discovery_configs,
            get_daily_discovery_max_targets,
            infer_daily_discovery_owner,
        )

        inferred_owner = infer_daily_discovery_owner(
            domain=normalized_domain,
            connected_slack_user_id=resulting_owner,
            github_repo=resulting_github_repo,
            config=existing_config,
        )

        if resulting_enabled and not inferred_owner:
            return Response(
                {'error': 'connected_slack_user_id is required when daily_discovery_enabled is true'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if resulting_enabled and not current_enabled:
            enabled_count = count_enabled_daily_discovery_configs(
                exclude_config_id=getattr(existing_config, 'id', None),
            )
            max_targets = get_daily_discovery_max_targets()
            if enabled_count >= max_targets:
                return Response(
                    {'error': f'No more than {max_targets} organizations may have daily_discovery_enabled=true'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Prepare defaults dynamically to allow partial updates
        # Only include fields that are present in the request data
        defaults = {}
        target_fields = [
            'connected_slack_user_id',
            'default_timezone',
            'daily_discovery_enabled',
            'daily_discovery_priority',
            'article_template',
            'design_guide',
            'resource_prompt',
            'company_context',
            'github_repo',
            'article_delivery_mode',
            'brand_name',
            'scan_summary',
            'tech_stack',
            'installed_packages',
            'pillar_strategy',
            'build_healing_hints',
            'repo_execution_contract',
            'article_path_pattern',
            'registry_path',
            'publish_targets',
            'default_publish_target_id',
            # Component-reuse round-trip: persist so an unchanged re-scan can short-circuit
            # and _build_article_artifact_reuse_decision can reuse components instead of
            # regenerating all ~29 on every scan.
            'scan_request_fingerprint',
            'article_system_setup_cache',
            'framework_component_specs',
        ]

        for field in target_fields:
            if field in data:
                defaults[field] = data[field]

        scan_head_sha = str(data.get('repo_head_sha') or data.get('commit_sha') or '').strip()
        scan_timestamp = timezone.now()
        if scan_head_sha:
            defaults['last_scanned_sha'] = scan_head_sha
            defaults['last_scanned_at'] = scan_timestamp

        if 'connected_slack_user_id' in defaults:
            defaults['connected_slack_user_id'] = resulting_owner or None
        if resulting_enabled and inferred_owner:
            defaults['connected_slack_user_id'] = inferred_owner
        if 'daily_discovery_priority' in data:
            defaults['daily_discovery_priority'] = resulting_priority

        if 'article_system' in data:
            current_article_system = resolve_article_system(getattr(org, 'content_config', None))
            defaults['article_system'] = merge_article_system(current_article_system, data.get('article_system'))
            if scan_head_sha:
                scan_default_branch = str(data.get('default_branch') or data.get('defaultBranch') or '').strip()
                scan_state = {
                    'githubRepo': str(data.get('github_repo') or resulting_github_repo or '').strip(),
                    'github_repo': str(data.get('github_repo') or resulting_github_repo or '').strip(),
                    'defaultBranch': scan_default_branch,
                    'default_branch': scan_default_branch,
                    'defaultBranchSha': scan_head_sha,
                    'default_branch_sha': scan_head_sha,
                    'repoHeadSha': scan_head_sha,
                    'repo_head_sha': scan_head_sha,
                    'status': 'completed',
                    'completedAt': scan_timestamp.isoformat(),
                    'completed_at': scan_timestamp.isoformat(),
                    'updatedAt': scan_timestamp.isoformat(),
                    'updated_at': scan_timestamp.isoformat(),
                }
                defaults['article_system']['scan'] = {
                    **dict(current_article_system.get('scan') or {}),
                    **{key: value for key, value in scan_state.items() if value not in (None, '')},
                }

        # Upsert config
        config, config_created = OrganizationContentConfig.objects.update_or_create(
            organization=org,
            defaults=defaults
        )
        
        # Handle generated_components array
        generated_components_data = data.get('generated_components', [])
        components_created = 0
        components_updated = 0
        
        for comp_data in generated_components_data:
            comp_name = comp_data.get('name')
            if not comp_name:
                continue
            
            comp_defaults = {
                'content': comp_data.get('content', ''),
                'source': comp_data.get('source', 'generated'),
                'original_path': comp_data.get('original_path'),
                'similarity_score': comp_data.get('similarity_score', 0.0),
                'matched_component': comp_data.get('matched_component'),
                'adaptation_notes': comp_data.get('adaptation_notes', ''),
            }
            
            _, created = GeneratedComponent.objects.update_or_create(
                organization=org,
                name=comp_name,
                defaults=comp_defaults
            )
            
            if created:
                components_created += 1
            else:
                components_updated += 1
        
        # Handle component_generation summary and component_mapping
        component_generation = data.get('component_generation', {})
        component_mapping_data = data.get('component_mapping', {})
        
        if component_generation or component_mapping_data:
            mapping_defaults = {
                'mapping_data': component_mapping_data,
            }
            
            # Extract stats from component_generation
            if component_generation:
                mapping_defaults['generation_status'] = component_generation.get('status')
                mapping_defaults['design_guide_path'] = component_generation.get('design_guide_path')
                mapping_defaults['failed_components'] = component_generation.get('failed_components', [])
                
                # Calculate totals from component_generation
                generated = component_generation.get('components_generated', 0)
                adapted = component_generation.get('components_adapted', 0)
                mapping_defaults['generated_count'] = generated
                mapping_defaults['matched_count'] = adapted
                mapping_defaults['total_components'] = generated + adapted
                
                # Storage info
                storage = component_generation.get('storage', {})
                if storage:
                    mapping_defaults['storage_local_path'] = storage.get('local_path')
                    mapping_defaults['storage_pr_url'] = storage.get('pr_url')
                    mapping_defaults['storage_branch_url'] = storage.get('branch_url')
            
            ComponentMapping.objects.update_or_create(
                organization=org,
                defaults=mapping_defaults
            )
        
        status_text = 'created' if org_created else 'updated'
        
        response_data = {
            'status': status_text,
            'org_id': org.id,
            'org_name': org.name,
            'domain': org.domain,
        }
        
        # Include component stats if components were processed
        if generated_components_data:
            response_data['components'] = {
                'created': components_created,
                'updated': components_updated,
                'total': len(generated_components_data)
            }
        
        return Response(response_data, status=status.HTTP_201_CREATED if org_created else status.HTTP_200_OK)


class ContentFactoryHealingRecordView(APIView):
    """
    GET/POST reusable healing records for Content Factory publish-time verification.
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    @staticmethod
    def _normalize_domain(domain: str) -> str:
        if not domain:
            return domain
        domain = domain.lower().strip()
        if domain.startswith('https://'):
            domain = domain[8:]
        elif domain.startswith('http://'):
            domain = domain[7:]
        if domain.startswith('www.'):
            domain = domain[4:]
        if '/' in domain:
            domain = domain.split('/')[0]
        return domain

    def get(self, request):
        records = ContentFactoryHealingRecord.objects.all()
        domain = self._normalize_domain(request.query_params.get("domain") or "")
        github_repo = str(request.query_params.get("github_repo") or "").strip()
        failure_kind = str(request.query_params.get("failure_kind") or "").strip()
        failure_family_key = str(request.query_params.get("failure_family_key") or "").strip()
        promotion_state = str(request.query_params.get("promotion_state") or "").strip()
        limit_raw = str(request.query_params.get("limit") or "").strip()

        if domain:
            records = records.filter(domain=domain)
        if github_repo:
            records = records.filter(github_repo=github_repo)
        if failure_kind:
            records = records.filter(failure_kind=failure_kind)
        if failure_family_key:
            records = records.filter(failure_family_key=failure_family_key)
        if promotion_state:
            records = records.filter(promotion_state=promotion_state)

        limit = 50
        if limit_raw:
            try:
                limit = max(1, min(int(limit_raw), 200))
            except ValueError:
                limit = 50

        serializer = ContentFactoryHealingRecordSerializer(records.order_by("-updated_at")[:limit], many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request):
        payload = dict(request.data or {})
        if "domain" in payload:
            payload["domain"] = self._normalize_domain(payload.get("domain"))

        serializer = ContentFactoryHealingRecordSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        domain = data["domain"]
        github_repo = data.get("github_repo") or ""
        failure_kind = data["failure_kind"]
        failure_family_key = data["failure_family_key"]
        organization = Organization.objects.filter(domain=domain).first()

        defaults = {
            "organization": organization,
            "exact_signature": data.get("exact_signature") or "",
            "summary": data.get("summary") or "",
            "normalized_failure": data.get("normalized_failure") or {},
            "changed_files": data.get("changed_files") or [],
            "patch_manifest": data.get("patch_manifest") or {},
            "validation_results": data.get("validation_results") or {},
            "evidence_artifacts": data.get("evidence_artifacts") or {},
            "snippet_or_rule": data.get("snippet_or_rule") or "",
            "applies_to": data.get("applies_to") or [],
            "promoted_payload": data.get("promoted_payload") or {},
            "promotion_state": data.get("promotion_state") or "candidate",
            "latest_run_id": data.get("latest_run_id") or "",
        }

        record, created = ContentFactoryHealingRecord.objects.update_or_create(
            domain=domain,
            github_repo=github_repo,
            failure_kind=failure_kind,
            failure_family_key=failure_family_key,
            defaults=defaults,
        )
        response_payload = ContentFactoryHealingRecordSerializer(record).data
        response_payload["sync_status"] = "created" if created else "updated"
        return Response(
            response_payload,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ContentFactoryComponentsView(APIView):
    """
    GET components for an organization.
    Path: GET /api/content-factory/org/components?domain=mlai.au
    Optional filters: name (partial match), source (generated/adapted)
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def _normalize_domain(self, domain: str) -> str:
        """Same domain normalization as ContentFactoryOrgConfigView."""
        if not domain:
            return domain
        domain = domain.lower().strip()
        if domain.startswith('https://'):
            domain = domain[8:]
        elif domain.startswith('http://'):
            domain = domain[7:]
        if domain.startswith('www.'):
            domain = domain[4:]
        if '/' in domain:
            domain = domain.split('/')[0]
        return domain

    def get(self, request):
        domain = request.query_params.get('domain')
        name_filter = request.query_params.get('name')
        source_filter = request.query_params.get('source')
        
        if not domain:
            return Response(
                {'error': 'domain query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        normalized_domain = self._normalize_domain(domain)
        
        try:
            org = Organization.objects.get(domain=normalized_domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found', 'domain': domain},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Build component queryset with filters
        components = GeneratedComponent.objects.filter(organization=org)
        
        if name_filter:
            components = components.filter(name__icontains=name_filter)
        
        if source_filter:
            components = components.filter(source=source_filter)
        
        # Get mapping stats if exists
        mapping_stats = {
            'matched_count': 0,
            'generated_count': 0,
            'total': 0
        }
        
        try:
            mapping = org.component_mapping
            mapping_stats = {
                'matched_count': mapping.matched_count,
                'generated_count': mapping.generated_count,
                'total': mapping.total_components
            }
        except ComponentMapping.DoesNotExist:
            pass
        
        # Get last updated timestamp from most recently updated component
        last_updated = None
        latest_component = components.order_by('-updated_at').first()
        if latest_component:
            last_updated = latest_component.updated_at.isoformat()
        
        # Serialize components (lightweight, without content)
        serializer = GeneratedComponentListSerializer(components, many=True)
        
        return Response({
            'domain': org.domain,
            'org_id': org.id,
            'component_count': components.count(),
            'last_updated': last_updated,
            'components': serializer.data,
            'mapping': mapping_stats
        }, status=status.HTTP_200_OK)


class ContentFactoryComponentDetailView(APIView):
    """
    GET a single component by name for an organization.
    Path: GET /api/content-factory/org/components/<name>?domain=mlai.au
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def _normalize_domain(self, domain: str) -> str:
        """Same domain normalization as ContentFactoryOrgConfigView."""
        if not domain:
            return domain
        domain = domain.lower().strip()
        if domain.startswith('https://'):
            domain = domain[8:]
        elif domain.startswith('http://'):
            domain = domain[7:]
        if domain.startswith('www.'):
            domain = domain[4:]
        if '/' in domain:
            domain = domain.split('/')[0]
        return domain

    def get(self, request, name):
        domain = request.query_params.get('domain')
        
        if not domain:
            return Response(
                {'error': 'domain query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        normalized_domain = self._normalize_domain(domain)
        
        try:
            org = Organization.objects.get(domain=normalized_domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found', 'domain': domain},
                status=status.HTTP_404_NOT_FOUND
            )
        
        try:
            component = GeneratedComponent.objects.get(organization=org, name=name)
        except GeneratedComponent.DoesNotExist:
            return Response(
                {'error': 'Component not found', 'name': name, 'domain': domain},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Serialize with full content
        serializer = GeneratedComponentSerializer(component)
        
        return Response(serializer.data, status=status.HTTP_200_OK)


class ContentFactoryTokenView(APIView):
    """
    On-demand token refresh endpoint for content-factory.

    GET /api/content-factory/token?domain=mlai.au
    GET /api/content-factory/token?slack_user_id=U12345

    Content-factory can call this endpoint mid-job to get a fresh GitHub token
    without needing to restart the entire pipeline.

    Supports both:
    - domain: Fetches org-level token (preferred)
    - slack_user_id: Fetches user-level token (legacy fallback)

    Returns:
        {
            "github_token": "ghu_xxxx...",
            "github_repo": "owner/repo",
            "expires_at": "2024-01-16T12:00:00Z" (optional),
            "source": "org" | "user"
        }
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def _normalize_domain(self, domain: str) -> str:
        if not domain:
            return domain
        domain = domain.lower().strip()
        if domain.startswith('https://'):
            domain = domain[8:]
        elif domain.startswith('http://'):
            domain = domain[7:]
        if domain.startswith('www.'):
            domain = domain[4:]
        if '/' in domain:
            domain = domain.split('/')[0]
        return domain

    def get(self, request):
        from integrations.services.github import ensure_valid_token, TokenRefreshError
        from integrations.services.article_generation import ensure_valid_org_token, ArticleGenerationError
        from integrations.services.github_app import (
            GitHubAppTokenError,
            create_installation_access_token,
            github_app_credentials_configured,
        )
        from integrations.models import UserIntegration

        domain = request.query_params.get('domain')
        slack_user_id = request.query_params.get('slack_user_id')
        requested_repo = str(request.query_params.get('github_repo') or '').strip()

        if not domain and not slack_user_id:
            return Response(
                {'error': 'Either domain or slack_user_id query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Try domain-based lookup first (org-level)
        if domain:
            normalized_domain = self._normalize_domain(domain)
            try:
                # Fetch config for additional context
                org = Organization.objects.get(domain=normalized_domain)
                config = org.content_config
                github_repo = requested_repo or str(config.github_repo or '').strip()

                if config.github_installation_id and github_repo:
                    if not github_app_credentials_configured():
                        logger.warning("GitHub App credentials are not configured for installation token lookup.")
                        return Response(
                            {
                                'error': 'GitHub App credentials are not configured',
                                'message': 'MLAI Tools GitHub App server credentials are missing. Configure GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY, then retry.',
                                'action_required': 'server_configuration_required',
                                'github_repo': github_repo,
                                'github_installation_id': config.github_installation_id,
                            },
                            status=status.HTTP_401_UNAUTHORIZED,
                        )
                    try:
                        installation_token = create_installation_access_token(
                            installation_id=config.github_installation_id,
                            repository=github_repo,
                            permission_mode='write',
                        )
                    except GitHubAppTokenError as exc:
                        logger.warning(
                            "GitHub App installation token lookup failed for domain=%s repo=%s installation_id=%s: %s",
                            normalized_domain,
                            github_repo,
                            config.github_installation_id,
                            exc,
                        )
                        return Response(
                            {
                                'error': 'GitHub App installation access failed',
                                'message': str(exc),
                                'action_required': 'auth_required',
                                'github_repo': github_repo,
                                'github_installation_id': config.github_installation_id,
                            },
                            status=status.HTTP_401_UNAUTHORIZED,
                        )

                    response_data = installation_token.as_content_factory_payload(domain=normalized_domain)
                    logger.info(
                        "Provided GitHub App installation token for %s repo=%s installation_id=%s",
                        normalized_domain,
                        github_repo,
                        config.github_installation_id,
                    )
                    return Response(response_data, status=status.HTTP_200_OK)

                fresh_token = ensure_valid_org_token(normalized_domain)
                response_data = {
                    'github_token': fresh_token,
                    'github_repo': github_repo or config.github_repo,
                    'domain': normalized_domain,
                    'source': 'org',
                    'token_source': 'github_oauth_user_token',
                }

                if config.github_token_expires_at:
                    response_data['expires_at'] = config.github_token_expires_at.isoformat()

                logger.info(f"Provided fresh org-level GitHub token for {normalized_domain}")
                return Response(response_data, status=status.HTTP_200_OK)

            except Organization.DoesNotExist:
                if not slack_user_id:
                    return Response(
                        {'error': f'Organization not found: {domain}'},
                        status=status.HTTP_404_NOT_FOUND
                    )
                # Fall through to user-level lookup
            except (ArticleGenerationError, TokenRefreshError) as e:
                if not slack_user_id:
                    logger.warning(f"Token refresh failed for org {domain}: {e}")
                    return Response(
                        {
                            'error': 'Token refresh failed',
                            'message': str(e),
                            'action_required': 'auth_required'
                        },
                        status=status.HTTP_401_UNAUTHORIZED
                    )
                # Fall through to user-level lookup

        # User-level lookup (legacy fallback)
        if slack_user_id:
            try:
                fresh_token = ensure_valid_token(slack_user_id)

                integration = UserIntegration.objects.get(slack_user_id=slack_user_id)

                response_data = {
                    'github_token': fresh_token,
                    'github_repo': integration.github_repo,
                    'slack_user_id': slack_user_id,
                    'source': 'user',
                    'token_source': 'github_oauth_user_token',
                }

                if integration.github_token_expires_at:
                    response_data['expires_at'] = integration.github_token_expires_at.isoformat()

                logger.info(f"Provided fresh user-level GitHub token for {slack_user_id}")
                return Response(response_data, status=status.HTTP_200_OK)

            except UserIntegration.DoesNotExist:
                return Response(
                    {'error': 'No integration found for this user'},
                    status=status.HTTP_404_NOT_FOUND
                )
            except TokenRefreshError as e:
                logger.warning(f"Token refresh failed for {slack_user_id}: {e}")
                return Response(
                    {
                        'error': 'Token refresh failed',
                        'message': str(e),
                        'action_required': 'auth_required'
                    },
                    status=status.HTTP_401_UNAUTHORIZED
                )

        return Response(
            {'error': 'No valid credentials found'},
            status=status.HTTP_404_NOT_FOUND
        )


class ContentFactoryGitHubStatusView(APIView):
    """
    Check GitHub connection status for an organization/domain.

    GET /api/content-factory/org/github-status?domain=mlai.au

    Returns:
        {
            "connected": true/false,
            "github_repo": "owner/repo",
            "github_user_name": "username",
            "token_valid": true/false,
            "expires_at": "2024-01-16T12:00:00Z" (optional)
        }
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        domain = request.query_params.get('domain')
        slack_user_id = str(request.query_params.get('slack_user_id') or '').strip()

        if not domain:
            return Response(
                {'error': 'domain query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        normalized_domain = _normalize_content_factory_domain(domain)

        try:
            org = Organization.objects.get(domain=normalized_domain)
        except Organization.DoesNotExist:
            return Response({
                'connected': False,
                'domain': normalized_domain,
                'message': 'Organization not found. Please set up the organization first.'
            }, status=status.HTTP_200_OK)

        from integrations.services.article_generation import resolve_content_factory_connection_for_domain

        connection_details = resolve_content_factory_connection_for_domain(
            normalized_domain,
            slack_user_id or None,
        )
        config = connection_details.get('config') or getattr(org, 'content_config', None)

        if not config and not connection_details.get('github_repo'):
            return Response({
                'connected': False,
                'domain': normalized_domain,
                'github_repo': None,
                'message': 'No GitHub token configured for this organization.'
            }, status=status.HTTP_200_OK)

        token_valid = not bool(connection_details.get('needs_github_auth'))

        response_data = {
            'connected': token_valid,
            'domain': normalized_domain,
            'github_repo': connection_details.get('github_repo') or getattr(config, 'github_repo', None),
            'github_user_name': getattr(config, 'github_user_name', None),
            'token_valid': token_valid,
            'connection_state': connection_details.get('connection_state'),
            'credential_source': connection_details.get('credential_source') or 'none',
        }

        if config.github_token_expires_at:
            response_data['expires_at'] = config.github_token_expires_at.isoformat()

        return Response(response_data, status=status.HTTP_200_OK)


class ContentFactoryGitHubReconnectView(APIView):
    """
    Start or confirm the Content Factory GitHub reconnect flow for a domain.
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        domain = request.data.get('domain')
        slack_user_id = str(request.data.get('slack_user_id') or '').strip()
        github_repo = str(request.data.get('github_repo') or '').strip() or None
        trigger = str(request.data.get('trigger') or 'manual').strip() or 'manual'
        pending_action = request.data.get('pending_action')

        normalized_domain = _normalize_content_factory_domain(domain)
        if not normalized_domain and not slack_user_id:
            return Response(
                {'error': 'domain or slack_user_id is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from integrations.services.article_generation import resolve_content_factory_connection_for_domain

        connection_details = (
            resolve_content_factory_connection_for_domain(
                normalized_domain,
                slack_user_id or None,
            )
            if normalized_domain
            else {
                'github_repo': github_repo,
                'connection_state': 'auth_required',
                'credential_source': 'none',
            }
        )

        resolved_repo = github_repo or (
            str(connection_details.get('github_repo') or '').strip() or None
        )
        connection_state = connection_details.get('connection_state') or 'auth_required'
        auth_url = _content_factory_github_auth_url(
            slack_user_id=slack_user_id,
            domain=normalized_domain or None,
        )

        response_payload = {
            'domain': normalized_domain or None,
            'github_repo': resolved_repo,
            'connection_state': connection_state,
            'credential_source': connection_details.get('credential_source') or 'none',
            'trigger': trigger,
            'pending_action': pending_action,
        }

        if connection_state == 'connected':
            response_payload.update(
                {
                    'status': 'already_connected',
                    'message': f"GitHub is already connected for {normalized_domain}.",
                }
            )
            return Response(response_payload, status=status.HTTP_200_OK)

        if connection_state == 'repo_selection_required':
            message = (
                f"GitHub is connected for {normalized_domain}, but Roo still needs a repository selected."
            )
        elif normalized_domain:
            message = f"GitHub needs to be connected for {normalized_domain} before Roo can continue."
        else:
            message = "GitHub needs to be connected before Roo can continue."

        response_payload.update(
            {
                'status': 'auth_started',
                'auth_url': auth_url,
                'message': message,
            }
        )
        return Response(response_payload, status=status.HTTP_200_OK)


class ScheduledDiscoveryReplayView(APIView):
    """
    Force a scheduled discovery enqueue for a specific user/domain/date.
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        from integrations.services.daily_discovery import enqueue_scheduled_discovery

        domain = request.data.get("domain")
        slack_user_id = request.data.get("slack_user_id")
        local_date_raw = request.data.get("local_date")
        force = bool(request.data.get("force"))

        if not domain or not slack_user_id:
            return Response(
                {"error": "domain and slack_user_id are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        local_date = None
        if local_date_raw:
            try:
                local_date = calendar_date.fromisoformat(str(local_date_raw))
            except ValueError:
                return Response(
                    {"error": "local_date must use YYYY-MM-DD format"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        result = enqueue_scheduled_discovery(
            slack_user_id=slack_user_id,
            domain=domain,
            local_date=local_date,
            force=force,
        )
        result_status = str(result.get("status") or "").strip().lower()
        http_status = status.HTTP_202_ACCEPTED if result_status == "queued" else status.HTTP_200_OK
        return Response(result, status=http_status)


class ResearchAutomationView(APIView):
    """Create or update a scheduled research automation and notification route."""

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        from content_factory.models import NotificationChannelType, NotificationConsentState
        from integrations.services.research_automations import create_or_update_research_automation

        domain = str(request.data.get("domain") or "").strip()
        channel_type = str(request.data.get("channel_type") or "").strip().lower()
        route_id = str(request.data.get("route_id") or request.data.get("email") or request.data.get("phone") or "").strip()
        timezone_name = str(request.data.get("timezone") or "Australia/Melbourne").strip()
        frequency_per_day = request.data.get("frequency_per_day") or request.data.get("frequency") or 1
        local_send_times = request.data.get("local_send_times") or []
        name = str(request.data.get("name") or "").strip()
        consented = bool(request.data.get("consented") or request.data.get("verified"))
        user = None

        if not domain:
            return Response({"error": "domain is required"}, status=status.HTTP_400_BAD_REQUEST)
        if channel_type not in NotificationChannelType.values:
            return Response(
                {"error": "channel_type must be slack, whatsapp, or email"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not route_id:
            return Response({"error": "route_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        if local_send_times is not None and not isinstance(local_send_times, list):
            return Response({"error": "local_send_times must be a list"}, status=status.HTTP_400_BAD_REQUEST)

        user_email = str(request.data.get("user_email") or (route_id if channel_type == "email" else "") or "").strip().lower()
        if user_email:
            UserModel = get_user_model()
            user, _created = UserModel.objects.get_or_create(email=user_email, defaults={"is_active": True})

        try:
            automation = create_or_update_research_automation(
                domain=domain,
                channel_type=channel_type,
                route_id=route_id,
                user=user,
                timezone_name=timezone_name,
                frequency_per_day=int(frequency_per_day),
                local_send_times=local_send_times,
                consent_state=(
                    NotificationConsentState.ACTIVE
                    if consented
                    else NotificationConsentState.PENDING
                ),
                name=name,
            )
        except Organization.DoesNotExist:
            return Response({"error": "Organization not found"}, status=status.HTTP_404_NOT_FOUND)
        except (TypeError, ValueError) as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        channel = automation.notification_channel
        return Response(
            {
                "status": "active" if channel.consent_state == NotificationConsentState.ACTIVE else "pending_consent",
                "automation_id": str(automation.id),
                "channel_id": str(channel.id),
                "channel_type": channel.channel_type,
                "consent_state": channel.consent_state,
                "timezone": automation.timezone,
                "frequency_per_day": automation.frequency_per_day,
                "local_send_times": automation.local_send_times,
            },
            status=status.HTTP_201_CREATED,
        )


class ResearchAutomationActionView(APIView):
    """Public signed action endpoint for email, WhatsApp, and Slack action URLs."""

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return self._handle(request)

    def post(self, request):
        return self._handle(request)

    def _handle(self, request):
        from django.core import signing as django_signing
        from integrations.services.notification_adapters import handle_automation_action_token

        token = str(request.query_params.get("token") or request.data.get("token") or "").strip()
        if not token:
            return Response({"error": "token is required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = handle_automation_action_token(token)
        except django_signing.BadSignature:
            return Response({"error": "Invalid or expired action token"}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.warning("Research automation action failed: %s", exc)
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_200_OK)


class NotificationChannelEmailVerifyView(APIView):
    """Public signed magic-link endpoint that activates an email channel."""

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        from django.core import signing as django_signing
        from integrations.services.notification_channels import (
            ChannelActionError,
            handle_email_verification_token,
        )

        token = str(request.query_params.get("token") or "").strip()
        result = "invalid"
        if token:
            try:
                handle_email_verification_token(token)
                result = "verified"
            except django_signing.SignatureExpired:
                result = "expired"
            except (django_signing.BadSignature, ChannelActionError):
                result = "invalid"
        frontend_base = ""
        for setting_name in ("FOUNDER_TOOLS_URL", "VIBE_RAISING_URL", "DEFAULT_FRONTEND_URL"):
            value = str(getattr(settings, setting_name, "") or "").strip()
            if value:
                frontend_base = value.rstrip("/")
                break
        if not frontend_base:
            frontend_base = "http://localhost:5173" if getattr(settings, "DEBUG", False) else "https://mlai.au"
        return HttpResponseRedirect(
            f"{frontend_base}/founder-tools/marketing/settings?emailChannel={result}"
        )


class ResearchAutomationWhatsAppWebhookView(APIView):
    """Meta WhatsApp Cloud API webhook for statuses and STOP opt-outs."""

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        verify_token = str(getattr(settings, "WHATSAPP_WEBHOOK_VERIFY_TOKEN", "") or "").strip()
        mode = request.query_params.get("hub.mode")
        token = request.query_params.get("hub.verify_token")
        challenge = request.query_params.get("hub.challenge")
        if mode == "subscribe" and verify_token and token == verify_token:
            return HttpResponse(challenge or "", status=200)
        return Response({"error": "Invalid verification token"}, status=status.HTTP_403_FORBIDDEN)

    def post(self, request):
        from integrations.services.notification_adapters import (
            handle_whatsapp_webhook,
            verify_whatsapp_webhook_signature,
        )

        signature = request.headers.get("X-Hub-Signature-256", "")
        if not verify_whatsapp_webhook_signature(body=request.body, signature=signature):
            return Response({"error": "Invalid signature"}, status=status.HTTP_403_FORBIDDEN)
        result = handle_whatsapp_webhook(request.data if isinstance(request.data, dict) else {})
        return Response(result, status=status.HTTP_200_OK)


class ContentFactoryOAuthInitiateView(APIView):
    """
    Initiate GitHub OAuth flow for a specific domain.

    POST /api/content-factory/oauth/initiate
    {
        "domain": "mlai.au",
        "slack_user_id": "U12345" (optional, for callback routing)
    }

    Returns:
        {
            "oauth_url": "https://github.com/apps/mlai-tools/installations/new?state=..."
        }
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def _normalize_domain(self, domain: str) -> str:
        if not domain:
            return domain
        domain = domain.lower().strip()
        if domain.startswith('https://'):
            domain = domain[8:]
        elif domain.startswith('http://'):
            domain = domain[7:]
        if domain.startswith('www.'):
            domain = domain[4:]
        if '/' in domain:
            domain = domain.split('/')[0]
        return domain

    def post(self, request):
        import secrets
        import urllib.parse
        from django.conf import settings

        domain = request.data.get('domain')
        slack_user_id = request.data.get('slack_user_id', '')

        if not domain:
            return Response(
                {'error': 'domain is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        normalized_domain = self._normalize_domain(domain)

        # Ensure organization exists (create if needed)
        org, _ = Organization.objects.get_or_create(
            domain=normalized_domain,
            defaults={'name': normalized_domain}
        )

        # Build state: domain::random_token::slack_user_id::type
        # The 'org' type distinguishes this from user-level OAuth
        rand_token = secrets.token_urlsafe(16)
        state = f"{normalized_domain}::{rand_token}::{slack_user_id}::org"

        # Store state in cache for validation (optional but recommended)
        from django.core.cache import cache
        cache.set(f"github_oauth_state:{rand_token}", state, timeout=600)  # 10 min expiry

        # GitHub App installation URL
        app_slug = "mlai-tools"
        install_url = f"https://github.com/apps/{app_slug}/installations/new"

        params = {"state": state}
        oauth_url = install_url + "?" + urllib.parse.urlencode(params)

        return Response({
            'oauth_url': oauth_url,
            'domain': normalized_domain,
            'state': state,
        }, status=status.HTTP_200_OK)


class ContentFactoryConnectGitHubView(APIView):
    """
    Save GitHub credentials for an organization after OAuth completion.

    POST /api/content-factory/org/connect-github
    {
        "domain": "mlai.au",
        "github_token": "ghu_xxx...",
        "github_refresh_token": "ghr_xxx...",
        "github_token_expires_at": "2024-01-16T12:00:00Z",
        "github_user_name": "username",
        "github_repo": "owner/repo",
        "github_installation_id": "12345"
    }

    Called by the OAuth callback to store org-level GitHub credentials.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def _normalize_domain(self, domain: str) -> str:
        if not domain:
            return domain
        domain = domain.lower().strip()
        if domain.startswith('https://'):
            domain = domain[8:]
        elif domain.startswith('http://'):
            domain = domain[7:]
        if domain.startswith('www.'):
            domain = domain[4:]
        if '/' in domain:
            domain = domain.split('/')[0]
        return domain

    def post(self, request):
        from django.utils.dateparse import parse_datetime

        data = request.data
        domain = data.get('domain')
        github_token = data.get('github_token')
        slack_user_id = data.get('slack_user_id')

        if not domain:
            return Response(
                {'error': 'domain is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not github_token:
            return Response(
                {'error': 'github_token is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        normalized_domain = self._normalize_domain(domain)

        # Get or create organization
        org, org_created = Organization.objects.get_or_create(
            domain=normalized_domain,
            defaults={'name': normalized_domain}
        )

        # Get or create config
        config, config_created = OrganizationContentConfig.objects.get_or_create(
            organization=org
        )

        # Update GitHub credentials
        config.github_token_encrypted = github_token

        if 'github_refresh_token' in data:
            config.github_refresh_token_encrypted = data['github_refresh_token']

        if 'github_token_expires_at' in data:
            expires_at = data['github_token_expires_at']
            if isinstance(expires_at, str):
                parsed_expires_at = parse_datetime(expires_at)
                if parsed_expires_at is not None:
                    config.github_token_expires_at = parsed_expires_at
            else:
                config.github_token_expires_at = expires_at

        if 'github_user_name' in data:
            config.github_user_name = data['github_user_name']

        if 'github_repo' in data:
            config.github_repo = data['github_repo']

        if 'github_installation_id' in data:
            config.github_installation_id = data['github_installation_id']

        if 'github_scopes' in data:
            config.github_scopes = data['github_scopes']

        if slack_user_id:
            config.connected_slack_user_id = slack_user_id

        config.save()

        logger.info(f"Connected GitHub for organization {normalized_domain}: repo={config.github_repo}, user={config.github_user_name}")

        return Response({
            'status': 'connected',
            'domain': normalized_domain,
            'github_repo': config.github_repo,
            'github_user_name': config.github_user_name,
        }, status=status.HTTP_200_OK)


def _content_package_from_run(run: Optional[ContentFactoryRun]) -> dict:
    if not run:
        return {}
    result = run.result or {}
    candidates = [
        result.get("content_package"),
        (result.get("result") or {}).get("content_package") if isinstance(result.get("result"), dict) else None,
    ]
    for package in candidates:
        if isinstance(package, dict) and package:
            return package
    return {}


def _load_content_package_for_callback(run_id: str, *, attempts: int = 3, delay_seconds: float = 0.35):
    run = None
    package = {}
    for attempt in range(1, attempts + 1):
        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        package = _content_package_from_run(run)
        if package:
            return run, package
        if attempt < attempts:
            time.sleep(delay_seconds)
    return run, package


TERMINAL_RUN_STATUSES = {
    ContentFactoryRunStatus.FAILED,
    ContentFactoryRunStatus.BLOCKED,
    ContentFactoryRunStatus.DENIED,
    ContentFactoryRunStatus.CANCELLED,
}
ARTICLE_SYSTEM_SETUP_ACTIVE_STATUSES = {
    "queued",
    "running",
    "processing",
    "pending",
    "starting",
    "in_progress",
    "preview_building",
    "preview_verifying",
    "repair_preview_building",
}
ARTICLE_SYSTEM_SETUP_RETRY_TERMINAL_STATUSES = {"failed", "blocked", "preview_failed", "fallback_ready"}
ARTICLE_SYSTEM_SETUP_FAILURE_METADATA_KEYS = (
    "failed_preview_url",
    "failedPreviewUrl",
    "failure_kind",
    "failureKind",
    "failed_step",
    "failedStep",
    "preview_failure_details",
    "previewFailureDetails",
    "directory_quality_gates",
    "directoryQualityGates",
    "directory_browser_repair",
    "directoryBrowserRepair",
    "directory_visual_style_report",
    "directoryVisualStyleReport",
    "directory_visual_repair",
    "directoryVisualRepair",
)


def _is_terminal_run_status(value: str) -> bool:
    return str(value or "").strip() in TERMINAL_RUN_STATUSES


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _article_system_setup_resume_generation(*payloads) -> int:
    for payload in payloads:
        mapping = _mapping(payload)
        for key in ("resume_generation", "resumeGeneration", "attempt_number", "attemptNumber"):
            value = mapping.get(key)
            if value in (None, ""):
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0


def _article_system_setup_current_retry_attempt(*payloads) -> bool:
    for payload in payloads:
        mapping = _mapping(payload)
        if not mapping:
            continue
        setup = _mapping(mapping.get("article_system_setup"))
        queue = _mapping(mapping.get("setup_queue")) or _mapping(setup.get("setup_queue"))
        status_value = str(mapping.get("status") or setup.get("status") or "").strip().lower()
        if mapping.get("is_current_attempt") is True or mapping.get("isCurrentAttempt") is True:
            return True
        if setup.get("is_current_attempt") is True or setup.get("isCurrentAttempt") is True:
            return True
        if queue.get("resumed") is True:
            return True
        if _article_system_setup_resume_generation(mapping, setup) > 0 and status_value in ARTICLE_SYSTEM_SETUP_ACTIVE_STATUSES:
            return True
    return False


def _article_system_setup_snapshot_is_current_retry(*, existing_run, data: dict, raw_payload: dict) -> bool:
    if not existing_run or existing_run.workflow != "article_system_setup":
        return False
    if str(data.get("workflow") or raw_payload.get("workflow") or "").strip() != "article_system_setup":
        return False
    incoming_status = str(data.get("status") or raw_payload.get("status") or "").strip().lower()
    if incoming_status not in ARTICLE_SYSTEM_SETUP_ACTIVE_STATUSES:
        return False
    return _article_system_setup_current_retry_attempt(data, raw_payload, data.get("result"), raw_payload.get("result"))


def _callback_event_emitted_at(data) -> Optional[datetime]:
    """
    Parse the emitted_at stamp content-factory adds to callback payloads.

    Returns None when the field is absent (payloads from older content-factory
    versions) or unparseable, so callers no-op gracefully.
    """
    from django.utils.dateparse import parse_datetime

    raw = str((data or {}).get("emitted_at") or "").strip()
    if not raw:
        return None
    try:
        parsed = parse_datetime(raw)
    except ValueError:
        return None
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime_timezone.utc)
    return parsed


def _callback_event_is_stale(*, existing_run: Optional[ContentFactoryRun], emitted_at: Optional[datetime]) -> bool:
    """
    True when a callback was emitted before the run's last synced event.

    content-factory retries deliveries from a durable outbox, so an old event
    can arrive after a newer one; applying it would roll the run state back.
    Events without emitted_at (or runs without a watermark yet) are never
    considered stale.
    """
    if existing_run is None or emitted_at is None:
        return False
    last_synced = existing_run.last_event_emitted_at
    return bool(last_synced and emitted_at < last_synced)


def _claim_callback_event(*, event_id: str, event_type: str, job_id: str, emitted_at: Optional[datetime]) -> bool:
    """
    Atomically claim a callback delivery by its event_id.

    Returns True when this delivery is the first acknowledgement of the event
    (caller should process it) and False when the event_id is already
    recorded. The unique constraint makes the claim race-safe across workers.
    Fails open on storage errors: reprocessing an event is recoverable,
    silently dropping one is not.
    """
    from content_factory.models import ContentFactoryCallbackEvent

    max_attempts = 3 if connection.vendor == "sqlite" else 1
    for attempt in range(max_attempts):
        try:
            _, created = ContentFactoryCallbackEvent.objects.get_or_create(
                event_id=event_id[:100],
                defaults={
                    "job_id": str(job_id or "")[:100],
                    "event_type": str(event_type or "")[:100],
                    "emitted_at": emitted_at,
                },
            )
            return created
        except OperationalError as exc:
            if _is_retryable_sqlite_lock(exc) and attempt < max_attempts - 1:
                time.sleep(0.15 * (attempt + 1))
                continue
            logger.warning("Failed to record callback event_id=%s; processing without dedupe: %s", event_id, exc)
            return True
        except Exception as exc:
            logger.warning("Failed to record callback event_id=%s; processing without dedupe: %s", event_id, exc)
            return True
    return True


def _release_callback_event(event_id: str) -> None:
    """
    Drop a claimed event_id after processing failed.

    The sender retries non-2xx deliveries; releasing the claim lets the retry
    be reprocessed instead of being treated as a duplicate.
    """
    from content_factory.models import ContentFactoryCallbackEvent

    try:
        ContentFactoryCallbackEvent.objects.filter(event_id=event_id[:100]).delete()
    except Exception as exc:
        logger.warning("Failed to release callback event_id=%s after processing failure: %s", event_id, exc)


def _sync_generation_callback_to_run(*, data: dict, run_status: str, step_status: str) -> Optional[ContentFactoryRun]:
    run_id = str(data.get("run_id") or data.get("job_id") or "").strip()
    if not run_id:
        return None

    step_key = str(
        data.get("failed_step")
        or data.get("blocked_step")
        or data.get("current_step")
        or data.get("step")
        or ""
    ).strip()
    workflow = str(data.get("workflow") or "direct_generate").strip() or "direct_generate"
    error_message = str(data.get("error") or data.get("error_message") or "").strip()
    error_code = str(data.get("error_code") or "").strip()
    existing_run = ContentFactoryRun.objects.filter(run_id=run_id).first()
    emitted_at = _callback_event_emitted_at(data)
    if _callback_event_is_stale(existing_run=existing_run, emitted_at=emitted_at):
        logger.info(
            "Ignoring stale generation callback for run %s: emitted_at=%s predates last synced event %s",
            run_id,
            emitted_at.isoformat(),
            existing_run.last_event_emitted_at.isoformat(),
        )
        return existing_run
    result = dict((existing_run.result if existing_run else None) or {})
    result.update(
        {
            "status": run_status,
            "run_id": run_id,
            "job_id": str(data.get("job_id") or run_id),
            "workflow": workflow,
            "current_step": step_key,
            "step": step_key,
            "error": error_message,
            "error_code": error_code,
            "retry_after_seconds": data.get("retry_after_seconds"),
            "next_step": data.get("next_step"),
            "rerunnable_step": data.get("rerunnable_step"),
        }
    )
    diagnostics = data.get("diagnostics")
    if isinstance(diagnostics, dict):
        result["diagnostics"] = diagnostics

    step_order = list((existing_run.step_order if existing_run else None) or [])
    if step_key and step_key not in step_order:
        step_order.append(step_key)

    run, _created = ContentFactoryRun.objects.update_or_create(
        run_id=run_id,
        defaults={
            "workflow": workflow,
            "domain": str(data.get("domain") or (existing_run.domain if existing_run else "") or ""),
            "github_repo": str(data.get("github_repo") or (existing_run.github_repo if existing_run else "") or ""),
            "slack_user_id": str(data.get("slack_user_id") or (existing_run.slack_user_id if existing_run else "") or ""),
            "status": run_status,
            "current_step": step_key or (existing_run.current_step if existing_run else ""),
            "approval_state": (existing_run.approval_state if existing_run else ContentFactoryApprovalState.NOT_REQUIRED),
            "artifact_root": (existing_run.artifact_root if existing_run else ""),
            "step_order": step_order,
            "acceptance_summary": (existing_run.acceptance_summary if existing_run else {}),
            "verification_summary": diagnostics if isinstance(diagnostics, dict) else (existing_run.verification_summary if existing_run else {}),
            "run_request": (existing_run.run_request if existing_run else {}),
            "result": result,
            "error": error_message,
            "resume_available": True,
            "last_event_emitted_at": emitted_at or (existing_run.last_event_emitted_at if existing_run else None),
        },
    )

    if step_key:
        display_order = step_order.index(step_key) if step_key in step_order else len(step_order)
        ContentFactoryRunStep.objects.update_or_create(
            run=run,
            step_key=step_key,
            defaults={
                "display_order": display_order,
                "required": True,
                "status": step_status,
                "attempts": 1,
                "message": error_message,
                "completed_at": timezone.now(),
                "error": error_message,
                "artifacts": data.get("failure_artifacts") or [],
            },
        )
    return run


def _sync_scan_callback_to_run(*, data: dict, approval_required: bool) -> Optional[ContentFactoryRun]:
    run_id = str(data.get("run_id") or data.get("job_id") or "").strip()
    if not run_id:
        return None

    existing_run = ContentFactoryRun.objects.filter(run_id=run_id).first()
    emitted_at = _callback_event_emitted_at(data)
    if _callback_event_is_stale(existing_run=existing_run, emitted_at=emitted_at):
        logger.info(
            "Ignoring stale scan callback for run %s: emitted_at=%s predates last synced event %s",
            run_id,
            emitted_at.isoformat(),
            existing_run.last_event_emitted_at.isoformat(),
        )
        return existing_run
    if existing_run and existing_run.status in {ContentFactoryRunStatus.CANCELLED, ContentFactoryRunStatus.DENIED}:
        logger.info(
            "Ignoring scan callback for terminal local run: run_id=%s status=%s",
            run_id,
            existing_run.status,
        )
        return existing_run

    scaffold_status = str(data.get("scaffold_status") or "").strip()
    readiness = data.get("article_system_readiness") if isinstance(data.get("article_system_readiness"), dict) else {}
    readiness_status = str(readiness.get("status") or "").strip()
    if approval_required:
        run_status = ContentFactoryRunStatus.AWAITING_CONFIRMATION
        approval_state = ContentFactoryApprovalState.APPROVAL_REQUIRED
        result_status = "awaiting_confirmation"
    elif scaffold_status == "queued":
        run_status = ContentFactoryRunStatus.RUNNING
        approval_state = ContentFactoryApprovalState.APPROVED
        result_status = "article_system_setup_queued"
    elif scaffold_status == "manual_blocked" or readiness_status == "manual_blocked":
        run_status = ContentFactoryRunStatus.BLOCKED
        approval_state = ContentFactoryApprovalState.NOT_REQUIRED
        result_status = "manual_blocked"
    else:
        run_status = ContentFactoryRunStatus.COMPLETED
        approval_state = ContentFactoryApprovalState.NOT_REQUIRED
        result_status = "completed"

    result = dict((existing_run.result if existing_run else None) or {})
    result.update(
        {
            "status": result_status,
            "run_id": run_id,
            "job_id": str(data.get("job_id") or run_id),
            "workflow": str(data.get("workflow") or "repo_scan"),
            "domain": str(data.get("domain") or (existing_run.domain if existing_run else "") or ""),
            "github_repo": str(data.get("github_repo") or (existing_run.github_repo if existing_run else "") or ""),
            "default_branch": str(data.get("default_branch") or data.get("defaultBranch") or "").strip(),
            "repo_head_sha": str(data.get("repo_head_sha") or data.get("commit_sha") or "").strip(),
            "commit_sha": str(data.get("commit_sha") or data.get("repo_head_sha") or "").strip(),
            "scan_completed_at": str(data.get("scan_completed_at") or "").strip(),
            "requested_action": data.get("requested_action"),
            "scaffold_required": bool(data.get("scaffold_required")),
            "scaffold_status": scaffold_status,
            "scaffold_plan": data.get("scaffold_plan") if isinstance(data.get("scaffold_plan"), dict) else {},
            "article_system": data.get("article_system") if isinstance(data.get("article_system"), dict) else {},
            "article_system_readiness": readiness,
            "article_system_setup": data.get("article_system_setup") if isinstance(data.get("article_system_setup"), dict) else {},
            "tech_stack": data.get("tech_stack") if isinstance(data.get("tech_stack"), dict) else {},
            "repo_profile": data.get("repo_profile") if isinstance(data.get("repo_profile"), dict) else {},
            "repository_classification": (
                data.get("repository_classification")
                if isinstance(data.get("repository_classification"), dict)
                else {}
            ),
            "article_surface_hint": data.get("article_surface_hint") if isinstance(data.get("article_surface_hint"), dict) else {},
            "article_surface_hint_status": str(data.get("article_surface_hint_status") or "ignored").strip(),
            "article_surface_mode": str(data.get("article_surface_mode") or "").strip(),
            "scan_purpose": str(data.get("scan_purpose") or "").strip(),
            "article_surface_resolution": data.get("article_surface_resolution") if isinstance(data.get("article_surface_resolution"), dict) else {},
            "matched_article_surface": data.get("matched_article_surface"),
            "publish_targets": data.get("publish_targets") if isinstance(data.get("publish_targets"), list) else [],
            "default_publish_target_id": data.get("default_publish_target_id"),
            "approve_url": data.get("approve_url"),
            "deny_url": data.get("deny_url"),
            "scaffold_queued": bool(data.get("scaffold_queued")),
            "scaffold_job_id": data.get("scaffold_job_id"),
            "setup_run_id": data.get("setup_run_id"),
            "preview_url": data.get("preview_url"),
            "pr_url": data.get("pr_url"),
            "live_preview_url": data.get("live_preview_url"),
            "components_generated": bool(data.get("components_generated")),
            "components_count": data.get("components_count") or 0,
            "component_names": data.get("component_names") if isinstance(data.get("component_names"), list) else [],
            "scaffold_reason": str(data.get("scaffold_reason") or "").strip(),
        }
    )
    detected_candidates = data.get("detected_candidates")
    if not isinstance(detected_candidates, list):
        scaffold_plan = result.get("scaffold_plan") if isinstance(result.get("scaffold_plan"), dict) else {}
        detected_candidates = scaffold_plan.get("detected_candidates")
    if isinstance(detected_candidates, list):
        result["detected_candidates"] = detected_candidates

    step_order = list((existing_run.step_order if existing_run else None) or [])
    if not step_order:
        step_order = ["load_repo_context", "scan_structure", "extract_components", "persist_org_config", "finalize"]

    run, _created = ContentFactoryRun.objects.update_or_create(
        run_id=run_id,
        defaults={
            "workflow": str(data.get("workflow") or "repo_scan"),
            "domain": result["domain"],
            "github_repo": result["github_repo"],
            "slack_user_id": str(data.get("slack_user_id") or (existing_run.slack_user_id if existing_run else "") or ""),
            "status": run_status,
            "current_step": str(data.get("current_step") or (existing_run.current_step if existing_run else "") or "finalize"),
            "approval_state": approval_state,
            "artifact_root": (existing_run.artifact_root if existing_run else ""),
            "step_order": step_order,
            "acceptance_summary": (existing_run.acceptance_summary if existing_run else {}),
            "verification_summary": (existing_run.verification_summary if existing_run else {}),
            "run_request": (existing_run.run_request if existing_run else {}),
            "result": result,
            "error": "" if run_status != ContentFactoryRunStatus.BLOCKED else result.get("scaffold_reason") or readiness.get("reason") or "",
            "resume_available": bool(existing_run.resume_available if existing_run else False),
            "last_event_emitted_at": emitted_at or (existing_run.last_event_emitted_at if existing_run else None),
        },
    )
    setup_run_id = str(result.get("setup_run_id") or "").strip()
    if setup_run_id:
        setup_result = dict(result.get("article_system_setup") or {})
        setup_result.setdefault("setup_run_id", setup_run_id)
        setup_result.setdefault("parent_run_id", run_id)
        existing_setup = ContentFactoryRun.objects.filter(run_id=setup_run_id).first()
        if not existing_setup:
            ContentFactoryRun.objects.create(
                run_id=setup_run_id,
                workflow="article_system_setup",
                domain=result["domain"],
                github_repo=result["github_repo"],
                slack_user_id=str(data.get("slack_user_id") or (existing_run.slack_user_id if existing_run else "") or ""),
                status=ContentFactoryRunStatus.RUNNING,
                current_step="queued",
                approval_state=ContentFactoryApprovalState.NOT_REQUIRED,
                step_order=["load_context", "validate_plan", "prepare_branch", "create_pull_request", "start_hosted_preview", "await_review"],
                run_request={
                    "workflow": "article_system_setup",
                    "domain": result["domain"],
                    "github_repo": result["github_repo"],
                    "parent_run_id": run_id,
                    "scan_run_id": run_id,
                },
                result=setup_result,
                error="",
            )
    return run


def _clear_pending_article_system_setup_for_domain(domain: str) -> None:
    domain = str(domain or "").strip()
    if not domain:
        return
    try:
        org = Organization.objects.filter(domain=domain).first()
        if not org:
            return
        config = org.content_config
        article_system = dict(config.article_system or {})
        if "pending_article_system_setup" not in article_system:
            return
        article_system.pop("pending_article_system_setup", None)
        config.article_system = article_system
        config.save(update_fields=["article_system", "updated_at"])
    except Exception as exc:
        logger.warning("Failed to clear pending article system setup for %s: %s", domain, exc)


def _update_pending_article_system_setup_for_domain(domain: str, **updates) -> None:
    domain = str(domain or "").strip()
    if not domain:
        return
    updates = sanitize_json_for_postgres(updates)
    try:
        org = Organization.objects.filter(domain=domain).first()
        if not org:
            return
        config = org.content_config
        article_system = sanitize_json_for_postgres(dict(config.article_system or {}))
        pending = sanitize_json_for_postgres(dict(article_system.get("pending_article_system_setup") or {}))
        clearable_empty_keys = {
            "error",
            "errorCode",
            "error_code",
            "livePreview",
            "live_preview",
            "stale",
            "staleReason",
            "stale_reason",
            "failureStep",
            "failure_step",
            "failedStep",
            "failed_step",
            "failedPreviewUrl",
            "failed_preview_url",
            "failureKind",
            "failure_kind",
            "previewFailureDetails",
            "preview_failure_details",
            "directoryQualityGates",
            "directory_quality_gates",
            "directoryBrowserRepair",
            "directory_browser_repair",
            "directoryVisualStyleReport",
            "directory_visual_style_report",
            "directoryVisualRepair",
            "directory_visual_repair",
        }
        explicit_empty_value_keys = {
            "previewUrl",
            "preview_url",
            "fallbackPreviewUrl",
            "fallback_preview_url",
        }
        for key, value in updates.items():
            if value not in (None, "") or (value == "" and key in explicit_empty_value_keys):
                pending[key] = value
            elif key in clearable_empty_keys:
                pending.pop(key, None)
            elif value is None and key in explicit_empty_value_keys:
                pending.pop(key, None)
        if not pending:
            return
        pending["updatedAt"] = timezone.now().isoformat()
        pending["updated_at"] = pending["updatedAt"]
        article_system["pending_article_system_setup"] = pending
        config.article_system = sanitize_json_for_postgres(article_system)
        config.save(update_fields=["article_system", "updated_at"])
    except Exception as exc:
        logger.warning("Failed to update pending article system setup for %s: %s", domain, exc)


def _mark_article_system_setup_generation_ready_for_domain(domain: str, *, pr_url="", preview_url="") -> None:
    domain = str(domain or "").strip()
    if not domain:
        return
    try:
        org = Organization.objects.filter(domain=domain).first()
        if not org:
            return
        config = org.content_config
        article_system = sanitize_json_for_postgres(dict(config.article_system or {}))
        pending = sanitize_json_for_postgres(dict(article_system.get("pending_article_system_setup") or {}))
        now = timezone.now().isoformat()
        pending.update(
            {
                "status": "merged",
                "setupStatus": "merged",
                "setup_status": "merged",
                "mergeStatus": "merged",
                "merge_status": "merged",
                "generationReady": True,
                "generation_ready": True,
                "updatedAt": now,
                "updated_at": now,
            }
        )
        if pr_url:
            pending["prUrl"] = pr_url
            pending["pr_url"] = pr_url
        if preview_url:
            pending["previewUrl"] = preview_url
            pending["preview_url"] = preview_url
        article_system["pending_article_system_setup"] = pending
        article_system["generationReady"] = True
        article_system["generation_ready"] = True
        article_system.setdefault("generationReadySource", "setup_pr_merged")
        article_system.setdefault("generation_ready_source", "setup_pr_merged")
        article_system.setdefault("generationReadyAt", now)
        article_system.setdefault("generation_ready_at", now)
        if str(article_system.get("state") or "").strip() not in {
            "existing",
            "ready",
            "detected",
            "registry_driven_seo_ready",
            "article_system_ready",
            "roo_scaffolded",
        }:
            article_system["state"] = "roo_scaffolded"
            article_system["source"] = article_system.get("source") or "setup_pr_merge"
            article_system["confidence"] = article_system.get("confidence") or "high"

        update_fields = ["article_system"]
        config.article_system = sanitize_json_for_postgres(article_system)
        if not config.articles_scaffolded:
            config.articles_scaffolded = True
            update_fields.append("articles_scaffolded")
        if pr_url and config.articles_scaffold_pr_url != pr_url:
            config.articles_scaffold_pr_url = pr_url
            update_fields.append("articles_scaffold_pr_url")
        if preview_url and config.articles_scaffold_preview_url != preview_url:
            config.articles_scaffold_preview_url = preview_url
            update_fields.append("articles_scaffold_preview_url")
        update_fields.append("updated_at")
        config.save(update_fields=update_fields)
    except Exception as exc:
        logger.warning("Failed to mark article system setup ready for %s: %s", domain, exc)


def _article_system_setup_common_callback_fields(*, data: dict, setup_payload: dict, result: dict) -> dict:
    resume_generation = _article_system_setup_resume_generation(data, setup_payload, result)
    fields = {
        "setup_run_id": str(data.get("setup_run_id") or data.get("run_id") or data.get("job_id") or "").strip(),
        "workflow": str(data.get("workflow") or "article_system_setup"),
        "current_step": data.get("current_step") or setup_payload.get("current_step") or result.get("current_step"),
        "failure_step": data.get("failure_step") or setup_payload.get("failure_step") or result.get("failure_step"),
        "resume_generation": resume_generation,
        "is_current_attempt": data.get("is_current_attempt") if data.get("is_current_attempt") is not None else True,
        "updated_at": data.get("updated_at") or setup_payload.get("updated_at") or timezone.now().isoformat(),
    }
    for key in (
        "diagnostics",
        "step_states",
        "directory_dependency_report",
        "directory_static_report",
        "directory_build_report",
        "repair_status",
        "worker_queue",
        "failure_kind",
        "failed_preview_url",
        "failedPreviewUrl",
        "failed_step",
        "failedStep",
        "preview_failure_details",
        "previewFailureDetails",
        "directory_quality_gates",
        "directoryQualityGates",
        "directory_browser_repair",
        "directoryBrowserRepair",
        "directory_visual_style_report",
        "directoryVisualStyleReport",
        "directory_visual_repair",
        "directoryVisualRepair",
        "output_excerpt",
    ):
        value = data.get(key)
        if value is None and isinstance(setup_payload, dict):
            value = setup_payload.get(key)
        if value is None and isinstance(result, dict):
            value = result.get(key)
        if value not in (None, "", {}, []):
            fields[key] = value
    return sanitize_json_for_postgres(fields)


def _sync_article_system_setup_callback_to_run(*, data: dict, event_type: str) -> Optional[ContentFactoryRun]:
    data = sanitize_json_for_postgres(data if isinstance(data, dict) else {})
    event_type = str(sanitize_json_for_postgres(event_type) or "")
    run_id = str(data.get("run_id") or data.get("job_id") or "").strip()
    if not run_id:
        return None

    existing_run = ContentFactoryRun.objects.filter(run_id=run_id).first()
    emitted_at = _callback_event_emitted_at(data)
    if _callback_event_is_stale(existing_run=existing_run, emitted_at=emitted_at):
        logger.info(
            "Ignoring stale article_system_setup callback for run %s: event=%s emitted_at=%s predates last synced event %s",
            run_id,
            event_type,
            emitted_at.isoformat(),
            existing_run.last_event_emitted_at.isoformat(),
        )
        return existing_run
    setup_payload = sanitize_json_for_postgres(
        data.get("article_system_setup") if isinstance(data.get("article_system_setup"), dict) else {}
    )
    live_preview = sanitize_json_for_postgres(
        data.get("live_preview") if isinstance(data.get("live_preview"), dict) else {}
    )
    result = sanitize_json_for_postgres(dict((existing_run.result if existing_run else None) or {}))
    incoming_generation = _article_system_setup_resume_generation(data, setup_payload)
    existing_generation = _article_system_setup_resume_generation(
        result,
        result.get("article_system_setup") if isinstance(result.get("article_system_setup"), dict) else {},
    )
    if existing_generation and incoming_generation < existing_generation:
        logger.info(
            "article_system_setup_callback_ignored_stale_attempt run_id=%s event=%s incoming_generation=%s current_generation=%s",
            run_id,
            event_type,
            incoming_generation,
            existing_generation,
        )
        return existing_run
    common_fields = _article_system_setup_common_callback_fields(data=data, setup_payload=setup_payload, result=result)
    preview_url_value = str(data.get("preview_url") or setup_payload.get("preview_url") or "").strip()
    fallback_preview_url_value = str(
        data.get("fallback_preview_url")
        or setup_payload.get("fallback_preview_url")
        or live_preview.get("fallbackPreviewUrl")
        or live_preview.get("fallback_preview_url")
        or ""
    ).strip()
    live_preview_url_value = str(data.get("live_preview_url") or setup_payload.get("live_preview_url") or "").strip()
    failed_preview_url_value = str(
        data.get("failed_preview_url")
        or data.get("failedPreviewUrl")
        or setup_payload.get("failed_preview_url")
        or setup_payload.get("failedPreviewUrl")
        or live_preview.get("failedPreviewUrl")
        or live_preview.get("failed_preview_url")
        or result.get("failed_preview_url")
        or result.get("failedPreviewUrl")
        or ""
    ).strip()
    failure_kind_value = str(
        data.get("failure_kind")
        or data.get("failureKind")
        or setup_payload.get("failure_kind")
        or setup_payload.get("failureKind")
        or live_preview.get("failureKind")
        or live_preview.get("failure_kind")
        or result.get("failure_kind")
        or result.get("failureKind")
        or ""
    ).strip()
    failed_step_value = str(
        data.get("failed_step")
        or data.get("failedStep")
        or setup_payload.get("failed_step")
        or setup_payload.get("failedStep")
        or live_preview.get("failedPhase")
        or live_preview.get("failed_phase")
        or result.get("failed_step")
        or result.get("failedStep")
        or ""
    ).strip()
    preview_failure_details = (
        data.get("preview_failure_details")
        or data.get("previewFailureDetails")
        or setup_payload.get("preview_failure_details")
        or setup_payload.get("previewFailureDetails")
        or result.get("preview_failure_details")
        or result.get("previewFailureDetails")
    )
    directory_quality_gates = (
        data.get("directory_quality_gates")
        or data.get("directoryQualityGates")
        or setup_payload.get("directory_quality_gates")
        or setup_payload.get("directoryQualityGates")
        or result.get("directory_quality_gates")
        or result.get("directoryQualityGates")
    )
    directory_browser_repair = (
        data.get("directory_browser_repair")
        or data.get("directoryBrowserRepair")
        or setup_payload.get("directory_browser_repair")
        or setup_payload.get("directoryBrowserRepair")
        or result.get("directory_browser_repair")
        or result.get("directoryBrowserRepair")
    )
    directory_visual_style_report = (
        data.get("directory_visual_style_report")
        or data.get("directoryVisualStyleReport")
        or setup_payload.get("directory_visual_style_report")
        or setup_payload.get("directoryVisualStyleReport")
        or result.get("directory_visual_style_report")
        or result.get("directoryVisualStyleReport")
    )
    directory_visual_repair = (
        data.get("directory_visual_repair")
        or data.get("directoryVisualRepair")
        or setup_payload.get("directory_visual_repair")
        or setup_payload.get("directoryVisualRepair")
        or result.get("directory_visual_repair")
        or result.get("directoryVisualRepair")
    )
    current_step_value = str(
        data.get("current_step")
        or data.get("step")
        or setup_payload.get("current_step")
        or setup_payload.get("currentStep")
        or (existing_run.current_step if existing_run else "")
        or "queued"
    ).strip()
    resume_generation = _article_system_setup_resume_generation(data, setup_payload, result)
    live_preview_exact = bool(live_preview.get("exactRender") or live_preview.get("exact_render"))
    if not live_preview_exact and not fallback_preview_url_value:
        candidate_preview = str(live_preview.get("previewUrl") or live_preview.get("preview_url") or "").strip()
        if candidate_preview and (
            live_preview.get("fullSiteBuildSkipped")
            or live_preview.get("full_site_build_skipped")
            or str(live_preview.get("renderConfidence") or live_preview.get("render_confidence") or "").strip().lower() == "fallback"
        ):
            fallback_preview_url_value = candidate_preview
    result.update(
        {
            "status": str(data.get("status") or setup_payload.get("status") or event_type).strip(),
            "event": event_type,
            "run_id": run_id,
            "job_id": str(data.get("job_id") or run_id),
            "workflow": str(data.get("workflow") or "article_system_setup"),
            "domain": str(data.get("domain") or (existing_run.domain if existing_run else "") or ""),
            "github_repo": str(data.get("github_repo") or (existing_run.github_repo if existing_run else "") or ""),
            "parent_run_id": data.get("parent_run_id"),
            "scan_run_id": data.get("scan_run_id") or data.get("parent_run_id"),
            "setup_run_id": run_id,
            "source_setup_run_id": data.get("source_setup_run_id") or data.get("setup_run_id") or run_id,
            "rescan_run_id": data.get("rescan_run_id") or setup_payload.get("rescan_run_id"),
            "merge_status": data.get("merge_status") or setup_payload.get("merge_status"),
            "article_system_setup": setup_payload,
            "pr_url": data.get("pr_url") or setup_payload.get("pr_url"),
            "pr_number": data.get("pr_number") or data.get("prNumber") or setup_payload.get("pr_number") or setup_payload.get("prNumber"),
            "preview_url": preview_url_value,
            "fallback_preview_url": fallback_preview_url_value,
            "failed_preview_url": failed_preview_url_value,
            "failure_kind": failure_kind_value,
            "failed_step": failed_step_value,
            "preview_failure_details": preview_failure_details,
            "directory_quality_gates": directory_quality_gates,
            "directory_browser_repair": directory_browser_repair,
            "directory_visual_style_report": directory_visual_style_report,
            "directory_visual_repair": directory_visual_repair,
            "live_preview_url": live_preview_url_value,
            "approve_url": data.get("approve_url") or setup_payload.get("approve_url"),
            "deny_url": data.get("deny_url") or setup_payload.get("deny_url"),
            "feedback_batch_id": data.get("feedback_batch_id") or result.get("feedback_batch_id"),
            "review_comments_path": data.get("review_comments_path") or result.get("review_comments_path"),
            "livePreview": live_preview,
            "live_preview": live_preview,
            "error_code": data.get("error_code") or setup_payload.get("error_code") or result.get("error_code"),
            "builder_run_url": data.get("builder_run_url") or live_preview.get("builderRunUrl") or live_preview.get("builder_run_url") or result.get("builder_run_url"),
            "failed_phase": data.get("failed_phase") or live_preview.get("failedPhase") or live_preview.get("failed_phase") or result.get("failed_phase"),
            "failed_command": data.get("failed_command") or live_preview.get("failedCommand") or live_preview.get("failed_command") or result.get("failed_command"),
            "log_excerpt": data.get("log_excerpt") or live_preview.get("logExcerpt") or live_preview.get("log_excerpt") or result.get("log_excerpt"),
            "retryable": (
                data.get("retryable")
                if data.get("retryable") is not None
                else setup_payload.get("retryable", result.get("retryable"))
            ),
            "retry_available": (
                data.get("retry_available")
                if data.get("retry_available") is not None
                else setup_payload.get("retry_available", result.get("retry_available"))
            ),
            "stale": data.get("stale") if data.get("stale") is not None else result.get("stale"),
            "stale_reason": data.get("stale_reason") or result.get("stale_reason"),
            "queue_name": data.get("queue_name") or setup_payload.get("queue_name") or result.get("queue_name"),
            "queued_at": data.get("queued_at") or setup_payload.get("queued_at") or result.get("queued_at"),
            "setup_queue": data.get("setup_queue") or result.get("setup_queue"),
            **common_fields,
            "current_step": current_step_value,
            "currentStep": current_step_value,
            "resume_generation": data.get("resume_generation") or setup_payload.get("resume_generation") or result.get("resume_generation"),
            "resumeGeneration": data.get("resumeGeneration") or setup_payload.get("resumeGeneration") or result.get("resumeGeneration"),
            "is_current_attempt": data.get("is_current_attempt") if data.get("is_current_attempt") is not None else setup_payload.get("is_current_attempt", result.get("is_current_attempt")),
            "isCurrentAttempt": data.get("isCurrentAttempt") if data.get("isCurrentAttempt") is not None else setup_payload.get("isCurrentAttempt", result.get("isCurrentAttempt")),
        }
    )

    if event_type == "article_system_setup_progress":
        setup_payload = dict(setup_payload)
        setup_status = str(data.get("status") or setup_payload.get("status") or "running").strip() or "running"
        if setup_status in {"processing", "in_progress"}:
            setup_status = "running"
        result = _clear_article_system_setup_retry_state(
            result,
            current_step=current_step_value,
            resume_generation=resume_generation or None,
        )
        setup_payload.update(common_fields)
        setup_payload["status"] = setup_status
        setup_payload["setup_run_id"] = run_id
        setup_payload["source_setup_run_id"] = result.get("source_setup_run_id") or run_id
        setup_payload["current_step"] = current_step_value
        setup_payload["currentStep"] = current_step_value
        setup_payload["retry_available"] = False
        setup_payload["retryAvailable"] = False
        setup_payload["retryable"] = False
        setup_payload["is_current_attempt"] = True
        setup_payload["isCurrentAttempt"] = True
        setup_payload.pop("error", None)
        setup_payload.pop("error_code", None)
        for key in ARTICLE_SYSTEM_SETUP_FAILURE_METADATA_KEYS:
            setup_payload.pop(key, None)
        result.update(common_fields)
        result["status"] = setup_status
        result["message"] = data.get("message") or result.get("message")
        result["article_system_setup"] = setup_payload
        result.pop("error", None)
        result.pop("error_code", None)
        result.pop("errors", None)
        result.pop("livePreview", None)
        result.pop("live_preview", None)
        for key in ARTICLE_SYSTEM_SETUP_FAILURE_METADATA_KEYS:
            result.pop(key, None)
        result["retry_available"] = False
        result["retryable"] = False
        run_status = ContentFactoryRunStatus.QUEUED if setup_status == "queued" else ContentFactoryRunStatus.RUNNING
        approval_state = ContentFactoryApprovalState.NOT_REQUIRED
        current_step = current_step_value
        error = ""
    elif event_type == "article_system_setup_completed":
        setup_payload = dict(setup_payload)
        setup_payload.update(common_fields)
        setup_payload["status"] = "merged_verifying"
        setup_payload["setup_run_id"] = run_id
        setup_payload["source_setup_run_id"] = result.get("source_setup_run_id") or run_id
        setup_payload["merge_status"] = result.get("merge_status") or "merged"
        if result.get("rescan_run_id"):
            setup_payload["rescan_run_id"] = result.get("rescan_run_id")
        result["status"] = "completed"
        result["merge_status"] = setup_payload["merge_status"]
        result["article_system_setup"] = setup_payload
        run_status = ContentFactoryRunStatus.COMPLETED
        approval_state = ContentFactoryApprovalState.APPROVED
        current_step = "completed"
        error = ""
    elif event_type == "article_system_setup_pr_created":
        setup_payload = dict(setup_payload)
        setup_payload.update(common_fields)
        setup_payload["status"] = "pr_created"
        setup_payload["setup_status"] = "pr_created"
        setup_payload["setupStatus"] = "pr_created"
        setup_payload["setup_run_id"] = run_id
        setup_payload["source_setup_run_id"] = result.get("source_setup_run_id") or run_id
        setup_payload["merge_status"] = result.get("merge_status") or "not_merged"
        setup_payload["mergeStatus"] = setup_payload["merge_status"]
        setup_payload["current_step"] = "create_pull_request"
        setup_payload["currentStep"] = "create_pull_request"
        if result.get("pr_url"):
            setup_payload["pr_url"] = result.get("pr_url")
            setup_payload["prUrl"] = result.get("pr_url")
        if result.get("pr_number") not in (None, ""):
            setup_payload["pr_number"] = result.get("pr_number")
            setup_payload["prNumber"] = result.get("pr_number")
        result["status"] = "setup_pr_created"
        result["setup_status"] = "pr_created"
        result["setupStatus"] = "pr_created"
        result["merge_status"] = setup_payload["merge_status"]
        result["mergeStatus"] = setup_payload["mergeStatus"]
        result["current_step"] = "create_pull_request"
        result["currentStep"] = "create_pull_request"
        result["article_system_setup"] = setup_payload
        run_status = ContentFactoryRunStatus.COMPLETED
        approval_state = ContentFactoryApprovalState.APPROVED
        current_step = "create_pull_request"
        error = ""
    elif event_type == "article_system_setup_preview_failed":
        setup_payload = dict(setup_payload)
        setup_payload.update(common_fields)
        setup_payload["status"] = "preview_failed"
        setup_payload["setup_run_id"] = run_id
        error_code = (
            data.get("error_code")
            or setup_payload.get("error_code")
            or live_preview.get("errorCode")
            or live_preview.get("error_code")
        )
        preview_error = str(
            data.get("error")
            or setup_payload.get("error")
            or live_preview.get("error")
            or "Articles setup preview could not be prepared."
        ).strip()
        setup_payload["error"] = preview_error
        setup_payload["error_code"] = error_code
        if failed_preview_url_value:
            setup_payload["failed_preview_url"] = failed_preview_url_value
            setup_payload["failedPreviewUrl"] = failed_preview_url_value
        if failure_kind_value:
            setup_payload["failure_kind"] = failure_kind_value
            setup_payload["failureKind"] = failure_kind_value
        if failed_step_value:
            setup_payload["failed_step"] = failed_step_value
            setup_payload["failedStep"] = failed_step_value
        if isinstance(preview_failure_details, dict):
            setup_payload["preview_failure_details"] = preview_failure_details
            setup_payload["previewFailureDetails"] = preview_failure_details
        if isinstance(directory_quality_gates, dict):
            setup_payload["directory_quality_gates"] = directory_quality_gates
            setup_payload["directoryQualityGates"] = directory_quality_gates
        if isinstance(directory_browser_repair, dict):
            setup_payload["directory_browser_repair"] = directory_browser_repair
            setup_payload["directoryBrowserRepair"] = directory_browser_repair
        if isinstance(directory_visual_style_report, dict):
            setup_payload["directory_visual_style_report"] = directory_visual_style_report
            setup_payload["directoryVisualStyleReport"] = directory_visual_style_report
        if isinstance(directory_visual_repair, dict):
            setup_payload["directory_visual_repair"] = directory_visual_repair
            setup_payload["directoryVisualRepair"] = directory_visual_repair
        setup_payload.pop("approve_url", None)
        setup_payload.pop("deny_url", None)
        if live_preview:
            live_preview = dict(live_preview)
            live_preview.setdefault("error", preview_error)
            if error_code:
                live_preview.setdefault("errorCode", error_code)
            if failed_preview_url_value:
                live_preview.setdefault("failedPreviewUrl", failed_preview_url_value)
                live_preview.setdefault("failed_preview_url", failed_preview_url_value)
            if failure_kind_value:
                live_preview.setdefault("failureKind", failure_kind_value)
                live_preview.setdefault("failure_kind", failure_kind_value)
            if failed_step_value:
                live_preview.setdefault("failedPhase", failed_step_value)
                live_preview.setdefault("failed_phase", failed_step_value)
        if "retryable" not in setup_payload:
            setup_payload["retryable"] = live_preview.get("retryable", True)
        setup_payload["retry_available"] = bool(
            data.get("retry_available")
            if data.get("retry_available") is not None
            else setup_payload.get("retryable", True)
        )
        result["status"] = "preview_failed"
        result["article_system_setup"] = setup_payload
        result["preview_url"] = ""
        result.pop("approve_url", None)
        result.pop("deny_url", None)
        result["error"] = preview_error
        result["error_code"] = setup_payload.get("error_code")
        if failed_preview_url_value:
            result["failed_preview_url"] = failed_preview_url_value
            result["failedPreviewUrl"] = failed_preview_url_value
        if failure_kind_value:
            result["failure_kind"] = failure_kind_value
            result["failureKind"] = failure_kind_value
        if failed_step_value:
            result["failed_step"] = failed_step_value
            result["failedStep"] = failed_step_value
        if isinstance(preview_failure_details, dict):
            result["preview_failure_details"] = preview_failure_details
            result["previewFailureDetails"] = preview_failure_details
        if isinstance(directory_quality_gates, dict):
            result["directory_quality_gates"] = directory_quality_gates
            result["directoryQualityGates"] = directory_quality_gates
        if isinstance(directory_browser_repair, dict):
            result["directory_browser_repair"] = directory_browser_repair
            result["directoryBrowserRepair"] = directory_browser_repair
        if isinstance(directory_visual_style_report, dict):
            result["directory_visual_style_report"] = directory_visual_style_report
            result["directoryVisualStyleReport"] = directory_visual_style_report
        if isinstance(directory_visual_repair, dict):
            result["directory_visual_repair"] = directory_visual_repair
            result["directoryVisualRepair"] = directory_visual_repair
        result["livePreview"] = live_preview
        result["live_preview"] = live_preview
        result["retryable"] = bool(setup_payload.get("retryable", True))
        result["retry_available"] = bool(setup_payload.get("retry_available", True))
        run_status = ContentFactoryRunStatus.BLOCKED
        approval_state = ContentFactoryApprovalState.NOT_REQUIRED
        current_step = "preview_failed"
        error = preview_error
    elif event_type == "article_system_setup_preview_ready" and not preview_url_value:
        setup_payload = dict(setup_payload)
        setup_payload.update(common_fields)
        setup_payload["status"] = "preview_building"
        setup_payload["setup_run_id"] = run_id
        setup_payload["source_setup_run_id"] = result.get("source_setup_run_id") or run_id
        setup_payload["live_preview_url"] = live_preview_url_value
        setup_payload.pop("preview_url", None)
        setup_payload.pop("approve_url", None)
        setup_payload.pop("deny_url", None)
        for key in ARTICLE_SYSTEM_SETUP_FAILURE_METADATA_KEYS:
            result.pop(key, None)
            setup_payload.pop(key, None)
        result["status"] = "preview_building"
        result["preview_url"] = ""
        result.pop("approve_url", None)
        result.pop("deny_url", None)
        result["article_system_setup"] = setup_payload
        result["livePreview"] = live_preview
        result["live_preview"] = live_preview
        run_status = ContentFactoryRunStatus.RUNNING
        approval_state = ContentFactoryApprovalState.NOT_REQUIRED
        current_step = "start_hosted_preview"
        error = ""
    elif event_type == "article_system_setup_preview_fallback_ready" or (
        event_type == "article_system_setup_preview_ready" and preview_url_value and not live_preview_exact
    ):
        fallback_preview_url_value = fallback_preview_url_value or preview_url_value
        setup_payload = dict(setup_payload)
        setup_payload.update(common_fields)
        setup_payload["status"] = "fallback_ready"
        setup_payload["setup_run_id"] = run_id
        setup_payload["source_setup_run_id"] = result.get("source_setup_run_id") or run_id
        setup_payload["preview_url"] = ""
        setup_payload["fallback_preview_url"] = fallback_preview_url_value
        setup_payload["live_preview_url"] = live_preview_url_value
        setup_payload.pop("approve_url", None)
        setup_payload.pop("deny_url", None)
        for key in ARTICLE_SYSTEM_SETUP_FAILURE_METADATA_KEYS:
            result.pop(key, None)
            setup_payload.pop(key, None)
        warning = str(
            data.get("error")
            or setup_payload.get("error")
            or "Exact articles setup preview is unavailable; the hosted URL is a route-scoped fallback and cannot be approved."
        ).strip()
        setup_payload["error"] = warning
        setup_payload["error_code"] = data.get("error_code") or setup_payload.get("error_code") or "article_system_setup_preview_not_exact"
        setup_payload["retryable"] = data.get("retryable") if data.get("retryable") is not None else setup_payload.get("retryable", True)
        result["status"] = "fallback_ready"
        result["preview_url"] = ""
        result.pop("approve_url", None)
        result.pop("deny_url", None)
        result["fallback_preview_url"] = fallback_preview_url_value
        result["article_system_setup"] = setup_payload
        result["livePreview"] = live_preview
        result["live_preview"] = live_preview
        result["error"] = warning
        result["error_code"] = setup_payload["error_code"]
        result["retryable"] = bool(setup_payload.get("retryable", True))
        result["retry_available"] = bool(setup_payload.get("retryable", True))
        run_status = ContentFactoryRunStatus.BLOCKED
        approval_state = ContentFactoryApprovalState.NOT_REQUIRED
        current_step = "fallback_ready"
        error = warning
    elif event_type == "article_system_setup_manual_merge_required":
        setup_payload = dict(setup_payload)
        setup_payload.update(common_fields)
        setup_payload["status"] = "manual_merge_required"
        setup_payload["setup_run_id"] = run_id
        setup_payload["source_setup_run_id"] = result.get("source_setup_run_id") or run_id
        setup_payload["merge_status"] = result.get("merge_status") or "manual_required"
        result["status"] = "manual_merge_required"
        result["merge_status"] = setup_payload["merge_status"]
        result["article_system_setup"] = setup_payload
        run_status = ContentFactoryRunStatus.BLOCKED
        approval_state = ContentFactoryApprovalState.APPROVED
        current_step = "manual_merge_required"
        error = str(data.get("message") or "Manual merge is required for this article system setup.")
    else:
        setup_payload = dict(setup_payload)
        setup_payload.update(common_fields)
        setup_payload["status"] = setup_payload.get("status") or "preview_ready"
        setup_payload["setup_run_id"] = run_id
        setup_payload["source_setup_run_id"] = result.get("source_setup_run_id") or run_id
        for key in ARTICLE_SYSTEM_SETUP_FAILURE_METADATA_KEYS:
            result.pop(key, None)
            setup_payload.pop(key, None)
        result["article_system_setup"] = setup_payload
        run_status = ContentFactoryRunStatus.AWAITING_APPROVAL
        approval_state = ContentFactoryApprovalState.APPROVAL_REQUIRED
        current_step = "await_review"
        error = ""

    result = sanitize_json_for_postgres(result)
    setup_payload = sanitize_json_for_postgres(setup_payload)
    live_preview = sanitize_json_for_postgres(live_preview)
    error = str(sanitize_json_for_postgres(error) or "")
    run_defaults = sanitize_json_for_postgres(
        {
            "workflow": "article_system_setup",
            "domain": result["domain"],
            "github_repo": result["github_repo"],
            "slack_user_id": str(data.get("slack_user_id") or (existing_run.slack_user_id if existing_run else "") or ""),
            "status": run_status,
            "current_step": current_step,
            "approval_state": approval_state,
            "artifact_root": (existing_run.artifact_root if existing_run else ""),
            "step_order": (existing_run.step_order if existing_run else []) or ["load_context", "validate_plan", "prepare_branch", "create_pull_request", "start_hosted_preview", "await_review"],
            "acceptance_summary": (existing_run.acceptance_summary if existing_run else {}),
            "verification_summary": (existing_run.verification_summary if existing_run else {}),
            "run_request": (existing_run.run_request if existing_run else {}) or {
                "workflow": "article_system_setup",
                "domain": result["domain"],
                "github_repo": result["github_repo"],
                "parent_run_id": data.get("parent_run_id"),
                "scan_run_id": data.get("scan_run_id") or data.get("parent_run_id"),
            },
            "result": result,
            "error": error,
            "resume_available": bool(result.get("retry_available") or (existing_run.resume_available if existing_run else False)),
            # Datetime watermark; passes through sanitize_json_for_postgres untouched.
            "last_event_emitted_at": emitted_at or (existing_run.last_event_emitted_at if existing_run else None),
        }
    )

    run, _created = ContentFactoryRun.objects.update_or_create(
        run_id=run_id,
        defaults=run_defaults,
    )

    parent_run_id = str(data.get("parent_run_id") or data.get("scan_run_id") or "").strip()
    if parent_run_id:
        parent = ContentFactoryRun.objects.filter(run_id=parent_run_id).first()
        if parent:
            parent_result = sanitize_json_for_postgres(dict(parent.result or {}))
            parent_setup = sanitize_json_for_postgres(dict(parent_result.get("article_system_setup") or {}))
            parent_setup.update(setup_payload)
            parent_setup["setup_run_id"] = run_id
            parent_result.update(
                {
                    "setup_run_id": run_id,
                    "source_setup_run_id": result.get("source_setup_run_id") or run_id,
                    "rescan_run_id": result.get("rescan_run_id"),
                    "merge_status": result.get("merge_status"),
                    "article_system_setup": parent_setup,
                    "preview_url": result.get("preview_url"),
                    "fallback_preview_url": result.get("fallback_preview_url"),
                    "failed_preview_url": result.get("failed_preview_url"),
                    "failure_kind": result.get("failure_kind"),
                    "failed_step": result.get("failed_step"),
                    "preview_failure_details": result.get("preview_failure_details"),
                    "directory_quality_gates": result.get("directory_quality_gates"),
                    "directory_browser_repair": result.get("directory_browser_repair"),
                    "directory_visual_style_report": result.get("directory_visual_style_report"),
                    "directory_visual_repair": result.get("directory_visual_repair"),
                    "pr_url": result.get("pr_url"),
                    "pr_number": result.get("pr_number"),
                    "live_preview_url": result.get("live_preview_url"),
                }
            )
            parent.result = sanitize_json_for_postgres(parent_result)
            if parent.status in {ContentFactoryRunStatus.QUEUED, ContentFactoryRunStatus.RUNNING}:
                parent.status = ContentFactoryRunStatus.COMPLETED
                parent.current_step = (
                    "article_system_setup_preview_failed"
                    if event_type == "article_system_setup_preview_failed"
                    else "article_system_setup_preview_fallback_ready"
                    if event_type == "article_system_setup_preview_fallback_ready"
                    else "article_system_setup_pr_created"
                    if event_type == "article_system_setup_pr_created"
                    else "article_system_setup_preview"
                )
            parent.save(update_fields=["status", "result", "current_step", "updated_at"])

    _update_pending_article_system_setup_for_domain(
        result["domain"],
        setupRunId=run_id,
        setup_run_id=run_id,
        status=setup_payload.get("status") or result.get("status"),
        setupStatus=setup_payload.get("status") or result.get("status"),
        setup_status=setup_payload.get("status") or result.get("status"),
        currentStep=current_step,
        current_step=current_step,
        setupCurrentStep=current_step,
        setup_current_step=current_step,
        resumeGeneration=result.get("resume_generation"),
        resume_generation=result.get("resume_generation"),
        isCurrentAttempt=result.get("is_current_attempt"),
        is_current_attempt=result.get("is_current_attempt"),
        retryAvailable=result.get("retry_available"),
        retry_available=result.get("retry_available"),
        error=error,
        errorCode=result.get("error_code") if event_type != "article_system_setup_progress" else "",
        error_code=result.get("error_code") if event_type != "article_system_setup_progress" else "",
        livePreview=result.get("livePreview") if event_type != "article_system_setup_progress" else "",
        live_preview=result.get("live_preview") if event_type != "article_system_setup_progress" else "",
        rescanRunId=result.get("rescan_run_id"),
        rescan_run_id=result.get("rescan_run_id"),
        prUrl=result.get("pr_url"),
        pr_url=result.get("pr_url"),
        prNumber=result.get("pr_number"),
        pr_number=result.get("pr_number"),
        previewUrl=result.get("preview_url"),
        preview_url=result.get("preview_url"),
        fallbackPreviewUrl=result.get("fallback_preview_url"),
        fallback_preview_url=result.get("fallback_preview_url"),
        failedPreviewUrl=result.get("failed_preview_url"),
        failed_preview_url=result.get("failed_preview_url"),
        failureKind=result.get("failure_kind"),
        failure_kind=result.get("failure_kind"),
        failedStep=result.get("failed_step"),
        failed_step=result.get("failed_step"),
        previewFailureDetails=result.get("preview_failure_details"),
        preview_failure_details=result.get("preview_failure_details"),
        directoryQualityGates=result.get("directory_quality_gates"),
        directory_quality_gates=result.get("directory_quality_gates"),
        directoryBrowserRepair=result.get("directory_browser_repair"),
        directory_browser_repair=result.get("directory_browser_repair"),
        directoryVisualStyleReport=result.get("directory_visual_style_report"),
        directory_visual_style_report=result.get("directory_visual_style_report"),
        directoryVisualRepair=result.get("directory_visual_repair"),
        directory_visual_repair=result.get("directory_visual_repair"),
        livePreviewUrl=result.get("live_preview_url"),
        live_preview_url=result.get("live_preview_url"),
        mergeStatus=result.get("merge_status"),
        merge_status=result.get("merge_status"),
    )
    if event_type == "article_system_setup_completed" and str(result.get("merge_status") or "").strip().lower() == "merged":
        _mark_article_system_setup_generation_ready_for_domain(
            result["domain"],
            pr_url=result.get("pr_url") or setup_payload.get("pr_url") or "",
            preview_url=result.get("preview_url") or setup_payload.get("preview_url") or "",
        )
    return run


def _clear_article_system_setup_retry_state(result, *, current_step="", resume_generation=None):
    cleaned = dict(result or {})
    updated_at = timezone.now().isoformat()
    failure_keys = (
        "livePreview",
        "live_preview",
        "error",
        "error_code",
        "errorCode",
        "errors",
        "stale",
        "stale_reason",
        "staleReason",
        "failure_step",
        "failureStep",
        "failed_step",
        "failedStep",
        "failed_phase",
        "failedPhase",
        "failed_command",
        "failedCommand",
        "log_excerpt",
        "logExcerpt",
        "builder_run_url",
        "builderRunUrl",
        *ARTICLE_SYSTEM_SETUP_FAILURE_METADATA_KEYS,
    )
    for key in failure_keys:
        cleaned.pop(key, None)
    if str(cleaned.get("status") or "").strip().lower() in ARTICLE_SYSTEM_SETUP_RETRY_TERMINAL_STATUSES:
        cleaned["status"] = "queued"
    if current_step:
        cleaned["current_step"] = current_step
        cleaned["currentStep"] = current_step
    if resume_generation not in (None, ""):
        cleaned["resume_generation"] = resume_generation
        cleaned["resumeGeneration"] = resume_generation
    cleaned["retry_available"] = False
    cleaned["retryAvailable"] = False
    cleaned["retryable"] = False
    cleaned["is_current_attempt"] = True
    cleaned["isCurrentAttempt"] = True
    cleaned["updated_at"] = updated_at
    cleaned["updatedAt"] = updated_at
    setup_payload = cleaned.get("article_system_setup") if isinstance(cleaned.get("article_system_setup"), dict) else {}
    if setup_payload:
        setup_payload = dict(setup_payload)
        for key in failure_keys:
            setup_payload.pop(key, None)
        if str(setup_payload.get("status") or "").strip().lower() in ARTICLE_SYSTEM_SETUP_RETRY_TERMINAL_STATUSES:
            setup_payload["status"] = "queued"
        if current_step:
            setup_payload["current_step"] = current_step
            setup_payload["currentStep"] = current_step
        if resume_generation not in (None, ""):
            setup_payload["resume_generation"] = resume_generation
            setup_payload["resumeGeneration"] = resume_generation
        setup_payload["retry_available"] = False
        setup_payload["retryAvailable"] = False
        setup_payload["retryable"] = False
        setup_payload["is_current_attempt"] = True
        setup_payload["isCurrentAttempt"] = True
        setup_payload["updated_at"] = updated_at
        setup_payload["updatedAt"] = updated_at
        cleaned["article_system_setup"] = setup_payload
    nested_result = cleaned.get("result") if isinstance(cleaned.get("result"), dict) else {}
    if nested_result:
        cleaned["result"] = _clear_article_system_setup_retry_state(
            nested_result,
            current_step=current_step,
            resume_generation=resume_generation,
        )
    return cleaned


class ContentFactoryCallbackView(APIView):
    """
    Receives callbacks from content-factory for various pipeline events.
    
    POST /api/content-factory/callback
    
    Event types:
    - topic_selection: Research complete, topic selected, awaiting confirmation
    - scan_progress: Non-terminal repository scan milestone update
    - discovery_progress: Non-terminal discovery milestone update
    - article_progress: Non-terminal article milestone update
    - generation_blocked: Non-terminal capacity or verifier block update
    - generation_pr_opened: Draft PR opened as the terminal reviewable outcome
    - article_complete: Article generated and published successfully
    - publish_bundle_ready: Delivery bundle packaged and ready
    - error: Pipeline failed with error
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        data = request.data
        event_type = data.get('event_type') or data.get('event')
        job_id = data.get('job_id')
        domain = data.get('domain', '')

        if not event_type:
            return Response(
                {'error': 'event_type is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not job_id:
            return Response(
                {'error': 'job_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        logger.info(f"Content Factory callback received: event={event_type}, job_id={job_id}, domain={domain}")

        # content-factory stamps each delivery with a unique event_id and
        # retries non-2xx responses from a durable outbox. Claim the event_id
        # before processing so a replay of an already-acknowledged event
        # returns 200 without reprocessing. Payloads from older
        # content-factory versions have no event_id and skip the guard.
        event_id = str(data.get('event_id') or '').strip()
        if event_id and not _claim_callback_event(
            event_id=event_id,
            event_type=str(event_type),
            job_id=str(job_id),
            emitted_at=_callback_event_emitted_at(data),
        ):
            logger.info(
                "Duplicate content-factory callback ignored: event=%s, job_id=%s, event_id=%s",
                event_type,
                job_id,
                event_id,
            )
            return Response(
                {
                    'status': 'duplicate',
                    'message': f'{event_type} callback already processed',
                    'job_id': job_id,
                    'event_id': event_id,
                },
                status=status.HTTP_200_OK,
            )

        try:
            response = self._dispatch_callback_event(data, event_type=event_type, job_id=job_id)
        except Exception as e:
            logger.exception(f"Error processing callback: {e}")
            if event_id:
                _release_callback_event(event_id)
            return Response(
                {'error': 'Internal server error processing callback'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        if event_id and not (200 <= response.status_code < 300):
            # Non-2xx tells the sender to retry; release the claim so the
            # retry is reprocessed instead of deduped.
            _release_callback_event(event_id)
        return response

    def _dispatch_callback_event(self, data, *, event_type, job_id):
        if event_type == 'topic_selection':
            return self._handle_topic_selection(data)
        elif event_type == 'article_complete':
            return self._handle_article_complete(data)
        elif event_type == 'error':
            return self._handle_error(data)
        elif event_type == 'auth_required':
            return self._handle_auth_required(data)
        elif event_type == 'scan_complete':
            return self._handle_scan_complete(data)
        elif event_type in {
            'article_system_setup_progress',
            'article_system_setup_preview_ready',
            'article_system_setup_preview_fallback_ready',
            'article_system_setup_revision_ready',
            'article_system_setup_progress',
            'article_system_setup_preview_failed',
            'article_system_setup_completed',
            'article_system_setup_pr_created',
            'article_system_setup_manual_merge_required',
        }:
            _sync_article_system_setup_callback_to_run(data=data, event_type=event_type)
            return Response(
                {'status': 'received', 'message': f'{event_type} callback processed', 'job_id': job_id},
                status=status.HTTP_200_OK,
            )
        elif event_type == 'website_baseline_complete':
            return self._handle_website_baseline_complete(data)
        elif event_type == 'generation_failed':
            return self._handle_generation_failed(data)
        elif event_type == 'generation_blocked':
            return self._handle_generation_blocked(data)
        elif event_type == 'generation_pr_opened':
            return self._handle_generation_pr_opened(data)
        elif event_type == 'scaffold_complete':
            return self._handle_scaffold_complete(data)
        elif event_type == 'delivery_mode_required':
            return self._handle_delivery_mode_required(data)
        elif event_type == 'draft_pr_created':
            return self._handle_draft_pr_created(data)
        elif event_type == 'preview_ready':
            return self._handle_preview_ready(data)
        elif event_type == 'content_ready':
            return self._handle_content_ready(data)
        elif event_type == 'publish_bundle_ready':
            return self._handle_publish_bundle_ready(data)
        elif event_type == 'article_review_ready':
            return self._handle_article_review_ready(data)
        elif event_type in {
            'article_review_preview_failed',
            'article_review_preview_fallback_ready',
            'article_review_preview_not_available',
        }:
            return self._handle_article_review_preview_event(data, event_type)
        elif event_type == 'discovery_progress':
            return self._handle_discovery_progress(data)
        elif event_type == 'article_progress':
            return self._handle_article_progress(data)
        elif event_type == 'scan_progress':
            return self._handle_scan_progress(data)
        else:
            logger.warning(f"Unknown event_type: {event_type}")
            return Response(
                {'status': 'ignored', 'message': f'Unknown event_type: {event_type}'},
                status=status.HTTP_200_OK
            )

    def _update_content_factory_job(self, *, job_id, domain, slack_user_id, status_value, error_message=None):
        from content_factory.models import ContentFactoryJob

        defaults = {
            'domain': domain or '',
            'slack_user_id': slack_user_id or '',
            'status': status_value,
        }
        if error_message is not None:
            defaults['error_message'] = error_message

        requested_by_slack_user_id = str((self.request.data or {}).get('requested_by_slack_user_id') or '').strip()
        try:
            from integrations.services.notification_adapters import (
                normalize_notification_context,
                resolve_automation_run,
            )

            notification_context = normalize_notification_context((self.request.data or {}).get('notification_context'))
        except Exception:
            notification_context = {}
        max_attempts = 3 if connection.vendor == 'sqlite' else 1
        last_error = None
        for attempt in range(max_attempts):
            try:
                job, _ = ContentFactoryJob.objects.update_or_create(
                    job_id=job_id,
                    defaults=defaults,
                )
                if requested_by_slack_user_id:
                    request_meta = dict(job.request_meta or {})
                    if request_meta.get('requested_by_slack_user_id') != requested_by_slack_user_id:
                        request_meta['requested_by_slack_user_id'] = requested_by_slack_user_id
                        job.request_meta = request_meta
                        job.save(update_fields=['request_meta', 'updated_at'])
                if notification_context:
                    request_meta = dict(job.request_meta or {})
                    request_meta["notification_context"] = notification_context
                    try:
                        automation_run = resolve_automation_run(notification_context)
                    except Exception:
                        automation_run = None
                    if automation_run:
                        request_meta.setdefault("trigger_source", "research_automation")
                        request_meta["automation_id"] = str(automation_run.automation_id)
                        request_meta["automation_run_id"] = str(automation_run.id)
                        if automation_run.request_payload:
                            request_meta.update(
                                {
                                    key: value
                                    for key, value in automation_run.request_payload.items()
                                    if key in {"user_email", "recipient_user_id"}
                                }
                            )
                    job.request_meta = request_meta
                    job.save(update_fields=['request_meta', 'updated_at'])
                return job
            except OperationalError as exc:
                last_error = exc
                if not _is_retryable_sqlite_lock(exc) or attempt == max_attempts - 1:
                    raise
                time.sleep(0.15 * (attempt + 1))
        raise last_error

    def _resolve_job_thread_context(self, *, job, data):
        channel_id = (job.slack_channel_id if job else None) or data.get('slack_channel_id') or ''
        root_message_ts = (
            (job.slack_root_message_ts if job else None)
            or data.get('slack_root_message_ts')
            or data.get('root_message_ts')
            or ''
        )
        thread_ts = (job.slack_thread_ts if job else None) or data.get('slack_thread_ts') or root_message_ts or ''
        if not root_message_ts:
            root_message_ts = thread_ts or ''
        return channel_id, root_message_ts, thread_ts

    def _callback_requested_by_slack_user_id(self, *, job, data):
        request_meta = dict(getattr(job, 'request_meta', {}) or {}) if job else {}
        return str(
            data.get('requested_by_slack_user_id')
            or request_meta.get('requested_by_slack_user_id')
            or ''
        ).strip()

    def _callback_recipient_slack_user_id(self, *, job, data, fallback_slack_user_id=None):
        requested_by_slack_user_id = self._callback_requested_by_slack_user_id(job=job, data=data)
        if requested_by_slack_user_id:
            return requested_by_slack_user_id
        return str(fallback_slack_user_id or '').strip()

    def _send_job_message(self, *, job, data, slack_user_id, text, blocks=None, allow_dm_fallback=True):
        from integrations.services.slack import SlackService

        channel_id, _root_message_ts, thread_ts = self._resolve_job_thread_context(job=job, data=data)
        recipient_slack_user_id = self._callback_recipient_slack_user_id(
            job=job,
            data=data,
            fallback_slack_user_id=slack_user_id,
        )

        if channel_id and thread_ts:
            SlackService.send_message(channel_id, text, blocks=blocks, thread_ts=thread_ts)
            return True
        if allow_dm_fallback and recipient_slack_user_id:
            SlackService.send_dm(recipient_slack_user_id, text, blocks=blocks)
            return True
        return False

    def _callback_dedupe_key(self, data, *, event_name):
        raw_key = str(data.get('dedupe_key') or '').strip()
        if raw_key:
            return raw_key
        pr_token = str(data.get('pr_number') or data.get('pr_url') or data.get('job_id') or 'unknown').strip()
        preview_token = str(data.get('preview_url') or '').strip() or 'no-preview'
        return f"{event_name}:{pr_token}:{preview_token}"

    def _callback_marker_present(self, *, job, bucket, event_name, dedupe_key):
        request_meta = dict(job.request_meta or {})
        bucket_payload = request_meta.get(bucket) or {}
        marker_list = bucket_payload.get(event_name) or []
        return dedupe_key in marker_list

    def _record_callback_marker(self, *, job, bucket, event_name, dedupe_key, extra_request_meta=None):
        request_meta = dict(job.request_meta or {})
        bucket_payload = dict(request_meta.get(bucket) or {})
        marker_list = [str(item) for item in (bucket_payload.get(event_name) or []) if str(item).strip()]
        if dedupe_key not in marker_list:
            marker_list.append(dedupe_key)
        bucket_payload[event_name] = marker_list[-25:]
        request_meta[bucket] = bucket_payload
        if extra_request_meta:
            request_meta.update(extra_request_meta)
        job.request_meta = request_meta
        job.save(update_fields=['request_meta', 'updated_at'])

    def _store_publish_callback_state(self, *, job, data, publish_stage, status_value):
        request_meta = dict(job.request_meta or {})
        request_meta['publish_stage'] = publish_stage
        for field in (
            'route_path',
            'intended_route_path',
            'preview_url',
            'artifact_preview_url',
            'preview_screenshot_urls',
            'preview_surface_kind',
            'preview_content_verified',
            'repo_preview_candidate_url',
            'preview_failure_reason',
            'primary_action',
            'primary_action_url',
            'primary_action_label',
            'primary_action_kind',
            'primary_action_verified',
            'primary_review_url',
            'primary_review_label',
            'review_surfaces',
            'review_surface_kind',
            'bundle_primary_path',
        ):
            if field in data:
                value = data.get(field)
                if field in {'preview_content_verified', 'primary_action_verified'}:
                    request_meta[field] = bool(value)
                elif field in {'preview_screenshot_urls', 'review_surfaces'}:
                    if field == 'preview_screenshot_urls':
                        normalized_values = [
                            str(item).strip()
                            for item in (value or [])
                            if str(item).strip()
                        ]
                    else:
                        normalized_values = [
                            item
                            for item in (value or [])
                            if isinstance(item, dict) and str(item.get('url') or '').strip()
                        ]
                    if normalized_values:
                        request_meta[field] = normalized_values
                    else:
                        request_meta.pop(field, None)
                elif field == 'primary_action':
                    if isinstance(value, dict) and str(value.get('url') or '').strip():
                        request_meta[field] = value
                    else:
                        request_meta.pop(field, None)
                elif value:
                    request_meta[field] = value
                else:
                    request_meta.pop(field, None)
        if 'route_is_live' in data:
            request_meta['route_is_live'] = bool(data.get('route_is_live'))
        if data.get('resolved_delivery_mode'):
            request_meta['resolved_delivery_mode'] = data.get('resolved_delivery_mode')
        if data.get('publish_resolution'):
            request_meta['publish_resolution'] = data.get('publish_resolution')

        update_fields = ['updated_at']
        if job.status != status_value:
            job.status = status_value
            update_fields.append('status')
        if data.get('pr_url') and job.pr_url != data.get('pr_url'):
            job.pr_url = data.get('pr_url')
            update_fields.append('pr_url')
        if job.error_message:
            job.error_message = ''
            update_fields.append('error_message')
        if request_meta != (job.request_meta or {}):
            job.request_meta = request_meta
            update_fields.append('request_meta')
        if len(update_fields) > 1:
            job.save(update_fields=update_fields)

    def _enrich_review_preview_payload(self, data):
        payload = dict(data or {})
        run_id = str(payload.get('run_id') or payload.get('job_id') or '').strip()
        pr_url = str(payload.get('pr_url') or '').strip()
        preview_url = str(payload.get('preview_url') or '').strip()
        artifact_preview_url = str(payload.get('artifact_preview_url') or '').strip()
        route_path = str(payload.get('route_path') or '').strip()
        intended_route_path = str(payload.get('intended_route_path') or '').strip()
        route_is_live = bool(payload.get('route_is_live')) if payload.get('route_is_live') is not None else bool(preview_url)
        preview_surface_kind = str(payload.get('preview_surface_kind') or '').strip()
        review_surface_kind = str(payload.get('review_surface_kind') or '').strip()
        primary_action_url = str(payload.get('primary_action_url') or '').strip()
        primary_action_label = str(payload.get('primary_action_label') or '').strip()
        primary_action_kind = str(payload.get('primary_action_kind') or '').strip()
        primary_review_url = str(payload.get('primary_review_url') or '').strip()
        primary_review_label = str(payload.get('primary_review_label') or '').strip()
        review_bundle_surface_kinds = {'fallback_bundle', 'patch_bundle', 'content_bundle'}
        is_review_bundle = review_surface_kind in review_bundle_surface_kinds

        def _surface_purpose(kind: str) -> str:
            normalized_kind = str(kind or '').strip()
            if normalized_kind in {'pull_request', 'review_pr'}:
                return 'code_review'
            if normalized_kind == 'remote_preview':
                return 'rendered_article_preview'
            if normalized_kind == 'repo_preview_candidate':
                return 'candidate_rendered_preview'
            if normalized_kind == 'artifact_preview':
                return 'preview_evidence'
            if normalized_kind == 'article':
                return 'published_article'
            if normalized_kind == 'intended_article':
                return 'intended_article_url'
            return 'review_surface'

        surfaces_by_key: dict[tuple[str, str], dict[str, Any]] = {}

        def _add_surface(
            *,
            kind: str,
            url: str,
            label: str,
            verified: bool,
            primary: bool = False,
            purpose: str = "",
        ) -> None:
            normalized_url = str(url or '').strip()
            normalized_kind = str(kind or '').strip()
            normalized_label = str(label or '').strip()
            if not normalized_url or not normalized_kind or not normalized_label:
                return
            key = (normalized_kind, normalized_url)
            payload_item = {
                'kind': normalized_kind,
                'label': normalized_label,
                'url': normalized_url,
                'verified': bool(verified),
                'primary': bool(primary),
                'purpose': str(purpose or '').strip() or _surface_purpose(normalized_kind),
            }
            existing = surfaces_by_key.get(key)
            if existing is None:
                surfaces_by_key[key] = payload_item
                return
            existing.update(
                {
                    'label': existing.get('label') or payload_item['label'],
                    'verified': bool(existing.get('verified') or payload_item['verified']),
                    'primary': bool(existing.get('primary') or payload_item['primary']),
                    'purpose': existing.get('purpose') or payload_item['purpose'],
                }
            )

        for item in (payload.get('review_surfaces') or []):
            if not isinstance(item, dict):
                continue
            _add_surface(
                kind=item.get('kind'),
                url=item.get('url'),
                label=item.get('label'),
                verified=bool(item.get('verified')),
                primary=bool(item.get('primary')),
                purpose=item.get('purpose'),
            )

        if not primary_action_url and primary_review_url:
            primary_action_url = primary_review_url
            primary_action_label = primary_action_label or primary_review_label

        is_content_factory_artifact_preview = (
            '/api/content-factory/runs/' in preview_url and '/preview' in preview_url
        )
        if not preview_surface_kind and preview_url and is_content_factory_artifact_preview:
            preview_surface_kind = 'artifact_preview'
        elif not preview_surface_kind and preview_url:
            preview_surface_kind = 'repo_preview'

        preview_content_verified_raw = payload.get('preview_content_verified')
        preview_content_verified = (
            bool(preview_content_verified_raw)
            if preview_content_verified_raw is not None
            else bool(preview_url) and preview_surface_kind == 'artifact_preview'
        )
        primary_action_verified_raw = payload.get('primary_action_verified')
        primary_action_verified = (
            bool(primary_action_verified_raw)
            if primary_action_verified_raw is not None
            else bool(primary_action_url)
        )

        if preview_url and preview_surface_kind == 'artifact_preview':
            artifact_preview_url = artifact_preview_url or preview_url
            preview_url = ''
            route_is_live = False
            if route_path and not intended_route_path:
                intended_route_path = route_path
            route_path = ''
        elif preview_url and preview_surface_kind == 'repo_preview' and preview_content_verified and not is_review_bundle:
            _add_surface(
                kind='remote_preview',
                url=preview_url,
                label='Open Preview',
                verified=True,
                purpose='rendered_article_preview',
            )
        elif preview_url and (is_review_bundle or (preview_surface_kind == 'repo_preview' and not preview_content_verified)):
            candidate_url = str(payload.get('repo_preview_candidate_url') or preview_url).strip()
            if candidate_url:
                payload['repo_preview_candidate_url'] = candidate_url
                _add_surface(
                    kind='repo_preview_candidate',
                    url=candidate_url,
                    label='Open Candidate Preview',
                    verified=False,
                    purpose='candidate_rendered_preview',
                )
            preview_url = ''
            preview_content_verified = False
            route_is_live = False
            if route_path and not intended_route_path:
                intended_route_path = route_path
            route_path = ''

        if not artifact_preview_url:
            for surface in surfaces_by_key.values():
                if surface.get('kind') == 'artifact_preview':
                    artifact_preview_url = str(surface.get('url') or '').strip()
                    break

        if run_id and not artifact_preview_url:
            run, content_package = _load_content_package_for_callback(run_id)
            if run and content_package:
                try:
                    artifact_preview_url = build_content_factory_preview_url(
                        request=self.request,
                        run_id=run.run_id,
                    )
                except Exception as exc:
                    logger.warning("Failed to build artifact preview URL for %s: %s", run_id, exc)

        if artifact_preview_url:
            _add_surface(
                kind='artifact_preview',
                url=artifact_preview_url,
                label='Open Evidence Preview',
                verified=True,
                purpose='preview_evidence',
            )
            if not preview_surface_kind:
                preview_surface_kind = 'artifact_preview'

        if pr_url:
            _add_surface(
                kind='review_pr' if is_review_bundle else 'pull_request',
                url=pr_url,
                label='Open review PR' if is_review_bundle else 'Open PR',
                verified=True,
                purpose='code_review',
            )

        if preview_url:
            _add_surface(
                kind='remote_preview',
                url=preview_url,
                label='Open Preview',
                verified=bool(preview_content_verified),
                purpose='rendered_article_preview',
            )

        repo_preview_candidate_url = str(payload.get('repo_preview_candidate_url') or '').strip()
        if primary_action_url and pr_url:
            if repo_preview_candidate_url and primary_action_url == repo_preview_candidate_url:
                primary_action_url = pr_url
                primary_action_label = 'Open review PR' if is_review_bundle else 'Open PR'
                primary_action_kind = 'review_pr' if is_review_bundle else 'pull_request'
                primary_action_verified = True
            elif artifact_preview_url and primary_action_url == artifact_preview_url and not preview_url:
                primary_action_url = pr_url
                primary_action_label = 'Open review PR' if is_review_bundle else 'Open PR'
                primary_action_kind = 'review_pr' if is_review_bundle else 'pull_request'
                primary_action_verified = True

        if not primary_action_url:
            if preview_url and preview_content_verified and not is_review_bundle:
                primary_action_url = preview_url
                primary_action_label = primary_action_label or 'Open Preview'
                primary_action_kind = primary_action_kind or 'remote_preview'
                primary_action_verified = True
            elif pr_url:
                primary_action_url = pr_url
                primary_action_label = primary_action_label or ('Open review PR' if is_review_bundle else 'Open PR')
                primary_action_kind = primary_action_kind or ('review_pr' if is_review_bundle else 'pull_request')
                primary_action_verified = True
            elif artifact_preview_url:
                primary_action_url = artifact_preview_url
                primary_action_label = primary_action_label or 'Open Evidence Preview'
                primary_action_kind = primary_action_kind or 'artifact_preview'
                primary_action_verified = True
        elif not primary_action_kind:
            if preview_url and primary_action_url == preview_url:
                primary_action_kind = 'remote_preview'
            elif artifact_preview_url and primary_action_url == artifact_preview_url:
                primary_action_kind = 'artifact_preview'
            elif pr_url and primary_action_url == pr_url:
                primary_action_kind = 'review_pr' if is_review_bundle else 'pull_request'
            else:
                primary_action_kind = review_surface_kind or 'review_surface'

        if primary_action_url:
            _add_surface(
                kind=primary_action_kind or 'review_surface',
                url=primary_action_url,
                label=primary_action_label or 'Open Review',
                verified=bool(primary_action_verified),
                primary=True,
            )

        review_surfaces = sorted(
            surfaces_by_key.values(),
            key=lambda item: (
                0 if item.get('primary') else 1,
                0 if item.get('verified') else 1,
                item.get('kind') or '',
                item.get('url') or '',
            ),
        )

        primary_action = {
            'url': primary_action_url,
            'label': primary_action_label or 'Open Review',
            'kind': primary_action_kind or 'review_surface',
            'verified': bool(primary_action_verified),
        } if primary_action_url else None

        payload['preview_url'] = preview_url
        payload['artifact_preview_url'] = artifact_preview_url
        payload['preview_surface_kind'] = preview_surface_kind
        payload['preview_content_verified'] = bool(preview_content_verified)
        payload['route_is_live'] = bool(route_is_live)
        payload['route_path'] = route_path
        payload['intended_route_path'] = intended_route_path
        payload['primary_action'] = primary_action
        payload['primary_action_url'] = primary_action_url
        payload['primary_action_label'] = primary_action_label or ('Open Review' if primary_action_url else '')
        payload['primary_action_kind'] = primary_action_kind or ('review_surface' if primary_action_url else '')
        payload['primary_action_verified'] = bool(primary_action_verified) if primary_action_url else False
        payload['primary_review_url'] = payload['primary_action_url']
        payload['primary_review_label'] = payload['primary_action_label']
        payload['review_surfaces'] = review_surfaces
        return payload

    def _handle_progress_callback(self, data, *, event_name, status_value, stage_titles, response_message):
        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        milestone_key = data.get('milestone_key', '')
        progress_id = data.get('progress_id') or f"{job_id}:{milestone_key}"
        message = data.get('message') or 'Progress update received.'

        try:
            milestone_index = int(data.get('milestone_index') or 0)
        except (TypeError, ValueError):
            milestone_index = 0
        try:
            milestone_count = int(data.get('milestone_count') or 0)
        except (TypeError, ValueError):
            milestone_count = 0

        job = self._update_content_factory_job(
            job_id=job_id,
            domain=domain,
            slack_user_id=slack_user_id,
            status_value=status_value,
        )

        posted_progress_ids = list(job.posted_progress_ids or [])
        if progress_id in posted_progress_ids:
            logger.info("Ignoring duplicate %s callback for %s (%s)", event_name, job_id, progress_id)
            return Response(
                {
                    'status': 'ignored',
                    'reason': 'duplicate_progress_id',
                    'job_id': job_id,
                    'progress_id': progress_id,
                },
                status=status.HTTP_200_OK,
            )

        if milestone_index and milestone_index <= int(job.last_progress_milestone_index or 0):
            logger.info(
                "Ignoring stale %s callback for %s (%s <= %s)",
                event_name,
                job_id,
                milestone_index,
                job.last_progress_milestone_index,
            )
            return Response(
                {
                    'status': 'ignored',
                    'reason': 'stale_milestone',
                    'job_id': job_id,
                    'progress_id': progress_id,
                },
                status=status.HTTP_200_OK,
            )

        channel_id, _root_message_ts, thread_ts = self._resolve_job_thread_context(job=job, data=data)
        if not channel_id or not thread_ts:
            logger.warning("Unable to route %s callback for %s: missing Slack thread context", event_name, job_id)
            return Response(
                {
                    'status': 'ignored',
                    'reason': 'missing_thread_context',
                    'job_id': job_id,
                    'progress_id': progress_id,
                },
                status=status.HTTP_200_OK,
            )

        stage_title = stage_titles.get(milestone_key, 'Progress update')
        fallback_text = f"{stage_title}: {message}"
        summary_text = message
        progress_blocks = build_progress_update_blocks(
            domain=domain,
            stage_title=stage_title,
            message=message,
            milestone_index=milestone_index or None,
            milestone_count=milestone_count or None,
        )

        try:
            job.last_progress_milestone_key = milestone_key
            job.last_progress_updated_at = timezone.now()
            job.still_working_pinged_at = None
            self._send_job_message(
                job=job,
                data=data,
                slack_user_id=slack_user_id,
                text=fallback_text,
                blocks=progress_blocks,
                allow_dm_fallback=False,
            )
            upsert_live_progress_card(
                job,
                data=data,
                summary_text=summary_text,
            )
        except Exception as exc:
            logger.warning("Failed to send %s notification for %s: %s", event_name, job_id, exc)
            return Response(
                {
                    'status': 'processed_with_error',
                    'job_id': job_id,
                    'progress_id': progress_id,
                    'message': str(exc),
                },
                status=status.HTTP_200_OK,
            )

        job.posted_progress_ids = posted_progress_ids + [progress_id]
        if milestone_index:
            job.last_progress_milestone_index = milestone_index
        job.save(
            update_fields=[
                'posted_progress_ids',
                'last_progress_milestone_index',
                'last_progress_milestone_key',
                'last_progress_updated_at',
                'still_working_pinged_at',
                'updated_at',
            ]
        )

        return Response(
            {
                'status': 'received',
                'message': response_message,
                'job_id': job_id,
                'progress_id': progress_id,
            },
            status=status.HTTP_200_OK,
        )

    def _handle_article_progress(self, data):
        return self._handle_progress_callback(
            data,
            event_name='article_progress',
            status_value='generating',
            stage_titles={
                'research_locked': 'Research locked',
                'draft_grounded': 'Draft grounded',
                'finishing_pass': 'Finishing pass',
            },
            response_message='Article progress callback processed',
        )

    def _handle_discovery_progress(self, data):
        return self._handle_progress_callback(
            data,
            event_name='discovery_progress',
            status_value='researching',
            stage_titles={
                'research_started': 'Research started',
                'candidate_pool_ready': 'Candidate pool ready',
            },
            response_message='Discovery progress callback processed',
        )

    def _handle_scan_progress(self, data):
        return self._handle_progress_callback(
            data,
            event_name='scan_progress',
            status_value='researching',
            stage_titles={
                'repo_analysis': 'Inspecting repository',
                'template_generation': 'Generating guidance',
                'finalizing': 'Finalizing scan',
            },
            response_message='Scan progress callback processed',
        )

    def _handle_delivery_mode_required(self, data):
        from integrations.services.notification_adapters import (
            normalize_notification_context,
            send_delivery_mode_required,
        )

        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        job = self._update_content_factory_job(
            job_id=job_id,
            domain=domain,
            slack_user_id=slack_user_id,
            status_value='awaiting_delivery_mode',
        )
        request_meta = dict(job.request_meta or {})
        if data.get('recommended_delivery_mode'):
            request_meta['recommended_delivery_mode'] = data.get('recommended_delivery_mode')
        requested_by_slack_user_id = self._callback_requested_by_slack_user_id(job=job, data=data)
        if requested_by_slack_user_id:
            request_meta['requested_by_slack_user_id'] = requested_by_slack_user_id
        if request_meta != (job.request_meta or {}):
            job.request_meta = request_meta
            job.save(update_fields=['request_meta', 'updated_at'])
        if normalize_notification_context(data.get("notification_context")):
            send_delivery_mode_required(data)

        return Response(
            {
                'status': 'processed',
                'job_id': job_id,
                'delivery_mode': None,
                'recommended_delivery_mode': data.get('recommended_delivery_mode'),
                'auto_selected': False,
            },
            status=status.HTTP_200_OK,
        )

    def _handle_draft_pr_created(self, data):
        data = self._enrich_review_preview_payload(data)
        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        pr_url = str(data.get('pr_url') or '').strip()
        pr_number = data.get('pr_number')
        route_path = str(data.get('route_path') or '').strip()
        preview_url = str(data.get('preview_url') or '').strip()
        preview_screenshot_urls = [
            str(item).strip()
            for item in (data.get('preview_screenshot_urls') or [])
            if str(item).strip()
        ]
        artifact_preview_url = str(data.get('artifact_preview_url') or '').strip()
        review_surface_kind = str(data.get('review_surface_kind') or '').strip()
        primary_action_url = str(data.get('primary_action_url') or '').strip()
        primary_action_label = str(data.get('primary_action_label') or '').strip()
        primary_action_kind = str(data.get('primary_action_kind') or '').strip()
        primary_action_verified = bool(data.get('primary_action_verified')) if data.get('primary_action_verified') is not None else bool(primary_action_url)
        primary_review_url = str(data.get('primary_review_url') or '').strip()
        primary_review_label = str(data.get('primary_review_label') or '').strip()
        review_surfaces = [
            item
            for item in (data.get('review_surfaces') or [])
            if isinstance(item, dict) and str(item.get('url') or '').strip()
        ]
        intended_route_path = str(data.get('intended_route_path') or '').strip()
        bundle_primary_path = str(data.get('bundle_primary_path') or '').strip()
        route_is_live = bool(data.get('route_is_live')) if data.get('route_is_live') is not None else bool(preview_url)
        dedupe_key = self._callback_dedupe_key(data, event_name='draft_pr_created')

        job = self._update_content_factory_job(
            job_id=job_id,
            domain=domain,
            slack_user_id=slack_user_id,
            status_value='generating',
        )
        self._store_publish_callback_state(
            job=job,
            data=data,
            publish_stage='awaiting_preview',
            status_value='generating',
        )

        if self._callback_marker_present(
            job=job,
            bucket='callback_notifications',
            event_name='draft_pr_created',
            dedupe_key=dedupe_key,
        ):
            logger.info("Ignoring duplicate draft_pr_created callback for %s (%s)", job_id, dedupe_key)
            return Response(
                {
                    'status': 'ignored',
                    'reason': 'duplicate_notification',
                    'job_id': job_id,
                    'dedupe_key': dedupe_key,
                },
                status=status.HTTP_200_OK,
            )

        slack_sent = False
        if pr_url:
            blocks = build_draft_pr_created_blocks(
                domain=domain,
                pr_url=pr_url,
                pr_number=pr_number,
                route_path=route_path,
                preview_url=preview_url,
                artifact_preview_url=artifact_preview_url,
                review_surface_kind=review_surface_kind,
                primary_action_url=primary_action_url,
                primary_action_label=primary_action_label,
                primary_action_kind=primary_action_kind,
                primary_action_verified=primary_action_verified,
                primary_review_url=primary_review_url,
                primary_review_label=primary_review_label,
                review_surfaces=review_surfaces,
                route_is_live=route_is_live,
                intended_route_path=intended_route_path,
                bundle_primary_path=bundle_primary_path,
                preview_screenshot_urls=preview_screenshot_urls,
            )
            try:
                slack_sent = self._send_job_message(
                    job=job,
                    data=data,
                    slack_user_id=slack_user_id,
                    text=(
                        f"Review bundle preview ready for {domain}: {primary_action_url or primary_review_url}"
                        if review_surface_kind in {'fallback_bundle', 'patch_bundle', 'content_bundle'} and preview_url and (primary_action_url or primary_review_url)
                        else f"Preview ready for {domain}: {primary_action_url or primary_review_url}"
                        if preview_url and (primary_action_url or primary_review_url)
                        else f"Draft PR ready for {domain}: {pr_url}"
                    ),
                    blocks=blocks,
                    allow_dm_fallback=True,
                )
            except Exception as exc:
                logger.warning("Failed to send draft_pr_created notification for %s: %s", job_id, exc)

        if slack_sent:
            self._record_callback_marker(
                job=job,
                bucket='callback_notifications',
                event_name='draft_pr_created',
                dedupe_key=dedupe_key,
            )

        return Response(
            {
                'status': 'processed',
                'job_id': job_id,
                'pr_url': pr_url or None,
                'slack_sent': slack_sent,
            },
            status=status.HTTP_200_OK,
        )

    def _handle_generation_pr_opened(self, data):
        data = self._enrich_review_preview_payload(data)
        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        pr_url = str(data.get('pr_url') or '').strip()
        pr_number = data.get('pr_number')
        route_path = str(data.get('route_path') or '').strip()
        preview_url = str(data.get('preview_url') or '').strip()
        preview_screenshot_urls = [
            str(item).strip()
            for item in (data.get('preview_screenshot_urls') or [])
            if str(item).strip()
        ]
        artifact_preview_url = str(data.get('artifact_preview_url') or '').strip()
        review_surface_kind = str(data.get('review_surface_kind') or '').strip()
        primary_action_url = str(data.get('primary_action_url') or '').strip()
        primary_action_label = str(data.get('primary_action_label') or '').strip()
        primary_action_kind = str(data.get('primary_action_kind') or '').strip()
        primary_action_verified = bool(data.get('primary_action_verified')) if data.get('primary_action_verified') is not None else bool(primary_action_url)
        primary_review_url = str(data.get('primary_review_url') or '').strip()
        primary_review_label = str(data.get('primary_review_label') or '').strip()
        review_surfaces = [
            item
            for item in (data.get('review_surfaces') or [])
            if isinstance(item, dict) and str(item.get('url') or '').strip()
        ]
        intended_route_path = str(data.get('intended_route_path') or '').strip()
        bundle_primary_path = str(data.get('bundle_primary_path') or '').strip()
        route_is_live = bool(data.get('route_is_live')) if data.get('route_is_live') is not None else bool(preview_url)
        verification_state = str(data.get('verification_state') or '').strip()
        reason_code = str(data.get('reason_code') or '').strip()
        review_required = bool(data.get('review_required', True))
        dedupe_key = self._callback_dedupe_key(data, event_name='generation_pr_opened')
        status_value = 'needs_review' if review_required else 'pr_opened'
        publish_stage = 'needs_review' if review_required else 'pr_opened'
        review_summary = (
            "Review bundle PR opened and ready for human review."
            if review_surface_kind in {'fallback_bundle', 'patch_bundle', 'content_bundle'}
            else "Draft PR opened and ready for human review."
            if review_required
            else "Draft PR opened."
        )

        job = self._update_content_factory_job(
            job_id=job_id,
            domain=domain,
            slack_user_id=slack_user_id,
            status_value=status_value,
            error_message='',
        )
        self._store_publish_callback_state(
            job=job,
            data=data,
            publish_stage=publish_stage,
            status_value=status_value,
        )

        request_meta = dict(job.request_meta or {})
        request_meta.update(
            {
                'review_required': review_required,
                'verification_state': verification_state,
                'reason_code': reason_code,
            }
        )
        if data.get('artifact_links') is not None:
            request_meta['artifact_links'] = data.get('artifact_links')
        if request_meta != (job.request_meta or {}):
            job.request_meta = request_meta
            job.save(update_fields=['request_meta', 'updated_at'])

        upsert_live_progress_card(
            job,
            data=data,
            summary_text=review_summary,
            failed=False,
        )

        if self._callback_marker_present(
            job=job,
            bucket='callback_notifications',
            event_name='generation_pr_opened',
            dedupe_key=dedupe_key,
        ):
            logger.info("Ignoring duplicate generation_pr_opened callback for %s (%s)", job_id, dedupe_key)
            return Response(
                {
                    'status': 'ignored',
                    'reason': 'duplicate_notification',
                    'job_id': job_id,
                    'dedupe_key': dedupe_key,
                },
                status=status.HTTP_200_OK,
            )

        slack_sent = False
        if pr_url:
            blocks = build_draft_pr_created_blocks(
                domain=domain,
                pr_url=pr_url,
                pr_number=pr_number,
                route_path=route_path,
                preview_url=preview_url,
                artifact_preview_url=artifact_preview_url,
                review_surface_kind=review_surface_kind,
                primary_action_url=primary_action_url,
                primary_action_label=primary_action_label,
                primary_action_kind=primary_action_kind,
                primary_action_verified=primary_action_verified,
                primary_review_url=primary_review_url,
                primary_review_label=primary_review_label,
                review_surfaces=review_surfaces,
                route_is_live=route_is_live,
                intended_route_path=intended_route_path,
                bundle_primary_path=bundle_primary_path,
                preview_screenshot_urls=preview_screenshot_urls,
            )
            try:
                slack_sent = self._send_job_message(
                    job=job,
                    data=data,
                    slack_user_id=slack_user_id,
                    text=(
                        f"Review bundle preview ready for {domain}: {primary_action_url or primary_review_url}"
                        if review_surface_kind in {'fallback_bundle', 'patch_bundle', 'content_bundle'} and preview_url and (primary_action_url or primary_review_url)
                        else f"Review bundle ready for {domain}: {pr_url}"
                        if review_surface_kind in {'fallback_bundle', 'patch_bundle', 'content_bundle'}
                        else f"Preview ready for review for {domain}: {primary_action_url or primary_review_url}"
                        if preview_url and (primary_action_url or primary_review_url)
                        else f"Draft PR ready for review for {domain}: {pr_url}"
                        if review_required
                        else f"Draft PR opened for {domain}: {pr_url}"
                    ),
                    blocks=blocks,
                    allow_dm_fallback=True,
                )
            except Exception as exc:
                logger.warning("Failed to send generation_pr_opened notification for %s: %s", job_id, exc)

        if slack_sent:
            self._record_callback_marker(
                job=job,
                bucket='callback_notifications',
                event_name='generation_pr_opened',
                dedupe_key=dedupe_key,
            )

        return Response(
            {
                'status': 'processed',
                'job_id': job_id,
                'pr_url': pr_url or None,
                'review_required': review_required,
                'job_status': status_value,
                'slack_sent': slack_sent,
            },
            status=status.HTTP_200_OK,
        )

    def _handle_preview_ready(self, data):
        from integrations.services.article_generation import ArticleGenerationError, publish_article

        data = self._enrich_review_preview_payload(data)
        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        pr_url = str(data.get('pr_url') or '').strip()
        preview_url = str(data.get('preview_url') or '').strip()
        preview_screenshot_urls = [
            str(item).strip()
            for item in (data.get('preview_screenshot_urls') or [])
            if str(item).strip()
        ]
        pr_number = data.get('pr_number')
        route_path = str(data.get('route_path') or '').strip()
        artifact_preview_url = str(data.get('artifact_preview_url') or '').strip()
        primary_action_url = str(data.get('primary_action_url') or '').strip()
        primary_action_label = str(data.get('primary_action_label') or '').strip()
        primary_action_kind = str(data.get('primary_action_kind') or '').strip()
        primary_action_verified = bool(data.get('primary_action_verified')) if data.get('primary_action_verified') is not None else bool(primary_action_url)
        primary_review_url = str(data.get('primary_review_url') or '').strip()
        primary_review_label = str(data.get('primary_review_label') or '').strip()
        review_surfaces = [
            item
            for item in (data.get('review_surfaces') or [])
            if isinstance(item, dict) and str(item.get('url') or '').strip()
        ]
        route_is_live = bool(data.get('route_is_live')) if data.get('route_is_live') is not None else bool(preview_url)
        dedupe_key = self._callback_dedupe_key(data, event_name='preview_ready')

        job = self._update_content_factory_job(
            job_id=job_id,
            domain=domain,
            slack_user_id=slack_user_id,
            status_value='awaiting_approval',
        )
        self._store_publish_callback_state(
            job=job,
            data=data,
            publish_stage='preview_ready',
            status_value='awaiting_approval',
        )

        notification_sent = False
        if not self._callback_marker_present(
            job=job,
            bucket='callback_notifications',
            event_name='preview_ready',
            dedupe_key=dedupe_key,
        ) and pr_url and preview_url:
            blocks = build_preview_ready_blocks(
                domain=domain,
                pr_url=pr_url,
                preview_url=preview_url,
                pr_number=pr_number,
                route_path=route_path,
                primary_action_url=primary_action_url,
                primary_action_label=primary_action_label,
                primary_action_kind=primary_action_kind,
                primary_action_verified=primary_action_verified,
                primary_review_url=primary_review_url,
                primary_review_label=primary_review_label,
                review_surfaces=review_surfaces,
                artifact_preview_url=artifact_preview_url,
                route_is_live=route_is_live,
                preview_screenshot_urls=preview_screenshot_urls,
            )
            try:
                notification_sent = self._send_job_message(
                    job=job,
                    data=data,
                    slack_user_id=slack_user_id,
                    text=f"Preview ready for {domain}: {primary_action_url or primary_review_url or preview_url}",
                    blocks=blocks,
                    allow_dm_fallback=True,
                )
            except Exception as exc:
                logger.warning("Failed to send preview_ready notification for %s: %s", job_id, exc)
            if notification_sent:
                self._record_callback_marker(
                    job=job,
                    bucket='callback_notifications',
                    event_name='preview_ready',
                    dedupe_key=dedupe_key,
                )

        if self._callback_marker_present(
            job=job,
            bucket='callback_actions',
            event_name='preview_ready_auto_approve',
            dedupe_key=dedupe_key,
        ):
            update_fields = ['updated_at']
            if job.status != 'generating':
                job.status = 'generating'
                update_fields.append('status')
            if job.error_message:
                job.error_message = ''
                update_fields.append('error_message')
            request_meta = dict(job.request_meta or {})
            if request_meta.get('publish_stage') != 'auto_approved':
                request_meta['publish_stage'] = 'auto_approved'
                job.request_meta = request_meta
                update_fields.append('request_meta')
            if len(update_fields) > 1:
                job.save(update_fields=update_fields)
            logger.info("Ignoring duplicate preview_ready auto-approve for %s (%s)", job_id, dedupe_key)
            return Response(
                {
                    'status': 'processed',
                    'job_id': job_id,
                    'auto_approved': True,
                    'deduped_auto_approve': True,
                    'slack_sent': notification_sent,
                },
                status=status.HTTP_200_OK,
            )

        try:
            result = publish_article(job_id, slack_user_id=slack_user_id, domain=domain)
            job.status = 'generating'
            job.error_message = ''
            update_fields = ['status', 'error_message', 'updated_at']
            if pr_url and job.pr_url != pr_url:
                job.pr_url = pr_url
                update_fields.append('pr_url')
            job.save(update_fields=update_fields)
            self._record_callback_marker(
                job=job,
                bucket='callback_actions',
                event_name='preview_ready_auto_approve',
                dedupe_key=dedupe_key,
                extra_request_meta={'publish_stage': 'auto_approved'},
            )
            logger.info("Auto-approved preview for job %s", job_id)
            return Response(
                {
                    'status': 'processed',
                    'job_id': job_id,
                    'auto_approved': True,
                    'slack_sent': notification_sent,
                    'cf_response': result,
                },
                status=status.HTTP_200_OK,
            )
        except ArticleGenerationError as exc:
            logger.warning("Failed to auto-approve preview for %s: %s", job_id, exc)
            job.error_message = str(exc)
            job.save(update_fields=['error_message', 'updated_at'])
            return Response(
                {
                    'status': 'deferred',
                    'job_id': job_id,
                    'message': str(exc),
                },
                status=status.HTTP_200_OK,
            )

    def _handle_content_ready(self, data):
        from integrations.services.notification_adapters import (
            normalize_notification_context,
            send_content_ready,
        )
        from integrations.services.slack import SlackService

        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        title = data.get('title') or data.get('topic') or 'Untitled article'

        job = self._update_content_factory_job(
            job_id=job_id,
            domain=domain,
            slack_user_id=slack_user_id,
            status_value='completed',
            error_message='',
        )
        request_meta = dict(job.request_meta or {})
        request_meta['publish_stage'] = 'content_ready'
        request_meta['publish_pr_url'] = reverse('content_job_publish_pr', args=[job_id])
        if data.get('promote_bundle_url'):
            request_meta['promote_bundle_url'] = data.get('promote_bundle_url')
        if data.get('publish_pr_url'):
            request_meta['source_publish_pr_url'] = data.get('publish_pr_url')
        if request_meta != (job.request_meta or {}):
            job.request_meta = request_meta
            job.save(update_fields=['request_meta', 'updated_at'])

        logger.info("Content-only article complete for job %s (%s)", job_id, domain)

        upsert_live_progress_card(
            job,
            data=data,
            summary_text="Article content is ready.",
        )

        callback_content_package = data.get("content_package")
        run = None
        content_package = callback_content_package if isinstance(callback_content_package, dict) else None
        if not content_package:
            run, content_package = _load_content_package_for_callback(job_id)
        preview_url = ""
        if content_package and not run:
            run = ContentFactoryRun.objects.filter(run_id=job_id).first()
        if content_package and run:
            try:
                preview_url = build_content_factory_preview_url(
                    request=self.request,
                    run_id=(run.run_id if run else job_id),
                )
            except Exception as exc:
                logger.warning("Failed to build preview URL for %s: %s", job_id, exc)
        else:
            logger.warning(
                "Content-only article ready for %s but durable content_package was unavailable after retries.",
                job_id,
            )
            content_package = {
                "title": title,
                "meta_description": data.get("meta_description") or "",
                "hero_image": data.get("hero_image") or {},
                "inline_images": data.get("inline_images") or [],
                "references": [],
                "article_json": {},
            }

        recipient_slack_user_id = self._callback_recipient_slack_user_id(
            job=job,
            data=data,
            fallback_slack_user_id=slack_user_id,
        )
        requested_by_slack_user_id = self._callback_requested_by_slack_user_id(job=job, data=data)
        notification_context = normalize_notification_context(data.get("notification_context"))

        if notification_context:
            send_content_ready(data)
        elif recipient_slack_user_id:
            channel_id, _root_message_ts, thread_ts = self._resolve_job_thread_context(job=job, data=data)
            publish_button_value = None
            if job_id and channel_id and thread_ts:
                publish_button_value = {
                    "job_id": job_id,
                    "domain": domain,
                    "slack_user_id": slack_user_id,
                    "requested_by_slack_user_id": requested_by_slack_user_id or recipient_slack_user_id,
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                }
            blocks = build_content_ready_blocks(
                domain=domain,
                content_package=content_package,
                preview_url=preview_url,
                publish_button_value=publish_button_value,
            )
            fallback_text = f"Article content ready for {domain}"
            try:
                if channel_id and thread_ts:
                    sent, _message_ts = SlackService.send_message(
                        channel_id,
                        fallback_text,
                        blocks=blocks,
                        thread_ts=thread_ts,
                    )
                    if sent and content_package:
                        for message in build_content_thread_messages(content_package):
                            SlackService.send_message(
                                channel_id,
                                message["text"],
                                blocks=message.get("blocks"),
                                thread_ts=thread_ts,
                            )
                else:
                    SlackService.send_dm(
                        recipient_slack_user_id,
                        fallback_text,
                        blocks=blocks,
                    )
            except Exception as exc:
                logger.warning(f"Failed to send content_ready notification to {recipient_slack_user_id}: {exc}")

        return Response(
            {
                'status': 'received',
                'message': 'Content ready callback processed',
                'job_id': job_id,
            },
            status=status.HTTP_200_OK,
        )

    def _handle_publish_bundle_ready(self, data):
        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        title = data.get('title') or data.get('topic') or 'Untitled article'
        publish_resolution = data.get('publish_resolution') or 'publish_bundle'
        suggested_target_path = (
            data.get('suggested_target_path')
            or data.get('primary_artifact_path')
            or data.get('article_markdown_path')
        )
        route_path = data.get('route_path')
        manual_apply_guidance = data.get('manual_apply_guidance') or []

        job = self._update_content_factory_job(
            job_id=job_id,
            domain=domain,
            slack_user_id=slack_user_id,
            status_value='completed',
            error_message='',
        )

        logger.info("Publish bundle ready for job %s (%s)", job_id, domain)

        upsert_live_progress_card(
            job,
            data=data,
            summary_text="Publish bundle is ready.",
        )

        if slack_user_id:
            details = []
            if route_path:
                details.append(f"*Route:* `{route_path}`")
            if suggested_target_path:
                details.append(f"*Target path:* `{suggested_target_path}`")
            if manual_apply_guidance:
                details.append(
                    "*Next step:* " + " ".join(str(item).strip() for item in manual_apply_guidance[:2] if str(item).strip())
                )
            details_text = "\n".join(details)
            if details_text:
                details_text = f"\n\n{details_text}"

            text = (
                f"✅ *Publish bundle ready* for {domain}\n\n"
                f"*{title}*\n\n"
                f"The article is packaged for `{publish_resolution}` delivery.{details_text}"
            )
            try:
                self._send_job_message(
                    job=job,
                    data=data,
                    slack_user_id=slack_user_id,
                    text=f"Publish bundle ready for {domain}",
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": text,
                            },
                        }
                    ],
                )
            except Exception as exc:
                logger.warning(f"Failed to send publish_bundle_ready notification to {slack_user_id}: {exc}")

        return Response(
            {
                'status': 'received',
                'message': 'Publish bundle ready callback processed',
                'job_id': job_id,
            },
            status=status.HTTP_200_OK,
        )

    def _handle_article_review_ready(self, data):
        data = self._enrich_review_preview_payload(data)
        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')

        job = self._update_content_factory_job(
            job_id=job_id,
            domain=domain,
            slack_user_id=slack_user_id,
            status_value='completed',
            error_message='',
        )
        request_meta = dict(job.request_meta or {})
        request_meta['publish_stage'] = 'article_review_ready'
        for field in (
            'live_preview_url',
            'component_manifest_path',
            'article_component_manifest_path',
            'route_path',
            'primary_artifact_path',
        ):
            if data.get(field):
                request_meta[field] = data.get(field)
        if request_meta != (job.request_meta or {}):
            job.request_meta = request_meta
            job.save(update_fields=['request_meta', 'updated_at'])

        logger.info("Article review draft ready for job %s (%s)", job_id, domain)
        return Response(
            {
                'status': 'received',
                'message': 'Article review ready callback processed',
                'job_id': job_id,
            },
            status=status.HTTP_200_OK,
        )

    def _handle_auth_required(self, data):
        """
        Handle 'auth_required' event: notify user to re-authenticate.
        Attempts automatic token refresh first.
        """
        from content_factory.models import ContentFactoryJob
        from integrations.services.github import refresh_github_token
        from integrations.services.article_generation import trigger_article_generation, confirm_topic

        job_id = data.get('job_id')
        slack_user_id = data.get('slack_user_id')
        domain = data.get('domain')
        error_message = data.get('message') or data.get('error_message') or data.get('error')
        github_repo = data.get('github_repo')
        reason_code = data.get('reason_code')
        workflow = data.get('workflow')

        logger.info(
            "Received auth_required callback for job %s (user %s, workflow=%s, repo=%s, reason=%s)",
            job_id,
            slack_user_id,
            workflow,
            github_repo,
            reason_code,
        )

        # Update job status
        job, created = ContentFactoryJob.objects.update_or_create(
            job_id=job_id,
            defaults={
                'domain': domain,
                'slack_user_id': slack_user_id,
                'status': 'auth_required',
                'error_message': error_message,
            }
        )
        requested_by_slack_user_id = self._callback_requested_by_slack_user_id(job=job, data=data)
        recipient_slack_user_id = self._callback_recipient_slack_user_id(
            job=job,
            data=data,
            fallback_slack_user_id=slack_user_id,
        )
        if requested_by_slack_user_id:
            request_meta = dict(job.request_meta or {})
            if request_meta.get('requested_by_slack_user_id') != requested_by_slack_user_id:
                request_meta['requested_by_slack_user_id'] = requested_by_slack_user_id
                job.request_meta = request_meta
                job.save(update_fields=['request_meta', 'updated_at'])

        # 1. Attempt Automatic Token Refresh
        refreshed = False
        if slack_user_id:
            try:
                logger.info(f"Attempting automatic GitHub token refresh for {slack_user_id}")
                refresh_github_token(slack_user_id)
                logger.info(f"Successfully refreshed GitHub token for {slack_user_id}")
                refreshed = True
            except Exception as e:
                logger.warning(f"Automatic token refresh failed for {slack_user_id}: {e}")

        # 2. Retry the Job if Refreshed
        if refreshed:
            try:
                # Scenario A: Initial Generation (Phase 1)
                if job.request_meta:
                    logger.info(f"Retrying article generation for job {job_id}")
                    # Reuse request_meta which contains the original article_request
                    trigger_article_generation(slack_user_id, job.request_meta)
                    return Response({
                        'status': 'retried', 
                        'job_id': job_id, 
                        'message': 'Token refreshed and job retried'
                    }, status=status.HTTP_200_OK)
                
                # Scenario B: Topic Confirmation (Phase 2)
                elif job.selected_keyword:
                     logger.info(f"Retrying topic confirmation for job {job_id}")
                     confirm_topic(
                         domain=domain,
                         confirmed_keyword=job.selected_keyword,
                         slack_user_id=slack_user_id,
                         requested_by_slack_user_id=requested_by_slack_user_id or None,
                         slack_channel_id=job.slack_channel_id,
                         slack_thread_ts=job.slack_thread_ts,
                         slack_root_message_ts=job.slack_root_message_ts or job.slack_thread_ts,
                     )
                     return Response({
                         'status': 'retried', 
                         'job_id': job_id, 
                         'message': 'Token refreshed and job retried'
                     }, status=status.HTTP_200_OK)
                
                else:
                    logger.warning(f"Could not retry job {job_id} - no request metadata found")
                    # If we can't retry, we still notify the user, 
                    # but maybe we should update status to 'auth_refreshed_manual_retry_needed'?
                    
            except Exception as e:
                logger.error(f"Failed to retry job {job_id} after token refresh: {e}")
                # Fallthrough to manual notification
        
        # 3. Fallback: Notify user via Slack (Manual Re-auth)
        try:
             self._send_auth_required_notification(
                 recipient_slack_user_id,
                 domain,
                 error_message,
                 job_id,
                 effective_slack_user_id=slack_user_id,
                 github_repo=github_repo,
                 reason_code=reason_code,
             )
        except Exception as e:
             logger.error(f"Failed to send auth_required notification: {e}")
             # Return success anyway to avoid crashing the caller (Content Factory)
             # The job status is already updated in DB so we can track it.
             return Response({'status': 'processed_with_error', 'error': str(e)}, status=status.HTTP_200_OK)

        return Response({'status': 'processed', 'job_id': job_id}, status=status.HTTP_200_OK)

    def _send_auth_required_notification(
        self,
        slack_user_id,
        domain,
        error_message,
        job_id,
        *,
        effective_slack_user_id=None,
        github_repo=None,
        reason_code=None,
    ):
        from integrations.services.slack import SlackService

        try:
            recipient_slack_user_id = str(slack_user_id or '').strip()
            effective_slack_user_id = str(effective_slack_user_id or recipient_slack_user_id or '').strip()
            delegated_request = bool(
                recipient_slack_user_id
                and effective_slack_user_id
                and recipient_slack_user_id != effective_slack_user_id
            )
            if delegated_request:
                text = (
                    f"GitHub auth for <@{effective_slack_user_id}> isn't available for {domain}. "
                    "Ask them to reconnect GitHub, then retry the delegated run."
                )
                SlackService.send_dm(recipient_slack_user_id, text)
                return

            auth_url = _content_factory_github_auth_url(
                slack_user_id=recipient_slack_user_id,
                domain=domain,
            )

            text = f"⚠️ GitHub Authentication Failed for {domain}"
            repo_line = f"\n*Repository:* `{github_repo}`" if github_repo else ""
            reason_line = f"\n*Reason:* {reason_code}" if reason_code else ""
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "⚠️ GitHub Authentication Failed",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"The content pipeline could not access your repository for *{domain}*."
                            f"{repo_line}{reason_line}\n\n*Error:* {error_message}"
                        ),
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "🔐 Re-authenticate GitHub",
                                "emoji": True
                            },
                            "style": "primary",
                            "url": auth_url
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "❌ Cancel",
                                "emoji": True
                            },
                            "style": "danger",
                            "action_id": "cancel_auth_required", # We might not handle this yet, but good practice
                            "value": job_id
                        }
                    ]
                }
            ]

            SlackService.send_dm(recipient_slack_user_id, text, blocks=blocks)
            
        except Exception as e:
            logger.error(f"Error constructing/sending Slack notification: {e}")
            raise

    def _handle_website_baseline_complete(self, data):
        """Persist website_baseline callback results for Vibe Marketing bootstrap."""
        job_id = str(data.get("run_id") or data.get("job_id") or "").strip()
        domain = str(data.get("domain") or "").strip().lower().removeprefix("www.")
        baseline = data.get("baseline") if isinstance(data.get("baseline"), dict) else {}
        organization = Organization.objects.filter(domain__iexact=domain).first()
        if not organization and domain:
            organization = Organization.objects.filter(domain__iendswith=domain).first()
        if not organization:
            return Response(
                {"status": "ignored", "message": "No organization matched website baseline domain."},
                status=status.HTTP_200_OK,
            )

        run, _created = ContentFactoryRun.objects.update_or_create(
            run_id=job_id or f"website-baseline-{organization.id}-{int(time.time())}",
            defaults={
                "workflow": "website_baseline",
                "domain": organization.domain,
                "github_repo": "",
                "slack_user_id": data.get("slack_user_id") or "",
                "status": ContentFactoryRunStatus.COMPLETED,
                "current_step": "finalize",
                "run_request": data.get("request") or {},
                "result": {
                    "status": "completed",
                    "workflow": "website_baseline",
                    "domain": organization.domain,
                    "baseline": baseline,
                    "warnings": data.get("warnings") or [],
                },
                "error": "",
            },
        )
        collected_at = _parse_optional_datetime(baseline.get("collectedAt") or baseline.get("collected_at")) or timezone.now()
        summary = baseline.get("summary") or ""
        if not isinstance(summary, dict):
            summary = {"text": str(summary or "")}
        try:
            overall_score = int(baseline.get("overallScore")) if baseline.get("overallScore") is not None else None
        except (TypeError, ValueError):
            overall_score = None
        WebsiteBaselineSnapshot.objects.update_or_create(
            organization=organization,
            run_id=run.run_id,
            defaults={
                "domain": organization.domain,
                "status": "completed",
                "collected_at": collected_at,
                "overall_score": overall_score,
                "summary": summary,
                "metrics": baseline.get("metrics") if isinstance(baseline.get("metrics"), dict) else {},
                "source_status": baseline.get("sourceStatus") if isinstance(baseline.get("sourceStatus"), dict) else {},
                "recommendations": baseline.get("recommendations") if isinstance(baseline.get("recommendations"), list) else [],
                "raw_payload": baseline,
            },
        )
        return Response({"status": "success", "message": "Website baseline callback processed"}, status=status.HTTP_200_OK)

    def _handle_scan_complete(self, data):
        """Handle scan_complete event from content-factory."""
        import json as _json
        from content_factory.models import ContentFactoryJob, OrganizationContentConfig
        from organizations.models import Organization
        from integrations.services.slack import SlackService

        job_id = data.get('job_id')
        run_id = data.get('run_id') or job_id
        workflow = data.get('workflow') or 'repo_scan'
        scaffold_queued = bool(data.get('scaffold_queued'))
        scaffold_job_id = data.get('scaffold_job_id') or ''
        requested_action = str(data.get('requested_action') or '').strip()
        scaffold_status = str(data.get('scaffold_status') or '').strip()
        approve_url = str(data.get('approve_url') or '').strip()
        deny_url = str(data.get('deny_url') or '').strip()
        approval_required = (
            requested_action == 'scaffold_publish_route'
            and scaffold_status == 'approval_required'
        )
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        components_generated = data.get('components_generated', False)
        components_count = data.get('components_count', 0)
        component_names = data.get('component_names', [])
        pillar_count = data.get('pillar_count', 0)
        pillar_names = data.get('pillar_names', [])
        publish_targets = data.get('publish_targets') if isinstance(data.get('publish_targets'), list) else []
        default_publish_target_id = data.get('default_publish_target_id')

        # Update job record if one exists
        job = ContentFactoryJob.objects.filter(job_id=job_id).first()
        if job:
            if job.status in {'cancelled', 'denied'}:
                logger.info(
                    "Ignoring scan_complete job update for terminal job: job_id=%s status=%s",
                    job_id,
                    job.status,
                )
                _sync_scan_callback_to_run(data=data, approval_required=approval_required)
                return Response(
                    {"status": "ignored", "message": "Scan run is terminal locally."},
                    status=status.HTTP_200_OK,
                )

            request_meta = dict(job.request_meta or {})
            request_meta.update(
                {
                    'type': request_meta.get('type') or 'scan',
                    'run_id': run_id,
                    'requested_action': requested_action,
                    'scaffold_status': scaffold_status,
                    'approve_url': approve_url,
                    'deny_url': deny_url,
                    'scaffold_plan': data.get('scaffold_plan') or request_meta.get('scaffold_plan'),
                    'tech_stack': data.get('tech_stack') or request_meta.get('tech_stack') or {},
                    'repo_profile': data.get('repo_profile') or request_meta.get('repo_profile') or {},
                    'repository_classification': (
                        data.get('repository_classification')
                        or request_meta.get('repository_classification')
                        or {}
                    ),
                }
            )
            job.status = 'awaiting_confirmation' if approval_required else 'completed'
            job.request_meta = request_meta
            job.save(update_fields=['status', 'request_meta', 'updated_at'])

        _sync_scan_callback_to_run(data=data, approval_required=approval_required)

        channel_id, _root_message_ts, thread_ts = self._resolve_job_thread_context(job=job, data=data)

        logger.info(
            "Scan complete for %s: run_id=%s workflow=%s components_generated=%s count=%s pillars=%s scaffold_queued=%s scaffold_job_id=%s",
            domain,
            run_id,
            workflow,
            components_generated,
            components_count,
            pillar_count,
            scaffold_queued,
            scaffold_job_id,
        )

        # Persist and resolve article-system readiness for messaging and auto-resume.
        has_pillars = False
        article_system = normalize_article_system(data.get('article_system'))
        setup_pending_after_scan = False
        try:
            from integrations.utils import normalize_domain

            org = Organization.objects.get(domain=normalize_domain(domain))
            config = org.content_config
            has_pillars = bool((config.pillar_strategy or {}).get('pillars'))
            raw_article_system = dict(config.article_system or {})
            pending_setup = dict(raw_article_system.get('pending_article_system_setup') or {})
            scan_head_sha = str(data.get('repo_head_sha') or data.get('commit_sha') or '').strip()
            scan_default_branch = str(data.get('default_branch') or data.get('defaultBranch') or '').strip()
            scan_completed_at = _parse_optional_datetime(data.get('scan_completed_at')) or timezone.now()
            scan_state = {
                'githubRepo': str(data.get('github_repo') or config.github_repo or '').strip(),
                'github_repo': str(data.get('github_repo') or config.github_repo or '').strip(),
                'defaultBranch': scan_default_branch,
                'default_branch': scan_default_branch,
                'defaultBranchSha': scan_head_sha,
                'default_branch_sha': scan_head_sha,
                'repoHeadSha': scan_head_sha,
                'repo_head_sha': scan_head_sha,
                'scanRunId': str(run_id or '').strip(),
                'scan_run_id': str(run_id or '').strip(),
                'status': 'completed',
                'completedAt': scan_completed_at.isoformat(),
                'completed_at': scan_completed_at.isoformat(),
                'updatedAt': timezone.now().isoformat(),
                'updated_at': timezone.now().isoformat(),
            }
            scan_state = {key: value for key, value in scan_state.items() if value not in (None, '')}
            pending_rescan_run_id = str(
                pending_setup.get('rescanRunId') or pending_setup.get('rescan_run_id') or ''
            ).strip()
            is_setup_verification_scan = bool(pending_rescan_run_id and str(run_id) == pending_rescan_run_id)
            if data.get('article_system') is not None:
                current_article_system = resolve_article_system(config)
                incoming_article_system = normalize_article_system(data.get('article_system'))
                if incoming_article_system.get('source') == 'scan':
                    if (
                        current_article_system.get('source') == 'manual_confirmed'
                        and current_article_system.get('state') in {'existing', 'roo_scaffolded'}
                        and incoming_article_system.get('state') in {'missing', 'ambiguous'}
                    ):
                        merged_article_system = current_article_system
                    elif (
                        current_article_system.get('state') == 'roo_scaffolded'
                        and incoming_article_system.get('state') == 'missing'
                        and incoming_article_system.get('confidence') == 'low'
                    ):
                        merged_article_system = current_article_system
                    else:
                        merged_article_system = incoming_article_system
                else:
                    merged_article_system = merge_article_system(current_article_system, incoming_article_system)

                update_fields = []
                verification_published = bool(
                    is_setup_verification_scan
                    and (article_system_ready(merged_article_system) or bool(publish_targets))
                )
                if pending_setup:
                    if verification_published:
                        merged_article_system.pop('pending_article_system_setup', None)
                        if not config.articles_scaffolded:
                            config.articles_scaffolded = True
                            update_fields.append('articles_scaffolded')
                        if pending_setup.get('prUrl') or pending_setup.get('pr_url'):
                            config.articles_scaffold_pr_url = pending_setup.get('prUrl') or pending_setup.get('pr_url')
                            update_fields.append('articles_scaffold_pr_url')
                        if pending_setup.get('previewUrl') or pending_setup.get('preview_url'):
                            config.articles_scaffold_preview_url = pending_setup.get('previewUrl') or pending_setup.get('preview_url')
                            update_fields.append('articles_scaffold_preview_url')
                    else:
                        merged_article_system['pending_article_system_setup'] = pending_setup
                if scan_state:
                    merged_article_system['scan'] = {
                        **dict(raw_article_system.get('scan') or {}),
                        **scan_state,
                    }
                if merged_article_system != (config.article_system or {}):
                    config.article_system = merged_article_system
                    update_fields.append('article_system')
                if publish_targets != (config.publish_targets or []):
                    config.publish_targets = publish_targets
                    update_fields.append('publish_targets')
                normalized_default_target_id = str(default_publish_target_id or '').strip() or None
                if normalized_default_target_id != config.default_publish_target_id:
                    config.default_publish_target_id = normalized_default_target_id
                    update_fields.append('default_publish_target_id')
                if scan_head_sha and scan_head_sha != str(config.last_scanned_sha or '').strip():
                    config.last_scanned_sha = scan_head_sha
                    update_fields.append('last_scanned_sha')
                if scan_completed_at and config.last_scanned_at != scan_completed_at:
                    config.last_scanned_at = scan_completed_at
                    update_fields.append('last_scanned_at')
                if update_fields:
                    update_fields.append('updated_at')
                    config.save(update_fields=update_fields)
                article_system = merged_article_system
            else:
                update_fields = []
                next_article_system = dict(config.article_system or {})
                if pending_setup and is_setup_verification_scan and publish_targets:
                    next_article_system.pop('pending_article_system_setup', None)
                    if not config.articles_scaffolded:
                        config.articles_scaffolded = True
                        update_fields.append('articles_scaffolded')
                if scan_state:
                    next_article_system['scan'] = {
                        **dict(next_article_system.get('scan') or {}),
                        **scan_state,
                    }
                if next_article_system != (config.article_system or {}):
                    config.article_system = next_article_system
                    update_fields.append('article_system')
                if publish_targets != (config.publish_targets or []):
                    config.publish_targets = publish_targets
                    update_fields.append('publish_targets')
                normalized_default_target_id = str(default_publish_target_id or '').strip() or None
                if normalized_default_target_id != config.default_publish_target_id:
                    config.default_publish_target_id = normalized_default_target_id
                    update_fields.append('default_publish_target_id')
                if scan_head_sha and scan_head_sha != str(config.last_scanned_sha or '').strip():
                    config.last_scanned_sha = scan_head_sha
                    update_fields.append('last_scanned_sha')
                if scan_completed_at and config.last_scanned_at != scan_completed_at:
                    config.last_scanned_at = scan_completed_at
                    update_fields.append('last_scanned_at')
                if update_fields:
                    update_fields.append('updated_at')
                    config.save(update_fields=update_fields)
                article_system = resolve_article_system(config)
            setup_pending_after_scan = bool((config.article_system or {}).get('pending_article_system_setup'))
        except (Organization.DoesNotExist, OrganizationContentConfig.DoesNotExist):
            pass

        article_system_state = article_system.get('state', 'missing')
        destination_summary = _scan_destination_summary(article_system, publish_targets)
        blocks = None

        if not slack_user_id:
            return Response({'status': 'received', 'job_id': job_id}, status=status.HTTP_200_OK)

        try:
            pending_resumed = False
            if slack_user_id and destination_summary and not approval_required and not scaffold_queued and not setup_pending_after_scan:
                try:
                    from integrations.models import UserIntegration
                    from integrations.services.article_generation import trigger_article_generation

                    integration = UserIntegration.objects.filter(slack_user_id=slack_user_id).first()
                    if integration and integration.pending_intent:
                        intent = integration.pending_intent
                        article_req = intent.get('article_request') or {}
                        if (
                            intent.get('type') == 'write_article'
                            and normalize_domain(article_req.get('domain', '')) == normalize_domain(domain)
                        ):
                            integration.pending_intent = None
                            integration.save(update_fields=['pending_intent'])
                            trigger_article_generation(slack_user_id, article_req)
                            pending_resumed = True
                            logger.info(f"Auto-resumed pending article intent after scan for {slack_user_id}/{domain}")
                except Exception as e:
                    logger.warning(f"Failed to auto-resume pending intent after scan for {domain}: {e}")

            if components_generated and components_count > 0:
                component_list = "\n".join(f"  • {name}" for name in component_names[:8])
                if len(component_names) > 8:
                    component_list += f"\n  • ...and {len(component_names) - 8} more"

                # Build pillar summary line
                pillar_line = ""
                if pillar_count and pillar_names:
                    pillar_display = ", ".join(pillar_names[:6])
                    if len(pillar_names) > 6:
                        pillar_display += f", +{len(pillar_names) - 6} more"
                    pillar_line = f"\n\n*{pillar_count} content pillars:* {pillar_display}"
                elif pillar_count:
                    pillar_line = f"\n\n*{pillar_count} content pillars* identified"

                if approval_required:
                    text_body = (
                        f"✅ *Scan complete for {domain}!*\n\n"
                        f"I've analysed your codebase and generated "
                        f"*{components_count} article components* "
                        f"matched to your website's design:\n"
                        f"{component_list}{pillar_line}\n\n"
                        f"The next step is to create an articles directory in your repo. "
                        f"This will set up content pillar directories, article components, "
                        f"an index page, and a demo article — submitted as a PR for your review."
                    )
                    blocks = [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": text_body}
                        },
                        {
                            "type": "actions",
                            "block_id": f"scaffold_confirm_{domain}",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Create Articles Directory"},
                                    "style": "primary",
                                    "action_id": "scaffold_confirm",
                                    "value": _json.dumps({
                                        "domain": domain,
                                        "slack_user_id": slack_user_id,
                                        "channel_id": channel_id,
                                        "thread_ts": thread_ts,
                                        "scan_run_id": run_id,
                                    })
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Skip for now"},
                                    "action_id": "scaffold_skip",
                                    "value": _json.dumps({
                                        "domain": domain,
                                        "slack_user_id": slack_user_id,
                                        "channel_id": channel_id,
                                        "thread_ts": thread_ts,
                                        "scan_run_id": run_id,
                                    })
                                }
                            ]
                        }
                    ]
                    fallback_text = f"✅ Scan complete for {domain}! Generated {components_count} components."
                elif destination_summary:
                    text_body = (
                        f"✅ *Scan complete for {domain}!*\\n\\n"
                        f"I've analysed your codebase and generated "
                        f"*{components_count} article components* "
                        f"matched to your website's design:\\n"
                        f"{component_list}{pillar_line}\\n\\n"
                        f"{destination_summary}"
                    )
                    if pending_resumed:
                        text_body += "\\n\\n🔄 *Resuming your article request automatically!* You'll get a notification shortly."
                    else:
                        text_body += "\\n\\nYou can now ask me to research or write an article."
                    fallback_text = text_body
                    blocks = None
                elif article_system_state == 'ambiguous':
                    detected_location = article_system.get('directory_path') or article_system.get('directory_name') or 'an existing content directory'
                    fallback_text = (
                        f"✅ *Scan complete for {domain}!*\\n\\n"
                        f"I found what looks like an article system at `{detected_location}`, "
                        f"but the detection confidence is low.\\n\\n"
                        f"You can tell me to use the detected system, rescan the repo, or scaffold a new articles directory."
                    )
                    blocks = None
                elif scaffold_queued:
                    text_body = (
                        f"✅ *Scan complete for {domain}!*\n\n"
                        f"I've analysed your codebase and generated "
                        f"*{components_count} article components* "
                        f"matched to your website's design:\n"
                        f"{component_list}{pillar_line}\n\n"
                        f"I've already queued article-directory setup in your repo, and I'll update you again when that PR is ready."
                    )
                    fallback_text = text_body
                    blocks = None
                elif has_pillars:
                    fallback_text = (
                        f"✅ *Scan complete for {domain}!*\n\n"
                        f"I've analysed your codebase and generated "
                        f"*{components_count} article components* "
                        f"matched to your website's design:\n"
                        f"{component_list}{pillar_line}\n\n"
                        f"This scan did not include scaffold approval metadata. Please run a fresh scan before creating the articles directory."
                    )
                    blocks = None
                else:
                    fallback_text = (
                        f"✅ *Scan complete for {domain}!*\n\n"
                        f"I've analysed your codebase and generated "
                        f"*{components_count} article components* "
                        f"matched to your website's design:\n{component_list}\n\n"
                        f"These components will be used to create articles that look native to your site.\n\n"
                        f"Would you like me to write your first article? Just say:\n"
                        f"  `@Roo write me an article about [topic]`"
                    )
                    blocks = None
            else:
                if approval_required:
                    fallback_text = (
                        f"✅ *Scan complete for {domain}!*\n\n"
                        f"I've analysed your repository and the next step is to create an articles directory in your repo.\n\n"
                        f"This will set up the safe publish route as a PR for your review."
                    )
                    blocks = [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": fallback_text}
                        },
                        {
                            "type": "actions",
                            "block_id": f"scaffold_confirm_{domain}",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Create Articles Directory"},
                                    "style": "primary",
                                    "action_id": "scaffold_confirm",
                                    "value": _json.dumps({
                                        "domain": domain,
                                        "slack_user_id": slack_user_id,
                                        "channel_id": channel_id,
                                        "thread_ts": thread_ts,
                                        "scan_run_id": run_id,
                                    })
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Skip for now"},
                                    "action_id": "scaffold_skip",
                                    "value": _json.dumps({
                                        "domain": domain,
                                        "slack_user_id": slack_user_id,
                                        "channel_id": channel_id,
                                        "thread_ts": thread_ts,
                                        "scan_run_id": run_id,
                                    })
                                }
                            ]
                        }
                    ]
                elif destination_summary:
                    fallback_text = (
                        f"✅ *Scan complete for {domain}!*\n\n"
                        f"{destination_summary}\n\n"
                        f"You can now ask me to research or write an article."
                    )
                    if pending_resumed:
                        fallback_text += "\n\n🔄 *Resuming your article request automatically!* You'll get a notification shortly."
                elif article_system_state == 'ambiguous':
                    fallback_text = (
                        f"✅ *Scan complete for {domain}!*\n\n"
                        f"I found what looks like an existing article system, but I’m not fully confident.\n\n"
                        f"You can tell me to use the detected system, rescan the repo, or scaffold a new articles directory."
                    )
                else:
                    fallback_text = (
                        f"✅ *Scan complete for {domain}!*\n\n"
                        f"I've analysed your codebase and I'm ready to help. "
                        f"You can now ask me to create blog pages or other content.\n\n"
                        f"To get started, say:\n"
                        f"  `@Roo write me an article about [topic]`"
                    )
                    blocks = None

            # Reply in-thread if we have context, fall back to DM
            if channel_id and thread_ts:
                SlackService.send_message(channel_id, fallback_text, blocks=blocks, thread_ts=thread_ts)
            else:
                SlackService.send_dm(slack_user_id, fallback_text, blocks=blocks)
        except Exception as e:
            logger.warning(f"Failed to send scan_complete notification to {slack_user_id}: {e}")

        return Response({
            'status': 'received',
            'message': 'Scan complete callback processed',
            'job_id': job_id,
        }, status=status.HTTP_200_OK)

    def _handle_generation_failed(self, data):
        """Handle generation_failed event from content-factory."""
        from content_factory.models import ContentFactoryJob
        from integrations.services.article_generation import (
            get_content_factory_article_cost_points,
            maybe_auto_refund_terminal_failure,
        )
        from integrations.services.daily_discovery import (
            is_scheduled_daily_job,
            mark_scheduled_dispatch_failed,
        )
        from integrations.services.notification_adapters import (
            normalize_notification_context,
            send_error,
        )
        from integrations.services.slack import SlackService

        job_id = data.get('job_id')
        run_id = data.get('run_id') or job_id
        workflow = data.get('workflow') or 'unknown'
        error_message = data.get('error', data.get('error_message', 'Unknown error'))
        error_code = data.get('error_code', 'INTERNAL_ERROR')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        diagnostics = _normalize_discovery_diagnostics(data.get('diagnostics'))
        diagnostics_text = _format_discovery_diagnostics(diagnostics)
        try:
            refund_points = int(data.get('refund_points') or 0)
        except (TypeError, ValueError):
            refund_points = 0
        auto_refunded = bool(data.get('auto_refunded'))

        # Update job record
        job, created = ContentFactoryJob.objects.update_or_create(
            job_id=job_id,
            defaults={
                'domain': domain,
                'slack_user_id': slack_user_id,
                'status': 'error',
                'error_message': f"[{error_code}] {error_message}",
            }
        )
        _sync_generation_callback_to_run(
            data=data,
            run_status=ContentFactoryRunStatus.FAILED,
            step_status=ContentFactoryStepStatus.FAILED,
        )
        scheduled_daily_job = is_scheduled_daily_job(job)

        channel_id, _root_message_ts, thread_ts = self._resolve_job_thread_context(job=job, data=data)

        logger.error(
            "Generation failed for job %s run %s workflow=%s (%s): [%s] %s",
            job_id,
            run_id,
            workflow,
            domain,
            error_code,
            error_message,
        )

        upsert_live_progress_card(
            job,
            data=data,
            summary_text=f"Run failed: {error_message}",
            failed=True,
        )

        if workflow in {'auto_discovery', 'direct_generate', 'confirmed_topic'} and not auto_refunded:
            auto_refunded, refund_points = maybe_auto_refund_terminal_failure(
                job,
                error_code=error_code,
                error_message=error_message,
            )

        mark_scheduled_dispatch_failed(
            job_id=job_id,
            error_message=f"[{error_code}] {error_message}",
        )

        if scheduled_daily_job:
            return Response({
                'status': 'received',
                'message': 'Generation failed callback processed',
                'job_id': job_id,
                'scheduled_daily_suppressed': True,
            }, status=status.HTTP_200_OK)

        if normalize_notification_context(data.get("notification_context")):
            send_error(data)
        elif slack_user_id:
            try:
                if error_code == 'PREREQUISITE_MISSING':
                    missing_step = data.get('missing_step', 'unknown')
                    if missing_step == 'scan':
                        message = (
                            f"⚠️ *{domain} needs to be scanned first.*\n\n"
                            f"{error_message}\n\n"
                            f"Say: `@Roo scan my codebase {domain}`"
                        )
                    elif missing_step == 'scaffold':
                        message = (
                            f"⚠️ *{domain} needs article scaffolding first.*\n\n"
                            f"{error_message}\n\n"
                            f"Say: `@Roo scaffold articles for {domain}`"
                        )
                    else:
                        message = (
                            f"⚠️ *Prerequisite missing for {domain}*\n\n"
                            f"{error_message}"
                        )
                elif error_code == 'MISSING_CONFIG':
                    message = (
                        f"❌ *Failed for {domain}*\n\n"
                        f"{error_message}\n\n"
                        f"Please make sure this domain is registered in the system."
                    )
                elif error_code == 'ARTICLE_SYSTEM_ACTION_REQUIRED':
                    recommended_action = data.get('recommended_action', 'scaffold')
                    if recommended_action == 'confirm_article_system':
                        message = (
                            f"⚠️ *{domain} needs article-system confirmation first.*\n\n"
                            f"{error_message}\n\n"
                            f"I found what may already be the right article directory, but I need confirmation before writing into it."
                        )
                    else:
                        message = (
                            f"⚠️ *{domain} needs an article system before writing.*\n\n"
                            f"{error_message}\n\n"
                            f"Ask me to scaffold articles for {domain}, or confirm the detected structure if one already exists."
                        )
                elif error_code == 'PUBLISH_TARGET_ACTION_REQUIRED':
                    message = (
                        f"⚠️ *{domain} needs a supported publish target before direct publish can continue.*\n\n"
                        f"{error_message}\n\n"
                        f"Roo stopped before changing the repository. You can retry in content-only mode, or add a supported publish target such as `.content-factory/target.yml`."
                    )
                elif error_code == 'CATALOG_MISSING_REQUIRED_COMPONENTS':
                    message = (
                        f"⚠️ *{domain} needs its article component catalog refreshed.*\n\n"
                        f"{error_message}\n\n"
                        f"Open the Connect repo & articles location step and run the repository scan again."
                    )
                elif error_code in ('INVALID_CREDENTIALS', 'REPO_NOT_FOUND'):
                    message = (
                        f"❌ *Failed for {domain}*\n\n"
                        f"{error_message}\n\n"
                        f"Please reconnect your GitHub account by saying:\n"
                        f"  `@Roo connect to my domain {domain}`"
                    )
                elif workflow == 'auto_discovery' and error_code == 'NO_OPPORTUNITIES':
                    message = (
                        f"⚠️ *Research for {domain} didn't find viable topics yet*\n\n"
                        f"{error_message}"
                    )
                    if diagnostics_text:
                        message += f"\n\n{diagnostics_text}"
                    message += (
                        "\n\nYou can still ask Roo to write about a specific topic, for example:\n"
                        f"  `@Roo write an article for {domain} about [topic]`\n\n"
                        f"This doesn't affect any scan or scaffold work already in progress."
                    )
                elif workflow == 'auto_discovery':
                    message = (
                        f"❌ *Research failed for {domain}*\n\n"
                        f"{error_message}\n\n"
                        f"This doesn't affect any scan or scaffold work already in progress."
                    )
                elif workflow == 'repo_scan':
                    message = (
                        f"❌ *Scan failed for {domain}*\n\n"
                        f"{error_message}\n\n"
                        f"If this keeps happening, please contact support."
                    )
                elif workflow == 'scaffold':
                    message = (
                        f"❌ *Articles directory setup failed for {domain}*\n\n"
                        f"{error_message}\n\n"
                        f"If this keeps happening, please contact support."
                    )
                else:
                    message = (
                        f"❌ *Task failed for {domain}*\n\n"
                        f"{error_message}\n\n"
                        f"If this keeps happening, please contact support."
                    )
                if workflow in {'auto_discovery', 'direct_generate', 'confirmed_topic'}:
                    if auto_refunded and refund_points > 0:
                        message += f"\n\nYour {refund_points} Roo points were refunded automatically."
                    else:
                        manual_refund_points = get_content_factory_article_cost_points(domain)
                        if manual_refund_points > 0:
                            message += (
                                f"\n\nIf this run failed and you want your {manual_refund_points} Roo points back, "
                                "message Dr Sam on Slack."
                            )
                # Reply in-thread if we have context, fall back to DM
                if channel_id and thread_ts:
                    SlackService.send_message(channel_id, message, thread_ts=thread_ts)
                else:
                    SlackService.send_dm(slack_user_id, message)
            except Exception as e:
                logger.warning(f"Failed to send generation_failed notification to {slack_user_id}: {e}")

        return Response({
            'status': 'received',
            'message': 'Generation failed callback processed',
            'job_id': job_id,
        }, status=status.HTTP_200_OK)

    def _handle_article_review_preview_event(self, data, event_type):
        """Handle article review preview lifecycle events from content-factory.

        Three events cover the hosted review preview after generation:
        - article_review_preview_fallback_ready: the exact build failed but a
          fallback render is reviewable; comments work, publish stays gated on
          the exact preview (policy enforced by content-factory's approve API).
        - article_review_preview_failed: no preview at all; the run is blocked
          with a classified reason (failure kind, failing file, attribution).
        - article_review_preview_not_available: the run cannot produce a
          preview in its current state (e.g. content-only delivery).
        """
        from content_factory.models import ContentFactoryJob

        job_id = data.get('job_id')
        run_id = data.get('run_id') or job_id
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        error_message = str(data.get('error') or '').strip()
        error_code = str(data.get('error_code') or '').strip()
        fallback_preview_url = str(data.get('fallback_preview_url') or '').strip()
        next_required_step = str(
            data.get('next_required_step') or data.get('nextRequiredStep') or ''
        ).strip()
        title = str(data.get('title') or '').strip()
        article_label = f' for "{title}"' if title else ''

        if event_type == 'article_review_preview_fallback_ready':
            job_status = 'needs_review'
            summary_text = 'Fallback preview ready for review; publish requires the exact build.'
            stored_error = ''
        elif event_type == 'article_review_preview_not_available':
            job_status = 'blocked'
            summary_text = error_message or 'Article preview is not available for this run.'
            stored_error = f"[{error_code}] {summary_text}" if error_code else summary_text
        else:
            job_status = 'blocked'
            summary_text = error_message or 'Hosted article review preview failed.'
            stored_error = f"[{error_code}] {summary_text}" if error_code else summary_text

        job, _created = ContentFactoryJob.objects.update_or_create(
            job_id=job_id,
            defaults={
                'domain': domain,
                'slack_user_id': slack_user_id,
                'status': job_status,
                'error_message': stored_error,
            },
        )

        try:
            upsert_live_progress_card(
                job,
                data=data,
                summary_text=summary_text,
                failed=event_type != 'article_review_preview_fallback_ready',
            )
        except Exception as exc:
            logger.warning(
                "Failed to update live progress card for %s callback on %s: %s",
                event_type,
                job_id,
                exc,
            )

        if event_type == 'article_review_preview_fallback_ready':
            text = (
                f":mag: *Fallback preview ready for review{article_label}* ({domain})\n"
                "The exact site build is unavailable, so this preview uses a fallback render. "
                "You can read and comment on the article now; approving and publishing require "
                "the exact preview."
            )
            if fallback_preview_url:
                text += f"\nPreview: {fallback_preview_url}"
        elif event_type == 'article_review_preview_not_available':
            text = f":no_entry_sign: *Article preview unavailable{article_label}* ({domain})\n{summary_text}"
            if next_required_step:
                text += f"\nNext step: `{next_required_step}`"
        else:
            text = (
                f":warning: *Article preview failed{article_label}* ({domain})\n{summary_text}\n"
                "Retry the preview from the run page, or regenerate the article if the failure "
                "is in the generated bundle."
            )

        try:
            self._send_job_message(job=job, data=data, slack_user_id=slack_user_id, text=text)
        except Exception as exc:
            logger.warning(
                "Failed to send %s notification for %s: %s",
                event_type,
                job_id,
                exc,
            )

        logger.info(
            "Article review preview event for job %s run %s (%s): %s [%s]",
            job_id,
            run_id,
            domain,
            event_type,
            error_code or 'no-code',
        )

        return Response({
            'status': 'received',
            'message': f'{event_type} callback processed',
            'job_id': job_id,
        }, status=status.HTTP_200_OK)

    def _handle_generation_blocked(self, data):
        """Handle generation_blocked event from content-factory."""
        from content_factory.models import ContentFactoryJob
        from integrations.services.article_generation import sync_blocked_job_state

        job_id = data.get('job_id')
        run_id = data.get('run_id') or job_id
        workflow = data.get('workflow') or 'unknown'
        error_message = data.get('error', data.get('error_message', 'Generation is blocked waiting for capacity.'))
        error_code = data.get('error_code', 'verifier_capacity_unavailable')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        blocked_step = data.get('blocked_step') or 'verify_build'
        preferred_queue = data.get('preferred_queue') or ''
        fallback_policy = data.get('fallback_policy') or ''
        retry_after_seconds = data.get('retry_after_seconds')

        job, _created = ContentFactoryJob.objects.update_or_create(
            job_id=job_id,
            defaults={
                'domain': domain,
                'slack_user_id': slack_user_id,
                'status': 'blocked',
                'error_message': f"[{error_code}] {error_message}",
            }
        )
        _sync_generation_callback_to_run(
            data=data,
            run_status=ContentFactoryRunStatus.BLOCKED,
            step_status=ContentFactoryStepStatus.BLOCKED,
        )
        sync_blocked_job_state(job, data, update_card=True, allow_visible_notification=True)

        logger.warning(
            "Generation blocked for job %s run %s workflow=%s (%s): [%s] %s",
            job_id,
            run_id,
            workflow,
            domain,
            error_code,
            error_message,
        )

        return Response({
            'status': 'received',
            'message': 'Generation blocked callback processed',
            'job_id': job_id,
        }, status=status.HTTP_200_OK)

    def _handle_scaffold_complete(self, data):
        """Handle scaffold_complete event from content-factory."""
        from content_factory.models import ContentFactoryJob, OrganizationContentConfig
        from organizations.models import Organization
        from integrations.services.slack import SlackService
        from integrations.utils import normalize_domain

        job_id = data.get('job_id')
        parent_run_id = data.get('parent_run_id') or ''
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        pr_url = data.get('pr_url')
        pillar_count = data.get('pillar_count', 0)
        component_count = data.get('component_count', 0)
        files_created = data.get('files_created', 0)
        already_exists = data.get('already_exists', False)
        error = data.get('error')
        setup_payload = data.get('article_system_setup') if isinstance(data.get('article_system_setup'), dict) else {}
        preview_url = str(data.get('preview_url') or setup_payload.get('preview_url') or '').strip()
        live_preview_url = str(data.get('live_preview_url') or setup_payload.get('live_preview_url') or '').strip()
        setup_status = str(
            data.get('setup_status')
            or setup_payload.get('status')
            or ('preview_ready' if preview_url else 'preview_building' if data.get('preview_dispatched') else 'preview_unavailable')
        ).strip() or 'preview_unavailable'
        build_verified = data.get('build_verified', False)

        # Update job record
        job = ContentFactoryJob.objects.filter(job_id=job_id).first()
        if job:
            if error:
                job.status = 'error'
                job.error_message = error
            else:
                job.status = 'completed'
                job.pr_url = pr_url
            job.save()

        parent_job = ContentFactoryJob.objects.filter(job_id=parent_run_id).first() if parent_run_id else None

        # Resolve thread context: scaffold job first, then parent scan job, then callback payload.
        channel_id = (
            (job.slack_channel_id if job else None)
            or (parent_job.slack_channel_id if parent_job else None)
            or data.get('slack_channel_id')
            or ''
        )
        thread_ts = (
            (job.slack_thread_ts if job else None)
            or (parent_job.slack_thread_ts if parent_job else None)
            or (parent_job.slack_root_message_ts if parent_job else None)
            or data.get('slack_thread_ts')
            or data.get('slack_root_message_ts')
            or ''
        )

        def _send(text, blocks=None):
            if channel_id and thread_ts:
                SlackService.send_message(channel_id, text, blocks=blocks, thread_ts=thread_ts)
            elif slack_user_id:
                SlackService.send_dm(slack_user_id, text, blocks=blocks)

        if error:
            logger.error(f"Scaffold failed for {domain}: {error}")
            try:
                _send(
                    f"Note: Could not set up article directories for *{domain}*: {error}\n"
                    f"This won't affect article generation -- directories will be created as needed."
                )
            except Exception as e:
                logger.warning(f"Failed to send scaffold error notification: {e}")
            return Response({
                'status': 'received',
                'message': 'Scaffold error processed',
                'job_id': job_id,
            }, status=status.HTTP_200_OK)

        # Persist scaffold PR/preview metadata. A newly created setup PR is not
        # a usable articles surface until it is merged and a verification scan
        # sees the article system on the default branch.
        normalized_domain = ''
        try:
            normalized_domain = normalize_domain(domain)
            org = Organization.objects.get(domain=normalized_domain)
            config = org.content_config
            article_system_payload = dict(config.article_system or {})
            if already_exists:
                existing_system = resolve_article_system(config)
                existing_system.update(
                    {
                        'state': 'existing',
                        'source': existing_system.get('source') or 'scan',
                    }
                )
                next_article_system = normalize_article_system(existing_system)
                next_article_system.pop('pending_article_system_setup', None)
                config.article_system = next_article_system
                config.articles_scaffolded = True
            else:
                pending = dict(article_system_payload.get('pending_article_system_setup') or {})
                pending.update(
                    {
                        'status': setup_status,
                        'setupStatus': setup_status,
                        'setup_status': setup_status,
                        'setupRunId': job_id,
                        'setup_run_id': job_id,
                        'sourceScanRunId': parent_run_id or pending.get('sourceScanRunId') or pending.get('source_scan_run_id') or '',
                        'source_scan_run_id': parent_run_id or pending.get('source_scan_run_id') or pending.get('sourceScanRunId') or '',
                        'prUrl': pr_url,
                        'pr_url': pr_url,
                        'previewUrl': preview_url,
                        'preview_url': preview_url,
                        'livePreviewUrl': live_preview_url,
                        'live_preview_url': live_preview_url,
                        'buildVerified': bool(build_verified),
                        'build_verified': bool(build_verified),
                        'updatedAt': timezone.now().isoformat(),
                        'updated_at': timezone.now().isoformat(),
                    }
                )
                article_system_payload['pending_article_system_setup'] = pending
                config.article_system = article_system_payload
            if pr_url:
                config.articles_scaffold_pr_url = pr_url
            if preview_url:
                config.articles_scaffold_preview_url = preview_url
            config.save()
            logger.info(f"Updated article setup metadata for {domain} after scaffold callback")
        except (Organization.DoesNotExist, OrganizationContentConfig.DoesNotExist) as e:
            logger.warning(f"Could not update article setup metadata for {domain}: {e}")

        # Check for pending article intent to auto-resume
        pending_resumed = False
        if slack_user_id and already_exists:
            try:
                from integrations.models import UserIntegration
                integration = UserIntegration.objects.filter(slack_user_id=slack_user_id).first()
                if integration and integration.pending_intent:
                    intent = integration.pending_intent
                    if intent.get('type') == 'write_article' and intent.get('article_request'):
                        article_req = intent['article_request']
                        # Only resume if intent is for the same domain
                        intent_domain = normalize_domain(article_req.get('domain', ''))
                        if intent_domain == normalized_domain:
                            # Clear intent first (prevent double-trigger)
                            integration.pending_intent = None
                            integration.save()

                            # Auto-trigger article generation
                            from integrations.services.article_generation import trigger_article_generation
                            trigger_article_generation(slack_user_id, article_req)
                            pending_resumed = True
                            logger.info(f"Auto-resumed pending article intent for {slack_user_id}/{domain}")
            except Exception as e:
                logger.warning(f"Failed to resume pending intent after scaffold: {e}")

        # Send Slack notification
        try:
            import json as _json

            if already_exists:
                details = []
                if pr_url:
                    details.append(f"🔗 *PR:* {pr_url}")
                if preview_url:
                    details.append(f"🔗 *Preview:* {preview_url}")
                detail_block = "\n\n".join(details)
                detail_suffix = f"{detail_block}\n\n" if detail_block else ""
                if pending_resumed:
                    _send(
                        f"📁 Articles directory already exists for *{domain}*.\n\n"
                        f"{detail_suffix}"
                        f"🔄 *Resuming your article request automatically!* You'll get a notification shortly."
                    )
                else:
                    _send(
                        f"📁 Articles directory already exists for *{domain}*.\n\n"
                        f"{detail_suffix}"
                        f"You're all set! To write your first article, say:\n"
                        f"  `@Roo write me an article about [topic]`"
                    )
            elif pr_url:
                preview_line = ""
                if preview_url:
                    preview_line = f"\n\n🔗 *Preview:* {preview_url}"
                build_status = "✅ Build passed" if build_verified else "⏳ Build pending"
                change_line = (
                    f"  • {files_created} total files\n"
                    if files_created
                    else "  • Reused the existing scaffold branch/PR\n"
                )
                text_body = (
                    f"📁 *Articles directory created for {domain}!*\n\n"
                    f"I've set up your content structure with:\n"
                    f"  • {pillar_count} content pillar directories\n"
                    f"  • {component_count} article components\n"
                    f"{change_line}"
                    f"  • {build_status}\n\n"
                    f"*Review the PR:* {pr_url}{preview_line}"
                )
                text_body += "\n\nReview this setup PR in GitHub. Topic research will unlock after it is merged and verified on the default branch."
                _send(
                    text_body,
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": text_body,
                            },
                        }
                    ],
                )
            else:
                _send(
                    f"📁 Articles directory scaffolded for *{domain}*, but "
                    f"the PR could not be created. Check the repo for a "
                    f"`feature/articles-scaffolding` branch."
                )
        except Exception as e:
            logger.warning(f"Failed to send scaffold_complete notification: {e}")

        return Response({
            'status': 'received',
            'message': 'Scaffold complete callback processed',
            'job_id': job_id,
        }, status=status.HTTP_200_OK)

    def _generate_topic_explanation(self, option_data, company_context=None, competitors=None):
        """Generate a user-friendly explanation for why this topic was chosen."""
        volume = option_data.get('volume', 0)
        difficulty = option_data.get('difficulty')
        difficulty_source = option_data.get('difficulty_source') or option_data.get('difficultySource') or 'missing'
        tier = option_data.get('tier', 'tier_4_discard')
        opportunity_index = option_data.get('opportunity_index', 0.0)
        
        parts = []
        
        # Volume assessment
        try:
            volume_val = int(volume)
        except (ValueError, TypeError):
            volume_val = 0
            
        if volume_val >= 2000:
            parts.append(f"High search volume ({volume_val:,}/mo)")
        elif volume_val >= 500:
            parts.append(f"Moderate search volume ({volume_val:,}/mo)")
        else:
            parts.append(f"Niche search volume ({volume_val:,}/mo)")
        
        # Difficulty assessment
        try:
            diff_val = int(difficulty)
        except (ValueError, TypeError):
            diff_val = None

        if difficulty_source not in {'dataforseo_labs', 'dataforseo_bulk'} or diff_val is None:
            parts.append("Difficulty is still being verified.")
        elif diff_val <= 20:
            parts.append("Very approachable; strong content could start getting traction in a few months.")
        elif diff_val <= 40:
            parts.append("Achievable with strong content and a realistic 4-6 month ranking window.")
        elif diff_val <= 60:
            parts.append("Moderate difficulty; likely needs a strong article, internal links, and time, often 6-9+ months.")
        elif diff_val <= 80:
            parts.append("Hard difficulty; usually needs authority, supporting content, and backlinks.")
        else:
            parts.append("Very hard difficulty; treat this as a long-term authority play.")
        
        # Tier-based reasoning
        tier_reasons = {
            'tier_1_blue_ocean': "This is an untapped opportunity where AI overviews haven't saturated the search results.",
            'tier_2_authority': "This topic helps establish your authority in the space.",
            'tier_3_long_tail': "A focused long-tail opportunity that can drive targeted traffic.",
        }
        if tier in tier_reasons:
            parts.append(tier_reasons[tier])
        
        # Company relevance (if context available)
        if company_context and len(str(company_context)) > 10:
            parts.append("This aligns with your company's focus areas.")
        
        # Competitor gap (if competitors listed)
        if competitors and isinstance(competitors, list) and len(competitors) > 0:
            existing_presence = False
            # Check if competitors are targeting this (simplified check based on provided competitor list in option, if any)
            # But here we just mention the competitors context generally if we knew more.
            # Since content factory returns specific competitor data per keyword, we could use that if available.
            # For now, just a generic statement if it's a gap analysis result
            pass
        
        return " ".join(parts)

    def _handle_topic_selection(self, data):
        """Handle topic_selection event from content-factory."""
        from content_factory.models import ContentFactoryJob, ScheduledDiscoveryDispatch
        from organizations.models import Organization
        from integrations.services.article_generation import (
            CONTENT_FACTORY_BILLING_STATUS_DEFERRED,
            SCHEDULED_DAILY_TRIGGER_SOURCE,
        )
        from integrations.services.daily_discovery import (
            get_daily_discovery_schedule_channel_name,
            is_scheduled_daily_job,
            mark_scheduled_dispatch_failed,
            mark_scheduled_dispatch_topic_selection_sent,
        )
        from integrations.services.notification_adapters import (
            normalize_notification_context,
            resolve_automation_run,
            send_topic_selection,
        )
        from integrations.services.slack import SlackService
        
        job_id = data.get('job_id')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')
        selection = data.get('selection', {})
        
        # Extract options (or wrap single selection if new format not sent)
        options = selection.get('options', [])
        if not options and selection.get('selected_keyword'):
            # Backwards compatibility
            options = [selection.copy()]
            selection['options'] = options
            
        # Limit to top 4 options
        options = options[:4]
        
        # Get or create job tracking record
        job, created = ContentFactoryJob.objects.update_or_create(
            job_id=job_id,
            defaults={
                'domain': domain,
                'slack_user_id': slack_user_id,
                'status': 'awaiting_confirmation',
                'selected_keyword': selection.get('selected_keyword', ''),
                'selection_reason': selection.get('selection_reason', ''),
                'selection_data': selection,
            }
        )
        requested_by_slack_user_id = self._callback_requested_by_slack_user_id(job=job, data=data)
        if requested_by_slack_user_id:
            request_meta = dict(job.request_meta or {})
            if request_meta.get('requested_by_slack_user_id') != requested_by_slack_user_id:
                request_meta['requested_by_slack_user_id'] = requested_by_slack_user_id
                job.request_meta = request_meta
                job.save(update_fields=['request_meta', 'updated_at'])
        job.last_progress_milestone_key = 'awaiting_confirmation'
        job.last_progress_updated_at = timezone.now()
        job.still_working_pinged_at = None
        job.save(update_fields=['last_progress_milestone_key', 'last_progress_updated_at', 'still_working_pinged_at', 'updated_at'])
        notification_context = normalize_notification_context(data.get("notification_context"))
        if notification_context:
            request_meta = dict(job.request_meta or {})
            request_meta["notification_context"] = notification_context
            automation_run = resolve_automation_run(notification_context)
            if automation_run:
                request_meta.setdefault("trigger_source", "research_automation")
                request_meta["automation_id"] = str(automation_run.automation_id)
                request_meta["automation_run_id"] = str(automation_run.id)
                if automation_run.request_payload:
                    request_meta.update(
                        {
                            key: value
                            for key, value in automation_run.request_payload.items()
                            if key in {"user_email", "recipient_user_id"}
                        }
                    )
            job.request_meta = request_meta
            job.save(update_fields=["request_meta", "updated_at"])
        dispatch = ScheduledDiscoveryDispatch.objects.filter(content_factory_job_id=job_id).first()
        scheduled_daily_job = is_scheduled_daily_job(job) or bool(dispatch)
        if scheduled_daily_job:
            request_meta = dict(job.request_meta or {})
            update_fields = []
            if request_meta.get("trigger_source") != SCHEDULED_DAILY_TRIGGER_SOURCE:
                request_meta["trigger_source"] = SCHEDULED_DAILY_TRIGGER_SOURCE
                update_fields.append("request_meta")
            if not job.billing_status:
                job.billing_status = CONTENT_FACTORY_BILLING_STATUS_DEFERRED
                update_fields.append("billing_status")
            if update_fields:
                job.request_meta = request_meta
                update_fields.append("updated_at")
                job.save(update_fields=update_fields)

        logger.info(f"Topic selection recorded for job {job_id}: {len(options)} options found")

        if notification_context and options:
            upsert_live_progress_card(
                job,
                data=data,
                summary_text="Research complete. Choose one of the topic options below to continue.",
            )
            send_topic_selection(data)
        elif slack_user_id and options:
            # Fetch organization context for explanations
            company_context = None
            competitors = []
            org = None
            try:
                # Simple normalization (should ideally match what other views do)
                normalized_domain = domain.lower().strip()
                if normalized_domain.startswith('https://'): normalized_domain = normalized_domain[8:]
                if normalized_domain.startswith('http://'): normalized_domain = normalized_domain[7:]
                if normalized_domain.startswith('www.'): normalized_domain = normalized_domain[4:]
                if '/' in normalized_domain: normalized_domain = normalized_domain.split('/')[0]
                
                org = Organization.objects.filter(domain__icontains=normalized_domain).first()
                if org:
                    config = getattr(org, 'content_config', None)
                    if config:
                        company_context = config.company_context
                    competitors = org.competitors or []
            except Exception as e:
                logger.warning(f"Could not fetch org context for explanations: {e}")

            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "📊 Article Topics Selected",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"I've researched content opportunities for *{domain}* and found {len(options)} great topics. Choose one to write:"
                    }
                }
            ]
            
            # Action buttons accumulator
            action_elements = []
            
            for idx, option in enumerate(options):
                keyword = option.get('keyword', option.get('selected_keyword', 'Unknown Topic'))
                display_title = option.get('suggested_title') or keyword
                volume = option.get('volume', 'N/A')
                difficulty = option.get('difficulty', 'N/A')
                score = option.get('opportunity_index', 'N/A')
                
                # Format score
                try:
                    score_val = float(score)
                    score_str = f"{score_val:.1f}"
                except (ValueError, TypeError):
                    score_str = str(score)

                # Use provided explanation or generate one
                explanation = option.get('explanation')
                if not explanation:
                    explanation = self._generate_topic_explanation(option, company_context, competitors)
                
                # Add section for this option
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{idx + 1}. {display_title}*\n"
                                f"`{keyword}`\n"
                                f"📈 {volume}/mo • 🎯 Difficulty: {difficulty}/100 • Score: {score_str}\n"
                                f"_{explanation}_"
                    }
                })
                
                # Create button for this option
                action_elements.append({
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": f"Op {idx + 1}: {keyword[:15]}..." if len(keyword) > 18 else f"Op {idx + 1}: {keyword}",
                        "emoji": True
                    },
                    "value": f"confirm_topic:{job_id}:{idx}",  # Include index in value
                    "action_id": f"confirm_topic_btn_{idx}"
                })

            # Add buttons row
            # Split into chunks of 5 if cleaner, but Slack allows 5 buttons per action block.
            # We add cancel at the end.
            
            # Add Cancel button
            action_elements.append({
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "❌ Cancel",
                    "emoji": True
                },
                "style": "danger",
                "value": f"cancel_topic:{job_id}",
                "action_id": "cancel_topic_btn"
            })
            
            # Add action block
            blocks.append({
                "type": "actions",
                "elements": action_elements
            })

            if scheduled_daily_job:
                owner_slack_user_id = str(
                    getattr(getattr(org, 'content_config', None), 'connected_slack_user_id', '') or slack_user_id
                ).strip()
                if owner_slack_user_id:
                    blocks.insert(
                        1,
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"<@{owner_slack_user_id}> your scheduled research for *{domain}* is ready.",
                            },
                        },
                    )

                channel_name = get_daily_discovery_schedule_channel_name()
                channel_id = SlackService.get_channel_id_by_name(channel_name)
                if not channel_id:
                    error_message = (
                        f"Scheduled discovery could not post to Slack because #{channel_name} could not be resolved."
                    )
                    mark_scheduled_dispatch_failed(job_id=job_id, error_message=error_message)
                    job.status = 'error'
                    job.error_message = error_message
                    job.save(update_fields=['status', 'error_message', 'updated_at'])
                    return Response(
                        {
                            'status': 'processed_with_error',
                            'message': error_message,
                            'job_id': job_id,
                        },
                        status=status.HTTP_200_OK,
                    )

                sent, message_ts = SlackService.send_message(
                    channel_id,
                    f"Scheduled topic selection ready for {domain}",
                    blocks=blocks,
                )
                if not sent or not message_ts:
                    error_message = (
                        f"Scheduled discovery could not post the topic selection card into #{channel_name}."
                    )
                    mark_scheduled_dispatch_failed(job_id=job_id, error_message=error_message)
                    job.status = 'error'
                    job.error_message = error_message
                    job.save(update_fields=['status', 'error_message', 'updated_at'])
                    return Response(
                        {
                            'status': 'processed_with_error',
                            'message': error_message,
                            'job_id': job_id,
                        },
                        status=status.HTTP_200_OK,
                    )

                job.slack_channel_id = channel_id
                job.slack_root_message_ts = message_ts
                job.slack_thread_ts = message_ts
                job.save(
                    update_fields=[
                        'slack_channel_id',
                        'slack_root_message_ts',
                        'slack_thread_ts',
                        'updated_at',
                    ]
                )
                mark_scheduled_dispatch_topic_selection_sent(
                    job_id=job_id,
                    slack_channel_id=channel_id,
                    slack_message_ts=message_ts,
                    slack_thread_ts=message_ts,
                )
            else:
                channel_id, _root_message_ts, thread_ts = self._resolve_job_thread_context(job=job, data=data)
                mark_scheduled_dispatch_topic_selection_sent(
                    job_id=job_id,
                    slack_channel_id=channel_id,
                    slack_thread_ts=thread_ts,
                )
                upsert_live_progress_card(
                    job,
                    data=data,
                    summary_text="Research complete. Choose one of the topic options below to continue.",
                )
                self._send_job_message(
                    job=job,
                    data=data,
                    slack_user_id=slack_user_id,
                    text="Topic selection ready for review",
                    blocks=blocks,
                )
        
        return Response({
            'status': 'received',
            'message': 'Topic selection callback processed',
            'job_id': job_id,
            'awaiting_confirmation': True,
        }, status=status.HTTP_200_OK)

    def _handle_article_complete(self, data):
        """Handle article_complete event from content-factory."""
        from content_factory.models import ContentFactoryJob
        from integrations.services.notification_adapters import (
            normalize_notification_context,
            send_content_ready,
        )
        from integrations.services.slack import SlackService

        job_id = data.get('job_id')
        article_url = data.get('article_url')
        pr_url = data.get('pr_url')
        article_title = data.get('article_title', '')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')

        # Update or create job record
        job, created = ContentFactoryJob.objects.update_or_create(
            job_id=job_id,
            defaults={
                'domain': domain,
                'slack_user_id': slack_user_id,
                'status': 'completed',
                'article_url': article_url,
                'pr_url': pr_url,
            }
        )

        # Resolve thread context: job first, then callback payload
        channel_id = (job.slack_channel_id if job else None) or data.get('slack_channel_id') or ''
        thread_ts = (job.slack_thread_ts if job else None) or data.get('slack_thread_ts') or ''

        logger.info(f"Article complete for job {job_id}: pr_url={pr_url}, title={article_title}")

        upsert_live_progress_card(
            job,
            data=data,
            summary_text="Article published and ready for review.",
        )

        if normalize_notification_context(data.get("notification_context")):
            send_content_ready(data)
        elif slack_user_id:
            title_line = f"*{article_title}*\n\n" if article_title else ""
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"✅ *Article Published!* for {domain}\n\n"
                            f"{title_line}"
                            f"The article has been generated and a Pull Request is ready.\n\n"
                            f"📄 *<{article_url}|View Article>*\n"
                            f"🔗 *<{pr_url}|View Pull Request>*"
                        )
                    }
                }
            ]
            fallback_text = f"Article generation complete for {domain}!"
            try:
                self._send_job_message(
                    job=job,
                    data=data,
                    slack_user_id=slack_user_id,
                    text=fallback_text,
                    blocks=blocks,
                )
            except Exception as e:
                logger.warning(f"Failed to send article_complete notification to {slack_user_id}: {e}")

        return Response({
            'status': 'received',
            'message': 'Article complete callback processed',
            'job_id': job_id,
        }, status=status.HTTP_200_OK)

    def _handle_error(self, data):
        """Handle error event from content-factory."""
        from content_factory.models import ContentFactoryJob
        from integrations.services.notification_adapters import (
            normalize_notification_context,
            send_error,
        )
        from integrations.services.slack import SlackService

        job_id = data.get('job_id')
        error_message = data.get('error_message', 'Unknown error')
        domain = data.get('domain', '')
        slack_user_id = data.get('slack_user_id', '')

        # Update or create job record
        job, created = ContentFactoryJob.objects.update_or_create(
            job_id=job_id,
            defaults={
                'domain': domain,
                'slack_user_id': slack_user_id,
                'status': 'error',
                'error_message': error_message,
            }
        )

        logger.error(f"Error callback for job {job_id}: {error_message}")

        # Notify user via the routed channel, falling back to Slack for legacy jobs.
        if normalize_notification_context(data.get("notification_context")):
            send_error(data)
        elif slack_user_id:
            try:
                blocks = [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": "❌ Content Pipeline Failed",
                            "emoji": True
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"The article generation pipeline encountered an error for *{domain}*.\n\n*Error:* {error_message}"
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "You can try again by requesting a new article."
                        }
                    }
                ]
                from integrations.services.article_generation import get_content_factory_article_cost_points

                refund_points = get_content_factory_article_cost_points(domain)
                if refund_points > 0:
                    blocks.append(
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"If this run failed and you want your {refund_points} Roo points back, "
                                    "message Dr Sam on Slack."
                                )
                            },
                        }
                    )
                self._send_job_message(
                    job=job,
                    data=data,
                    slack_user_id=slack_user_id,
                    text=f"Content pipeline error for {domain}",
                    blocks=blocks,
                )
                logger.info(f"Sent error notification to {slack_user_id} for job {job_id}")
            except Exception as e:
                logger.warning(f"Failed to send error notification to {slack_user_id}: {e}")

        return Response({
            'status': 'received',
            'message': 'Error callback processed',
            'job_id': job_id,
        }, status=status.HTTP_200_OK)


# =============================================================================
# SEO Research API Views
# =============================================================================

from content_factory.models import (
    ResearchedKeyword, KeywordVelocity, AISaturation, PAQuestion,
    SemanticCluster, ClusterMembership, TopicMap, WrittenArticle, ResearchSession,
    KeywordStatus, TopicFeedback
)
from content_factory.serializers import (
    ResearchedKeywordListSerializer, ResearchedKeywordDetailSerializer,
    KeywordBulkUpsertSerializer, SemanticClusterSerializer,
    ClusterBulkUpsertSerializer, TopicMapSerializer, WrittenArticleSerializer,
    WrittenArticleCreateSerializer, ResearchSessionSerializer,
    KeywordStatusUpdateSerializer, SEODashboardSerializer,
    ResearchFeedbackSerializer, TopicFeedbackRequestSerializer,
)
from content_factory.topic_feedback import (
    list_topic_feedback,
    record_topic_feedback,
    restore_topic_feedback,
    serialize_topic_feedback,
)


class SEOKeywordListView(APIView):
    """
    GET /api/seo/keywords/?domain=example.com&status=pending&tier=tier_1_blue_ocean

    List keywords with filtering and sorting.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        domain = request.query_params.get('domain')
        if not domain:
            return Response(
                {'error': 'domain query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            org = Organization.objects.get(domain=domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        qs = ResearchedKeyword.objects.filter(
            organization=org
        ).prefetch_related(
            'velocity_snapshots', 'ai_saturation_snapshots', 'paa_questions'
        )

        # Apply filters
        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)

        tier_filter = request.query_params.get('tier')
        if tier_filter:
            qs = qs.filter(tier=tier_filter)

        source_filter = request.query_params.get('source')
        if source_filter:
            qs = qs.filter(source=source_filter)

        # Sorting
        sort_by = request.query_params.get('sort', '-opportunity_index')
        qs = qs.order_by(sort_by)

        # Limit
        limit = request.query_params.get('limit', 100)
        try:
            limit = int(limit)
        except ValueError:
            limit = 100

        offset = request.query_params.get('offset', 0)
        try:
            offset = int(offset)
        except ValueError:
            offset = 0

        qs = qs[offset:offset + limit]

        serializer = ResearchedKeywordListSerializer(qs, many=True)
        return Response({
            'domain': domain,
            'count': len(serializer.data),
            'keywords': serializer.data
        }, status=status.HTTP_200_OK)


class SEOKeywordDetailView(APIView):
    """
    GET /api/seo/keywords/<uuid>/

    Get detailed keyword data including velocity/saturation history.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, pk):
        try:
            keyword = ResearchedKeyword.objects.prefetch_related(
                'velocity_snapshots', 'ai_saturation_snapshots',
                'paa_questions', 'cluster_memberships__cluster'
            ).get(pk=pk)
        except ResearchedKeyword.DoesNotExist:
            return Response(
                {'error': 'Keyword not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = ResearchedKeywordDetailSerializer(keyword)
        return Response(serializer.data, status=status.HTTP_200_OK)


class SEOKeywordBulkUpsertView(APIView):
    """
    POST /api/seo/keywords/bulk/

    Bulk upsert keywords from content-factory research results.
    This is the main endpoint called by content-factory after research.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        serializer = KeywordBulkUpsertSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        domain = serializer.validated_data['domain']
        keywords_data = serializer.validated_data['keywords']

        try:
            org = Organization.objects.get(domain=domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': f'Organization not found for domain: {domain}'},
                status=status.HTTP_404_NOT_FOUND
            )

        created_count = 0
        updated_count = 0

        with transaction.atomic():
            for kw_data in keywords_data:
                keyword_text = kw_data.get('keyword', '').strip()
                if not keyword_text:
                    continue

                keyword_normalized = keyword_text.lower().strip()
                velocity = kw_data.get('velocity_data') or kw_data.get('velocity') or {}
                related_keywords = kw_data.get('related_keywords') or kw_data.get('relatedKeywords') or []
                monthly_searches = (
                    kw_data.get('monthly_searches')
                    or kw_data.get('monthlySearches')
                    or velocity.get('daily_volumes')
                    or velocity.get('dailyVolumes')
                    or []
                )

                defaults = {
                    'keyword': keyword_text,
                    'volume': kw_data.get('volume', 0),
                    'difficulty': kw_data.get('difficulty', 50),
                    'difficulty_source': kw_data.get('difficulty_source') or kw_data.get('difficultySource') or 'legacy_default',
                    'intent': kw_data.get('intent', 'informational'),
                    'tier': kw_data.get('tier', 'tier_4_discard'),
                    'opportunity_index': kw_data.get('opportunity_index', 0.0),
                    'source': kw_data.get('source', 'seed'),
                    'source_detail': kw_data.get('source_detail'),
                    'competitor_urls': kw_data.get('competitor_urls', []),
                    'related_keywords': related_keywords if isinstance(related_keywords, list) else [],
                    'monthly_searches': monthly_searches if isinstance(monthly_searches, list) else [],
                    'cluster_fingerprint': kw_data.get('cluster_fingerprint', ''),
                }

                keyword_obj, created = ResearchedKeyword.objects.update_or_create(
                    organization=org,
                    keyword_normalized=keyword_normalized,
                    defaults=defaults
                )

                if created:
                    created_count += 1
                else:
                    updated_count += 1

                # Create velocity snapshot if provided
                if velocity:
                    KeywordVelocity.objects.create(
                        keyword=keyword_obj,
                        absolute_volume=velocity.get('absolute_volume', 0),
                        velocity_score=velocity.get('velocity_score', 0.0),
                        trend_status=velocity.get('trend_status', 'stable'),
                        daily_volumes=velocity.get('daily_volumes', []),
                    )

                # Create AI saturation snapshot if provided
                ai_sat = kw_data.get('ai_saturation')
                if ai_sat:
                    AISaturation.objects.create(
                        keyword=keyword_obj,
                        domain=domain,
                        ai_overview_present=ai_sat.get('ai_overview_present', False),
                        ai_overview_quality=ai_sat.get('ai_overview_quality', 'none'),
                        featured_snippet_present=ai_sat.get('featured_snippet_present', False),
                        video_carousel_present=ai_sat.get('video_carousel_present', False),
                        knowledge_panel_present=ai_sat.get('knowledge_panel_present', False),
                        saturation_score=ai_sat.get('saturation_score', 0.0),
                        hostility_score=ai_sat.get('hostility_score', 0.0),
                        hostility_recommendation=ai_sat.get('hostility_recommendation', 'high_priority'),
                        serp_features=ai_sat.get('serp_features', []),
                    )

                # Create PAA questions if provided
                paa_questions = kw_data.get('paa_questions', [])
                for i, paa in enumerate(paa_questions):
                    question_text = paa.get('question', '').strip()
                    if not question_text:
                        continue
                    PAQuestion.objects.get_or_create(
                        keyword=keyword_obj,
                        question_normalized=question_text.lower().strip()[:500],
                        defaults={
                            'question': question_text,
                            'domain': domain,
                            'answer_snippet': paa.get('answer_snippet', ''),
                            'source_url': paa.get('source_url'),
                            'depth': paa.get('depth', 1),
                            'has_ai_overview': paa.get('has_ai_overview', False),
                            'order': i,
                        }
                    )

        return Response({
            'created': created_count,
            'updated': updated_count,
            'total': len(keywords_data)
        }, status=status.HTTP_200_OK)


class SEOKeywordStatusUpdateView(APIView):
    """
    PATCH /api/seo/keywords/<uuid>/status/

    Update keyword status (pending -> approved -> written, etc.)
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def patch(self, request, pk):
        try:
            keyword = ResearchedKeyword.objects.get(pk=pk)
        except ResearchedKeyword.DoesNotExist:
            return Response(
                {'error': 'Keyword not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = KeywordStatusUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        new_status = serializer.validated_data['status']
        written_article_id = serializer.validated_data.get('written_article_id')

        keyword.status = new_status
        keyword.status_changed_at = timezone.now()

        if written_article_id:
            try:
                article = WrittenArticle.objects.get(pk=written_article_id)
                keyword.written_article = article
            except WrittenArticle.DoesNotExist:
                pass

        keyword.save()

        return Response({
            'id': str(keyword.id),
            'status': keyword.status,
            'updated_at': keyword.status_changed_at
        }, status=status.HTTP_200_OK)


class SEOKeywordResearchFeedbackView(APIView):
    """
    POST /api/seo/keywords/research-feedback/

    Persist research exposure, selection, and temporary rejections without
    changing the keyword lifecycle status.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        serializer = ResearchFeedbackSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        domain = serializer.validated_data['domain']
        shown_keywords = serializer.validated_data.get('shown_keywords', [])
        selected_keyword = serializer.validated_data.get('selected_keyword')
        rejected_keywords = serializer.validated_data.get('rejected_keywords', [])

        try:
            org = Organization.objects.get(domain=domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': f'Organization not found for domain: {domain}'},
                status=status.HTTP_404_NOT_FOUND
            )

        cooldown_days = int(os.environ.get("RESEARCH_TOPIC_COOLDOWN_DAYS", "7"))
        now = timezone.now()
        shown_count = 0
        selected_count = 0
        rejected_count = 0

        with transaction.atomic():
            for keyword_text in shown_keywords:
                keyword = ResearchedKeyword.objects.filter(
                    organization=org,
                    keyword_normalized=keyword_text.lower().strip()
                ).first()
                if not keyword:
                    continue
                keyword.times_shown += 1
                keyword.last_shown_at = now
                keyword.save(update_fields=['times_shown', 'last_shown_at'])
                shown_count += 1

            if selected_keyword:
                keyword = ResearchedKeyword.objects.filter(
                    organization=org,
                    keyword_normalized=selected_keyword.lower().strip()
                ).first()
                if keyword:
                    keyword.times_selected += 1
                    keyword.last_selected_at = now
                    keyword.save(update_fields=['times_selected', 'last_selected_at'])
                    selected_count = 1

            for keyword_text in rejected_keywords:
                keyword = ResearchedKeyword.objects.filter(
                    organization=org,
                    keyword_normalized=keyword_text.lower().strip()
                ).first()
                if not keyword:
                    continue
                keyword.times_rejected += 1
                keyword.last_rejected_at = now
                keyword.cooldown_until = now + timezone.timedelta(days=cooldown_days)
                keyword.save(update_fields=['times_rejected', 'last_rejected_at', 'cooldown_until'])
                rejected_count += 1

        return Response({
            'shown_updated': shown_count,
            'selected_updated': selected_count,
            'rejected_updated': rejected_count,
            'cooldown_days': cooldown_days,
        }, status=status.HTTP_200_OK)


class SEOTopicFeedbackView(APIView):
    """
    GET/POST /api/seo/topic-feedback/

    Persist explicit startup/domain-scoped topic feedback independently from
    researched keyword lifecycle status.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        domain = str(request.query_params.get('domain') or '').strip()
        if not domain:
            return Response({'error': 'domain query parameter is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            org = Organization.objects.get(domain=domain)
        except Organization.DoesNotExist:
            return Response({'error': f'Organization not found for domain: {domain}'}, status=status.HTTP_404_NOT_FOUND)

        feedback_type = str(request.query_params.get('feedback_type') or 'declined').strip() or 'declined'
        include_restored = str(request.query_params.get('include_restored') or '').lower() in {'1', 'true', 'yes'}
        try:
            limit = max(1, min(int(request.query_params.get('limit', 100)), 500))
        except (TypeError, ValueError):
            limit = 100
        try:
            offset = max(0, int(request.query_params.get('offset', 0)))
        except (TypeError, ValueError):
            offset = 0

        feedback = list_topic_feedback(
            org,
            feedback_type=feedback_type,
            include_restored=include_restored,
            limit=limit,
            offset=offset,
        )
        return Response(
            {
                'domain': domain,
                'count': len(feedback),
                'feedback': [serialize_topic_feedback(item) for item in feedback],
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        serializer = TopicFeedbackRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        domain = str(serializer.validated_data.get('domain') or '').strip()
        if not domain:
            return Response({'error': 'domain is required'}, status=status.HTTP_400_BAD_REQUEST)

        keyword = str(serializer.validated_data['keyword'] or '').strip()
        if not keyword:
            return Response({'error': 'keyword is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            org = Organization.objects.get(domain=domain)
        except Organization.DoesNotExist:
            return Response({'error': f'Organization not found for domain: {domain}'}, status=status.HTTP_404_NOT_FOUND)

        feedback, created = record_topic_feedback(
            org,
            keyword=keyword,
            feedback_type=serializer.validated_data.get('feedback_type') or 'declined',
            reason_code=serializer.validated_data.get('reason_code') or 'not_appropriate',
            reason_text=serializer.validated_data.get('reason_text'),
            decline_scope=serializer.validated_data.get('decline_scope') or 'similar',
            source=serializer.validated_data.get('source') or 'homepage_topic_card',
            session_id=serializer.validated_data.get('session_id'),
        )
        return Response(
            {**serialize_topic_feedback(feedback), 'created': created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class SEOTopicFeedbackRestoreView(APIView):
    """POST /api/seo/topic-feedback/<uuid>/restore/"""
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, pk):
        try:
            feedback = TopicFeedback.objects.select_related('organization').get(pk=pk)
        except TopicFeedback.DoesNotExist:
            return Response({'error': 'Topic feedback not found'}, status=status.HTTP_404_NOT_FOUND)

        restored = restore_topic_feedback(feedback)
        return Response({**serialize_topic_feedback(restored), 'restored': True}, status=status.HTTP_200_OK)


class SEOClusterListView(APIView):
    """
    GET /api/seo/clusters/?domain=example.com

    List semantic clusters for an organization.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        domain = request.query_params.get('domain')
        if not domain:
            return Response(
                {'error': 'domain query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            org = Organization.objects.get(domain=domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        clusters = SemanticCluster.objects.filter(
            organization=org
        ).prefetch_related('member_keywords__keyword')

        serializer = SemanticClusterSerializer(clusters, many=True)
        return Response({
            'domain': domain,
            'count': len(serializer.data),
            'clusters': serializer.data
        }, status=status.HTTP_200_OK)


class SEOClusterBulkUpsertView(APIView):
    """
    POST /api/seo/clusters/bulk/

    Bulk create/update clusters from content-factory topic map.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        serializer = ClusterBulkUpsertSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        domain = serializer.validated_data['domain']
        clusters_data = serializer.validated_data['clusters']

        try:
            org = Organization.objects.get(domain=domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': f'Organization not found for domain: {domain}'},
                status=status.HTTP_404_NOT_FOUND
            )

        created_count = 0
        updated_count = 0

        with transaction.atomic():
            for cluster_data in clusters_data:
                cluster_id = cluster_data.get('cluster_id')
                pillar_keyword = cluster_data.get('pillar_keyword', '')
                member_keywords = cluster_data.get('keywords', [])

                if cluster_id is None:
                    continue

                defaults = {
                    'pillar_keyword': pillar_keyword,
                    'average_similarity': cluster_data.get('average_similarity', 0.0),
                    'total_volume': cluster_data.get('total_volume', 0),
                    'avg_difficulty': cluster_data.get('avg_difficulty', 0.0),
                    'avg_velocity': cluster_data.get('avg_velocity', 0.0),
                    'topic_tier': cluster_data.get('topic_tier', 'tier_4_discard'),
                }

                cluster_obj, created = SemanticCluster.objects.update_or_create(
                    organization=org,
                    cluster_id=cluster_id,
                    defaults=defaults
                )

                if created:
                    created_count += 1
                else:
                    updated_count += 1

                # Link member keywords to cluster
                for kw_text in member_keywords:
                    keyword_normalized = kw_text.lower().strip()
                    try:
                        keyword_obj = ResearchedKeyword.objects.get(
                            organization=org,
                            keyword_normalized=keyword_normalized
                        )
                        ClusterMembership.objects.update_or_create(
                            keyword=keyword_obj,
                            cluster=cluster_obj,
                            defaults={
                                'is_pillar': keyword_normalized == pillar_keyword.lower().strip(),
                            }
                        )
                    except ResearchedKeyword.DoesNotExist:
                        # Keyword not found, skip membership creation
                        pass

        return Response({
            'created': created_count,
            'updated': updated_count,
            'total': len(clusters_data)
        }, status=status.HTTP_200_OK)


class SEOWrittenArticleCreateView(APIView):
    """
    GET/POST /api/seo/articles/

    List or create written article records and update keyword status.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        domain = request.query_params.get('domain')
        if not domain:
            return Response(
                {'error': 'domain query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            org = Organization.objects.get(domain=domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': f'Organization not found for domain: {domain}'},
                status=status.HTTP_404_NOT_FOUND
            )

        try:
            limit = max(1, min(int(request.query_params.get('limit', 100)), 1000))
        except (TypeError, ValueError):
            limit = 100
        try:
            offset = max(0, int(request.query_params.get('offset', 0)))
        except (TypeError, ValueError):
            offset = 0

        qs = WrittenArticle.objects.filter(organization=org).order_by('-created_at')
        total_count = qs.count()
        serializer = WrittenArticleSerializer(qs[offset:offset + limit], many=True)
        return Response({
            'domain': domain,
            'count': len(serializer.data),
            'total_count': total_count,
            'articles': serializer.data,
        }, status=status.HTTP_200_OK)

    def post(self, request):
        serializer = WrittenArticleCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        domain = serializer.validated_data['domain']
        primary_keyword = serializer.validated_data['primary_keyword']

        try:
            org = Organization.objects.get(domain=domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': f'Organization not found for domain: {domain}'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Get job reference if provided
        job = None
        job_id = serializer.validated_data.get('job_id')
        if job_id:
            from content_factory.models import ContentFactoryJob
            try:
                job = ContentFactoryJob.objects.get(job_id=job_id)
            except ContentFactoryJob.DoesNotExist:
                pass

        defaults = {
            'title': serializer.validated_data['title'],
            'category': serializer.validated_data['category'],
            'primary_keyword': primary_keyword,
            'article_url': serializer.validated_data.get('article_url'),
            'pr_url': serializer.validated_data.get('pr_url'),
            'job': job,
            'published_at': timezone.now(),
        }
        article, created = WrittenArticle.objects.update_or_create(
            organization=org,
            slug=serializer.validated_data['slug'],
            defaults=defaults,
        )

        # A PR URL only proves a PR exists; merge/live state is confirmed later
        # by the publish-status refresh. Never downgrade an existing status.
        desired_status = ArticlePublishStatus.PR_OPEN if defaults.get('pr_url') else ArticlePublishStatus.WRITTEN
        status_fields = advance_publish_status(article, desired_status)
        if status_fields:
            article.save(update_fields=sorted(set(status_fields)))

        # Update keyword status to written if it exists
        keyword_normalized = primary_keyword.lower().strip()
        try:
            keyword = ResearchedKeyword.objects.get(
                organization=org,
                keyword_normalized=keyword_normalized
            )
            keyword.status = KeywordStatus.WRITTEN
            keyword.written_article = article
            keyword.status_changed_at = timezone.now()
            keyword.save()
        except ResearchedKeyword.DoesNotExist:
            pass

        return Response({
            'id': str(article.id),
            'slug': article.slug,
            'status': 'created' if created else 'updated'
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class SEODashboardView(APIView):
    """
    GET /api/seo/dashboard/?domain=example.com

    Aggregate dashboard data for SEO research.
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        domain = request.query_params.get('domain')
        if not domain:
            return Response(
                {'error': 'domain query parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            org = Organization.objects.get(domain=domain)
        except Organization.DoesNotExist:
            return Response(
                {'error': 'Organization not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        keywords = ResearchedKeyword.objects.filter(organization=org)

        data = {
            'domain': domain,
            'total_keywords': keywords.count(),
            'by_status': {
                'pending': keywords.filter(status='pending').count(),
                'approved': keywords.filter(status='approved').count(),
                'in_progress': keywords.filter(status='in_progress').count(),
                'written': keywords.filter(status='written').count(),
                'skipped': keywords.filter(status='skipped').count(),
            },
            'by_tier': {
                'blue_ocean': keywords.filter(tier='tier_1_blue_ocean').count(),
                'authority': keywords.filter(tier='tier_2_authority').count(),
                'long_tail': keywords.filter(tier='tier_3_long_tail').count(),
                'discard': keywords.filter(tier='tier_4_discard').count(),
            },
            'top_opportunities': ResearchedKeywordListSerializer(
                keywords.filter(status='pending').order_by('-opportunity_index')[:10],
                many=True
            ).data,
            'clusters': SemanticCluster.objects.filter(organization=org).count(),
            'articles_written': WrittenArticle.objects.filter(organization=org).count(),
        }

        return Response(data, status=status.HTTP_200_OK)


class ContentFactoryOrgDomainsView(APIView):
    """
    Return all known organization domains for fuzzy matching.

    GET /api/content-factory/orgs/domains
    """
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        from django.core.cache import cache
        from integrations.utils import normalize_domain

        cache_key = "content_factory_org_domains"
        domains = cache.get(cache_key)

        if domains is None:
            raw_domains = Organization.objects.values_list('domain', flat=True).distinct()
            domains = sorted({normalize_domain(d) for d in raw_domains if d})
            cache.set(cache_key, domains, 300)  # 5-minute cache

        return Response(domains, status=status.HTTP_200_OK)


def _parse_optional_datetime(value):
    from django.utils.dateparse import parse_datetime

    if not value:
        return None
    if hasattr(value, "tzinfo"):
        return value
    return parse_datetime(value)


def _content_factory_run_result_payload(run: ContentFactoryRun) -> dict:
    payload = run.result or {}
    return payload if isinstance(payload, dict) else {}


def _content_factory_run_meta(run: ContentFactoryRun) -> dict:
    meta = _content_factory_run_result_payload(run).get(VALLEY_META_KEY) or {}
    return meta if isinstance(meta, dict) else {}


def _set_content_factory_run_meta(run: ContentFactoryRun, meta: dict) -> None:
    payload = dict(_content_factory_run_result_payload(run))
    payload[VALLEY_META_KEY] = dict(meta or {})
    run.result = payload


def _serialize_content_factory_run(run: ContentFactoryRun) -> dict:
    steps = {}
    for step in run.steps.order_by("display_order", "id"):
        attempts = []
        for attempt in step.attempt_history.order_by("attempt"):
            attempts.append(
                {
                    "attempt": attempt.attempt,
                    "status": attempt.status,
                    "message": attempt.message or None,
                    "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
                    "completed_at": attempt.completed_at.isoformat() if attempt.completed_at else None,
                    "artifacts": attempt.artifacts or [],
                    "error": attempt.error or None,
                    "input_path": attempt.input_path or None,
                    "output_path": attempt.output_path or None,
                    "notes_path": attempt.notes_path or None,
                    "status_path": attempt.status_path or None,
                }
            )
        steps[step.step_key] = {
            "name": step.step_key,
            "required": step.required,
            "status": step.status,
            "attempts": step.attempts,
            "message": step.message or None,
            "started_at": step.started_at.isoformat() if step.started_at else None,
            "completed_at": step.completed_at.isoformat() if step.completed_at else None,
            "artifacts": step.artifacts or [],
            "error": step.error or None,
            "latest_attempt_path": step.latest_attempt_path or None,
            "attempt_history": attempts,
        }
    return {
        "run_id": run.run_id,
        "workflow": run.workflow,
        "domain": run.domain,
        "github_repo": run.github_repo,
        "slack_user_id": run.slack_user_id,
        "status": run.status,
        "current_step": run.current_step,
        "artifact_root": run.artifact_root,
        "step_order": run.step_order or [],
        "acceptance_summary": run.acceptance_summary or {},
        "verification_summary": run.verification_summary or {},
        "approval_state": run.approval_state,
        "resume_available": run.resume_available,
        "error": run.error or None,
        "result": run.result or {},
        "run_request": run.run_request or {},
        "step_states": steps,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
    }


def _is_retryable_sqlite_lock(exc: Exception) -> bool:
    return connection.vendor == "sqlite" and "database is locked" in str(exc).lower()


def _content_factory_run_snapshot_unchanged(run: ContentFactoryRun, *, data: dict, step_states: dict) -> bool:
    core_fields = {
        "workflow": data["workflow"],
        "domain": data.get("domain") or "",
        "github_repo": data.get("github_repo") or "",
        "slack_user_id": data.get("slack_user_id") or "",
        "status": data["status"],
        "current_step": data.get("current_step") or "",
        "approval_state": data.get("approval_state") or ContentFactoryApprovalState.NOT_REQUIRED,
        "artifact_root": data.get("artifact_root") or "",
        "step_order": data.get("step_order") or [],
        "acceptance_summary": data.get("acceptance_summary") or {},
        "verification_summary": data.get("verification_summary") or {},
        "run_request": data.get("run_request") or {},
        "result": data.get("result") or {},
        "error": data.get("error") or "",
        "resume_available": bool(data.get("resume_available")),
    }
    for field, expected in core_fields.items():
        if getattr(run, field) != expected:
            return False

    ordered_steps = data.get("step_order") or list(step_states.keys())
    existing_steps = {step.step_key: step for step in run.steps.all()}
    if set(existing_steps) != set(ordered_steps):
        return False
    for index, step_key in enumerate(ordered_steps):
        step = existing_steps.get(step_key)
        if step is None:
            return False
        state_payload = dict(step_states.get(step_key) or {"name": step_key})
        expected_step = {
            "display_order": index,
            "required": bool(state_payload.get("required", True)),
            "status": state_payload.get("status", ContentFactoryStepStatus.PENDING),
            "attempts": int(state_payload.get("attempts", 0)),
            "message": state_payload.get("message") or "",
            "started_at": _parse_optional_datetime(state_payload.get("started_at")),
            "completed_at": _parse_optional_datetime(state_payload.get("completed_at")),
            "error": state_payload.get("error") or "",
            "latest_attempt_path": state_payload.get("latest_attempt_path") or "",
            "artifacts": state_payload.get("artifacts") or [],
        }
        for field, expected in expected_step.items():
            if getattr(step, field) != expected:
                return False

        attempts = {attempt.attempt: attempt for attempt in step.attempt_history.all()}
        incoming_attempts = {
            int(attempt_payload.get("attempt", 0)): attempt_payload
            for attempt_payload in state_payload.get("attempt_history", [])
        }
        if set(attempts) != set(incoming_attempts):
            return False
        for attempt_number, attempt_payload in incoming_attempts.items():
            attempt = attempts.get(attempt_number)
            if attempt is None:
                return False
            expected_attempt = {
                "status": attempt_payload.get("status", ContentFactoryStepStatus.PENDING),
                "message": attempt_payload.get("message") or "",
                "started_at": _parse_optional_datetime(attempt_payload.get("started_at")),
                "completed_at": _parse_optional_datetime(attempt_payload.get("completed_at")),
                "artifacts": attempt_payload.get("artifacts") or [],
                "error": attempt_payload.get("error") or "",
                "input_path": attempt_payload.get("input_path") or "",
                "output_path": attempt_payload.get("output_path") or "",
                "notes_path": attempt_payload.get("notes_path") or "",
                "status_path": attempt_payload.get("status_path") or "",
            }
            for field, expected in expected_attempt.items():
                if getattr(attempt, field) != expected:
                    return False
    return True


def _sync_content_factory_run_snapshot(*, run_id: str, data: dict, step_states: dict):
    data = sanitize_json_for_postgres(data if isinstance(data, dict) else {})
    step_states = sanitize_json_for_postgres(step_states if isinstance(step_states, dict) else {})
    with transaction.atomic():
        existing_run = (
            ContentFactoryRun.objects.select_for_update()
            .prefetch_related("steps", "steps__attempt_history")
            .filter(run_id=run_id)
            .first()
        )
        if existing_run is not None and _content_factory_run_snapshot_unchanged(existing_run, data=data, step_states=step_states):
            existing_run._content_factory_sync_unchanged = True
            return existing_run, False

        run, created = ContentFactoryRun.objects.update_or_create(
            run_id=run_id,
            defaults={
                "workflow": data["workflow"],
                "domain": data.get("domain") or "",
                "github_repo": data.get("github_repo") or "",
                "slack_user_id": data.get("slack_user_id") or "",
                "status": data["status"],
                "current_step": data.get("current_step") or "",
                "approval_state": data.get("approval_state") or ContentFactoryApprovalState.NOT_REQUIRED,
                "artifact_root": data.get("artifact_root") or "",
                "step_order": data.get("step_order") or [],
                "acceptance_summary": data.get("acceptance_summary") or {},
                "verification_summary": data.get("verification_summary") or {},
                "run_request": data.get("run_request") or {},
                "result": data.get("result") or {},
                "error": data.get("error") or "",
                "resume_available": bool(data.get("resume_available")),
            },
        )

        seen_steps = set()
        ordered_steps = data.get("step_order") or list(step_states.keys())
        for index, step_key in enumerate(ordered_steps):
            state_payload = dict(step_states.get(step_key) or {"name": step_key})
            step, _ = ContentFactoryRunStep.objects.update_or_create(
                run=run,
                step_key=step_key,
                defaults={
                    "display_order": index,
                    "required": bool(state_payload.get("required", True)),
                    "status": state_payload.get("status", ContentFactoryStepStatus.PENDING),
                    "attempts": int(state_payload.get("attempts", 0)),
                    "message": state_payload.get("message") or "",
                    "started_at": _parse_optional_datetime(state_payload.get("started_at")),
                    "completed_at": _parse_optional_datetime(state_payload.get("completed_at")),
                    "error": state_payload.get("error") or "",
                    "latest_attempt_path": state_payload.get("latest_attempt_path") or "",
                    "artifacts": state_payload.get("artifacts") or [],
                },
            )
            seen_steps.add(step_key)

            for attempt_payload in state_payload.get("attempt_history", []):
                ContentFactoryRunStepAttempt.objects.update_or_create(
                    step=step,
                    attempt=int(attempt_payload.get("attempt", 0)),
                    defaults={
                        "status": attempt_payload.get("status", ContentFactoryStepStatus.PENDING),
                        "message": attempt_payload.get("message") or "",
                        "started_at": _parse_optional_datetime(attempt_payload.get("started_at")),
                        "completed_at": _parse_optional_datetime(attempt_payload.get("completed_at")),
                        "artifacts": attempt_payload.get("artifacts") or [],
                        "error": attempt_payload.get("error") or "",
                        "input_path": attempt_payload.get("input_path") or "",
                        "output_path": attempt_payload.get("output_path") or "",
                        "notes_path": attempt_payload.get("notes_path") or "",
                        "status_path": attempt_payload.get("status_path") or "",
                    },
                )

        if seen_steps:
            ContentFactoryRunStep.objects.filter(run=run).exclude(step_key__in=seen_steps).delete()

    return run, created


class ContentFactoryRunView(APIView):
    """
    GET/PUT durable Content Factory run snapshots.

    GET /api/content-factory/runs/<run_id>
    PUT /api/content-factory/runs/<run_id>
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        if not run:
            return Response({"error": "Run not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_content_factory_run(run), status=status.HTTP_200_OK)

    def put(self, request, run_id: str):
        existing_run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        payload = sanitize_json_for_postgres(dict(request.data))
        payload["run_id"] = run_id
        serializer = ContentFactoryRunSyncSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        step_states = data.get("step_states", {}) or {}
        incoming_status = str(data.get("status") or "").strip()

        if (
            existing_run is not None
            and existing_run.status == ContentFactoryRunStatus.CANCELLED
            and data.get("status") != ContentFactoryRunStatus.CANCELLED
        ):
            return Response(
                {
                    "error": "run_cancelled",
                    "detail": "This run was cancelled and cannot accept more workflow updates.",
                    "run_id": run_id,
                    "status": existing_run.status,
                },
                status=status.HTTP_409_CONFLICT,
            )

        if (
            existing_run is not None
            and _is_terminal_run_status(existing_run.status)
            and incoming_status not in {
                ContentFactoryRunStatus.COMPLETED,
                ContentFactoryRunStatus.FAILED,
                ContentFactoryRunStatus.BLOCKED,
                ContentFactoryRunStatus.DENIED,
                ContentFactoryRunStatus.CANCELLED,
            }
            and not _article_system_setup_snapshot_is_current_retry(
                existing_run=existing_run,
                data=data,
                raw_payload=payload if isinstance(payload, dict) else {},
            )
        ):
            response_payload = _serialize_content_factory_run(existing_run)
            response_payload["sync_status"] = "ignored_terminal_state"
            return Response(response_payload, status=status.HTTP_200_OK)

        max_attempts = 3 if connection.vendor == "sqlite" else 1
        for attempt_number in range(1, max_attempts + 1):
            try:
                run, created = _sync_content_factory_run_snapshot(
                    run_id=run_id,
                    data=data,
                    step_states=step_states,
                )
                break
            except OperationalError as exc:
                if not _is_retryable_sqlite_lock(exc) or attempt_number == max_attempts:
                    raise
                logger.warning(
                    "Retrying Content Factory run sync for %s after SQLite lock (%s/%s).",
                    run_id,
                    attempt_number,
                    max_attempts,
                )
                time.sleep(0.25 * attempt_number)

        response_payload = _serialize_content_factory_run(run)
        response_payload["sync_status"] = (
            "unchanged"
            if getattr(run, "_content_factory_sync_unchanged", False)
            else "created"
            if created
            else "updated"
        )
        if run.status == ContentFactoryRunStatus.COMPLETED:
            from content_factory.vibe_marketing_views import _persist_completed_article_memory_if_possible

            _persist_completed_article_memory_if_possible(run)
        return Response(
            response_payload,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ContentFactoryRunValleyJobView(APIView):
    """
    Track active Valley Celery jobs for a durable run.

    POST /api/content-factory/runs/<run_id>/valley-jobs
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        if not run:
            return Response({"error": "Run not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = ContentFactoryRunValleyJobSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        job_id = serializer.validated_data["job_id"]
        transition = serializer.validated_data["transition"]
        reason = serializer.validated_data.get("reason") or ""

        meta = _content_factory_run_meta(run)
        tracked_job_ids = [
            str(item).strip()
            for item in list(meta.get("tracked_job_ids") or [])
            if str(item).strip()
        ]
        if transition in {"queued", "started"}:
            if job_id not in tracked_job_ids:
                tracked_job_ids.append(job_id)
        elif transition == "finished":
            tracked_job_ids = [item for item in tracked_job_ids if item != job_id]

        meta["tracked_job_ids"] = tracked_job_ids
        meta["last_tracked_job_transition"] = {
            "job_id": job_id,
            "transition": transition,
            "reason": reason,
            "recorded_at": timezone.now().isoformat(),
        }
        _set_content_factory_run_meta(run, meta)
        run.save(update_fields=["result", "updated_at"])

        return Response(
            {
                "run_id": run_id,
                "job_id": job_id,
                "transition": transition,
                "tracked_job_ids": tracked_job_ids,
            },
            status=status.HTTP_200_OK,
        )


class ContentFactoryRunPreviewView(View):
    """
    Public signed preview for any run with a stored content package.

    GET /api/content-factory/runs/<run_id>/preview?sig=...
    """

    def get(self, request, run_id: str):
        signature = str(request.GET.get("sig") or "").strip()
        if not signature:
            return HttpResponse(
                render_content_preview_error_page(
                    title="Preview unavailable",
                    message="This preview link is missing its signature.",
                ),
                status=403,
                content_type="text/html; charset=utf-8",
            )

        try:
            validate_content_factory_preview_signature(run_id, signature)
        except signing.SignatureExpired:
            return HttpResponse(
                render_content_preview_error_page(
                    title="Preview link expired",
                    message="This content preview link has expired. Ask Roo to generate a fresh one from Slack.",
                ),
                status=410,
                content_type="text/html; charset=utf-8",
            )
        except signing.BadSignature:
            return HttpResponse(
                render_content_preview_error_page(
                    title="Preview unavailable",
                    message="This content preview link is invalid.",
                ),
                status=403,
                content_type="text/html; charset=utf-8",
            )

        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        if not run:
            return HttpResponse(
                render_content_preview_error_page(
                    title="Preview unavailable",
                    message="The requested content preview could not be found.",
                ),
                status=404,
                content_type="text/html; charset=utf-8",
            )

        content_package = _content_package_from_run(run)
        if not content_package:
            return HttpResponse(
                render_content_preview_error_page(
                    title="Preview unavailable",
                    message="This run does not have a stored content package yet.",
                ),
                status=404,
                content_type="text/html; charset=utf-8",
            )

        return HttpResponse(
            render_content_preview_page(
                domain=run.domain,
                content_package=content_package,
            ),
            content_type="text/html; charset=utf-8",
        )


class ContentFactoryRunArtifactsView(APIView):
    """
    GET artifact manifest for a durable run snapshot.

    GET /api/content-factory/runs/<run_id>/artifacts
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        if not run:
            return Response({"error": "Run not found"}, status=status.HTTP_404_NOT_FOUND)

        payload = _serialize_content_factory_run(run)
        return Response(
            {
                "run_id": payload["run_id"],
                "workflow": payload["workflow"],
                "artifact_root": payload["artifact_root"],
                "steps": payload["step_states"],
            },
            status=status.HTTP_200_OK,
        )


class ContentFactoryRunControlView(APIView):
    """
    Control approval and resume metadata for durable runs.

    POST /api/content-factory/runs/<run_id>/approve
    POST /api/content-factory/runs/<run_id>/deny
    POST /api/content-factory/runs/<run_id>/resume
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str, action: str):
        from integrations.services.article_generation import ArticleGenerationError, publish_article_as_pr
        from content_factory.models import ContentFactoryJob

        serializer = ContentFactoryRunControlSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        actor = serializer.validated_data.get("actor") or "content-factory"

        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        job = ContentFactoryJob.objects.filter(job_id=run_id).first()

        if action in {"promote-bundle", "publish-pr"}:
            if not run and not job:
                return Response({"error": "Run not found"}, status=status.HTTP_404_NOT_FOUND)

            try:
                effective_slack_user_id = str((job.slack_user_id if job else None) or "").strip() or None
                requested_by_slack_user_id = (
                    str(((job.request_meta or {}) if job else {}).get("requested_by_slack_user_id") or "").strip()
                )
                if requested_by_slack_user_id == str(effective_slack_user_id or "").strip():
                    requested_by_slack_user_id = ""

                publish_kwargs = {
                    "slack_user_id": effective_slack_user_id,
                    "domain": ((job.domain if job else None) or (run.domain if run else None)),
                    "slack_channel_id": (job.slack_channel_id if job else ""),
                    "slack_thread_ts": (job.slack_thread_ts if job else ""),
                    "slack_root_message_ts": (job.slack_root_message_ts if job else ""),
                }
                if requested_by_slack_user_id:
                    publish_kwargs["requested_by_slack_user_id"] = requested_by_slack_user_id

                result = publish_article_as_pr(run_id, **publish_kwargs)
                return Response(result, status=status.HTTP_200_OK)
            except ArticleGenerationError as exc:
                return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)

        if not run:
            return Response({"error": "Run not found"}, status=status.HTTP_404_NOT_FOUND)

        if action == "approve":
            run.approval_state = ContentFactoryApprovalState.APPROVED
            run.status = ContentFactoryRunStatus.RUNNING
        elif action == "deny":
            run.approval_state = ContentFactoryApprovalState.DENIED
            run.status = ContentFactoryRunStatus.DENIED
        elif action == "resume":
            run.resume_available = True
            if run.status in {
                ContentFactoryRunStatus.FAILED,
                ContentFactoryRunStatus.BLOCKED,
                ContentFactoryRunStatus.DENIED,
            }:
                run.status = ContentFactoryRunStatus.QUEUED
            if run.workflow == "article_system_setup":
                current_step = str(request.data.get("current_step") or request.data.get("step") or run.current_step or "queued").strip()
                resume_generation = _article_system_setup_resume_generation(run.result or {}) + 1
                run.result = _clear_article_system_setup_retry_state(
                    run.result or {},
                    current_step=current_step,
                    resume_generation=resume_generation,
                )
                run.current_step = current_step
                run.resume_available = False
                run.error = ""
        else:
            return Response({"error": "Unsupported action"}, status=status.HTTP_400_BAD_REQUEST)

        run.save(update_fields=["approval_state", "status", "current_step", "resume_available", "result", "error", "updated_at"])
        return Response(
            {
                "run_id": run_id,
                "action": action,
                "actor": actor,
                "status": run.status,
                "approval_state": run.approval_state,
                "resume_available": run.resume_available,
            },
            status=status.HTTP_200_OK,
        )
