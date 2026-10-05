"""Tests for the ABR verification helper (B2) and the registration gate (B3)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from content_factory.vibe_marketing_views import verify_company_with_abr
from vibe_raising import registration as reg
from vibe_raising.registration import (
    CompanyRegistrationError,
    attempt_company_verification,
    company_is_verified,
    company_registration_status,
    verify_and_persist_company_registration,
)

# Consistent company pair (see tests_acn_validators): ABN == 2 check digits + ACN.
COMPANY_ABN = "89000000019"
COMPANY_ACN = "000000019"
OTHER_ACN = "010499966"
NON_COMPANY_ABN = "94807394137"


class _FakeResponse:
    def __init__(self, *, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def _company_xml(acn="000000019", entity_code="PRV", status="Active"):
    return f"""<?xml version="1.0" encoding="utf-8"?>
    <ABRPayloadSearchResults xmlns="http://abr.business.gov.au/ABRXMLSearch/">
      <response>
        <businessEntity202001>
          <ABN><identifierValue>{COMPANY_ABN}</identifierValue></ABN>
          <entityStatus><entityStatusCode>{status}</entityStatusCode></entityStatus>
          <entityType><entityTypeCode>{entity_code}</entityTypeCode><entityDescription>Australian Private Company</entityDescription></entityType>
          <ASICNumber>{acn}</ASICNumber>
          <mainName><organisationName>EXAMPLE PTY LTD</organisationName></mainName>
        </businessEntity202001>
      </response>
    </ABRPayloadSearchResults>"""


_NON_COMPANY_XML = """<?xml version="1.0" encoding="utf-8"?>
<ABRPayloadSearchResults xmlns="http://abr.business.gov.au/ABRXMLSearch/">
  <response>
    <businessEntity202001>
      <ABN><identifierValue>94807394137</identifierValue></ABN>
      <entityStatus><entityStatusCode>Active</entityStatusCode></entityStatus>
      <entityType><entityTypeCode>OIE</entityTypeCode><entityDescription>Other Incorporated Entity</entityDescription></entityType>
      <mainName><organisationName>MLAI AUS INC</organisationName></mainName>
    </businessEntity202001>
  </response>
</ABRPayloadSearchResults>"""


@override_settings(ABR_LOOKUP_AUTHENTICATION_GUID="abr-guid")
class VerifyCompanyWithAbrTests(SimpleTestCase):
    def _verify(self, xml=None, *, status_code=200, raise_exc=False, identifier=COMPANY_ABN):
        def fake_get(url, params=None, timeout=None):
            if raise_exc:
                raise RuntimeError("boom")
            return _FakeResponse(status_code=status_code, text=xml or "")

        with patch("content_factory.vibe_marketing_views.http_client.get", side_effect=fake_get):
            return verify_company_with_abr(identifier)

    def test_acn_uses_asic_lookup_and_resolves_abn(self):
        with patch("content_factory.vibe_marketing_views.http_client.get", return_value=_FakeResponse(text=_company_xml())) as get:
            result = verify_company_with_abr(COMPANY_ACN)
        self.assertTrue(result["found"])
        self.assertEqual(result["abn"], COMPANY_ABN)
        self.assertTrue(get.call_args.args[0].endswith("/SearchByASICv201408"))
        self.assertEqual(get.call_args.kwargs["params"]["searchString"], COMPANY_ACN)

    def test_wrong_abn_response_does_not_verify_requested_abn(self):
        result = self._verify(_NON_COMPANY_XML)
        self.assertFalse(result["found"])

    def test_wrong_acn_response_does_not_verify_requested_acn(self):
        result = self._verify(_company_xml(), identifier=OTHER_ACN)
        self.assertFalse(result["found"])

    def test_malformed_abr_acn_cannot_be_replaced_with_derived_acn(self):
        for acn in ("123", "000000018", "abc000000019"):
            with self.subTest(acn=acn):
                result = self._verify(_company_xml(acn=acn))
                self.assertFalse(result["found"])

    def test_malformed_and_abr_error_responses_fail_closed(self):
        for body in ("<broken", "<response><exception><exceptionDescription>Invalid GUID</exceptionDescription></exception></response>"):
            with self.subTest(body=body):
                result = self._verify(body)
                self.assertFalse(result["reachable"])
                self.assertFalse(result["found"])

    def test_registered_company_is_recognised(self):
        result = self._verify(_company_xml())
        self.assertTrue(result["configured"])
        self.assertTrue(result["reachable"])
        self.assertTrue(result["found"])
        self.assertTrue(result["active"])
        self.assertTrue(result["is_company"])
        self.assertEqual(result["acn"], COMPANY_ACN)
        self.assertEqual(result["entity_type_code"], "PRV")

    def test_legal_and_all_trading_names_are_available_for_identity_matching(self):
        xml = _company_xml().replace("</mainName>", """</mainName>
          <businessName><organisationName>First Brand</organisationName></businessName>
          <businessName><organisationName>Second Brand</organisationName></businessName>""")
        self.assertEqual(self._verify(xml)["names"], ["EXAMPLE PTY LTD", "First Brand", "Second Brand"])

    def test_pretty_printed_abr_response_is_parsed(self):
        # Real ABR responses are indented, so <ABN> carries whitespace text before its
        # inner <identifierValue>. The parser must skip the wrapper and read the leaf.
        pretty = f"""<?xml version="1.0" encoding="utf-8"?>
        <ABRPayloadSearchResults xmlns="http://abr.business.gov.au/ABRXMLSearch/">
          <response>
            <businessEntity202001>
              <ABN>
                <identifierValue>{COMPANY_ABN}</identifierValue>
                <isCurrentIndicator>Y</isCurrentIndicator>
              </ABN>
              <entityStatus>
                <entityStatusCode>Active</entityStatusCode>
              </entityStatus>
              <ASICNumber>{COMPANY_ACN}</ASICNumber>
              <entityType>
                <entityTypeCode>PRV</entityTypeCode>
              </entityType>
              <mainName>
                <organisationName>EXAMPLE PTY LTD</organisationName>
              </mainName>
            </businessEntity202001>
          </response>
        </ABRPayloadSearchResults>"""
        result = self._verify(pretty)
        self.assertTrue(result["found"])
        self.assertTrue(result["is_company"])
        self.assertEqual(result["acn"], COMPANY_ACN)
        self.assertEqual(result["entity_type_code"], "PRV")

    def test_inactive_company_is_not_a_company(self):
        result = self._verify(_company_xml(status="Cancelled"))
        self.assertTrue(result["found"])
        self.assertFalse(result["active"])
        self.assertFalse(result["is_company"])

    def test_non_company_active_abn_is_not_a_company(self):
        def fake_get(url, params=None, timeout=None):
            return _FakeResponse(text=_NON_COMPANY_XML)

        with patch("content_factory.vibe_marketing_views.http_client.get", side_effect=fake_get):
            result = verify_company_with_abr(NON_COMPANY_ABN)
        self.assertTrue(result["found"])
        self.assertTrue(result["active"])
        self.assertIsNone(result["acn"])
        self.assertFalse(result["is_company"])

    def test_unreachable_marks_not_reachable(self):
        result = self._verify(raise_exc=True)
        self.assertTrue(result["configured"])
        self.assertFalse(result["reachable"])
        self.assertFalse(result["is_company"])

    def test_http_error_marks_not_reachable(self):
        result = self._verify(_company_xml(), status_code=503)
        self.assertFalse(result["reachable"])

    @override_settings(ABR_LOOKUP_AUTHENTICATION_GUID="")
    def test_unconfigured_reports_not_configured(self):
        result = verify_company_with_abr(COMPANY_ABN)
        self.assertFalse(result["configured"])
        self.assertFalse(result["reachable"])


def _abr_ok(**overrides):
    base = {
        "configured": True,
        "reachable": True,
        "found": True,
        "active": True,
        "abn": COMPANY_ABN,
        "is_company": True,
        "acn": COMPANY_ACN,
        "entity_type_code": "PRV",
    }
    base.update(overrides)
    return lambda abn: base


class _StubCompany:
    def __init__(self):
        self.abn = None
        self.acn = None
        self.entity_type_code = ""
        self.abr_verified_at = None
        self.registered = False
        self.saved = False

    def save(self, *args, **kwargs):
        self.saved = True


class VerifyAndPersistTests(SimpleTestCase):
    def _run(self, *, abn=COMPANY_ABN, acn=None, verifier=None, save=True):
        company = _StubCompany()
        verify_and_persist_company_registration(
            company,
            abn=abn,
            acn=acn,
            save=save,
            abr_verifier=verifier or _abr_ok(),
        )
        return company

    def test_success_persists_verified_company(self):
        company = self._run()
        self.assertTrue(company.registered)
        self.assertEqual(company.abn, COMPANY_ABN)
        self.assertEqual(company.acn, COMPANY_ACN)
        self.assertEqual(company.entity_type_code, "PRV")
        self.assertIsNotNone(company.abr_verified_at)
        self.assertTrue(company.saved)

    def test_save_false_mutates_without_writing(self):
        company = self._run(save=False)
        self.assertTrue(company.registered)
        self.assertFalse(company.saved)

    def test_blank_abn_raises_required(self):
        with self.assertRaises(CompanyRegistrationError) as ctx:
            self._run(abn="")
        self.assertEqual(ctx.exception.code, reg.ABN_REQUIRED)
        self.assertEqual(ctx.exception.field, "abn")

    def test_bad_abn_checksum_raises_invalid(self):
        with self.assertRaises(CompanyRegistrationError) as ctx:
            self._run(abn="94807394138")
        self.assertEqual(ctx.exception.code, reg.ABN_INVALID)

    def test_unreachable_abr_fails_closed(self):
        with self.assertRaises(CompanyRegistrationError) as ctx:
            self._run(verifier=_abr_ok(reachable=False))
        self.assertEqual(ctx.exception.code, reg.ABR_UNVERIFIABLE)

    def test_unconfigured_abr_fails_closed(self):
        with self.assertRaises(CompanyRegistrationError) as ctx:
            self._run(verifier=_abr_ok(configured=False, reachable=False))
        self.assertEqual(ctx.exception.code, reg.ABR_UNVERIFIABLE)

    def test_inactive_entity_raises_not_registered(self):
        with self.assertRaises(CompanyRegistrationError) as ctx:
            self._run(verifier=_abr_ok(active=False))
        self.assertEqual(ctx.exception.code, reg.NOT_A_REGISTERED_COMPANY)

    def test_mlai_association_qualifies_without_acn(self):
        company = self._run(abn=NON_COMPANY_ABN, verifier=_abr_ok(
            abn=NON_COMPANY_ABN, is_company=False, acn=None, entity_type_code="OIE"))
        self.assertTrue(company_is_verified(company))
        self.assertIsNone(company.acn)
        self.assertEqual(company.entity_type_code, "OIE")

    def test_sole_trader_active_abn_is_also_verifiable(self):
        company = self._run(abn=NON_COMPANY_ABN, verifier=_abr_ok(
            abn=NON_COMPANY_ABN, is_company=False, acn=None, entity_type_code="IND"))
        self.assertTrue(company_is_verified(company))

    def test_missing_active_status_or_mismatched_abn_fails_closed(self):
        for values in ({"active": None}, {"abn": NON_COMPANY_ABN}, {"found": False}):
            with self.subTest(values=values), self.assertRaises(CompanyRegistrationError):
                self._run(verifier=_abr_ok(**values))

    def test_acn_only_resolves_abn_from_abr(self):
        company = self._run(abn=None, acn=COMPANY_ACN)
        self.assertEqual(company.abn, COMPANY_ABN)
        self.assertEqual(company.acn, COMPANY_ACN)

    def test_malformed_supplied_acn_cannot_be_silently_ignored(self):
        for value in ("123", "abc000000019", "000000018"):
            with self.subTest(value=value), self.assertRaises(CompanyRegistrationError) as ctx:
                self._run(acn=value)
            self.assertEqual(ctx.exception.code, reg.ACN_INVALID)

    def test_association_cannot_claim_an_unrelated_acn(self):
        with self.assertRaises(CompanyRegistrationError) as ctx:
            self._run(abn=NON_COMPANY_ABN, acn=COMPANY_ACN, verifier=_abr_ok(
                abn=NON_COMPANY_ABN, acn=None, entity_type_code="OIE"))
        self.assertEqual(ctx.exception.code, reg.ACN_MISMATCH)

    def test_unexpected_verifier_failure_is_structured(self):
        def failing_verifier(abn):
            raise TimeoutError("provider failure")
        with self.assertRaises(CompanyRegistrationError) as ctx:
            self._run(verifier=failing_verifier)
        self.assertEqual(ctx.exception.code, reg.ABR_UNVERIFIABLE)

    def test_supplied_acn_mismatch_raises(self):
        with self.assertRaises(CompanyRegistrationError) as ctx:
            self._run(acn=OTHER_ACN)
        self.assertEqual(ctx.exception.code, reg.ACN_MISMATCH)
        self.assertEqual(ctx.exception.field, "acn")

    def test_acn_disagreeing_with_abn_raises_mismatch(self):
        # ABR returns an ACN that doesn't match the one embedded in the ABN.
        with self.assertRaises(CompanyRegistrationError) as ctx:
            self._run(verifier=_abr_ok(acn=OTHER_ACN))
        self.assertEqual(ctx.exception.code, reg.ACN_MISMATCH)

    def test_invalid_acn_checksum_raises(self):
        # 94807394137 passes the ABN checksum but its embedded ACN (807394137) does not
        # pass the ACN checksum — a self-consistent value that is still not a real ACN.
        with self.assertRaises(CompanyRegistrationError) as ctx:
            self._run(abn=NON_COMPANY_ABN, verifier=_abr_ok(abn=NON_COMPANY_ABN, acn="807394137"))
        self.assertEqual(ctx.exception.code, reg.ACN_INVALID)

    def test_falls_back_to_derived_acn_when_abr_omits_it(self):
        company = self._run(verifier=_abr_ok(acn=None))
        self.assertEqual(company.acn, COMPANY_ACN)

    @override_settings(VIBE_RAISING_SKIP_ABR_VERIFICATION=True)
    def test_skip_flag_cannot_manufacture_verification(self):
        company = _StubCompany()
        with self.assertRaises(CompanyRegistrationError):
            verify_and_persist_company_registration(company, abn=COMPANY_ABN,
                abr_verifier=_abr_ok(configured=False, reachable=False))
        self.assertFalse(company.registered)
        self.assertIsNone(company.abr_verified_at)

    @override_settings(VIBE_RAISING_SKIP_ABR_VERIFICATION=True)
    def test_skip_flag_still_rejects_bad_abn(self):
        with self.assertRaises(CompanyRegistrationError):
            verify_and_persist_company_registration(_StubCompany(), abn="94807394138")


class VerificationStatusTests(SimpleTestCase):
    def test_failure_clears_stale_verification_and_reports_reason(self):
        company = _StubCompany()
        verify_and_persist_company_registration(company, abn=COMPANY_ABN, abr_verifier=_abr_ok())
        self.assertFalse(attempt_company_verification(company, abn="94807394138", save=False))
        self.assertFalse(company.registered)
        self.assertIsNone(company.abr_verified_at)
        self.assertEqual(company_registration_status(company), {
            "verified": False, "code": "ABN_INVALID", "detail": reg._DEFAULT_MESSAGES[reg.ABN_INVALID], "field": "abn"})

    def test_self_declared_registration_does_not_qualify(self):
        company = _StubCompany()
        company.registered = True
        company.abn = COMPANY_ABN
        self.assertFalse(company_is_verified(company))

    def test_legacy_skip_abr_stamp_without_entity_type_does_not_qualify(self):
        company = _StubCompany()
        verify_and_persist_company_registration(company, abn=COMPANY_ABN, abr_verifier=_abr_ok())
        company.entity_type_code = ""
        self.assertFalse(company_is_verified(company))

    def test_company_dto_includes_machine_readable_status(self):
        from founder_tools.models import VibeRaisingCompany
        from founder_tools.serializers import FounderCompanySerializer
        company = VibeRaisingCompany(name="MLAI", abn=NON_COMPANY_ABN)
        self.assertFalse(attempt_company_verification(company, abn="bad", save=False))
        data = FounderCompanySerializer(company).data
        self.assertFalse(data["registrationVerification"]["verified"])
        self.assertEqual(data["registrationVerification"]["code"], "ABN_INVALID")
        self.assertEqual(data["registrationVerification"]["field"], "abn")

    def test_changing_or_clearing_abn_removes_old_verification(self):
        for value in (NON_COMPANY_ABN, "", None):
            with self.subTest(value=value):
                company = _StubCompany()
                verify_and_persist_company_registration(company, abn=COMPANY_ABN, abr_verifier=_abr_ok())
                reg.set_unverified_company_abn(company, value)
                self.assertFalse(company_is_verified(company))
                self.assertIsNone(company.abr_verified_at)
                self.assertIsNone(company.acn)


@override_settings(ABR_LOOKUP_AUTHENTICATION_GUID="abr-guid")
class CompanySaveContractTests(SimpleTestCase):
    """Exercise the shared Chat save handler with storage stubbed, never migrated."""

    def save_company(self, body, xml=""):
        from founder_tools.models import VibeRaisingCompany
        from founder_tools import views
        company = VibeRaisingCompany(name="New startup")
        company.save = MagicMock()
        company.refresh_from_db = MagicMock()
        profile = SimpleNamespace(pk="profile-fixture", role="founder", active_company_id="existing")
        request = SimpleNamespace(data={"name": "MLAI", "createNew": True, **body}, user=SimpleNamespace())
        with (
            patch.object(views, "get_or_create_founder_profile", return_value=profile),
            patch.object(views.VibeRaisingProfile.objects, "select_for_update", return_value=SimpleNamespace(get=MagicMock(return_value=profile))),
            patch("founder_tools.services.find_company_with_domain", return_value=None),
            patch.object(views, "VibeRaisingCompany", return_value=company),
            patch.object(views, "ensure_company_organization"),
            patch.object(views, "apply_shared_startup_details"),
            patch("content_factory.vibe_marketing_views.http_client.get", return_value=_FakeResponse(text=xml)),
        ):
            response = views.FounderToolsCompanyView.post.__wrapped__(views.FounderToolsCompanyView(), request)
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_chat_company_save_returns_nfp_verification(self):
        data = self.save_company({"abn": NON_COMPANY_ABN}, _NON_COMPANY_XML)
        self.assertTrue(data["registrationVerification"]["verified"])
        self.assertTrue(data["abrVerifiedAt"])
        self.assertEqual(data["abn"], NON_COMPANY_ABN)
        self.assertIsNone(data["acn"])

    def test_chat_acn_only_save_resolves_company_abn(self):
        data = self.save_company({"acn": COMPANY_ACN}, _company_xml())
        self.assertTrue(data["registrationVerification"]["verified"])
        self.assertEqual(data["abn"], COMPANY_ABN)
        self.assertEqual(data["acn"], COMPANY_ACN)

    def test_chat_save_cannot_self_assert_verification(self):
        data = self.save_company({"abn": "bad", "registered": True,
            "abrVerifiedAt": "2026-09-01T00:00:00Z", "registrationVerification": {"verified": True}})
        self.assertFalse(data["registered"])
        self.assertFalse(data["registrationVerification"]["verified"])
        self.assertIsNone(data["abrVerifiedAt"])
        self.assertEqual(data["registrationVerification"]["code"], "ABN_INVALID")
