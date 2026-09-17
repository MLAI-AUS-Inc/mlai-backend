"""Owner-reviewed editorial policy, isolated from general organisation updates."""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from founder_tools.services import get_founder_company_context
from organizations.models import Organization
from .models import OrganizationContentConfig
from .editorial_catalog import (
    EDIT_FIELDS, CatalogConflict, approve_catalog, review_payload, update_catalog,
)


def _lock_catalog_owner(context, user_id, organization):
    """Recheck mutable ownership after the organisation lock, before policy IO.

    Keep the profile and company rows locked through the catalogue write. The
    earlier product-context lookup may have completed before a role change,
    company transfer/deletion or tenant relink committed.
    """
    profile = VibeRaisingProfile.objects.select_for_update().get(
        pk=context.profile.pk, user_id=user_id,
    )
    if profile.role != VibeRaisingProfile.ROLE_FOUNDER:
        raise PermissionError("Only company founders can review this catalog")
    company = VibeRaisingCompany.objects.select_for_update().get(
        pk=context.company.pk, profile_id=profile.pk,
    )
    if (
        company.organization_id != organization.pk
        or context.organization.pk != organization.pk
        or context.organization.domain != organization.domain
    ):
        raise CatalogConflict("Company configuration changed; reload the catalog before saving or approving")


def mutate_catalog_response(
    organization_lookup, payload, *, owner_context=None, owner_user_id=None, approving=False,
):
    """Lock and reread before validation; failures cannot partially save policy."""
    try:
        if (owner_context is None) != (owner_user_id is None) or (approving and owner_context is None):
            raise ValueError("An authenticated company owner is required for owner catalog operations")
        with transaction.atomic():
            org = Organization.objects.select_for_update().get(**organization_lookup)
            if owner_context is not None:
                _lock_catalog_owner(owner_context, owner_user_id, org)
            config = OrganizationContentConfig.objects.filter(organization=org).first()
            strategy = config.pillar_strategy if config else {}
            if not approving:
                payload = dict(payload)
                reference = payload.pop("suggestion_reference", None)
                updated = update_catalog(strategy, payload)
                if reference is not None:
                    if owner_context is None:
                        raise ValueError("Only the company owner may accept research suggestions")
                    updated = record_suggestion_review(updated, reference, org, owner_user_id)
            else:
                updated = approve_catalog(
                    strategy, payload, actor_id=f"user:{owner_user_id}", approved_at=timezone.now(),
                )
            # Validate the exact response before making any persistent change.
            response = review_payload(updated)
            if updated != strategy:
                OrganizationContentConfig.objects.update_or_create(
                    organization=org, defaults={"pillar_strategy": updated},
                )
        return Response(response)
    except (PermissionError, VibeRaisingProfile.DoesNotExist):
        return Response({"error": "Only company founders can review this catalog"}, status=403)
    except VibeRaisingCompany.DoesNotExist:
        return Response({"error": "Company configuration not found"}, status=404)
    except Organization.DoesNotExist:
        return Response({"error": "Organisation not found"}, status=404)
    except CatalogConflict as exc:
        return Response({"error": str(exc), "code": "editorial_catalog_conflict"}, status=409)
    except ValueError as exc:
        return Response({"error": str(exc), "code": "invalid_editorial_catalog"}, status=400)


def service_catalog_update(domain, data):
    """Called only after the service API has authenticated its Roo credential."""
    if set(data) - (EDIT_FIELDS | {"domain"}):
        return Response({"error": "Save editorial catalog changes separately from other organisation settings"}, status=400)
    return mutate_catalog_response({"domain": domain}, {key: value for key, value in data.items() if key != "domain"})


class EditorialCatalogView(APIView):
    # Inherit the product's JWT/Origin authentication; a service key is not an
    # owner identity and must not gain this approval capability.
    permission_classes = [IsAuthenticated]

    def _context(self, request):
        company_id = request.query_params.get("company_id")
        body_id = request.data.get("company_id") if isinstance(request.data, dict) else None
        if company_id and body_id and str(company_id) != str(body_id):
            return None, Response({"error": "Conflicting company_id values"}, status=400)
        company_id = company_id or body_id
        if not company_id:
            return None, Response({"error": "An explicit company_id is required"}, status=400)
        try:
            context = get_founder_company_context(request.user, company_id=company_id)
            return context, None
        except PermissionError:
            return None, Response({"error": "Only company founders can review this catalog"}, status=403)
        except (VibeRaisingCompany.DoesNotExist, Organization.DoesNotExist):
            return None, Response({"error": "Company configuration not found"}, status=404)
        except (ValueError, DjangoValidationError):
            return None, Response({"error": "Invalid company_id"}, status=400)

    def get(self, request):
        context, error = self._context(request)
        if error is not None:
            return error
        config = OrganizationContentConfig.objects.filter(organization=context.organization).first()
        try:
            payload = review_payload(config.pillar_strategy if config else {})
            if request.query_params.get("include_suggestions") == "1":
                payload["research_suggestions"] = latest_profile_suggestions(context.organization)
            return Response(payload)
        except ValueError:
            return Response({"error": "Stored catalog requires administrator repair"}, status=409)

    def put(self, request):
        return self._mutate(request)

    def _mutate(self, request, *, approving=False):
        if not isinstance(request.data, dict):
            return Response({"error": "Expected a JSON object"}, status=400)
        context, error = self._context(request)
        if error is not None:
            return error
        payload = {key: value for key, value in request.data.items() if key != "company_id"}
        return mutate_catalog_response(
            {"pk": context.organization.pk}, payload,
            owner_context=context, owner_user_id=request.user.pk, approving=approving,
        )


class EditorialCatalogApprovalView(EditorialCatalogView):
    http_method_names = ["post", "options"]

    def post(self, request):
        return self._mutate(request, approving=True)


from rest_framework.throttling import UserRateThrottle

class EditorialSuggestionThrottle(UserRateThrottle):
    rate = "12/hour"
    scope = "editorial_suggestions"


class EditorialBriefSuggestionView(EditorialCatalogView):
    http_method_names = ["post", "options"]
    throttle_classes = [EditorialSuggestionThrottle]

    def post(self, request):
        import requests
        if not isinstance(request.data, dict):
            return Response({"error": "Expected a JSON object"}, status=400)
        from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers
        context, error = self._context(request)
        if error is not None:
            return error
        topic = request.data.get("topic")
        country = request.data.get("country")
        version = request.data.get("expected_editorial_catalog_version")
        if not isinstance(topic, str) or not 3 <= len(topic.strip()) <= 500 or not isinstance(country, str) or len(country) != 2 or type(version) is not int:
            return Response({"error": "Supply a topic, country and current catalogue revision"}, status=400)
        remote = _content_factory_remote_config()
        if not remote["enabled"]:
            return Response({"error": "Brief suggestions are unavailable; you can enter the brief manually"}, status=503)
        try:
            result = requests.post(remote["base_url"] + "/api/org/editorial-brief-suggestion", headers=_content_factory_headers(), timeout=100,
                json={"domain": context.organization.domain, "topic": topic.strip(), "country": country,
                      "expected_editorial_catalog_version": version, "audience_id": request.data.get("audience_id") or None})
            if result.status_code != 200:
                return Response({"error": "The suggestion could not be prepared. Reload profiles or enter a brief manually."}, status=result.status_code if result.status_code in {400,409,422,503} else 502)
            return Response(result.json())
        except (requests.RequestException, ValueError):
            return Response({"error": "Brief suggestions are temporarily unavailable"}, status=503)


def latest_profile_suggestions(organization):
    """Recover evidence from this startup's saved scans without activating policy."""
    from copy import deepcopy
    from workflow_runs.models import ContentFactoryRun
    runs = ContentFactoryRun.objects.filter(organization=organization, domain=organization.domain, workflow="startup_autofill").order_by("-created_at")[:10]
    for run in runs:
        result = run.result if isinstance(run.result, dict) else {}
        sources = [result, result.get("result", {})]
        for source in sources:
            if not isinstance(source, dict):
                continue
            payload = source.get("autofill", source)
            suggestion = payload.get("editorialSuggestions") if isinstance(payload, dict) else None
            if isinstance(suggestion, dict) and suggestion.get("domain") == organization.domain and suggestion.get("researchRunId") == run.run_id:
                request = run.run_request if isinstance(run.run_request, dict) else {}
                return {**deepcopy(suggestion), "sourceCatalogVersion": request.get("editorial_catalog_version")}
    return None


class EditorialArticlesView(EditorialCatalogView):
    http_method_names = ["get", "options"]

    def get(self, request):
        from django.db.models import Q
        from .models import WrittenArticle
        from .vibe_marketing_views import _serialize_written_article
        context, error = self._context(request)
        if error is not None:
            return error
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
        except (ValueError, TypeError):
            return Response({"error": "Invalid page offset"}, status=400)
        base = WrittenArticle.objects.filter(organization=context.organization)
        query = base
        audience = request.query_params.get("audience_id", "")
        action = request.query_params.get("offer_id", "")
        if audience == "__unknown__":
            query = query.filter(audience_id="")
        elif audience:
            query = query.filter(audience_id=audience)
        if action == "__none__":
            query = query.filter(conversion_intent="none")
        elif action:
            query = query.filter(offer_id=action)
        search = request.query_params.get("q", "").strip()[:200]
        if search:
            query = query.filter(Q(title__icontains=search) | Q(primary_keyword__icontains=search))
        rows = list(query.order_by("-created_at", "id")[offset:offset + 25])
        return Response({"articles": [_serialize_written_article(row) for row in rows], "total": query.count(), "offset": offset, "limit": 25,
            "audienceIds": list(base.exclude(audience_id="").order_by("audience_id").values_list("audience_id", flat=True).distinct()),
            "offerIds": list(base.exclude(offer_id="").order_by("offer_id").values_list("offer_id", flat=True).distinct())})


def record_suggestion_review(strategy, reference, organization, user_id):
    """Keep research provenance separate from the approved business definition."""
    from copy import deepcopy
    from workflow_runs.models import ContentFactoryRun
    if not isinstance(reference, dict) or reference.get("kind") not in {"audience", "offer"}:
        raise ValueError("Invalid research suggestion reference")
    run = ContentFactoryRun.objects.filter(organization=organization, domain=organization.domain, workflow="startup_autofill", run_id=reference.get("research_run_id")).first()
    if run is None:
        raise ValueError("The original startup research is unavailable")
    result = run.result if isinstance(run.result, dict) else {}
    suggestion = None
    for source in [result, result.get("result", {})]:
        if not isinstance(source, dict):
            continue
        payload = source.get("autofill", source)
        envelope = payload.get("editorialSuggestions", {}) if isinstance(payload, dict) else {}
        if not isinstance(envelope, dict) or envelope.get("domain") != organization.domain or envelope.get("researchRunId") != run.run_id:
            continue
        suggestion = next((p for p in envelope.get("profiles", []) if isinstance(p, dict) and p.get("id") == reference.get("suggestion_id")), None)
        if suggestion:
            break
    if not suggestion or (reference["kind"] == "offer" and not suggestion.get("action")):
        raise ValueError("The selected research suggestion is unavailable")
    field = "audience_options" if reference["kind"] == "audience" else "cta_options"
    entry = next((e for e in strategy["editorial_catalog"][field] if e["id"] == reference.get("entry_id") and e["version"] == reference.get("entry_version") and e["status"] == "draft"), None)
    if entry is None:
        raise ValueError("The saved draft does not match the research review")
    updated = deepcopy(strategy)
    history = updated.setdefault("editorial_suggestion_reviews", [])
    request = run.run_request if isinstance(run.run_request, dict) else {}
    receipt = {"research_run_id": run.run_id, "suggestion_id": suggestion["id"], "kind": reference["kind"], "entry_id": entry["id"], "entry_version": entry["version"],
        "source_catalog_version": request.get("editorial_catalog_version"), "saved_catalog_version": strategy["editorial_catalog"]["version"],
        "reviewed_by": f"user:{user_id}", "reviewed_at": timezone.now().isoformat(), "suggestion": deepcopy(suggestion)}
    key = (receipt["kind"], receipt["entry_id"], receipt["entry_version"])
    if not any((r.get("kind"),r.get("entry_id"),r.get("entry_version")) == key for r in history):
        history.append(receipt)
    return updated
