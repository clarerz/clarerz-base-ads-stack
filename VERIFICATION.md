# Refactor verification

## Scope and sequence

The first verification ran 24 existing behavior tests and 8 new refactor tests.
It did not include an independent audit of the tests before using their results.
A subsequent audit reviewed the assertions against the documented API and the
committed implementation, added boundary and failure cases, and reran verification.

## Test quality review

- Tests assert exact returned values, fields, and error messages. Success/failure
  expectations are not inferred from the implementation's validation rules.
- Each test gets fresh modules and temporary storage. Image, account, and ad
  records cannot leak across tests or modify the user's data directory.
- Real SQLite storage is exercised; separate processes check durable account,
  ad, and image reads. A legacy schema is built independently of the repository.
- Mocks are limited to extension dependencies and deliberate failures. A real
  registry write failure and a SQLite trigger exercise actual failure boundaries.
- URL tests explicitly check metadata ingestion; they do not claim to test
  downloading bytes or decoding the sample image.

## Coverage

The suite contains 44 test methods, with parameterized subtests covering:

| Area | Checks |
| --- | --- |
| Account/ad API | Creation, account replacement, owner reference, reads, ID coercion, text normalization, metadata, missing records. |
| Validation | Required fields, wrong input types, unknown fields, text rules, malformed URLs/files, hash length/character boundaries, Unicode digits. |
| Reads | Full objects, exact projection, string/tuple/set projections, empty projections, invalid field types. |
| Images | URL/file ingestion, file content hashes/copies, deduplication, hash-only publication, malformed adapter outputs, failed registry writes. |
| Domain objects | Object returns, dictionary serialization, immutability. |
| Extensibility | Extra validation rules, custom publish stage, injected image storage and clock. |
| Persistence | Fresh-process reads, legacy schema compatibility, failed-insert rollback, concurrent SQLite ad ID allocation. |
| Failure handling | Rejection before ingestion, no ad after image failure, object exceptions, dictionary error translation. |

## Bugs found and corrected

1. The inherited image registry swallowed write errors and could allow publishing
   to report success for an image that was not persisted. Write failures now propagate.
2. The new publisher accepted malformed nonempty hashes from custom image storage.
   It now checks the normalized handle's format before persisting an ad.
3. The inherited numeric coercion used `isdigit()` without handling characters
   such as superscripts that `int()` cannot parse. They now produce the normal
   invalid-ID behavior.

The new regression tests failed before these fixes and passed afterward.

## Regression detection

Eight deliberate mutations were made in temporary copies. Each ran the full
suite, produced assertion failures, and was detected:

- Accept unknown input fields.
- Accept nonexistent creators.
- Skip custom validation rules.
- Ignore image normalization.
- Return extra fields in projections.
- Make domain objects mutable.
- Return missing persisted ads.
- Ignore the injected clock.

This checks that the suite can detect these specific regressions; it is not an
exhaustive mutation score or a proof of correctness.

## Result and limits

All 44 tests pass using:

```bash
python3 -m unittest discover -s tests -v
```

The suite covers the current local API and refactor requirements. It does not
establish production readiness. Inherited limitations include metadata-only URL
ingestion, no image-format validation, unchecked existence of caller-provided
hashes, and a JSON image registry without concurrent-writer coordination or an
atomic transaction shared with the ads database. The concurrency test covers
SQLite ad allocation, not concurrent image ingestion. Disk exhaustion, process
crashes during file writes, and every possible extension are not simulated.
