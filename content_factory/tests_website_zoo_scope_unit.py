"""Real read-only scope/command chain; database and network are forbidden."""
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase
from django.test import override_settings
from django.utils import timezone

from . import website_goal as goals, website_zoo_scope as scope

COMPANY = '11111111-1111-4111-8111-111111111111'
CONNECTION = '22222222-2222-4222-8222-222222222222'
GOAL = '33333333-3333-4333-8333-333333333333'
CHILD = '44444444-4444-4444-8444-444444444444'
SECRET = 'github_pat_zooScopePrivateCanary'
PRIVATE_PATH = 'private/customer/source-tree'


def observation():
    return {'schema_version': 1, 'mode': 'ready-sandbox', 'acceptance_profile': 'ready',
        'run_id': 'night-one', 'manifest_digest': 'a' * 64,
        'cases': [{'id': 'fixture-one', 'framework': 'html', 'fault': 'none'}],
        'policy_cases': ['declared-policy'],
        'results': [{'case_id': 'fixture-one', 'original_goal_id': GOAL,
            'started_binding': {'companyId': COMPANY, 'connectionId': CONNECTION, 'connectionGeneration': 2}}]}


def rows(value):
    query = MagicMock()
    for name in ('only', 'filter', 'select_related', 'annotate'):
        getattr(query, name).return_value = query
    query.first.side_effect = lambda: value() if callable(value) else value
    return query


class ZooScopeTests(SimpleTestCase):
    def setUp(self):
        self.company = SimpleNamespace(pk=UUID(COMPANY), organization_id=7)
        self.connection = SimpleNamespace(pk=UUID(CONNECTION), organization_id=7,
            organization=SimpleNamespace(domain='fixture.example'), generation=2, repository_id=99,
            configuration_version=3, branch='main', app_root=PRIVATE_PATH, blockers=[])
        self.goal = goals.new_goal(GOAL, goals.scope_for(self.connection, COMPANY), now=timezone.now())
        self.goal['child_run_history'] = ['fixture-run']
        self.operation = SimpleNamespace(pk=UUID(GOAL), payload={'company_id': COMPANY}, receipt={'goal': self.goal})
        request = {'workflow': 'repo_scan', 'website_connection_id': CONNECTION, 'connection_generation': 2,
            'repository_id': 99, 'domain': 'fixture.example', 'github_repo': 'mlai/fixture',
            'operation_id': CHILD, 'operation_attempt': 1, 'deletion_epoch': 0, 'client_request_id': SECRET,
            'expected_source_sha': 'b' * 40, 'branch': 'main', 'app_root': PRIVATE_PATH,
            'goal_budget': {'goal_id': GOAL, 'scope_digest': goals.digest(self.goal['scope'])},
            'github_token': SECRET, 'source_content': PRIVATE_PATH}
        self.connection.github_repo = 'mlai/fixture'
        self.run = SimpleNamespace(run_id='fixture-run', workflow='repo_scan', domain='fixture.example',
            github_repo='mlai/fixture', run_request=request, scope_generation=1, scope_state_version=9)
        self.child = SimpleNamespace(payload={'run_id': 'fixture-run', 'attempt': 1})
        self.order = []

    def boundaries(self):
        from founder_tools.models import VibeRaisingCompany
        from workflow_runs.models import ContentFactoryRun
        from .models import OrganizationContentConfig
        from .website_models import WebsiteConnectionOperation
        stack = ExitStack()
        def companies(*args):
            self.order.append('company_lookup')
            return rows(lambda: self.company)
        stack.enter_context(patch.object(VibeRaisingCompany.objects, 'only', side_effect=companies))
        stack.enter_context(patch.object(OrganizationContentConfig.objects, 'select_related',
            return_value=rows(lambda: SimpleNamespace(website_connection=self.connection))))
        def operations(**kwargs):
            return rows(lambda: self.operation if kwargs['action'] == 'prepare' else self.child)
        stack.enter_context(patch.object(WebsiteConnectionOperation.objects, 'filter', side_effect=operations))
        stack.enter_context(patch.object(ContentFactoryRun.objects, 'annotate', return_value=rows(lambda: self.run)))
        return stack

    def report(self, value=None):
        with self.boundaries():
            return scope.scope_report(value or observation())

    def test_actual_goal_and_saved_child_produce_closed_binding_digests_only(self):
        before = deepcopy(self.run.run_request)
        result = self.report()
        case = result['cases'][0]
        self.assertEqual((case['state'], case['runs_complete']), ('bound', True))
        self.assertEqual(case['organization_id'], '7')
        locator = case['runs'][0]
        self.assertEqual(locator['execution'], {'generation': 1, 'state_version': 9})
        self.assertEqual(locator['workflow'], 'repo_scan')
        self.assertIn('client_request_id', locator['scope_fields'])
        self.assertTrue(goals.DIGEST.fullmatch(locator['request_scope_digest']))
        self.assertTrue(goals.DIGEST.fullmatch(locator['goal_lineage_digest']))
        self.assertEqual(result['policy_cases'], ['declared-policy'])
        self.assertTrue(result['scope_evidence_only'])
        self.assertNotIn('effective_rollout', result)
        self.assertNotIn('eligibility', json.dumps(result))
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(PRIVATE_PATH, json.dumps(result))
        self.assertEqual(self.run.run_request, before)

    def test_scope_digest_is_from_closed_original_fields_not_arbitrary_request_data(self):
        first = self.report()['cases'][0]['runs'][0]
        self.run.run_request['github_token'] = 'different-secret'
        self.run.run_request['source_content'] = 'different-private-source'
        second = self.report()['cases'][0]['runs'][0]
        self.assertEqual(first, second)
        self.run.run_request['client_request_id'] = 'different-original-request'
        third = self.report()['cases'][0]['runs'][0]
        self.assertNotEqual(first['request_scope_digest'], third['request_scope_digest'])

    def test_missing_case_stays_in_autonomous_denominator_without_model_read(self):
        value = observation(); value['results'] = []
        result = self.report(value)
        self.assertEqual(result['cases'][0]['reason'], 'not_observed')
        self.assertEqual(self.order, [])
        self.assertEqual(result['policy_cases'], ['declared-policy'])

    def test_changed_company_selection_generation_or_original_goal_never_borrow_scope(self):
        mutations = [lambda: setattr(self.company, 'organization_id', None),
            lambda: setattr(self.connection, 'pk', UUID(CHILD)),
            lambda: setattr(self.connection, 'generation', 3),
            lambda: self.operation.payload.update(company_id=CHILD),
            lambda: self.goal.update(goal_id=CHILD),
            lambda: self.goal.update(target='first_article_live'),
            lambda: self.goal['scope'].update(configuration_revision=4)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.setUp(); mutate()
                result = self.report()['cases'][0]
                self.assertEqual(result['state'], 'unknown')
                self.assertEqual(result['runs'], [])

    def test_missing_or_contradictory_child_lineage_is_unknown_without_latest_run_fallback(self):
        mutations = [lambda: setattr(self, 'run', None),
            lambda: self.run.run_request.pop('goal_budget'),
            lambda: self.run.run_request['goal_budget'].update(goal_id=CHILD),
            lambda: self.run.run_request.update(first_article_goal_operation_id=CHILD),
            lambda: self.run.run_request.update(repository_id=True),
            lambda: self.run.run_request.update(connection_generation=3),
            lambda: self.run.run_request.update(domain='another.example'),
            lambda: self.run.run_request.update(deletion_epoch=1),
            lambda: self.run.run_request.update(operation_attempt=2),
            lambda: self.child.payload.update(run_id='another-run')]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.setUp(); mutate()
                case = self.report()['cases'][0]
                self.assertEqual(case['state'], 'bound')
                self.assertFalse(case['runs_complete'])
                self.assertEqual(case['reason'], 'child_lineage_unproved')
                self.assertEqual(case['runs'], [])

    def test_original_source_and_scope_survive_trusted_post_merge_configuration_change(self):
        self.run.run_request['configuration_revision'] = 1
        self.run.run_request['goal_budget']['scope_digest'] = 'c' * 64
        case = self.report()['cases'][0]
        self.assertTrue(case['runs_complete'])
        self.assertEqual(case['scope_digest'], goals.digest(self.goal['scope']))

    def test_article_workflow_maps_to_actual_cf_stream_and_requires_original_goal_link(self):
        self.run.workflow = 'article_generation'
        self.run.run_request.update(first_article_goal_id=GOAL, first_article_goal_operation_id=GOAL)
        self.assertEqual(self.report()['cases'][0]['runs'][0]['workflow'], 'direct_generate')
        self.run.run_request['source_run_id'] = 'source-run'
        self.assertEqual(self.report()['cases'][0]['runs'][0]['workflow'], 'confirmed_topic')
        self.run.run_request['first_article_goal_id'] = CHILD
        self.assertFalse(self.report()['cases'][0]['runs_complete'])

    def test_malformed_saved_data_and_credential_shaped_child_ids_never_escape(self):
        self.connection.blockers = [{'deletion_epoch': SECRET}]
        case = self.report()['cases'][0]
        self.assertEqual(case['reason'], 'saved_scope_unavailable')
        self.assertNotIn(SECRET, json.dumps(case))
        self.setUp(); self.goal['child_run_history'] = [SECRET]
        case = self.report()['cases'][0]
        self.assertEqual(case['reason'], 'child_references_unproved')
        self.assertNotIn(SECRET, json.dumps(case))

    def test_input_denominators_reject_duplicate_unexpected_malformed_and_missing_policy_scope(self):
        for damage in ('duplicate-case', 'duplicate-row', 'unexpected-row', 'no-policy', 'duplicate-policy', 'bad-mode', 'bad-case-id'):
            with self.subTest(damage=damage):
                value = observation()
                if damage == 'duplicate-case': value['cases'].append(deepcopy(value['cases'][0]))
                if damage == 'duplicate-row': value['results'].append(deepcopy(value['results'][0]))
                if damage == 'unexpected-row': value['results'][0]['case_id'] = 'not-declared'
                if damage == 'no-policy': value.pop('policy_cases')
                if damage == 'duplicate-policy': value['policy_cases'] *= 2
                if damage == 'bad-mode': value['mode'] = []
                if damage == 'bad-case-id': value['results'][0]['case_id'] = []
                with self.assertRaises(scope.ScopeInputError): scope.select_cases(value)

    def test_full_goal_target_is_selected_without_using_runtime_declarations(self):
        value = observation()
        value.update(schema_version=3, mode='one-shot-sandbox', acceptance_profile='first-article',
            declared_rollout={'credentials': SECRET})
        self.goal['target'] = 'first_article_live'
        for item in self.goal['milestones']:
            item['required'] = True
        result = self.report(value)
        self.assertEqual(result['target'], 'first_article_live')
        self.assertTrue(result['cases'][0]['runs_complete'])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_command_sets_snapshot_and_read_only_before_the_first_model_lookup(self):
        from .management.commands import inspect_articles_zoo_scope as command
        cursor = MagicMock()
        cursor.__enter__.return_value.execute.side_effect = self.order.append
        output = StringIO()
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'observation.json'; path.write_text(json.dumps(observation()))
            with self.boundaries(), patch.object(command, 'database', SimpleNamespace(vendor='postgresql', in_atomic_block=False,
                    connection=None, cursor=lambda: cursor)), \
                    patch.object(command.transaction, 'atomic', return_value=nullcontext()):
                call_command('inspect_articles_zoo_scope', observation=str(path), stdout=output)
        self.assertEqual(self.order[:2], ['SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY', 'company_lookup'])
        self.assertTrue(json.loads(output.getvalue())['cases'][0]['runs_complete'])

    def test_non_postgres_and_invalid_file_fail_before_any_model_lookup_without_echoing_path(self):
        from .management.commands import inspect_articles_zoo_scope as command
        for vendor in ('sqlite', 'postgresql'):
            with self.boundaries(), patch.object(command, 'database', SimpleNamespace(vendor=vendor,
                    in_atomic_block=False, connection=None)), \
                    self.assertRaises(CommandError) as raised:
                call_command('inspect_articles_zoo_scope', observation='/private/' + SECRET)
            self.assertNotIn(SECRET, str(raised.exception))
        self.assertEqual(self.order, [])

    def test_exact_actual_query_compiles_with_postgres_without_any_database_connection(self):
        from django.db.backends.postgresql.base import DatabaseWrapper
        from django.db.models.query import QuerySet
        from founder_tools.models import VibeRaisingCompany
        from workflow_runs.models import ContentFactoryRun
        from .models import OrganizationContentConfig
        database = DatabaseWrapper({'ENGINE': 'django.db.backends.postgresql', 'NAME': 'unused',
            'OPTIONS': {}, 'AUTOCOMMIT': True, 'TIME_ZONE': None}, alias='scope_compile_only')
        queries = []
        def compile_only(query):
            sql, parameters = query.query.get_compiler(connection=database).as_sql()
            queries.append((query.model, sql))
            if query.model is VibeRaisingCompany: return self.company
            if query.model is OrganizationContentConfig: return SimpleNamespace(website_connection=self.connection)
            if query.model is ContentFactoryRun: return self.run
            return self.operation if 'prepare' in parameters else self.child
        with patch.object(QuerySet, 'first', new=compile_only):
            result = scope.scope_report(observation())
        self.assertTrue(result['cases'][0]['runs_complete'])
        self.assertEqual(len(queries), 5)
        config_sql = next(sql for model, sql in queries if model is OrganizationContentConfig)
        self.assertNotIn('github_token_encrypted', config_sql)
        run_sql = next(sql for model, sql in queries if model is ContentFactoryRun)
        self.assertIn('scope_generation', run_sql)
        self.assertIn('scope_state_version', run_sql)
        self.assertNotIn('"result",', run_sql)
        self.assertIsNone(database.connection)

    def test_nested_or_manual_transaction_is_refused_before_orm_or_a_new_transaction(self):
        from .management.commands import inspect_articles_zoo_scope as command
        for atomic, connection in ((True, None), (False, object())):
            fake = SimpleNamespace(vendor='postgresql', in_atomic_block=atomic,
                connection=connection, get_autocommit=lambda: False)
            with self.boundaries(), patch.object(command, 'database', fake), \
                    patch.object(command.transaction, 'atomic') as start, self.assertRaises(CommandError):
                call_command('inspect_articles_zoo_scope', observation='/private/' + SECRET)
            start.assert_not_called()
        self.assertEqual(self.order, [])

    def test_read_only_setup_failure_has_fixed_error_and_precedes_orm(self):
        from django.db import DatabaseError
        from .management.commands import inspect_articles_zoo_scope as command
        cursor = MagicMock()
        cursor.__enter__.return_value.execute.side_effect = DatabaseError(SECRET)
        fake = SimpleNamespace(vendor='postgresql', in_atomic_block=False, connection=None, cursor=lambda: cursor)
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'observation.json'; path.write_text(json.dumps(observation()))
            with self.boundaries(), patch.object(command, 'database', fake), \
                    patch.object(command.transaction, 'atomic', return_value=nullcontext()), \
                    self.assertRaises(CommandError) as raised:
                call_command('inspect_articles_zoo_scope', observation=str(path))
        self.assertEqual(str(raised.exception), 'Read-only zoo scope inspection is unavailable.')
        self.assertEqual(self.order, [])

    def test_actual_existing_dispatch_chain_request_satisfies_the_owned_locator_contract(self):
        from workflow_runs.models import ContentFactoryRun
        from .tests_website_goal_chain_unit import RealGoalChainTests
        created = []
        original = ContentFactoryRun.__init__
        def record(run, *args, **kwargs):
            original(run, *args, **kwargs); created.append(run)
        flags = {**RealGoalChainTests._overridden_settings, 'ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED': True}
        with override_settings(**flags), patch.object(ContentFactoryRun, '__init__', new=record):
            RealGoalChainTests().test_enrolled_goal_executes_real_context_journey_dispatch_and_remote_queue()
        actual = next(run for run in created if run.run_id == 'scan-child')
        request = actual.run_request
        self.run = actual
        self.operation.pk = request['goal_budget']['goal_id']
        self.connection.pk = UUID(request['website_connection_id'])
        self.connection.generation = request['connection_generation']
        self.connection.repository_id = request['repository_id']
        self.connection.organization.domain = actual.domain
        self.connection.github_repo = actual.github_repo
        self.child.payload = {'run_id': actual.run_id, 'attempt': request['operation_attempt']}
        with self.boundaries():
            locator = scope._run_locator(actual.run_id, company=self.company,
                connection=self.connection, operation=self.operation)
        self.assertIsNotNone(locator)
        self.assertEqual(locator['workflow'], 'repo_scan')


class ZooScopeFileTests(SimpleTestCase):
    def test_regular_file_is_read_without_creating_any_other_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); path = root / 'observation.json'
            path.write_text(json.dumps(observation()))
            before = path.stat().st_mtime_ns
            self.assertEqual(scope.read_observation(path), observation())
            self.assertEqual(path.stat().st_mtime_ns, before)
            self.assertEqual(list(root.iterdir()), [path])

    def test_missing_malformed_oversized_symlink_parent_link_fifo_and_directory_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            good = root / 'good.json'; good.write_text(json.dumps(observation()))
            malformed = root / 'malformed'; malformed.write_text(SECRET)
            oversized = root / 'oversized'; oversized.write_bytes(b' ' * (scope.MAX_BYTES + 1))
            link = root / 'link'; link.symlink_to(good)
            parent = root / 'parent'; parent.symlink_to(root, target_is_directory=True)
            nested = root / 'nested'; nested.mkdir(); (nested / 'good.json').write_text(json.dumps(observation()))
            fifo = root / 'fifo'; os.mkfifo(fifo)
            for path in (root / 'missing', malformed, oversized, link, parent / 'good.json',
                    parent / 'nested' / 'good.json', fifo, root):
                with self.subTest(path=path), self.assertRaises(scope.ScopeInputError) as raised:
                    scope.read_observation(path)
                self.assertEqual(str(raised.exception), 'Invalid or unavailable bounded zoo observation.')

    def test_a_file_changed_during_read_is_unknown(self):
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'observation.json'; path.write_text(json.dumps(observation()))
            real = os.fstat
            calls = []
            def changing(descriptor):
                value = real(descriptor); calls.append(value)
                if len(calls) == 2:
                    return SimpleNamespace(st_size=value.st_size, st_mtime_ns=value.st_mtime_ns + 1, st_ino=value.st_ino)
                return value
            with patch.object(scope.os, 'fstat', side_effect=changing), self.assertRaises(scope.ScopeInputError):
                scope.read_observation(path)
