"""Tests for the ABR verification helper (B2) and the registration gate (B3)."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from content_factory.vibe_marketing_views import verify_company_with_abr
from vibe_raising import registration as reg
from vibe_raising.registration import (
    CompanyRegistrationError,
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
    def _verify(self, xml=None, *, status_code=200, raise_exc=False):
        def fake_get(url, params=None, timeout=None):
            if raise_exc:
                raise RuntimeError("boom")
            return _FakeResponse(status_code=status_code, text=xml or "")

        with patch("content_factory.vibe_marketing_views.http_client.get", side_effect=fake_get):
            return verify_company_with_abr(COMPANY_ABN)

    def test_registered_company_is_recognised(self):
        result = self._verify(_company_xml())
        self.assertTrue(result["configured"])
        self.assertTrue(result["reachable"])
        self.assertTrue(result["found"])
        self.assertTrue(result["active"])
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

    def test_non_company_raises_not_registered(self):
        with self.assertRaises(CompanyRegistrationError) as ctx:
            self._run(verifier=_abr_ok(is_company=False))
        self.assertEqual(ctx.exception.code, reg.NOT_A_REGISTERED_COMPANY)

    def test_supplied_acn_mismatch_raises(self):
        with self.assertRaises(CompanyRegistrationError) as ctx:
            self._run(acn=OTHER_ACN)
        self.assertEqual(ctx.exception.code, reg.ACN_MISMATCH)
        self.assertEqual(ctx.exception.field, "acn")

    def test_acn_disagreeing_with_abn_raises_mismatch(self):
        # ABR returns an ACN that doesn't match the one embedded in the ABN.
        with self.assertRaises(CompanyRegistrationError) as ctx:
            self._run(verifier=_abr_ok(acn="000000018"))
        self.assertEqual(ctx.exception.code, reg.ACN_MISMATCH)

    def test_invalid_acn_checksum_raises(self):
        # 94807394137 passes the ABN checksum but its embedded ACN (807394137) does not
        # pass the ACN checksum — a self-consistent value that is still not a real ACN.
        with self.assertRaises(CompanyRegistrationError) as ctx:
            self._run(abn=NON_COMPANY_ABN, verifier=_abr_ok(acn="807394137"))
        self.assertEqual(ctx.exception.code, reg.ACN_INVALID)

    def test_falls_back_to_derived_acn_when_abr_omits_it(self):
        company = self._run(verifier=_abr_ok(acn=None))
        self.assertEqual(company.acn, COMPANY_ACN)

    @override_settings(VIBE_RAISING_SKIP_ABR_VERIFICATION=True)
    def test_skip_flag_bypasses_abr_but_keeps_checksums(self):
        # No verifier should be consulted; ACN is derived and still checksum-gated.
        company = _StubCompany()
        verify_and_persist_company_registration(company, abn=COMPANY_ABN)
        self.assertTrue(company.registered)
        self.assertEqual(company.acn, COMPANY_ACN)

    @override_settings(VIBE_RAISING_SKIP_ABR_VERIFICATION=True)
    def test_skip_flag_still_rejects_bad_abn(self):
        with self.assertRaises(CompanyRegistrationError):
            verify_and_persist_company_registration(_StubCompany(), abn="94807394138")
