# Test verification

## Test quality review

- Tests assert exact returned values, fields, and error messages. Expected values
  are literals or derived independently (for example, `hashlib` over known bytes),
  not computed by the code under test.
- Each test gets fresh modules and temporary storage. Image, account, and ad
  records cannot leak across tests or modify the user's data directory.
- Real SQLite storage is exercised; separate processes check durable account, ad,
  image, and video reads. A legacy schema is built independently of the repository.
- Failures are injected at real boundaries where possible: SQLite triggers reject
  writes, the image registry rename or write fails, and a real repository has one
  lookup replaced. Mock repositories are limited to read failures that triggers
  cannot produce; storage doubles that pass validation return explicit records.
- Pagination helpers are bounded, so a cursor that never ends fails instead of
  hanging the suite.

## Coverage

76 test methods, with parameterized subtests, cover:

| Area | Checks |
| --- | --- |
| Accounts and tokens | Creation, one-time token stored as a hash, rename with token, no overwrite by ID, token reissue, legacy accounts without tokens. |
| Authentication | Every write and image listing rejects missing, wrong, non-string, and other accounts' tokens; authentication runs before ownership checks; failed lookups fail closed. |
| Ad publishing | Full stored record, ID uniqueness, text and ID normalization, unknown and missing fields, exactly one image field, link-only `image`, hash and name resolution, video ads. |
| Image ownership | Upload and link ingestion grant ownership; other accounts' and unstored images are rejected; names are per account and unique within it. |
| Uploads | Content hashes, durable copies in the data directory, deduplication, real 30 MiB image boundary, video size limit, partial-copy cleanup, input validation before storage. |
| Listings | Newest-first keyset pagination, tie order, stability across new rows, inclusive/exclusive time filters, invalid parameters, storage failures. |
| Reads | Full objects, projections of every supported shape, invalid projections, missing records. |
| Image registry | Seeded records, metadata-only URL ingestion, damaged registry left untouched, interrupted writes keep the previous registry. |
| Extensibility | Extra validation rules, custom publish stage, injected image storage and clock, validator using the publisher's storage. |
| Persistence | Fresh-process reads, legacy schema migration, failed-insert rollback, concurrent ad ID allocation, error dictionaries for storage failures. |

## Regression detection

Each production rule was broken in a temporary copy and the full suite was run;
every mutation below produced failures:

- Accept unknown fields or nonexistent creators; skip custom rules or image
  normalization; return extra projected fields or missing ads; ignore the clock.
- Make a domain object mutable.
- Skip authentication on publish, upload, video rename, or image listing; let a
  failed token lookup pass; allow an account to be overwritten by ID.
- Remove the size cap or change the image limit's units.
- Skip image ownership or image-store checks; check a different storage than the
  publisher's; accept non-link `image` values or more than one image field; skip
  the store check for names.
- Ignore name conflicts, report any integrity error as a name conflict, drop the
  unique name index, or clear a name on an unnamed re-upload.
- Read a damaged registry as empty or lose a registry write.
- Reverse the tie order, make `since` exclusive or `until` inclusive, or accept
  an image cursor with a trailing newline.
- Skip the video extension check, or raise from `rename_video` on storage failure.

This shows the suite detects these specific regressions; it is not an exhaustive
mutation score or a proof of correctness.

## Result and limits

All 76 tests pass using:

```bash
python3 -m unittest discover -s tests -v
```

Not covered: closing a connection when schema setup fails, and the race where two
processes add the same column. `image_store.get_image`, `image_exists`, and
`validate_image_reference` have no callers and no tests. Remaining limitations:
metadata-only URL ingestion, no image-format validation, a JSON image registry
without concurrent-writer coordination (simultaneous ingestions can lose a record)
or a transaction shared with the ads database, and rejected uploads (duplicate
name, failed video insert) can leave unowned bytes in storage. Disk exhaustion and
process crashes are simulated only at the registry write and the copy loop.
