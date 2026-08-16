# ADR 005 — Presigned PUT with server-side size enforcement

## Context

S3 offers two presigned-upload primitives:

1. **Presigned PUT** (`generate_presigned_url` with `put_object`). A single URL that clients hit with `PUT`. Simple, works from `curl`, from browsers via `fetch(url, { method: 'PUT' })`, from any HTTP library.
2. **Presigned POST** (`generate_presigned_post`). Returns a URL + a set of form fields that the client submits as a `multipart/form-data` POST. Supports policy conditions like `content-length-range`, `starts-with`, and exact `Content-Type` matches.

The critical difference for us is size enforcement. Presigned POST lets you sign a `content-length-range: [1, 26214400]` condition. S3 rejects the upload at the edge if the body is outside the range — no bytes hit disk, no charges, no post-processing needed.

Presigned PUT cannot carry a content-length-range condition. The client is trusted to send what they declared. Server-side we can HEAD the object after the fact and reject if it's too big — but the bytes did land, briefly, and we paid for them.

## Decision

Use **presigned PUT**, and enforce the size ceiling server-side in the `process` Lambda.

Why:

1. **Client integration is trivial.** A single URL + `PUT` is what every HTTP client already knows how to do. Presigned POST requires the client to build a multipart form matching the exact field ordering, and mistakes give opaque S3 errors.
2. **The size threshold (25 MB) is small.** Even if a client uploads a 100 MB file (declared as 5 MB), the cost of the wasted PUT is a fraction of a cent, and lifecycle rules expire it in 7 days.
3. **Content-Type is still signed.** `generate_presigned_url` accepts a `ContentType` param. If the client sends anything else, S3 rejects the PUT — so `content_type` in the DynamoDB row is trustworthy without a HEAD.
4. **Server-side re-verification is happening anyway.** `process` does a HEAD to check extension + size + record actual size. Adding the size-limit check there is one comparison.

Rejected uploads are tagged `quarantine=true`, marked `REJECTED` in DynamoDB, and produce a `file_rejected` SNS event. The S3 lifecycle rule then expires them after 7 days.

## Consequences

- **We pay for the bytes on rejection.** A malicious client can burn some bandwidth by uploading oversize files. Bandwidth in is free on S3, so this is only the PUT-request cost — trivial. If this became a vector, we'd add rate limiting at the API layer.
- **URL expiry: 15 minutes for PUT, 24 hours for GET.** Chosen for a specific reason each:
  - **15-minute PUT.** Long enough for a user to notice their file dialog is open and hit upload. Short enough that a stolen URL from a proxy log becomes worthless quickly. Also aligns with the presigned-URL max signing lifetime for STS-derived credentials in some AWS auth paths.
  - **24-hour GET.** Long enough for a recipient to notice the email in a typical inbox flow (morning check → later that day). The email body includes the exact UTC expiry so recipients can plan.
- **The `notify` Lambda re-generates the GET URL at send time.** That means the expiry clock starts from *email send*, not from *upload*. A recipient who checks their email 20 hours after upload still has ~24 hours from the notification.
- **We can't offer resumable uploads.** Presigned PUT is single-shot. For files past 25 MB (which we don't accept anyway), we'd need multipart-upload presigning, which is a much larger client contract.
