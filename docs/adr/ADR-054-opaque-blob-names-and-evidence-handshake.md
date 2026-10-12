# ADR-054 — Opaque blob names and the file-evidence handshake row

**Status:** Accepted  
**Date:** 2026-10-12  
**Increment:** BUILD-09  
**Relates to:** D-215 (handshake vs attaching command) / D-217 / ADR-014 / ADR-024 / schema_v1.sql `evidence_payload_ck`

## Context
`POST /evidence` creates the item and `POST /evidence/{id}/file` attaches the bytes (API_CONTRACT.md:203-204).
The `evidence_payload_ck` CHECK requires a `blob_uri` for FILE/SCREENSHOT rows at insert time, before any bytes
exist. Separately, the first implementation used the user's filename inside the blob path; names with spaces,
`#`, `%`, `+` or non-ASCII characters came back percent-encoded from the SDK, which broke the quarantine move
and made SAS signatures fail (caught in code review).

## Decision
1. The handshake row mints `EvidenceItem.id` before its first flush and stores a planned URI
   (`planned://evidence/<event>/<id>/<uuid>.<ext>`) with `malware_scan_status = PENDING` and no checksum.
   `attach_file` replaces it with the real URI. A handshake whose bytes never arrive stays PENDING, never counts
   toward completion and never yields a download URL.
2. Blob names are opaque and ASCII-only: `<dr_event_id>/<evidence_id>/<uuid4>.<ext>`, the extension taken from
   the allowlist. User text never reaches the object-store path. `file_name` is display metadata, sanitised
   (`_safe_file_name`: control characters stripped, path separators replaced, trimmed, capped at 255).
3. `ObjectStore.split_blob_url` decodes path segments; `signed_download_url` appends the SAS token to the base
   URI only; `move` copies then deletes (quarantine = `quarantine/<original name>`).
4. Both evidence POSTs require `Idempotency-Key`: the handshake creates a domain row and the attach is the
   attaching command (D-215's exemption is for a *pure* pre-signed handshake, which this is not). The attach
   route checks the header before parsing the form and hashes filename + bytes (the BUILD-05 multipart rule).

## Alternatives rejected
- Inserting the row only on attach — the handshake would have no id for the client to upload against.
- Keeping the filename in the blob path and percent-encoding consistently — fragile across Azurite, Azure and
  Event Grid, and still subject to name-length and reserved-character limits.
- Exempting the attach route from the key — it is the domain command that changes what counts as evidence.

## Consequences
- Downloads should set `Content-Disposition` from the sanitised `file_name` (follow-up when the UI lands).
- Tests: `test_evidence_api.py::test_awkward_file_names_upload_quarantine_and_download_fine`,
  `::test_control_characters_and_long_names_are_sanitised`, `::test_upload_replays_the_original_and_needs_a_key`;
  `test_object_store.py::test_names_with_spaces_and_reserved_characters_round_trip_move_and_sign`.
