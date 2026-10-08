"""Profile/logo contract checks with isolated storage, no database or network."""
from contextlib import nullcontext
from io import BytesIO
from types import SimpleNamespace as Obj
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image
from rest_framework.exceptions import ValidationError

from content_factory.editorial_catalog import merge_strategy, public_strategy
from content_factory.models import OrganizationContentConfig
from founder_tools import profile_fields as fields
from founder_tools.logo_images import encode_company_logo, MAX_LOGO_BYTES
from founder_tools.serializers import FounderCompanyUpsertSerializer, _serialize_startup_profile
from organizations.models import Organization


class ProfileFieldsTests(SimpleTestCase):
    def test_sparse_validation_preserves_omission_and_explicit_empty_values(self):
        serializer = FounderCompanyUpsertSerializer(data={"name": "Acme", "shortDescription": ""})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["shortDescription"], "")
        for key in ("domain", "companyContext", "problemSolved", "founderProfiles", "hasRevenue", "targetAudience"):
            self.assertNotIn(key, serializer.validated_data)
        self.assertEqual(fields.validate_profile_fields({"hasRevenue": "", "founderProfiles": []}),
                         {"hasRevenue": "", "founderProfiles": [], "founderNames": []})

    def test_structured_founders_and_revenue_round_trip_without_erasing_other_namespaces(self):
        profile = {"founderProfiles": [{"name": "Ada", "linkedinUrl": "https://www.linkedin.com/in/ada"}], "hasRevenue": "No"}
        config = Obj(pillar_strategy={"startup_branding": {"avatarUrl": "logo"},
                                     "editorial_catalog": {"version": 2}}, save=Mock())
        organization = Obj(pk=5, startup_profile=Obj(founder_names=["Ada"], stage="Seed", notes="",
            company_aliases=[], domain_aliases=[], competitor_domains=[], positive_keywords=[]))
        with patch.object(fields.transaction, "atomic", return_value=nullcontext()), \
             patch.object(Organization.objects, "select_for_update") as orgs, \
             patch.object(OrganizationContentConfig.objects, "select_for_update") as configs, \
             patch.object(OrganizationContentConfig.objects, "filter") as read:
            orgs.return_value.get.return_value = organization
            configs.return_value.get_or_create.return_value = (config, False)
            read.return_value.first.return_value = config
            fields.save_profile_details(organization, fields.validate_profile_fields(profile))
            result = _serialize_startup_profile(organization)
            self.assertEqual({key: result[key] for key in profile}, profile)
            fields.save_profile_details(organization, {"hasRevenue": "Yes"})
            self.assertEqual(fields.profile_details(organization)["founderProfiles"], profile["founderProfiles"])
            self.assertEqual(config.pillar_strategy["startup_branding"], {"avatarUrl": "logo"})
            self.assertEqual(config.pillar_strategy["editorial_catalog"], {"version": 2})

    def test_invalid_fields_are_rejected_before_writes(self):
        for payload in ({"defaultTimezone": "invalid/zone"}, {"hasRevenue": "maybe"},
                        {"founderProfiles": [{"name": "Ada", "linkedinUrl": "https://evil.test/in/ada"}]},
                        {"founderProfiles": [{"name": ""}]}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                fields.validate_profile_fields(payload)

    def test_saving_provisional_profile_promotes_it_even_without_optional_field_edits(self):
        config = Obj(pillar_strategy={fields.PROFILE_KEY: {"researchDraft": True, "hasRevenue": "No"}}, save=Mock())
        with patch.object(fields.transaction, "atomic", return_value=nullcontext()), \
             patch.object(Organization.objects, "select_for_update"), \
             patch.object(OrganizationContentConfig.objects, "select_for_update") as configs:
            configs.return_value.get_or_create.return_value = (config, False)
            fields.save_profile_details(Obj(pk=5), {"name": "Saved startup"}, config=config)
        self.assertEqual(config.pillar_strategy[fields.PROFILE_KEY], {"hasRevenue": "No"})
        config.save.assert_called_once()

    def test_managed_lists_exclude_only_explicit_research_drafts(self):
        queryset = Mock()
        fields.visible_companies(queryset)
        queryset.filter.assert_called_once_with(
            organization__content_config__pillar_strategy__startup_profile_details__researchDraft=True)
        queryset.exclude.assert_called_once_with(pk__in=queryset.filter.return_value.values.return_value)

    def test_scan_merge_cannot_replace_or_export_profile_and_brand_metadata(self):
        saved = {"startup_profile_details": {"hasRevenue": "No"}, "startup_branding": {"avatarUrl": "saved"}}
        updated = merge_strategy(saved, {"startup_branding": {"avatarUrl": "forged"}, "topics": ["new"]})
        self.assertEqual(updated["startup_branding"], saved["startup_branding"])
        self.assertEqual(updated["startup_profile_details"], saved["startup_profile_details"])
        self.assertEqual(public_strategy(updated), {"topics": ["new"]})

    def test_removed_canonical_logo_does_not_resurrect_legacy_company_avatar(self):
        company = Obj(organization_id=5, organization=Obj(pk=5), avatar_url="legacy")
        with patch.object(fields, "organization_branding", return_value={"avatarUrl": ""}):
            self.assertEqual(fields.company_avatar_url(company), "")
        with patch.object(fields, "organization_branding", return_value={}):
            self.assertEqual(fields.company_avatar_url(company), "legacy")

    def test_branding_changes_require_established_owner_and_preserve_profile(self):
        from founder_tools.services import DomainOwnershipError
        company = Obj(pk="company", organization_id=5, avatar_url="before", save=Mock())
        organization = Obj(pk=5)
        config = Obj(pillar_strategy={"startup_profile_details": {"hasRevenue": "Yes"}}, save=Mock())
        with patch.object(Organization.objects, "select_for_update") as orgs, \
             patch.object(OrganizationContentConfig.objects, "select_for_update") as configs, \
             patch("founder_tools.services.user_may_use_organization", return_value=False) as owns:
            orgs.return_value.get.return_value = organization
            configs.return_value.get_or_create.return_value = (config, False)
            with self.assertRaises(DomainOwnershipError):
                fields.save_company_branding.__wrapped__(company, Obj(id=1), "new")
            config.save.assert_not_called()
            company.save.assert_not_called()
            owns.return_value = True
            fields.save_company_branding.__wrapped__(company, Obj(id=1), "")
            self.assertEqual(config.pillar_strategy["startup_branding"], {"avatarUrl": "", "companyId": "company"})
            self.assertEqual(config.pillar_strategy["startup_profile_details"], {"hasRevenue": "Yes"})
            self.assertIsNone(company.avatar_url)


class LogoImagesTests(SimpleTestCase):
    def test_firebase_download_url_fits_company_field_without_truncation(self):
        from founder_tools.models import VibeRaisingCompany
        field = VibeRaisingCompany._meta.get_field("avatar_url")
        url = (
            "https://firebasestorage.googleapis.com/v0/b/mlai-main-website.firebasestorage.app/o/"
            f"company-avatars%2F{uuid4()}%2F{uuid4().hex}.png?alt=media&token={uuid4()}"
        )
        self.assertGreater(len(url), 200)
        self.assertEqual(field.max_length, 2048)
        self.assertEqual(field.clean(url, None), url)

    def image_upload(self, *, size=(32, 16), color=(255, 0, 0, 128)):
        output = BytesIO()
        Image.new("RGBA", size, color).save(output, format="PNG")
        return SimpleUploadedFile("logo.png", output.getvalue(), content_type="image/png")

    def test_logo_derivative_is_square_png_with_transparent_padding_and_alpha(self):
        result = Image.open(encode_company_logo(self.image_upload()))
        self.assertEqual((result.format, result.size, result.mode), ("PNG", (512, 512), "RGBA"))
        self.assertEqual(result.getpixel((0, 0))[3], 0)
        self.assertEqual(result.getpixel((256, 256))[3], 128)

    def test_invalid_and_oversized_uploads_fail_before_storage(self):
        for upload in (SimpleUploadedFile("logo.png", b"not an image"),
                       Obj(size=MAX_LOGO_BYTES + 1)):
            with self.assertRaises(ValueError):
                encode_company_logo(upload)

    def test_excessive_dimensions_are_rejected_before_decode(self):
        image = Obj(format="PNG", width=20_000, height=2, size=(20_000, 2), load=Mock())
        with patch("PIL.Image.open", return_value=image), self.assertRaises(ValueError):
            encode_company_logo(Obj(size=10))
        image.load.assert_not_called()


class ProfileSaveRegistrationTests(SimpleTestCase):
    def test_sparse_profile_save_preserves_registration_without_abr_call(self):
        from founder_tools import views
        company_id = uuid4()
        company = Obj(id=company_id, abn="saved-abn", acn="saved-acn", registered=True,
                      save=Mock(), refresh_from_db=Mock())
        profile = Obj(pk=1, role="founder", active_company_id=company_id)
        with patch.object(views, "get_or_create_founder_profile", return_value=profile), \
             patch.object(views.VibeRaisingProfile.objects, "select_for_update") as profiles, \
             patch.object(views, "get_object_or_404", return_value=company), \
             patch.object(views, "ensure_company_organization"), \
             patch.object(views, "apply_shared_startup_details"), \
             patch.object(views, "attempt_company_verification") as verify, \
             patch.object(views, "set_unverified_company_abn") as set_abn, \
             patch.object(views, "FounderCompanySerializer", return_value=Obj(data={"name": "New name"})):
            profiles.return_value.get.return_value = profile
            response = views.FounderToolsCompanyView.post.__wrapped__(views.FounderToolsCompanyView(),
                Obj(user=Obj(), data={"companyId": str(company_id), "name": "New name"}))
            self.assertEqual(response.status_code, 200)
            self.assertEqual((company.abn, company.acn, company.registered), ("saved-abn", "saved-acn", True))
            verify.assert_not_called()
            set_abn.assert_not_called()
            views.FounderToolsCompanyView.post.__wrapped__(views.FounderToolsCompanyView(),
                Obj(user=Obj(), data={"companyId": str(company_id), "name": "New name", "abn": ""}))
            set_abn.assert_called_once_with(company, None)
            verify.assert_called_once_with(company, abn="saved-abn", acn=None)
