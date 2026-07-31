# Roo Data Access

`data_access` exposes curated read-only resources to Roo through:

- `GET /api/v1/data/catalog/?requester_slack_id=U123`
- `POST /api/v1/data/query/`

Roo never sends SQL and the backend never exposes arbitrary Django models. Each resource is registered in Python with explicit `allowed_fields`, filters, ordering, limits, and role scopes.

The catalog is requester-scoped. It includes only resources and operations
available to the supplied Slack actor; its fields are the allow-listed fields
that actor can query on those resources.

On PostgreSQL, query execution runs inside a read-only transaction with a 30 second statement timeout. This is a backend safety net, not a substitute for resource allow-lists and role scopes.

## Adding A Resource

Add a `Resource` in `data_access/registry.py` with:

- a resolver, usually `ModelResolver(Model)`
- safe `FieldSpec` entries only
- explicit `Policy` entries
- `default_limit` and `max_limit`

Do not add blacklist-style `blocked_columns`. If a field is not allow-listed, it does not exist to Roo.

Use resource names, not model names, as the public contract. A resource may be a Django model projection, a join-friendly projection, or a service-backed virtual resource.

## Query Semantics

Supported filter operators are `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `in`, and `icontains`.

`icontains` means case-insensitive substring search and only works on fields marked `searchable=True`.

Paginated responses include `returned_count`, `limit`, `offset`, and `has_more`. They do not include total matching row counts in v1.

## Permission Rules

There is no global admin bypass. Every request goes through the resource policy. Staff and superusers can have broad scopes only when the resource explicitly grants those roles.

Never allow-list tokens, secrets, encrypted credentials, raw payloads, raw attachment data, storage paths, or cursor internals.

Actor roles are derived at request time from the requesting Slack ID:

- `authenticated_slack`
- `user`
- `django_staff`
- `django_superuser`
- `points_admin:admin`
- `points_admin:committee`
- `points_admin:portfolio_lead`
- `points_admin:partner`
- `founder`
- `investor`

Common scopes are:

- `self_user`: records linked to the requester's Django user ID
- `self_slack`: records linked to the requester Slack ID
- `founder_org`: records linked to one of the requester's Vibe Raising/startup organizations
- `founder_domain`: records linked to one of the requester's organization domains
- `all`: unrestricted within the resource, only when explicitly granted by that resource

## Example Requests

Catalog:

```bash
curl -H "X-API-Key: $ROO_API_KEY" \
  "$MLAI_BACKEND_URL/api/v1/data/catalog/?requester_slack_id=U123"
```

Count Vibe Raising companies visible to a requester:

```bash
curl -X POST "$MLAI_BACKEND_URL/api/v1/data/query/" \
  -H "X-API-Key: $ROO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "requester_slack_id": "U123",
    "resource": "vibe_raising_companies",
    "operation": "count"
  }'
```

List Content Factory failed jobs visible to a requester:

```bash
curl -X POST "$MLAI_BACKEND_URL/api/v1/data/query/" \
  -H "X-API-Key: $ROO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "requester_slack_id": "U123",
    "resource": "content_factory_jobs",
    "fields": ["job_id", "domain", "status", "error_message", "created_at"],
    "filters": [{"field": "status", "operator": "eq", "value": "error"}],
    "limit": 20,
    "offset": 0
  }'
```

## Extension Checklist

1. Add only safe fields to the resource allow-list.
2. Define resource-specific policies; never rely on an admin bypass.
3. Mark `searchable=True` only for fields that can tolerate `icontains` searches.
4. Set conservative `default_limit` and `max_limit`.
5. Add or update tests for field exposure, role scope, pagination, and sensitive-field assertions.
6. If Roo should infer the resource from natural language, add a mapping in `roo/skills/executor.py` and route coverage in Roo tests.
