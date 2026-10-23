import io
from datetime import datetime, timezone

from django.test import SimpleTestCase

from crm.contact_import import (
    ImportFileError,
    parse_consent_datetime,
    plan_columns,
    read_csv,
)


class ColumnPlanTests(SimpleTestCase):
    def test_common_export_headers_map_to_contact_fields(self):
        plan = plan_columns(
            ["Email Address", "First Name", "LAST_NAME", "Company", "OPTIN_TIME", "Favourite model"]
        )
        self.assertEqual(
            plan,
            {
                "Email Address": "email",
                "First Name": "first_name",
                "LAST_NAME": "last_name",
                "Company": "company",
                "OPTIN_TIME": "consent_at",
                "Favourite model": "attributes.favourite_model",
            },
        )

    def test_overrides_win_and_unknown_fields_are_rejected(self):
        plan = plan_columns(["Work email", "Notes"], {"work email": "email", "Notes": "ignore"})
        self.assertEqual(plan, {"Work email": "email", "Notes": "ignore"})
        with self.assertRaises(ImportFileError):
            plan_columns(["email", "x"], {"x": "password"})

    def test_a_file_without_an_email_column_is_refused(self):
        with self.assertRaises(ImportFileError):
            plan_columns(["Name", "Company"])

    def test_a_second_column_for_the_same_field_becomes_an_attribute(self):
        plan = plan_columns(["email", "e-mail"])
        self.assertEqual(plan, {"email": "email", "e-mail": "attributes.e_mail"})


class ParseRowsTests(SimpleTestCase):
    def parse(self, text):
        return read_csv(io.StringIO(text))

    def test_rows_are_validated_deduplicated_and_merged(self):
        _, parsed = self.parse(
            "Email,First name,Company,Tags,Opt-in date,Source\n"
            "ada@example.com,Ada,,founder; speaker,2025-03-04 09:30:00,Website form\n"
            "not-an-email,Bob,,,,\n"
            "ADA@example.com,,Engines Ltd,speaker,,\n"
            ",Nobody,,,,\n"
        )

        self.assertEqual(parsed.counts["rows"], 4)
        self.assertEqual(parsed.counts["invalid"], 2)
        self.assertEqual(parsed.counts["duplicate"], 1)
        self.assertEqual([error["row"] for error in parsed.errors], [3, 5])
        self.assertNotIn("@", str(parsed.errors))
        [row] = parsed.rows
        self.assertEqual(row.email, "ada@example.com")
        self.assertEqual(row.profile, {"first_name": "Ada", "company": "Engines Ltd"})
        self.assertEqual(row.tags, ["founder", "speaker"])
        self.assertEqual(row.consent_at, datetime(2025, 3, 4, 9, 30, tzinfo=timezone.utc))
        self.assertEqual(row.consent_source, "Website form")

    def test_non_https_linkedin_is_kept_as_an_attribute(self):
        _, parsed = self.parse("email,linkedin\nada@example.com,linkedin.com/in/ada\n")
        self.assertEqual(parsed.rows[0].profile, {})
        self.assertEqual(parsed.rows[0].attributes, {"linkedin": "linkedin.com/in/ada"})

    def test_whatsapp_opt_in_column(self):
        plan, parsed = self.parse(
            "email,mobile,WhatsApp opt-in\nada@example.com,0412 345 678,Yes\nbob@example.com,,no\n"
        )
        self.assertEqual(plan["WhatsApp opt-in"], "whatsapp_opt_in")
        self.assertEqual(plan["mobile"], "phone")
        self.assertEqual([row.whatsapp_opt_in for row in parsed.rows], [True, False])
        self.assertEqual(parsed.rows[0].profile["phone"], "0412 345 678")

    def test_unparseable_consent_dates_are_counted(self):
        _, parsed = self.parse("email,consent date\nada@example.com,last tuesday\n")
        self.assertIsNone(parsed.rows[0].consent_at)
        self.assertEqual(parsed.counts["consent_date_unparsed"], 1)

    def test_empty_file_is_refused(self):
        with self.assertRaises(ImportFileError):
            self.parse("")


class ConsentDateTests(SimpleTestCase):
    def test_iso_and_australian_formats(self):
        expected = datetime(2025, 3, 4, tzinfo=timezone.utc)
        self.assertEqual(parse_consent_datetime("2025-03-04"), expected)
        self.assertEqual(parse_consent_datetime("04/03/2025"), expected)
        self.assertEqual(
            parse_consent_datetime("2025-03-04T10:00:00+10:00"),
            datetime(2025, 3, 4, tzinfo=timezone.utc),
        )
        self.assertIsNone(parse_consent_datetime("13/13/2025"))
