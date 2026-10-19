# GitHub action delivery observation

The private runtime observation resolves the saved original fixture goal before
reading the GitHub App delivery archive. App JWTs and full webhook payloads stay
on the backend. Only typed actor IDs, event GUIDs, bounded action codes and
configuration/window digests enter the reduced response. Customer repositories
are excluded by the fixed fixture repository prefix and saved repository ID.

The reader uses App-JWT-authenticated `GET /app/hook/deliveries` with opaque
cursor pagination and no success-only filter, then fixed delivery-detail reads.
This includes failed ingress and force pushes that disappear from ancestry.
Redeliveries share one GUID; contradictory payload identities cannot qualify.
Installation permission/repository changes are checked against the saved
installation and repository, rather than inferred from their latest settings.

The observation window starts at original goal admission and ends two minutes
after its completed target or stable owner/policy stop. Open goals, future
windows, pagination/size/time limits, malformed data and unavailable credentials
stay partial. Reads cover at most 48 hours, within GitHub.com's documented
three-day delivery retention. App configuration is checked before and after the
read. Redirects, ambient proxies/netrc credentials and arbitrary API paths are
refused. The endpoint performs no redelivery or provider mutation.

The private disconnect-policy reader can select the retained original window
after exactly one disconnect. The backend independently requires the same
currently configured connection, a cancelled original operation, the one-step
generation and configuration revision changes, and every other saved authority
field unchanged. Reconnection, erasure, repository/configuration changes and
older generations cannot use this historical path. It grants observation only.

`read_profile()` provides a reduced preflight configuration fingerprint to freeze
before the cohort. The zoo must require matching preflight configuration and
two stable snapshots of the same original window, and independently join its
mandatory internal owner-transport and GitHub review/merge/hosting observations.
`coverage: complete` here means the enumerated delivery-attempt source only.
It does not claim that GitHub generated every possible event or that the entire
goal involved zero human actions. Missing provider/source coverage remains an
unsuccessful qualification result.

Primary API references:
- [GitHub App webhook endpoints](https://docs.github.com/en/rest/apps/webhooks)
- [Delivery retention](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/viewing-webhook-deliveries)
- [Installation event availability](https://docs.github.com/en/webhooks/webhook-events-and-payloads#installation)
