# Roo rate-card reads

`GET /api/v1/points/rate-card/` returns standard volunteer activities and
their Roo point values. It is an authenticated, read-only endpoint.

## Authentication

Readers may use an authenticated browser account or the existing
`HasRooApiKey` service credential contract. Service callers supply
`X-API-Key` or `Authorization: Api-Key <key>` matching a configured
`ROO_API_KEY`, `INTERNAL_API_KEY`, or legacy `MLAI_API_KEY`. The shared
permission also supports its existing environment fallbacks.

An unrelated organisational-memory admin key does not authorize this read.
Missing or invalid credentials receive the configured DRF 401/403 response.
The endpoint does not accept writes.

## Response

A successful response is HTTP 200 with an unpaginated array of active
templates, ordered by name. Each row contains `name`, `alias`, `points`,
`description`, and `is_active`.

HTTP 200 with `[]` means no active rates are configured. A failed request
does not establish whether rates exist. Consumers must distinguish failed
reads from successful empty results.

The permission repair uses actual permission classes and accepts Roo's
dedicated service key. It changes neither the schema nor the stored rates;
no migration or reseeding is needed.

## Verification

`roo.tests_rate_card.RateCardTests` uses `SimpleTestCase` with an empty
database set, unsaved fixtures, and mocked queryset evaluation. Run it with
a database-free test runner; no database creation or migration is needed.
These tests exercise the registered route, authentication, serialization,
empty responses, and write rejection. The active filter and ordering are
checked without fetching rows. They do not verify deployed database contents.
