# Browser account cookie binding

A cancelled email verification can still set an HttpOnly cookie after its JavaScript response is rejected. The current browser identity and the cookie can then refer to different accounts. Protecting only account restoration does not protect an already mounted feature request.

Updated browser clients send `X-MLAI-Public-Key` from protected local custody on authenticated API requests, including multipart/export requests and cookie refresh. The value is a public identity binding, not a credential. Cookie authentication compares it against the session key before device/community checks, account activity updates or view execution. Cookie refresh compares it under the existing credential-row lock before rotating tokens. A malformed or mismatched explicit binding returns HTTP401 with `error=account_identity_mismatch`; clients must stop and request sign-in rather than refresh or replay the operation. Failure does not clear newer cookies.

No header retains existing client behavior. Native bearer authentication and explicit-token refresh are unchanged. Email-code verification and password sign-in establish a new verified identity and do not attach the old identity binding. CORS explicitly admits the header for the existing approved origins; it does not expand those origins.

Deploy the backend guard before releasing the new browser client. Old-server account-response checks remain a defensive client fence, but an old server cannot enforce the new binding on arbitrary feature writes. This is a coordinated backend/client release requirement.

Validation includes actual PostgreSQL account/refresh tests, current-key acceptance, malformed/foreign-key denial without credential or activity mutation, native compatibility, preflight admission and handler spies proving no foreign read/resume effect. The client also exercises a real delayed HttpOnly cookie in local Chromium. No real provider or recipient is involved.
