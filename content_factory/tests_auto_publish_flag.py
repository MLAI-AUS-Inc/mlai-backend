"""Tests for the org-level auto-publish flag wiring.

`OrganizationContentConfig.auto_publish` opts an org into automatic publishing:
generated article PRs auto-merge once automated build/preview verification passes,
with no human review. `requires_review` is an explicit override that forces human
review (PR only) and wins over `auto_publish`.
"""
from types import SimpleNamespace

from django.test import TestCase

from content_factory.models import OrganizationContentConfig
from content_factory.vibe_marketing_views import _org_config_enables_auto_publish
from organizations.models import Organization


class OrgAutoPublishFlagTests(TestCase):
    def _context(self, **config_kwargs):
        org = Organization.objects.create(domain="statdoctor.app", name="StatDoctor")
        OrganizationContentConfig.objects.create(organization=org, **config_kwargs)
        return SimpleNamespace(organization=org)

    def test_auto_publish_enables_auto_merge(self):
        self.assertTrue(_org_config_enables_auto_publish(self._context(auto_publish=True)))

    def test_requires_review_overrides_auto_publish(self):
        context = self._context(auto_publish=True, requires_review=True)
        self.assertFalse(_org_config_enables_auto_publish(context))

    def test_disabled_by_default(self):
        self.assertFalse(_org_config_enables_auto_publish(self._context()))

    def test_missing_organization_is_safe(self):
        self.assertFalse(_org_config_enables_auto_publish(SimpleNamespace(organization=None)))
