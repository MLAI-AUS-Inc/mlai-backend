# Native CMS provider acceptance design

V2 section 3.3 requires disposable WordPress connect → capture → publish →
disconnect acceptance and WordPress/Ghost zoo coverage. Existing journey tests
exercise durable ORM, billing, leases and callbacks with synthetic provider
replies. This lane adds actual provider API/rendering evidence; it does not
replace those journey tests or certify a public customer site.

The remote GitHub runner creates digest-pinned WordPress, MySQL and Ghost
containers. Fixed loopback ports and synthetic `.test` origins are the only
destinations. Temporary provider accounts and keys exist only inside that job;
keys stay in a mode-0600 runner-temporary file and are never printed, uploaded
or obtained from repository/environment secrets. Teardown removes the containers
and volumes even after failure. No local Docker/database command is part of
agent validation.

The ordinary disposable database runner remains responsible for the fresh
backend database, excluded dotenv, bounded environment and loopback-only socket
guard. Its explicit provider option is permitted only in GitHub Actions for
this repository and reads only the fixed runner-temporary credential file.
The acceptance test uses a real transaction boundary, production owner connect,
encrypted key persistence, paginated capture and disconnect. Production post,
media, public-body and withdrawal contracts process actual provider responses.
Recorded source content replaces paid generation. No production authority flag,
credential, transport rule or model is changed.

A test-only connection maps the exact synthetic host to its fixed loopback HTTP
port and supplies the proxy HTTPS header needed by each fixture. Production
request construction, authentication, body parsing and no-redirect checks stay
in use. This lane does **not** exercise public DNS, TLS, external hosting, the
complete first-article goal or nightly 90% certification. The existing original
goal ORM fixture covers the durable flow independently; a public zoo run still
requires the separately approved sandbox resources.

Each provider must capture more than one page, retain original posts and native
fields, upload one inert raster, create one operation-marked article, verify
the real rendered body, reconcile a deliberately discarded create reply with
GET only, return the same post to draft, observe 404/410 separately and erase
local key authority on disconnect. Output reports only closed stage names,
counts and the pinned image versions. Provider bodies, cookies, keys and
personal records are excluded from artifacts.

Primary references checked on 18 October 2026:

- [WordPress application-password authentication](https://developer.wordpress.org/rest-api/using-the-rest-api/authentication/)
- [WP-CLI application-password creation](https://developer.wordpress.org/cli/commands/user/application-password/create/)
- [WordPress official image definitions](https://github.com/docker-library/official-images/blob/master/library/wordpress)
- [Ghost Admin API](https://docs.ghost.org/admin-api/)
- [Ghost HTML post creation](https://docs.ghost.org/admin-api/posts/creating-a-post/)
- [Ghost official image definitions](https://github.com/docker-library/official-images/blob/master/library/ghost)

All article rollout flags remain off. This is draft implementation; actual CI
results must be recorded after the new lane executes.

The first remote run pulled and started both CMS containers, then both fixed
loopback readiness checks timed out before authentication. Docker's internal
bridge did not expose the published listener to the runner. A digest-pinned
Nginx gateway now joins the internal bridge and a separate host-facing bridge;
the CMS/database containers retain only the internal network and no published
ports. The gateway has two fixed upstreams, checks the synthetic Host and turns
off access logs. It supplies no arbitrary forwarding destination. See Docker's
[Compose network isolation](https://docs.docker.com/compose/how-tos/networking/)
and [port publishing](https://docs.docker.com/engine/network/port-publishing/).
Actual acceptance still requires the repaired remote matrix to pass.

The gateway repaired both readiness checks. The next remote run reached
bootstrap and exposed two harness protocol errors: Compose replaced the CLI
image's default command, so it needs an explicit `wp` executable; Ghost's
successful session acknowledgement can contain plain `Created` rather than
JSON. The bootstrap now invokes `wp` and accepts the session only from a
successful response with an actual cookie, without parsing its acknowledgement
body. Regressions cover both cases without Docker or a network request.

Both bootstraps then passed and the real database fixture ran. Its synthetic
channel hint had incorrectly put a generator meta element outside `<head>`,
which the production classifier correctly ignores. The acceptance fixture now
reads the actual CMS homepage through the same fixed loopback gateway and uses
the existing explicit owner platform choice. Public hints still grant neither
authentication nor publication proof. The actual provider matrix must be rerun.

Real connect then exposed a WordPress compatibility gap: the transport assumed
`/wp-json/`, which the default plain-permalink installation does not route. It
now sends one fixed `index.php?rest_route=...` request, including media upload,
without discovering another credential origin, following redirects or retrying
a write. Caller query validation still forbids overriding the route. This is
WordPress's documented [plain-permalink API form](https://developer.wordpress.org/rest-api/using-the-rest-api/discovery/);
its [front-controller implementation](https://developer.wordpress.org/reference/functions/get_rest_url/)
uses `index.php` to avoid POST redirects. A simulated plain-permalink server
regression fails on the previous transport.

Ghost's [6.71 integration output mapper](https://github.com/TryGhost/Ghost/blob/v6.71.0/ghost/core/server/api/endpoints/utils/serializers/output/mappers/integrations.js)
already combines the key ID and secret. The fixture now accepts that combined
key instead of adding the ID twice, with an exact closed shape check and no
secret output. Its corrected protocol regression also fails before the fix.

After these compatibility corrections, all 1,558 database-free journey cases
and 60 focused fixture/transport/owner/capture cases pass. All 595 test modules
are assigned, 849 production failure emissions are audited and vendored digests
pass (remote pinned provenance not rechecked locally). Model checks report no
changes. No database or container was run locally. Actual remote WordPress/Ghost
acceptance remains required.

Both actual provider identity checks now pass. The next fixture assertion
compared a newly created model's string UUID with a reloaded model's UUID
object. It now compares the canonical UUID strings, retaining the requirement
that replay returns the same connection. This changes the fixture only.

Final local validation passes 109 database-free fixture/assignment/validation
route and CMS contract cases. Python syntax and the workflow/Compose YAML parse;
all 595 test modules are assigned. Isolation assertions check pinned images,
internal networking, loopback ports, both providers, absence of workflow secrets
and unconditional credential/volume cleanup. These checks do not start a CMS or
a database.

The next real run reached original post creation. WordPress correctly rejected
the fixture's seven-word body as insufficient public article evidence; the
fixture now supplies more than 30 words. Ghost's HTML importer drops sibling
comments, including the two ownership boundaries, and normalizes element bytes.
Create now sends one native Lexical HTML card containing the exact marked body.
Recovery still requires those exact bytes, both markers and the original native
metadata; it additionally accepts Ghost's exact newline-delimited card wrapper.
There is no fuzzy content comparison or additional POST. Two regressions fail
before this change; 116 focused contract/transport/fixture cases pass afterward.
The actual provider matrix must still pass.

Primary source inspected at Koenig commit
`4da831801b9ca065e4b30abe96c8b33227133e6c`:
[HTML importer](https://github.com/TryGhost/Koenig/blob/4da831801b9ca065e4b30abe96c8b33227133e6c/packages/kg-default-nodes/src/nodes/html/html-parser.ts),
[native HTML card schema tests](https://github.com/TryGhost/Koenig/blob/4da831801b9ca065e4b30abe96c8b33227133e6c/packages/kg-default-nodes/test/nodes/html.test.ts),
[card renderer](https://github.com/TryGhost/Koenig/blob/4da831801b9ca065e4b30abe96c8b33227133e6c/packages/kg-default-nodes/src/nodes/html/html-renderer.ts).
The container remains pinned independently; its next actual run is required to
validate that version's behavior.
