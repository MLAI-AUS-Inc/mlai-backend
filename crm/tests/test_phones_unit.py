from django.test import SimpleTestCase

from crm.phones import NO_PHONE, normalize_contact_phone, sms_allowed, sms_status_for


class NormalizeContactPhoneTests(SimpleTestCase):
    def test_australian_mobiles_in_every_common_format(self):
        for raw in (
            "0412 345 678",
            "0412-345-678",
            "(04) 1234 5678",
            "412345678",
            "61412345678",
            "+61 412 345 678",
            "+61 0412 345 678",
            "0061 412 345 678",
        ):
            with self.subTest(raw=raw):
                phone = normalize_contact_phone(raw)
                self.assertEqual(phone.e164, "+61412345678")
                self.assertTrue(phone.is_au_mobile)

    def test_landlines_and_other_countries(self):
        landline = normalize_contact_phone("(03) 9123 4567")
        self.assertEqual(landline.e164, "+61391234567")
        self.assertFalse(landline.is_au_mobile)
        uk = normalize_contact_phone("+44 7700 900123")
        self.assertEqual(uk.e164, "+447700900123")
        self.assertFalse(uk.is_au_mobile)

    def test_unreadable_numbers(self):
        for raw in ("", None, "call me", "12345", "+0123456789", "7700 900123", "+1234567890123456"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_contact_phone(raw), NO_PHONE)


class SmsRulesTests(SimpleTestCase):
    def test_sms_status_from_a_number(self):
        self.assertEqual(sms_status_for(normalize_contact_phone("0412345678"), "none"), "active")
        self.assertEqual(sms_status_for(normalize_contact_phone("0391234567"), "none"), "invalid")
        self.assertEqual(sms_status_for(normalize_contact_phone("+447700900123"), "none"), "active")
        self.assertEqual(sms_status_for(NO_PHONE, "none"), "none")

    def test_an_opt_out_always_sticks(self):
        self.assertEqual(sms_status_for(normalize_contact_phone("0412345678"), "opted_out"), "opted_out")

    def test_allowed_prefixes(self):
        self.assertTrue(sms_allowed("+61412345678", ["+614"]))
        self.assertFalse(sms_allowed("+447700900123", ["+614"]))
        self.assertFalse(sms_allowed("", ["+614"]))
