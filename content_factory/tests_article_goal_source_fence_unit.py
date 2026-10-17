"""An unlisted workflow cannot borrow a source-fenced goal's generic authority."""
from uuid import uuid4

from . import website_article_source_fence as fences
from django.test import SimpleTestCase, override_settings
from . import tests_article_credential_inventory_unit as fixtures
from .website_contract import WebsiteAuthorityError
from .website_operations import validate_operation


@override_settings(**fixtures.ArticleCredentialInventoryTests._overridden_settings)
class ArticleGoalSourceFenceTests(SimpleTestCase):
    def setUp(self):
        fixture = fixtures.ArticleCredentialInventoryTests("runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        for key in ("parent", "connection", "operation", "data", "mint", "issue", "tokens"):
            setattr(self, key, getattr(fixture, key))

    def test_unlisted_goal_operation_cannot_mint_after_parent_source_fence(self):
        self.parent.receipt[fences.KEY] = str(uuid4())
        self.assertNotIn(fences.KEY, self.operation.receipt)
        with self.assertRaises(WebsiteAuthorityError): self.issue()
        self.mint.assert_not_called()
        self.assertEqual(self.tokens(), [])

    def test_unknown_original_operation_cannot_bypass_goal_fence_as_restoration(self):
        self.parent.receipt[fences.KEY] = str(uuid4())
        for options in ({}, {"worker_cleanup": True}, {"restoration": True}):
            with self.subTest(options=options), self.assertRaises(WebsiteAuthorityError):
                validate_operation(self.connection, self.data, **options)

    def test_cancel_receipt_still_removes_effects_without_reopening_goal_authority(self):
        self.parent.receipt[fences.KEY] = str(uuid4())
        self.operation.state = "cancelled"
        self.operation.payload["run_id"] = self.data["run_id"]
        self.data["status"] = "cancelled"
        self.assertIs(validate_operation(self.connection, self.data, cancellation=True), self.operation)
        self.assertIs(validate_operation(self.connection, self.data, cancellation_receipt=True), self.operation)
