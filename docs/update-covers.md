# Startup update covers

Founders may upload a cover or request a generated illustration from their current
draft. Generating/uploading creates an unselected asset; only saving an update
selects it, and publication still requires approval of the exact saved revision.

## API

All routes require authenticated founder access to an explicitly selected
`?company_id=<owned-company-id>`. Existing company ownership resolution applies.

| POST route under `/api/v1/vibe-raising/` | Input | Response |
| --- | --- | --- |
| `updates/covers/upload/` | Multipart `image` | `201 {coverImage}` |
| `updates/covers/generate/` | JSON `requestId` (UUID), `updateText`, optional `direction` | `202 {jobToken, status: "generating"}` |
| `updates/covers/status/` | JSON `jobToken` | `{status: "generating"}`, `{status: "ready", coverImage}`, or `{status: "failed", detail}` |

`coverImage` contains an HTTPS `url`, width, height, source (`upload` or
`generated`), model and a signed `assetToken`. The client may add a short `alt`.
Send the whole object as `coverImage` in the existing update save API. Explicit
`null` removes the cover; omitting the field preserves it. Server receipts bind
assets to their organisation and make URL/model/dimension tampering ineffective.

The canonical revision stores `cover_image` inside its existing JSON memo. Cover
changes alter the revision hash and require a fresh publication approval. Text
regeneration inherits the selected cover. Published reads use the frozen
published revision even when the founder is editing a replacement. Draft/editor
and monthly-update responses expose `coverImage`; monthly responses also expose
`coverImageUrl` for update cards. No database migration is involved.

## Generation and storage

- Uses the existing server-side `OPENAI_API_KEY` and Firebase storage setup.
- `STARTUP_UPDATE_COVER_IMAGE_MODEL` defaults to `gpt-image-2.5-flare`.
- `STARTUP_UPDATE_COVER_PROMPT_MODEL` defaults to `gpt-6-astra`, which invokes the
  image tool in a background Responses request. The image tool is explicitly
  configured for GPT Image 2.5 Flare, high quality, WebP, 1536 × 864.
- Prompt uses a bounded excerpt of the draft and optional visual direction.
  It asks for a considered, slightly abstract editorial metaphor with no text or
  logos and a central motif suitable for square cropping.
- Background Responses stores the request at OpenAI (`store=true`) for polling.
  The application never logs draft prompts or provider error bodies.
- Poll every four seconds. Signed job receipts expire after one hour and are
  bound to both the founder and company. The UI can resume waiting after a
  refresh in the same browser session. It stops waiting after eight minutes.
- The shared Django cache keeps start/result receipts for one hour. Start requests
  use a provider idempotency key. The browser creates a new request ID only for a
  new generation attempt. Throttles: six generation starts/hour, twenty uploads/hour,
  thirty polls/minute per authenticated user.
- Uploads accept actual JPEG/PNG/WebP raster data up to 10 MiB / 24 megapixels.
  Animated, invalid and oversized images are rejected. EXIF orientation is applied;
  metadata is stripped and images are reencoded to WebP, at most 2400 pixels/edge.
- Assets use `startup-update-covers/<organisation>/<unique-id>.webp` and the
  existing Firebase download-token media pattern. Generated completion uses
  create-only storage writes, so retries cannot invalidate a published URL.
- Replacing/removing a cover never deletes old assets referenced by earlier
  revisions. Unselected assets are retained; this change adds no cleanup worker.

Roll out the backend before the frontend. Without the API key/model access,
generation returns guidance to upload instead. No production model call or
deployment is part of local validation.

Official API contracts: [image generation](https://developers.openai.com/api/docs/guides/image-generation),
[background requests](https://developers.openai.com/api/docs/guides/background),
[GPT Image 2.5 Flare](https://developers.openai.com/api/docs/models/gpt-image-2.5-flare).

## Isolated tests without a database

This harness does not load `.env`, production settings, credentials or migration
state. Run with a Python 3.11 environment containing the repository requirements:

```python
import unittest
from django.conf import settings
settings.configure(
    SECRET_KEY="isolated-cover-test-key", OPENAI_API_KEY="", DATABASES={},
    INSTALLED_APPS=[], REST_FRAMEWORK={"UNAUTHENTICATED_USER": None},
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
import django
django.setup()
suite = unittest.defaultTestLoader.loadTestsFromNames([
    "startup_updates.tests_covers", "startup_updates.tests_evidence_contract",
])
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
```

Checks cover raster validation, receipts, cross-company/user access, background
model configuration, idempotence, terminal errors, frozen-content hashes,
regeneration/removal semantics, endpoint authentication and storage-token reuse.
