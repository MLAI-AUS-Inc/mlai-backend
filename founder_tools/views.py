from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations.models import ExternalServiceConnection

from .models import VibeRaisingCompany, VibeRaisingProfile
from .serializers import (
    FounderActiveCompanySerializer,
    FounderCompanySerializer,
    FounderCompanyUpsertSerializer,
    FounderProfileSerializer,
    FounderProfileUpsertSerializer,
    serialize_founder_bootstrap,
)
from .services import (
    CompanyDomainChangeBlocked,
    CompanyDomainClaimedError,
    DuplicateCompanyDomainError,
    apply_company_domain_change,
    apply_shared_startup_details,
    assert_company_domain_available,
    claimed_organization,
    domain_is_available_to,
    ensure_company_organization,
    get_or_create_founder_profile,
    offboard_company,
    queue_domain_claim_alert,
    seed_new_company_founder_and_author,
    set_active_company,
)
from vibe_raising.registration import (
    attempt_company_verification,
    set_unverified_company_abn,
)


def _connector_summaries(user):
    try:
        from integrations.services.external_connectors import active_organization_for_user

        connections = ExternalServiceConnection.objects.filter(user=user).order_by("provider", "-updated_at")
        organization = active_organization_for_user(user)
        if organization is not None:
            # Scope the connector summary to the active startup so two startups
            # under one login don't see each other's connections.
            connections = connections.filter(organization=organization)
    except Exception:
        return []

    summaries = []
    seen = set()
    for connection in connections:
        if connection.provider in seen:
            continue
        seen.add(connection.provider)
        summaries.append(
            {
                "provider": connection.provider,
                "status": connection.status,
                "accountLabel": connection.account_label,
                "updatedAt": connection.updated_at.isoformat() if connection.updated_at else None,
            }
        )
    return summaries


class FounderToolsBootstrapView(APIView):
    def get(self, request):
        profile = get_or_create_founder_profile(request.user)
        payload = serialize_founder_bootstrap(request.user, profile)
        payload["connectors"] = _connector_summaries(request.user)
        return Response(payload, status=status.HTTP_200_OK)


class FounderToolsProfileView(APIView):
    def get(self, request):
        from content_factory.founder_journey_cohort import account_payload

        profile = get_or_create_founder_profile(request.user)
        payload = FounderProfileSerializer(profile).data
        # P1 (IF-P1-02): additive, present only while V2 is on.
        founder_journey = account_payload(request.user)
        if founder_journey is not None:
            payload = {**payload, "founderJourney": founder_journey}
        return Response(payload, status=status.HTTP_200_OK)

    def put(self, request):
        return self._upsert(request)

    def post(self, request):
        return self._upsert(request)

    def _upsert(self, request):
        serializer = FounderProfileUpsertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = get_or_create_founder_profile(request.user)
        profile.role = VibeRaisingProfile.ROLE_FOUNDER
        profile.organization_name = None
        profile.save(update_fields=["role", "organization_name", "updated_at"])
        return Response(FounderProfileSerializer(profile).data, status=status.HTTP_200_OK)


class FounderToolsCompanyView(APIView):
    def get(self, request):
        from .profile_fields import visible_companies
        profile = get_or_create_founder_profile(request.user)
        companies = visible_companies(profile.companies.select_related("organization").all())
        return Response(FounderCompanySerializer(companies, many=True).data, status=status.HTTP_200_OK)

    def post(self, request):
        from .one_field_start import one_field_create_requested
        if one_field_create_requested(request.data):
            return self._one_field_create(request)
        # The claim check can fail after company rows were written (binding to
        # an existing organisation), so the save runs in its own transaction and
        # the ops alert is queued only once that transaction has rolled back.
        try:
            with transaction.atomic():
                return self._save_company(request)
        except CompanyDomainClaimedError as exc:
            queue_domain_claim_alert(organization=exc.organization, claimant_user_id=request.user.id)
            return exc.response()

    def _one_field_create(self, request):
        """P1.5 (IF-P1-06): create a new-journey startup from one field, once per Idempotency-Key."""
        from content_factory.founder_journey_cohort import NEW_JOURNEY, assign_on_create, find_company_for_create_key
        from content_factory.startup_first_profile import record_create
        from . import one_field_start

        body, error, derived_name = one_field_start.prepare_body(request.data)
        if error is not None:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)
        key_sha = one_field_start.create_key_sha256(request)
        locked = False
        if key_sha:
            locked = one_field_start.acquire_create_lock(request.user.pk, key_sha)
            if not locked:
                outcome, company = one_field_start.wait_for_replay(
                    get_or_create_founder_profile(request.user), request.user.pk, key_sha)
                if outcome == "replay":
                    return self._one_field_response(company, status_code=status.HTTP_200_OK)
                if outcome == "busy":
                    return Response(one_field_start.CREATE_IN_PROGRESS, status=status.HTTP_409_CONFLICT)
                locked = True
        try:
            with transaction.atomic():
                profile = get_or_create_founder_profile(request.user)
                profile = VibeRaisingProfile.objects.select_for_update().get(pk=profile.pk)
                if key_sha:
                    existing = find_company_for_create_key(profile, key_sha)
                    if existing is not None:
                        return self._one_field_response(existing, status_code=status.HTTP_200_OK)
                response = self._save_company(request, data=body)
                if response.status_code != status.HTTP_200_OK:
                    return response
                company = VibeRaisingCompany.objects.select_related("organization").get(pk=response.data["id"])
                cohort = assign_on_create(company.organization, company_id=company.pk, create_key_sha256=key_sha)
                if cohort == NEW_JOURNEY:
                    record_create(company.organization, derived_name=derived_name)
                return self._one_field_response(company, status_code=status.HTTP_201_CREATED, cohort=cohort)
        except CompanyDomainClaimedError as exc:
            queue_domain_claim_alert(organization=exc.organization, claimant_user_id=request.user.id)
            return exc.response()
        finally:
            if locked:
                one_field_start.release_create_lock(request.user.pk, key_sha)

    @staticmethod
    def _one_field_response(company, *, status_code, cohort=None):
        from content_factory.founder_journey_cohort import cohort as raw_cohort

        company.refresh_from_db()
        payload = dict(FounderCompanySerializer(company).data)
        payload["founderJourney"] = {"version": 1, "cohort": cohort or raw_cohort(company.organization)}
        return Response(payload, status=status_code)

    def _save_company(self, request, data=None):
        profile = get_or_create_founder_profile(request.user)
        profile = VibeRaisingProfile.objects.select_for_update().get(pk=profile.pk)
        if profile.role != VibeRaisingProfile.ROLE_FOUNDER:
            profile.role = VibeRaisingProfile.ROLE_FOUNDER
            profile.organization_name = None
            profile.save(update_fields=["role", "organization_name", "updated_at"])

        serializer = FounderCompanyUpsertSerializer(data=request.data if data is None else data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        company_id = data.pop("companyId", None)
        create_new = bool(data.pop("createNew", False))
        confirm_domain_change = bool(data.pop("confirmDomainChange", False))
        plain_fields = {
            key: data[key]
            for key in ("name", "domain", "location")
            if key in data
        }

        # A new company, or the first save of an unsaved research workspace.
        first_save = False
        try:
            if company_id:
                company = get_object_or_404(VibeRaisingCompany, pk=company_id, profile=profile)
            elif create_new:
                from .profile_fields import is_research_workspace
                from .services import find_company_with_domain
                provisional = find_company_with_domain(profile, plain_fields.get("domain"))
                reuse = bool(provisional and is_research_workspace(provisional))
                company = provisional if reuse else VibeRaisingCompany(profile=profile)
                first_save = True
            else:
                company = profile.companies.filter(name__iexact=data["name"]).first()
                first_save = company is None
                company = company or VibeRaisingCompany(profile=profile)

            if "domain" in plain_fields:
                assert_company_domain_available(
                    profile, plain_fields["domain"], exclude_company_id=company.id
                )
                is_admin = bool(
                    getattr(request.user, "is_staff", False) or getattr(request.user, "is_superuser", False)
                )
                # First-claim-wins tenancy: a founder may only take a domain that
                # is unclaimed or already theirs (parity with the vibe-raising
                # company endpoint).
                if (
                    plain_fields["domain"]
                    and not is_admin
                    and not domain_is_available_to(request.user, plain_fields["domain"])
                ):
                    raise CompanyDomainClaimedError(claimed_organization(plain_fields["domain"]))
                if plain_fields["domain"]:
                    # Rename the org in place when safe; otherwise a re-point
                    # strands the old org's data and needs confirmation.
                    apply_company_domain_change(
                        company, plain_fields["domain"], confirmed=confirm_domain_change
                    )

            for field, value in plain_fields.items():
                setattr(company, field, value)
            if "abn" in data:
                set_unverified_company_abn(company, data["abn"])
            if "registered" in data:
                company.registered = bool(data.get("registered"))
            if "default_audience_visibility" in data:
                company.default_audience_visibility = data["default_audience_visibility"]
            company.save()

            # Verify explicitly edited identifiers only. An unrelated profile edit
            # must not invalidate saved registration when ABR is unavailable.
            if "abn" in data or "acn" in data:
                attempt_company_verification(company, abn=company.abn, acn=data.get("acn"))
        except DuplicateCompanyDomainError as exc:
            transaction.set_rollback(True)
            return Response(
                {
                    "detail": str(exc),
                    "code": "duplicate_company_domain",
                    "field": "domain",
                    "companyId": str(exc.existing_company.id),
                },
                status=status.HTTP_409_CONFLICT,
            )
        except CompanyDomainChangeBlocked as exc:
            transaction.set_rollback(True)
            return Response(
                {
                    "detail": str(exc),
                    "code": "company_domain_change_moves_data",
                    "field": "domain",
                    "companyId": str(exc.company.id),
                    "currentDomain": exc.current_domain,
                    "newDomain": exc.new_domain,
                    "data": exc.data_summary,
                },
                status=status.HTTP_409_CONFLICT,
            )

        ensure_company_organization(company)
        apply_shared_startup_details(user=request.user, company=company, data=data)
        if first_save:
            seed_new_company_founder_and_author(user=request.user, company=company)
        if profile.active_company_id is None:
            set_active_company(profile, company)
        company.refresh_from_db()

        return Response(FounderCompanySerializer(company).data, status=status.HTTP_200_OK)


class FounderToolsCompanyDetailView(APIView):
    def delete(self, request, company_id):
        """Offboard a company: revoke its integrations, purge its data, remove it.

        Ownership is enforced via ``profile.companies`` (a company belonging to
        another founder 404s). Returns the offboarding summary plus the refreshed
        profile so the client can re-render the company list and active company.
        """
        profile = get_or_create_founder_profile(request.user)
        company = get_object_or_404(
            profile.companies.select_related("organization"),
            pk=company_id,
        )
        summary = offboard_company(company)
        profile.refresh_from_db()
        return Response(
            {"offboarding": summary, "profile": FounderProfileSerializer(profile).data},
            status=status.HTTP_200_OK,
        )


class FounderToolsActiveCompanyView(APIView):
    @transaction.atomic
    def post(self, request):
        profile = get_or_create_founder_profile(request.user)
        serializer = FounderActiveCompanySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        company = get_object_or_404(
            VibeRaisingCompany,
            pk=serializer.validated_data["companyId"],
            profile=profile,
        )
        set_active_company(profile, company)
        return Response(FounderProfileSerializer(profile).data, status=status.HTTP_200_OK)


class FounderToolsCompanyMonthlyUpdatesView(APIView):
    """Toggle automated monthly investor updates for a company (self-serve).

    Sets ``monthly_updates_enabled`` on the founder's (user, organization)
    binding, which is what the valley scheduler reads from
    monthly-dispatch-targets — so enabling it here schedules this company for
    monthly updates without any server-config edit. Ownership is enforced via
    ``profile.companies``.
    """

    @transaction.atomic
    def post(self, request, company_id):
        profile = get_or_create_founder_profile(request.user)
        company = get_object_or_404(
            profile.companies.select_related("organization"),
            pk=company_id,
        )
        if not company.organization_id:
            return Response(
                {"detail": "Add a company domain before enabling monthly updates."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw = request.data.get("enabled", True)
        enabled = raw if isinstance(raw, bool) else str(raw).strip().lower() in {"1", "true", "yes", "on"}

        from startup_updates.models import UserStartupBinding

        binding, _created = UserStartupBinding.objects.get_or_create(
            user=request.user,
            organization=company.organization,
        )
        if binding.monthly_updates_enabled != enabled:
            binding.monthly_updates_enabled = enabled
            binding.save(update_fields=["monthly_updates_enabled", "updated_at"])

        return Response(
            {"companyId": str(company.id), "monthlyUpdatesEnabled": enabled},
            status=status.HTTP_200_OK,
        )
