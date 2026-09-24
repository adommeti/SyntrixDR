# ADR-040 — `openpyxl` for `.xlsx` reading and test fixture generation

**Status:** Accepted
**Date:** 2026-09-19
**Increment:** BUILD-05
**Relates to:** D-221 · D-236

## Context

D-221's Excel import needs to read arbitrary `.xlsx` workbooks (unknown headers, unknown column
order) server-side in Python. The three fixture workbooks required by the test plan (D-236:
fictitious-data-only header variants) also need to be generated somewhere reviewable, rather than
committed as opaque binary `.xlsx` files that can't be diffed.

## Decision

Use `openpyxl` for both roles:

- `apps/api/app/plans_import/xlsx_parser.py` reads uploaded workbooks with it (pure-Python, no
  native/C-extension dependency, actively maintained, the de facto standard for `.xlsx` in Python).
- `apps/api/tests/fixtures/imports/workbooks.py` builds the three fixture workbooks in-memory with
  the same library (`Workbook()` → append rows → save to a `BytesIO` buffer), so the fixtures are
  plain, reviewable Python literals in version control instead of committed binary files, and stay
  automatically in sync with whatever headers `mapping.py` actually expects.

## Alternatives rejected

- **`pandas` + `openpyxl`/`xlrd` engine** — pulls in a much heavier dependency (numpy, pandas) for
  a need that's just "read cells, in header/row order, with the source row number" — no numerical
  analysis, indexing, or DataFrame semantics required.
- **Committed binary `.xlsx` fixture files** — not reviewable in a PR diff, and drift silently from
  the mapper's expected headers with no compiler/test to catch it until a test fails at runtime;
  in-memory generation makes the exact fixture content part of the readable test source.
- **`xlsxwriter` for fixture generation only (write-only, faster)** — would mean two libraries doing
  overlapping jobs (`openpyxl` to read real uploads, `xlsxwriter` to write fixtures) for no real
  benefit at this data volume; one library for both roles is simpler.

## Consequences

- `openpyxl` is now a runtime dependency (`apps/api/pyproject.toml`), not just a test dependency —
  future increments touching `.xlsx` import/export should reuse it rather than adding a second
  Excel library.
- No product behavior changes; this is purely a library choice.
