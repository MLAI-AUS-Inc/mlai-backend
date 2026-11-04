"""Single-pass PostgreSQL aggregation of normalized audience facts.

The input is the caller's authorized queryset. JSON paths and taxonomies are
server-owned, and all values are bound parameters. No rows or raw evidence leave
the database; the result has at most one count per configured metric.
"""

from __future__ import annotations

from datetime import timezone

from django.core.exceptions import EmptyResultSet
from django.db import connections

from .audience_profiles import AUDIENCE_PROFILE_KEY
from .audience_taxonomy import ACTIVITY_SOURCES, ACTIVITY_STATUSES, FACETS, FACET_STATUSES, TAXONOMY_VERSION


def empty_counts() -> dict:
    """Return all configured metric keys, including zero counts for absent facts."""

    result = {"total": 0}
    for facet, spec in FACETS.items():
        result.update({f"{facet}_{status}": 0 for status in FACET_STATUSES})
        result.update({f"{facet}_option_{index}": 0 for index, _ in enumerate(spec["options"])})
    for source in ACTIVITY_SOURCES:
        result[f"activity_{source}_known"] = 0
        result.update({f"activity_{source}_{status}": 0 for status in ACTIVITY_STATUSES})
    return result


def postgres_profile_counts(base_queryset, *, now=None) -> dict:
    """Aggregate each stored facet/value once instead of checking every option per row.

    OFFSET 0 deliberately keeps the small lateral projections from being
    flattened: a toasted attributes document is extracted once per contact,
    and the validity guard is shared by all of that contact's fact rows.
    """

    result = empty_counts()
    connection = connections[base_queryset.db]
    scoped = base_queryset.values("pk", "attributes")
    if not scoped.query.is_sliced and not scoped.query.distinct_fields:
        scoped = scoped.order_by()
    try:
        scoped_sql, scoped_params = scoped.query.get_compiler(using=base_queryset.db).as_sql()
    except EmptyResultSet:
        return result
    observed_until = now.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z") if now else None
    sql = """
        SELECT metric.kind, metric.facet, metric.dimension, COUNT(*)
        FROM (""" + scoped_sql + """) AS scoped
        CROSS JOIN LATERAL (
            SELECT scoped.attributes -> %s AS profile OFFSET 0
        ) AS extracted
        CROSS JOIN LATERAL (
            SELECT CASE
                WHEN extracted.profile -> 'schema_version' = '1'::jsonb
                 AND extracted.profile ->> 'taxonomy_version' = %s
                THEN extracted.profile ELSE '{}'::jsonb END AS profile
            OFFSET 0
        ) AS trusted
        CROSS JOIN LATERAL (
            SELECT 'total'::text AS kind, ''::text AS facet, ''::text AS dimension
            UNION ALL
            SELECT fact.kind, f.key, fact.dimension
            FROM jsonb_each(CASE WHEN jsonb_typeof(trusted.profile -> 'facets') = 'object'
                            THEN trusted.profile -> 'facets' ELSE '{}'::jsonb END) AS f
            CROSS JOIN LATERAL (
                SELECT 'facet_status'::text AS kind, f.value ->> 'status' AS dimension
                WHERE f.value ->> 'status' = ANY(%s)
                UNION ALL
                SELECT 'facet_value'::text, value
                FROM (
                    SELECT DISTINCT value
                    FROM jsonb_array_elements_text(
                        CASE WHEN jsonb_typeof(f.value -> 'values') = 'array'
                             THEN f.value -> 'values' ELSE '[]'::jsonb END
                    ) AS options(value)
                ) AS distinct_options
                WHERE f.value -> 'status' = '"known"'::jsonb
                  AND f.value -> 'value_count' >= '1'::jsonb
            ) AS fact
            WHERE f.key = ANY(%s)
            UNION ALL
            SELECT observation.kind, activity.key, observation.dimension
            FROM jsonb_each(CASE WHEN jsonb_typeof(trusted.profile -> 'activity') = 'object'
                            THEN trusted.profile -> 'activity' ELSE '{}'::jsonb END) AS activity
            CROSS JOIN LATERAL (
                SELECT 'activity_status'::text AS kind, activity.value ->> 'status' AS dimension
                WHERE activity.value ->> 'status' = ANY(%s)
                UNION ALL
                SELECT 'activity_known'::text, ''::text
                WHERE activity.value -> 'identity_status' = '"verified"'::jsonb
                  AND activity.value -> 'last_observed_at' IS NOT NULL
                  AND activity.value -> 'last_observed_at' <> 'null'::jsonb
                  AND (%s::text IS NULL OR activity.value -> 'last_observed_at' <= to_jsonb(%s::text))
            ) AS observation
            WHERE activity.key = ANY(%s)
        ) AS metric
        GROUP BY metric.kind, metric.facet, metric.dimension
    """
    parameters = [*scoped_params, AUDIENCE_PROFILE_KEY, TAXONOMY_VERSION,
                  list(FACET_STATUSES), list(FACETS), list(ACTIVITY_STATUSES),
                  observed_until, observed_until, list(ACTIVITY_SOURCES)]
    option_indices = {facet: {value: index for index, value in enumerate(spec["options"])}
                      for facet, spec in FACETS.items()}
    with connection.cursor() as cursor:
        cursor.execute(sql, parameters)
        for kind, facet, dimension, count in cursor.fetchall():
            if kind == "total":
                key = "total"
            elif kind == "facet_status":
                key = f"{facet}_{dimension}"
            elif kind == "facet_value":
                index = option_indices.get(facet, {}).get(dimension)
                if index is None:
                    continue
                key = f"{facet}_option_{index}"
            elif kind == "activity_status":
                key = f"activity_{facet}_{dimension}"
            else:
                key = f"activity_{facet}_known"
            if key in result:
                result[key] = count
    return result
