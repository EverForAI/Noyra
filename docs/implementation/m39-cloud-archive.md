# M39: Cloud Archive

`S3ArchiveProvider` is an optional S3-compatible adapter for AWS S3, Cloudflare R2,
Backblaze B2, and MinIO. It accepts an injected client, so credentials never enter
the model gateway or SQLite subject state. Uploads carry a SHA-256 metadata checksum;
downloads verify it. Calls retry with bounded exponential backoff and failures remain
recoverable by the caller. Local storage remains authoritative while the cloud is
unavailable. Install the optional `cloud` dependency only when the selected deployment
needs the boto3 client.
