# Content island map regions

`ContentIslandRegion` stores an organisation's stable audience groups. Migration
`content_factory.0051_content_island_regions` creates the region table and the
nullable `ContentIsland.region` relation. It has no data backfill: the frontend
uses deterministic client regions until the next normal island refresh writes
server groups. Applying this migration requires separate approval for the target
environment and reviewed migration plan.

After every edge rebuild, `rebuild_island_regions` takes the organisation lock
and partitions non-archived islands using average linkage over normalized stored
centroids. Empty or invalid vectors follow their strongest stored edge or enter
“More topics”. At most eight groups use the shared eight-colour region palette.
No embeddings or naming provider calls are exposed to the browser.

New groups match saved groups by greatest Jaccard membership overlap of at least
0.5, with deterministic tie breaks. Matches keep their slug, name, name source
and colour. An island stays with a surviving old group unless the candidate
centroid improves cosine similarity by at least 0.05; the candidate excludes the
moving island to avoid self-attraction. Empty groups are removed. The synchronous
fallback name uses shared leading pillar words or the top opportunity's name.
`name_source=founder` is reserved for a future founder naming flow; generated
region naming is not required by this contract.

The bootstrap graph now includes:

```json
{
  "nodes": [{"slug": "coding-assistants", "regionSlug": "ai-coding"}],
  "regions": [{"slug": "ai-coding", "name": "AI coding", "colorKey": "blue",
               "islandSlugs": ["coding-assistants"]}]
}
```

Only visible nodes are included in region membership, ordered by opportunity
then slug. Regions without visible members are omitted. Region display values
come from `select_related("region")` in the existing node query, so island count
does not add queries. The existing island fingerprint aggregate also includes the
joined region modification time, so display edits invalidate bootstrap caches.
Existing clients can ignore these additive fields.

Database-free regression checks run with:

```sh
python scripts/test_without_database.py content_factory.tests_island_regions_unit content_factory.tests_island_removal_unit content_factory.tests_island_research_unit tests.test_vibe_marketing_bootstrap_performance
python scripts/test_without_database.py --check-models
```

`tests.test_content_islands.ContentIslandRegionPersistenceTests` covers saved
identity, cache invalidation, region cleanup, organisation isolation and the three-query bootstrap.
Its database execution, including test migration construction, requires the
repository's explicit migration approval.
