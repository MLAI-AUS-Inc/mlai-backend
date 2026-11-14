"""Execution snapshots contain bounded metadata, never repository source."""
import copy
import unittest
import json
from pathlib import Path
from roo import cybertest_delivery as delivery
from roo.cybertest_policy import CheckoutError
from roo.test_cybertest_delivery import basic


def plan():
    contract = delivery.purchase_contract({**basic(), 'commitSha': 'c'*40, 'sourceManifestSha256': 'd'*64})
    return {'algorithm': 'basic-source-v1', 'contract': contract, 'commitSha': contract['commitSha'],
            'sourceManifestSha256': contract['sourceManifestSha256'], 'scope': ['get-client-referrals'],
            'route': 'supabase', 'model': 'kimi-k3', 'pricing': {'input': 3, 'cachedInput': .3, 'output': 15},
            'contextSha256': 'a'*64,
            'execution': {'maxSourceBytes': 1000000, 'maxFiles': 100, 'maxSourceBatches': 10,
                          'maxDurationMs': 60000, 'maxModelUsd': 5, 'reportReserveUsd': 1},
            'workload': {'files': 1, 'sourceBytes': 10, 'sourceBatches': 1, 'inputTokenUpperBound': 100,
                         'outputTokenLimit': 16384, 'sourceReserveUsd': .3},
            'checks': contract['checks'], 'batchRanges': [[{'file': 'functions/hello/index.ts', 'start': 0, 'end': 10}]]}


class ExecutionPlanTests(unittest.TestCase):
    def test_customer_scope_contains_the_purchased_checks_but_not_internal_execution_settings(self):
        from roo import cybertest_plan
        value = json.loads((Path(__file__).parent/'fixtures/upgrade-execution-plan-v1.json').read_text())
        public = cybertest_plan.public_purchase_scope({**value['contract'], 'executionPlan': value})
        self.assertEqual(public['runtime_targets'], ['hello'])
        self.assertEqual(public['source_checks'], [
            {'kind': c['kind'], 'files': c['files']} for c in value['sourceChecks']])
        self.assertEqual(public['commit_sha'], value['commitSha'])
        self.assertEqual(public['phase'], 'upgrade')
        self.assertEqual(public['total_points'], 48)
        self.assertEqual(public['due_points'], 24)
        self.assertNotIn('policy', public)
        self.assertNotIn('pricing', public)
        self.assertNotIn('model', public)
        self.assertEqual(cybertest_plan.public_purchase_scope({}), None)

    def test_actual_upgrade_plan_roundtrips_and_rejects_undeclared_or_unbounded_work(self):
        from roo.cybertest_plan import execution_plan
        value = json.loads((Path(__file__).parent/'fixtures/upgrade-execution-plan-v1.json').read_text())
        self.assertEqual(execution_plan(value, value['contract']), value)
        for mutate in [lambda p: p['runtimeTargets'].append(p['runtimeTargets'][0]),
                       lambda p: p['runtimeTargets'][0].update(name='../outside'),
                       lambda p: p['sourceChecks'][0].update(files=['../outside']),
                       lambda p: p['sourceChecks'][0].update(rawSource='source'),
                       lambda p: p['sourceChecks'][0].update(checkId='undeclared'),
                       lambda p: p['sourceChecks'][0].update(kind='source_batch'),
                       lambda p: p['execution'].update(maxParallelRuntime=3),
                       lambda p: p['workload'].update(requestReserveUsd=1000),
                       lambda p: p['workload'].update(runtimeTargets=2),
                       lambda p: p.update(baseReportSha256='invalid'),
                       lambda p: p.update(parentAuditId='invalid'),
                       lambda p: p.update(coverage=[{'raw': 'secret'}]),
                       lambda p: p['policy']['large'].update(maxDurationMs=0)]:
            changed = copy.deepcopy(value)
            mutate(changed)
            with self.subTest(changed=changed), self.assertRaises(CheckoutError):
                execution_plan(changed, value['contract'])

    def test_actual_node_plan_is_accepted_without_source_or_field_loss(self):
        from roo.cybertest_plan import execution_plan
        value = json.loads((Path(__file__).parent/'fixtures/basic-execution-plan-v1.json').read_text())
        self.assertEqual(execution_plan(value, value['contract']), value)

    def normalize(self, value):
        from roo import cybertest_plan
        return cybertest_plan.execution_plan(value, plan()['contract'])

    def test_bounded_snapshot_roundtrips_and_is_copied(self):
        value = plan()
        actual = self.normalize(value)
        self.assertEqual(actual, value)
        actual['execution']['maxFiles'] = 1
        self.assertEqual(value['execution']['maxFiles'], 100)

    def test_rejects_raw_source_changed_contract_and_unbounded_metadata(self):
        for mutate in [lambda v: v.update(rawSource='secret'),
                       lambda v: v['batchRanges'][0][0].update(content='source'),
                       lambda v: v['batchRanges'][0][0].update(file='../outside'),
                       lambda v: v['contract'].update(planId='f'*64),
                       lambda v: v.update(checks=[None]),
                       lambda v: v['execution'].update(maxDurationMs=0),
                       lambda v: v['execution'].update(reportReserveUsd=5),
                       lambda v: v['workload'].update(sourceBatches=2),
                       lambda v: v['execution'].update(maxModelUsd=10**1000),
                       lambda v: v.update(scope=['different'])]:
            value = copy.deepcopy(plan())
            mutate(value)
            # Scope is bound by the quote selection, not by this metadata validator.
            if value['scope'] == ['different']:
                self.assertEqual(self.normalize(value)['scope'], ['different'])
            else:
                with self.assertRaises(CheckoutError):
                    self.normalize(value)
