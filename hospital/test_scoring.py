import csv
from collections import Counter
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from .models import Team
from .views import SOLUTION_PATH, custom_score, find_label_episodes, map_state_label


User = get_user_model()


class ScoringUnitTests(SimpleTestCase):
    def test_latent_state_mapping_matches_generator(self):
        expected = {
            0: 0, 9: 0, 10: 0, 16: 0,
            1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1, 7: 1, 8: 1,
            11: 2, 12: 2, 13: 2, 14: 2, 15: 2,
            17: 3,
        }
        self.assertEqual({state: map_state_label(state) for state in range(18)}, expected)

    def test_death_is_scored_as_crisis(self):
        self.assertEqual(custom_score([0, 1, 2, 3], [0, 1, 2, 3]), 8)
        self.assertEqual(custom_score([2, 3], [3, 2]), 6)
        self.assertEqual(custom_score([3], [0]), -10)

    def test_episodes_do_not_merge_across_patient_boundaries(self):
        labels = [0, 2, 2, 2, 2, 0]
        self.assertEqual(
            find_label_episodes(labels, {2, 3}, rows_per_encounter=3),
            [(1, 3), (3, 5)],
        )


class AustralianGroundTruthTests(SimpleTestCase):
    def test_solution_has_expected_new_holdout_composition(self):
        label_counts = Counter()
        usage_counts = Counter()
        last_id = 0
        with SOLUTION_PATH.open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(reader.fieldnames, ['ID', 'predicted_label', 'Usage'])
            for expected_id, row in enumerate(reader, start=1):
                self.assertEqual(int(row['ID']), expected_id)
                label_counts[int(row['predicted_label'])] += 1
                usage_counts[row['Usage']] += 1
                last_id = expected_id

        self.assertEqual(last_id, 452_880)
        self.assertEqual(label_counts, {0: 331_167, 1: 81_617, 2: 31_471, 3: 8_625})
        self.assertEqual(usage_counts, {'Private': 272_160, 'Public': 180_720})


class SubmissionScoringTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email='scorer@example.com',
            password='password',
            first_name='Score',
            last_name='Tester',
        )
        teammate = User.objects.create_user(
            email='scorer-teammate@example.com', password='password'
        )
        team = Team.objects.create(team_name='Scoring Test Team')
        team.members.add(self.user, teammate)
        self.client.force_authenticate(self.user)

    def upload(self, rows):
        content = StringIO()
        writer = csv.writer(content, lineterminator='\n')
        writer.writerow(['ID', 'predicted_label'])
        writer.writerows(rows)
        upload = SimpleUploadedFile(
            'submission.csv', content.getvalue().encode('utf-8'), content_type='text/csv'
        )
        return self.client.post(
            '/api/v1/hackathons/hospital/submit_predictions/',
            {'predictions_csv': upload},
            format='multipart',
        )

    @patch('hospital.views.load_ground_truth')
    def test_endpoint_scores_all_four_reported_classes(self, load_ground_truth):
        load_ground_truth.return_value = [
            {'ID': '1', 'predicted_label': '0', 'Usage': 'Public'},
            {'ID': '2', 'predicted_label': '1', 'Usage': 'Public'},
            {'ID': '3', 'predicted_label': '2', 'Usage': 'Private'},
            {'ID': '4', 'predicted_label': '3', 'Usage': 'Private'},
        ]

        response = self.upload([(1, 0), (2, 1), (3, 2), (4, 3)])

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['score'], 8)
        self.assertEqual(payload['accuracy'], 1.0)
        self.assertEqual(payload['feedback']['class_stats']['3']['name'], 'Death')
        self.assertEqual(payload['feedback']['clinical_metrics']['patients_saved'], 1)
        self.assertEqual(payload['feedback']['clinical_metrics']['false_alarms'], 0)

    def test_endpoint_scores_full_australian_holdout(self):
        rows = []
        with SOLUTION_PATH.open(encoding='utf-8-sig', newline='') as handle:
            for row in csv.DictReader(handle):
                rows.append((row['ID'], row['predicted_label']))

        response = self.upload(rows)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['score'], 283_522)
        self.assertEqual(payload['accuracy'], 1.0)
        self.assertEqual(payload['feedback']['class_stats']['0']['total'], 331_167)
        self.assertEqual(payload['feedback']['class_stats']['1']['total'], 81_617)
        self.assertEqual(payload['feedback']['class_stats']['2']['total'], 31_471)
        self.assertEqual(payload['feedback']['class_stats']['3']['total'], 8_625)
        self.assertEqual(payload['feedback']['missed_crises_total'], 0)
        self.assertEqual(payload['feedback']['clinical_metrics']['false_alarms'], 0)

    def test_endpoint_rejects_out_of_order_ids(self):
        response = self.upload([(2, 0)])

        self.assertEqual(response.status_code, 400)
        self.assertIn('expected 1', response.json()['detail'])
