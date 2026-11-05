"""Stable region refresh decisions without a database or provider calls."""
import copy
import math
from types import SimpleNamespace
from unittest import TestCase

from .island_regions import compute_island_regions


def fixture(groups=6, size=10):
    return [SimpleNamespace(slug=f'theme-{group}-{index:02}', name=f'Theme {group} topic {index}',
        pillar_keyword=f'theme {group} topic {index}', opportunity_score=100 - index,
        centroid_embedding=[1 if dimension == group else 0 for dimension in range(groups)])
        for group in range(groups) for index in range(size)]


class IslandRegionComputationTests(TestCase):
    def test_deterministic_partition_and_unique_colors(self):
        islands = fixture()
        regions = compute_island_regions(islands)
        self.assertEqual(regions, compute_island_regions(list(reversed(islands))))
        self.assertEqual(len(regions), 6)
        members = [slug for region in regions for slug in region['island_slugs']]
        self.assertEqual(sorted(members), sorted(row.slug for row in islands))
        self.assertEqual(len(set(members)), len(islands))
        self.assertEqual(len(set(region['color_key'] for region in regions)), 6)
        self.assertTrue(all(region['name'].startswith('Theme') for region in regions))

    def test_shared_pillar_names_preserve_familiar_acronyms(self):
        islands = fixture(groups=3, size=5)
        prefixes = ('ai coding', 'abn seo', 'api b2b')
        for index, island in enumerate(islands):
            island.pillar_keyword = f'{prefixes[index // 5]} topic {index}'
        names = {row['name'] for row in compute_island_regions(islands)}
        self.assertEqual(names, {'AI Coding Topic', 'ABN SEO Topic', 'API B2B Topic'})

    def test_matched_identity_founder_name_and_color_survive_changed_score(self):
        islands = fixture()
        previous = compute_island_regions(islands)
        previous[0].update(name='Founder chosen audience', name_source='founder')
        islands[2].opportunity_score = 9000
        refreshed = compute_island_regions(islands, previous)
        stable = lambda rows: [(row['slug'], row['name'], row['name_source'], row['color_key']) for row in rows]
        self.assertEqual(stable(refreshed), stable(previous))

    def test_hysteresis_keeps_a_borderline_island_then_allows_a_clear_move(self):
        islands = fixture(groups=3, size=10)
        previous = compute_island_regions(islands)
        current_region = next(row for row in previous if islands[0].slug in row['island_slugs'])
        islands[0].centroid_embedding = [0.70, 0.72, 0]
        refreshed = compute_island_regions(islands, previous)
        self.assertIn(islands[0].slug, next(row for row in refreshed if row['slug'] == current_region['slug'])['island_slugs'])
        islands[0].centroid_embedding = [0.20, 0.98, 0]
        refreshed = compute_island_regions(islands, previous)
        self.assertNotIn(islands[0].slug, next(row for row in refreshed if row['slug'] == current_region['slug'])['island_slugs'])

    def test_missing_centroids_use_strongest_edge_or_more_topics(self):
        islands = fixture(groups=3, size=5)
        islands.extend([SimpleNamespace(slug='manual', name='Manual', pillar_keyword='', opportunity_score=1,
                                      centroid_embedding=[]),
                        SimpleNamespace(slug='detached', name='Detached', pillar_keyword='', opportunity_score=0,
                                      centroid_embedding=[])])
        regions = compute_island_regions(islands, edges=[('manual', 'theme-1-00', 0.8)])
        linked = next(row for row in regions if 'manual' in row['island_slugs'])
        self.assertIn('theme-1-00', linked['island_slugs'])
        self.assertEqual(next(row for row in regions if 'detached' in row['island_slugs'])['name'], 'More topics')

    def test_sparse_and_invalid_vectors_are_finite_bounded_and_input_is_unchanged(self):
        islands = fixture(groups=15, size=1)
        islands[0].centroid_embedding = [math.nan]
        before = copy.deepcopy(islands)
        regions = compute_island_regions(islands)
        self.assertLessEqual(len(regions), 8)
        self.assertTrue(all(math.isfinite(value) for row in regions for value in row['centroid_embedding']))
        self.assertEqual(islands[1:], before[1:])
        self.assertTrue(math.isnan(islands[0].centroid_embedding[0]))
        self.assertEqual(compute_island_regions([]), [])
