"""Founder opt-in billing; requesting identity stays on every ledger entry."""
from django.db import transaction
from rest_framework.response import Response
from rest_framework.views import APIView

from .billing import mask_billing_email
from .website_contract import WebsiteAuthorityError


def billing_user_for_organization(organization, requester):
    """Use an opted-in, active founder account, otherwise the requesting account."""
    payer = getattr(organization, "billing_user", None)
    if payer is None:
        return requester
    if not payer.is_active or not organization.founder_companies.filter(profile__user_id=payer.pk).exists():
        raise WebsiteAuthorityError("company_billing_founder_unavailable",
            "The company billing founder is unavailable. Ask a founder to update company billing.", status=409)
    return payer


def resolve_content_billing_user(requester, domain):
    """Apply company billing only after the requester's ordinary tenant checks."""
    from organizations.models import Organization
    from founder_tools.services import user_may_use_organization
    from .billing import normalize_content_factory_domain
    organization = Organization.objects.select_related("billing_user").filter(domain=normalize_content_factory_domain(domain)).first()
    if organization is None or not user_may_use_organization(requester, organization):
        return requester
    return billing_user_for_organization(organization, requester)


def other_founder_has_points(organization, requester, required):
    """Report availability only; never expose another founder's balance or identity."""
    from roo.services import PointsService
    for company in organization.founder_companies.filter(profile__user__is_active=True).exclude(profile__user_id=requester.pk).select_related("profile__user")[:20]:
        balance = PointsService.get_balance(company.profile.user)
        if int(balance.get("digital_service_balance_microroo") or 0) >= required * 1_000_000:
            return True
    return False


def billing_summary(organization, requester):
    """Return a fresh company-aware balance and the free preparation quote."""
    from roo.services import PointsService
    from .billing import get_content_factory_article_cost_points
    payer = billing_user_for_organization(organization, requester)
    balance = PointsService.get_balance(payer)
    points = int(balance.get("digital_service_balance_microroo") or 0) // 1_000_000
    cost = get_content_factory_article_cost_points(organization.domain)
    return {"setupPoints": 0, "articlePoints": cost, "balance": points,
        "signedInAccount": mask_billing_email(requester.email), "billingAccount": mask_billing_email(payer.email),
        "companyBillingEnabled": bool(organization.billing_user_id), "billingFounderIsYou": payer.pk == requester.pk,
        "canPrepare": True, "canGenerate": points >= cost,
        "otherFounderHasPoints": other_founder_has_points(organization, requester, cost) if points < cost else False}


class CompanyBillingView(APIView):
    """A founder may opt in their own account, or remove their prior opt-in."""
    def get(self, request):
        from .website_views import _context
        context, _, error = _context(request)
        if error:
            return error
        try:
            return Response({"companyBilling": billing_summary(context.organization, request.user)})
        except WebsiteAuthorityError as exc:
            return Response(exc.as_dict(), status=exc.status)

    def post(self, request):
        from .website_views import _context
        from organizations.models import Organization
        context, _, error = _context(request)
        if error:
            return error
        if not isinstance(request.data.get("opt_in"), bool):
            return Response({"code": "billing_opt_in_required", "detail": "Choose whether your account will pay for company articles."}, status=400)
        with transaction.atomic():
            organization = Organization.objects.select_for_update().get(pk=context.organization.pk)
            if organization.billing_user_id not in (None, request.user.pk):
                return Response({"code": "billing_founder_already_set", "detail": "The current billing founder must opt out before another founder can opt in."}, status=409)
            organization.billing_user = request.user if request.data["opt_in"] else None
            organization.save(update_fields=["billing_user"])
        return Response({"companyBilling": billing_summary(organization, request.user)})
