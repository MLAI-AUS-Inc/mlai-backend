# E5: pin the independently readable hosted palette

Adopt renderer revision 2 of the producer's prepared static bundle contract at
immutable CF commit 29d75bb58cc4025e2ea05cb10f4daedae334f70d. The prior renderer
can put white text on white cards in a valid dark-site profile. The producer now
chooses page/card text independently and validates opaque sRGB shell contrast.

Vendor the exact pure contract, schema and synthetic example and refresh their
three immutable hashes. Keep rejecting rehashed unreadable styles in backend
ZIP validation. Neither this palette check nor a bundle checksum grants owner,
provider, payment, approval, domain/TLS or live authority. HostedChannel remains
unavailable; all rollout flags remain off. No migration or service is required.

Tests must decode the exact revision-2 fixture, preserve original article bytes,
reject the old renderer revision and readdressed white-on-white styles, and keep
all archive/closed-field/source-provenance checks. Run the strict database/network
denying suites and immutable producer CI check. Full hosted acceptance still
requires the owner-selected infrastructure and separate private controls.

Validation: all 50 database/network-denying contract, channel-interface/discovery
and CI-reuse cases pass, including exact producer export decoding, renderer
downgrade refusal and rehashed white-on-white CSS refusal. Local immutable hashes
pass and all 599 test modules remain fully selected. Actual remote parity and
the complete CI lanes are pending; no provider capability or runtime flag changes.
