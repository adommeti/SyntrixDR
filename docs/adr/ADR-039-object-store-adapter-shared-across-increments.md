# ADR-039 — Single `ObjectStore` adapter, built by BUILD-05 and reused by BUILD-09

**Status:** Accepted
**Date:** 2026-09-19
**Increment:** BUILD-05
**Relates to:** D-217 · BUILD-09-evidence-validation-files.md

## Context

D-217 specifies "**one** `ObjectStore` adapter on `azure-storage-blob`" (Azure Blob in Azure,
Azurite locally). BUILD-09's own prompt file names "object store adapter (Azurite)" as its
session-a scope. BUILD-05 depends only on BUILD-04 and runs before BUILD-09, but Excel import
needs to store the uploaded workbook in blob storage before BUILD-09 exists.

Two increments both need the same adapter; D-217's "one adapter" wording settles which one builds
it, but that reading wasn't obvious without cross-checking both prompt files, so it's recorded here
rather than re-derived (or accidentally rebuilt) when BUILD-09 starts.

## Decision

`apps/api/app/core/storage.py::ObjectStore` is built once, in BUILD-05, as a small
upload/download wrapper around `azure-storage-blob` (`BlobServiceClient`, connection-string auth —
same string works against Azurite locally and real Azure Blob in prod, no code branch needed).
BUILD-09 imports and reuses this exact class unmodified; it does not redefine, subclass, or
duplicate it. Any container/path conventions BUILD-09 needs (e.g. evidence file containers) are
passed as arguments to the existing `upload`/`download` methods, not new adapter code.

## Alternatives rejected

- **Wait for BUILD-09 to build it, block BUILD-05's upload feature** — not viable; BUILD-05 has no
  dependency on BUILD-09 per the increment ordering, and Excel import cannot function without blob
  storage.
- **BUILD-05 builds a narrower, import-specific storage helper; BUILD-09 builds the "real" one
  later** — would violate D-217's explicit "one adapter" requirement and create two code paths to
  the same Azure service with no functional difference.

## Consequences

- BUILD-09's own session-a plan must not re-scope "build the object store adapter" as new work —
  it should cite this ADR and `core/storage.py` directly.
- Any future change to blob storage behavior (retry policy, container naming, SAS tokens) is a
  single-file change reused by every caller, not a per-module divergence to reconcile later.
- No product behavior changes; this is purely a build-ordering/module-ownership convention.
