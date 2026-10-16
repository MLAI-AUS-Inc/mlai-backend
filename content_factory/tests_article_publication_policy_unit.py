"""One publish decision across actual capability/journey/status/prepare readers."""
from copy import deepcopy
from itertools import product
from types import SimpleNamespace
from uuid import uuid4

from django.test import SimpleTestCase
from django.utils import timezone

from .activation import article_capabilities
from .website_journey import project_journey
from .website_goal_runner import ready
from .website_status import project_status


class ArticlePublicationPolicyTests(SimpleTestCase):
    def project(self,integrated,writable,deployed,*,write_allowed=True):
        evidence={"verified":integrated,"branch":"main","sha":"a"*40}
        if deployed is not None:evidence["publishingVerified"]=deployed
        capabilities=article_capabilities(SimpleNamespace(github_repo="owner/site"),domain="fixture.test",
            account={"owned":True,"saved":True},evidence=evidence,
            repository_access={"verified":True,"writable":writable,"branch":"main","sha":"a"*40})
        target={"key":"native","adapter":"react_article_system"}
        if deployed is True:target["deployment_receipt"]={"status":"passed","source_sha":"a"*40,
            "connection_generation":1,"target_id":"native","public_url":"https://fixture.test/articles",
            "checked_at":timezone.now().isoformat()}
        journey=project_journey(company_id=9,domain="fixture.test",capabilities=capabilities,target=target,
            website={"status":"connected","repositoryId":42,"verifiedSha":"a"*40,
                "connectionGeneration":1,"writePolicy":{"allowed":write_allowed}})
        return capabilities,journey

    def status_done(self,journey):
        op=SimpleNamespace(pk=uuid4(),receipt={},state="completed",action="prepare",next_attempt_at=None,
            updated_at=timezone.now(),connection=SimpleNamespace(github_repo="owner/site",organization_id=7))
        scope={"company_id":"9","connection_id":str(uuid4()),"connection_generation":1,"configuration_revision":1}
        return project_status(op,scope=scope,source_sha="a"*40,journey=journey)["state"]=="done"

    def test_truth_table_across_actual_capabilities_journey_prepare_and_status(self):
        for integrated,writable,deployed in product((False,True),(False,True),(False,True,None)):
            with self.subTest(integrated=integrated,writable=writable,deployed=deployed):
                capabilities,journey=self.project(integrated,writable,deployed)
                expected=integrated and writable and deployed is True
                self.assertEqual(capabilities["canPublishArticle"],expected)
                self.assertEqual(journey["capabilities"]["canPublishArticle"],expected)
                self.assertEqual(ready(SimpleNamespace(receipt={}),journey),expected)
                self.assertEqual(self.status_done(journey),expected)

    def test_paused_write_policy_overrides_a_previously_ready_capability(self):
        _,journey=self.project(True,True,True,write_allowed=False)
        self.assertFalse(journey["capabilities"]["canPublishArticle"])
        self.assertFalse(ready(SimpleNamespace(receipt={}),journey))
        self.assertFalse(self.status_done(journey))

    def test_bare_legacy_publish_label_cannot_replace_live_or_write_facts(self):
        for writable,deployed in ((False,True),(True,False)):
            _,journey=self.project(True,writable,deployed)
            journey=deepcopy(journey);journey["capabilities"]["canPublishArticle"]=True
            self.assertFalse(ready(SimpleNamespace(receipt={}),journey))
            self.assertFalse(self.status_done(journey))
