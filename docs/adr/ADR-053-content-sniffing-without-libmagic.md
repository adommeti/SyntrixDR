# ADR-053 — Server-side content sniffing for the upload allowlist without libmagic

**Status:** Accepted  
**Date:** 2026-10-12  
**Increment:** BUILD-09  
**Relates to:** D-225 (allowlist) / SECURITY_REVIEW.md:105-107 ("do not trust client MIME/filename") / ADR-024

## Context
Evidence uploads must be checked against the D-225 allowlist, and the security review says neither the client's
`Content-Type` nor the filename may be trusted. The usual answer is libmagic (`python-magic`), which adds a
native library to every API container, the CI runner and the Azure image, and still reports generic types for
the Office formats that matter here (`xlsx`, `docx` are ZIP containers).

## Decision
`validation_evidence/sniff.py` is a hand-written, pure-Python sniffer covering exactly the allowlisted
extensions: magic numbers for `pdf`, `png`, `jpg`/`jpeg`; ZIP-container inspection for `zip`, `xlsx` (an
`xl/` part), `docx` (a `word/` part); UTF-8-decodable text without NUL bytes for `txt`, `log`, `csv`; a JSON
parse (catching `RecursionError` as well as `ValueError`) for `json`. The bytes must match the *claimed*
extension; an empty file never matches. The sniffed MIME (`CANONICAL_MIME`) is what gets stored and what is
checked against `file_policy.allowed_mime_types`; the client's `Content-Type` is ignored. `update_file_policy`
derives the MIME allowlist from the extension allowlist through the same map, and refuses extensions the
sniffer cannot check (`UNKNOWN_EXTENSION`), so the two lists can never disagree and an un-sniffable type can
never be allowed.

## Alternatives rejected
- `python-magic` / libmagic — a native dependency in three environments for a fixed 11-type list, and weaker
  on Office containers.
- Trusting the declared `Content-Type` — explicitly forbidden by SECURITY_REVIEW.md:107.
- Extension-only checks — a PNG named `.pdf` would pass.

## Consequences
- Adding an allowlisted type means adding a sniff rule and a canonical MIME; the file-policy command enforces
  this at the API.
- Tests: `test_file_sniff.py` (table of matches and mismatches, deep-JSON case),
  `test_evidence_api.py::test_rejected_uploads` (PNG-as-PDF, empty, oversize, off-list).
