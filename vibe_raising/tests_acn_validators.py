"""Unit tests for the ABN/ACN validation helpers (no DB, no network)."""

from django.test import SimpleTestCase

from vibe_raising.validators import (
    COMPANY_ENTITY_TYPE_CODES,
    acn_from_abn,
    format_acn,
    is_registered_company_entity_type,
    normalize_abn,
    normalize_acn,
    validate_abn_checksum,
    validate_acn_checksum,
)

# Mathematically-consistent company pairs: ABN == 2 check digits + 9-digit ACN, and
# both halves pass their own checksums.
COMPANY_ABN_A = "89000000019"
COMPANY_ACN_A = "000000019"
COMPANY_ABN_B = "25010499966"
COMPANY_ACN_B = "010499966"

# A valid ABN that is NOT a company (MLAI Aus Inc, an incorporated association): the
# ABN checksum passes but the trailing nine digits are not a valid ACN.
NON_COMPANY_ABN = "94807394137"


class ValidateAbnChecksumTests(SimpleTestCase):
    def test_accepts_known_valid_abns(self):
        for abn in (COMPANY_ABN_A, COMPANY_ABN_B, NON_COMPANY_ABN, "51824753556"):
            self.assertTrue(validate_abn_checksum(abn), abn)

    def test_accepts_formatted_abn(self):
        self.assertTrue(validate_abn_checksum("94 807 394 137"))

    def test_rejects_bad_checksum(self):
        self.assertFalse(validate_abn_checksum("94807394138"))

    def test_rejects_wrong_length(self):
        for value in ("", "123", "9480739413", "948073941370"):
            self.assertFalse(validate_abn_checksum(value), value)

    def test_rejects_none_and_non_numeric(self):
        self.assertFalse(validate_abn_checksum(None))
        self.assertFalse(validate_abn_checksum("abcdefghijk"))


class ValidateAcnChecksumTests(SimpleTestCase):
    def test_accepts_known_valid_acns(self):
        for acn in (COMPANY_ACN_A, COMPANY_ACN_B):
            self.assertTrue(validate_acn_checksum(acn), acn)

    def test_accepts_formatted_acn(self):
        self.assertTrue(validate_acn_checksum("010 499 966"))

    def test_rejects_bad_checksum(self):
        self.assertFalse(validate_acn_checksum("000000018"))

    def test_rejects_wrong_length(self):
        for value in ("", "123", "00000001", "0000000190"):
            self.assertFalse(validate_acn_checksum(value), value)

    def test_rejects_none(self):
        self.assertFalse(validate_acn_checksum(None))


class AcnFromAbnTests(SimpleTestCase):
    def test_derives_acn_from_company_abn(self):
        self.assertEqual(acn_from_abn(COMPANY_ABN_A), COMPANY_ACN_A)
        self.assertEqual(acn_from_abn(COMPANY_ABN_B), COMPANY_ACN_B)

    def test_derived_acn_is_checksum_valid_for_company_abn(self):
        derived = acn_from_abn(COMPANY_ABN_A)
        self.assertTrue(validate_acn_checksum(derived))

    def test_derived_acn_fails_checksum_for_non_company_abn(self):
        # The derivation always returns nine digits, but for a non-company ABN they are
        # not a real ACN — the checksum is what actually gates company-ness.
        derived = acn_from_abn(NON_COMPANY_ABN)
        self.assertEqual(len(derived), 9)
        self.assertFalse(validate_acn_checksum(derived))

    def test_handles_formatting_and_bad_input(self):
        self.assertEqual(acn_from_abn("89 000 000 019"), COMPANY_ACN_A)
        self.assertIsNone(acn_from_abn("123"))
        self.assertIsNone(acn_from_abn(None))


class NormalizeAndFormatTests(SimpleTestCase):
    def test_normalize_abn(self):
        self.assertEqual(normalize_abn("94 807 394 137"), "94807394137")
        self.assertIsNone(normalize_abn("123"))
        self.assertIsNone(normalize_abn(None))

    def test_normalize_acn(self):
        self.assertEqual(normalize_acn("010 499 966"), "010499966")
        self.assertIsNone(normalize_acn("12345678"))
        self.assertIsNone(normalize_acn(None))

    def test_format_acn(self):
        self.assertEqual(format_acn("010499966"), "010 499 966")
        self.assertEqual(format_acn("010 499 966"), "010 499 966")

    def test_format_acn_passes_through_non_acn(self):
        self.assertEqual(format_acn("pending"), "pending")
        self.assertEqual(format_acn(None), "")


class EntityTypeTests(SimpleTestCase):
    def test_company_codes_pass(self):
        for code in COMPANY_ENTITY_TYPE_CODES:
            self.assertTrue(is_registered_company_entity_type(code))

    def test_case_insensitive_and_trimmed(self):
        self.assertTrue(is_registered_company_entity_type(" prv "))

    def test_non_company_codes_fail(self):
        for code in ("IND", "DTT", "FPT", "", None):
            self.assertFalse(is_registered_company_entity_type(code), code)


class NonprofitEntityTypeTests(SimpleTestCase):
    def test_nonprofit_codes_pass(self):
        from vibe_raising.validators import NONPROFIT_ENTITY_TYPE_CODES, is_nonprofit_entity_type

        for code in NONPROFIT_ENTITY_TYPE_CODES:
            self.assertTrue(is_nonprofit_entity_type(code))
        self.assertTrue(is_nonprofit_entity_type(" oie "))

    def test_company_and_blank_codes_are_not_nonprofit(self):
        from vibe_raising.validators import is_nonprofit_entity_type

        for code in ("PRV", "PUB", "IND", "", None):
            self.assertFalse(is_nonprofit_entity_type(code), code)
