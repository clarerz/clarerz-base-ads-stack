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

147 standard-library test methods (236 with the HTTP API suites, 302 with the browser tests), with parameterized subtests, cover:

| Area | Checks |
| --- | --- |
| Accounts and tokens | Creation, one-time token stored as a hash, rename with token, no overwrite by ID, token reissue, legacy accounts without tokens. |
| Authentication | Every write, image and video listing, and `get_video` rejects missing, wrong, non-string, and other accounts' tokens; authentication runs before ownership checks; failed lookups fail closed; missing videos and wrong tokens get the same error, and so do failed video and token lookups (an outage that fails only token lookups could still tell existing IDs from missing ones). |
| Ad publishing | Full stored record, ID uniqueness, text and ID normalization, IDs bounded to 0..2^63-1, hex-only hash-like text, unknown and missing fields, exactly one image field, link-only `image`, hash and name resolution, video ads, one clock reading per publish. |
| Image ownership | Upload and link ingestion grant ownership; other accounts' and unstored images are rejected; names are per account and unique within it. |
| Uploads | Content hashes, durable copies in the data directory, one copy per content even across suffixes and concurrent uploads, known content found by hash before any write (including older `<hash><suffix>` files), sources that change while being stored rejected, HTTP 409 `004` for an account's own byte-identical re-uploads but not for other accounts', real 30 MiB image boundary, video size limit, partial-copy cleanup, image and video extensions, unreadable sources as invalid input, header dimensions for PNG, GIF, JPEG, and WEBP with a bounded JPEG walk, input validation before storage. |
| Listings | Ads, videos, and images: newest-first keyset pagination, tie order, stability across new rows, inclusive/exclusive time filters combined with cursors, invalid parameters, storage failures; image URL, stored dimensions, and the pre-cutoff placeholder; single-image fetch by hash or name. |
| Previews | Image lookup by hash and name, `preview_url` on image and video listings and video routes, link images previewed by source URL, served bytes with sniffed content types, `Range`, cache lifetime bounded by expiry, security headers; media served only under the owning account, by its token (other and wrong tokens 401) or a URL signed for it; media it does not own 404 even when the bytes are stored; missing, altered, extended, reused (other content, kind, or account), wrong-key, revoked (token reissued), and expired signatures and missing files all 404; no key means no previews, but the token still works. |
| Ad previews | Every format listed with its size and warnings (cut text, cropped images, none for videos), one format by `ad_format`; pages render link images by link, uploaded images and videos through signed media URLs, video playback per format, a labeled box for unshowable or unowned bytes, per-format text cuts; ad text, account names, and links escaped; page security headers; token required (missing, wrong, other accounts', and non-Bearer tokens 401; accounts with no issued token cannot preview, even with a URL signed over the missing hash), unknown account 400, other accounts' and missing ads 404, unknown format 400; missing, altered, extended, reused (other format, ad, or account, or a media signature), revoked, and expired signatures 404; a preview signature never opens media; media links in a page expire with its preview URL; bearer access to the page; no key means no URLs; storage failures 503. End to end against a real server: image, video, and link ads previewed in every format with their embedded media fetched over HTTP, the CSP `media-src` matching the real host, token rules on both routes, and preview URLs (with their media links) shared across processes until a token reissued in another process revokes them. |
| Share links | `share_url` and `share_expires` on the previews listing (null without a key); the share page frames feed, story, and banner and carries each format's warnings; valid 7 days, with embedded preview and media URLs expiring with the link; missing, altered, extended, reused (other ad, account, or scope), wrong-key, revoked (token reissued), expired, and other accounts' or missing ads, and a server with no media key (even with a token), all get the same HTML 404; bearer access with 10-minute embedded URLs; security headers; session-cookie previews signed with the stored token hash, surviving sign-out and revoked by reissuing the token; `/media` `max-age` capped at 600 s. |
| Ad editing | `update_ad` and `PATCH /accounts/{id}/ads/{ad_id}`: text and media changes, type kept, other accounts' ads hidden, rules run on the merged draft and publisher hooks (`persist` and `normalize_image`), text checked before media as in publishing, body shape, legacy ads, schema v4 `update_time`, bearer and session-cookie auth, `503` without a publisher `update`. The previews listing reports each format's displayed text and the media; share pages show "Last edited"; story videos have controls. |
| Reads | Full objects, projections of every supported shape including comma-separated strings, invalid projections, missing records, `get_video`. |
| Image registry | Seeded records, unreadable seed file fails loudly, metadata-only URL ingestion, damaged registry left untouched, interrupted writes keep the previous registry, concurrent URL and file ingestions from several processes all kept. |
| Extensibility | Extra validation rules, custom publish stage, injected image storage and clock (shared by the service and its default publisher), custom upload and video validators, validator using the publisher's storage. |
| Web logins | One login per account; ASCII username and password rules with boundaries; case-insensitive usernames; salted scrypt storage; sign-up only by the current access token (never a session); login by username or account ID (not non-ASCII digits); identical failures with exactly one scrypt run each; 30-second throttle after 5 failures; 12-hour expiry; logout; token rotation ends the login and allows a re-claim. |
| Session cookie | Every account route works with the cookie plus `X-Ads-UI` for its own account only (`/videos/{id}` of others stays 404); ignored without the header or with any `Authorization` header; never the admin key; `Secure`/`HttpOnly`/`SameSite=Strict` attributes and the opt-out; `no-store`; session responses carry signed preview URLs that expire in 10 minutes. |
| Web UI | Served files and their CSP and headers, no inline code or HTML insertion; in Chromium: sign-up, sign-in by account ID and keyboard, sign-out, reload, field errors, uploads with selection, duplicates, video name check, paging of media and ads, ad creation with each media source, one source at a time, rows with names and cut text, text-only rendering, and expired sessions returning to sign-in. Preview dialog: three placements drawn directly (no frames) and matching the preview pages' markup, geometry, and video playback; warnings, share link and expiry, copy fallback, close button, share link surviving sign-out, expired session closing the dialog, and editable placements without a media key. Text editing in the dialog: hover and focus outline, click and keyboard editing, blur saves, Escape and window blur do not, unchanged (including CRLF) and invalid text, one edit at a time with media elements kept across a save, saving on close, failures after close shown in the status line, reopening an ad waiting for its pending edit, stale messages cleared by a later save, and focus kept on the opener and edit controls across re-renders. Media replacement: image click and overlay, separate video button, picker, upload, duplicate reuse, wrong file type, failed save keeping the upload, late uploads kept out of other dialogs. |
| Persistence | Fresh-process reads, versioned migration of legacy and v1 schemas (once, concurrently, without locking current databases), repair of early `image_owners` shapes, the ads-by-creator index, failed-insert rollback, concurrent ad ID allocation, `003` error dictionaries for storage failures. |

## Requirements trace (ad preview editing)

Requirement text is in `docs/specs/2026-11-17-ads-preview-editing-design.md` (a local
spec; this table does not depend on it). Browser tests (`test_ui_browser`) are the
only evidence for REQ-007 to REQ-017 and REQ-020 to REQ-022 in the browser, so they
must not be skipped.

| REQ | Verifying tests |
| --- | --- |
| REQ-001 | `AdsStackTests.test_update_ad_changes_text`; `ApiTests.test_patch_edits_text` |
| REQ-002 | `AdsStackTests.test_update_ad_replaces_media`, `test_link_image_owned_at_edit_time`; `ApiTests.test_patch_replaces_media` |
| REQ-003 | `AdsStackTests.test_update_ad_keeps_type`; `ApiTests.test_patch_rejects_type_change` |
| REQ-004 | `AdsStackTests.test_update_ad_hides_other_ads`; `ApiTests.test_patch_authenticates_and_hides_other_ads`; `CookieAuthTests.test_patch_with_session_cookie` |
| REQ-005 | `AdsStackTests.test_update_runs_rules_on_merged_draft`, `test_update_uses_publisher_hooks` (`persist` and `normalize_image`); `ApiTests.test_patch_without_publisher_update_is_503` |
| REQ-006 | `AdsStackTests.test_update_body_shape`; `ApiTests.test_patch_body_shape`, `test_json_bodies_are_capped_before_auth` |
| REQ-007 | `ApiTests.test_preview_css_sizes_match_formats`; browser: `PreviewTests.test_preview_pages_keep_their_geometry`, `test_placements_match_preview_pages`, `test_video_playback_matches_share_pages` |
| REQ-008 | browser: `TextEditTests.test_text_outline_only_on_hover_or_focus` |
| REQ-009 | browser: `TextEditTests.test_click_edit_blur_saves`, `test_unchanged_text_sends_nothing`, `test_unchanged_crlf_text_sends_nothing`, `test_escape_cancels`, `test_window_blur_does_not_save` |
| REQ-010 | browser: `TextEditTests.test_invalid_text_stays_open` |
| REQ-011 | `ApiTests.test_share_page_shows_edit`; browser: `TextEditTests.test_click_edit_blur_saves`, `test_later_save_clears_listing_error`, `MediaReplaceTests.test_replace_image_upload` |
| REQ-012 | browser: `MediaReplaceTests.test_image_replace_cue_and_picker`, `test_unavailable_image_stays_named` |
| REQ-013 | `ApiTests.test_story_video_has_controls`; browser: `ShareLinkTests.test_share_story_controls_reachable`, `PreviewTests.test_video_playback_matches_share_pages`, `MediaReplaceTests.test_video_playback_and_replace_control` |
| REQ-014 | browser: `MediaReplaceTests.test_replace_image_upload`, `test_replace_with_duplicate`, `test_wrong_type_file`, `test_failed_update_keeps_upload`, `test_late_upload_does_not_touch_new_dialog`, `test_late_upload_failure_goes_to_status`, `test_video_playback_and_replace_control` |
| REQ-015 | browser: `TextEditTests.test_keyboard_editing`, `test_focus_returns_to_opener_after_save`, `MediaReplaceTests.test_keyboard_editing`, `test_replace_image_upload`, `test_tab_from_text_to_video_keeps_focus` |
| REQ-016 | `UiRouteTests.test_ui_files_are_served_with_a_strict_policy`, `test_ui_source_has_no_inline_code_or_html_insertion`; browser: `PreviewTests.test_placements_match_preview_pages` |
| REQ-017 | browser: `NoMediaKeyPreviewTests.test_no_media_key_placements_editable`, `PreviewTests.test_broken_media_retries_once`, `TextEditTests.test_media_retry_keeps_open_box` |
| REQ-018 | `ApiTests.test_listing_reports_text_and_media` |
| REQ-019 | `AdsStackTests.test_text_edit_on_legacy_ad` |
| REQ-020 | browser: `PreviewTests.test_expired_session_from_preview`, `TextEditTests.test_expired_session_during_text_save` |
| REQ-021 | browser: `TextEditTests.test_close_saves_pending_text`, `test_close_reports_failure_in_status`, `test_late_response_does_not_touch_new_dialog`, `test_failed_save_without_busy_element`, `test_reopening_waits_for_pending_save`, `test_waiting_preview_gives_way_to_another_ad`, `MediaReplaceTests.test_reopening_waits_for_pending_upload` |
| REQ-022 | browser: `TextEditTests.test_one_edit_at_a_time`, `MediaReplaceTests.test_one_edit_at_a_time`, `test_text_save_keeps_media_elements` |
| REQ-023 | `AdsStackTests.test_repository_update_ad`, `test_schema_v4_adds_update_time`, `test_update_ad_changes_text`; `ApiTests.test_share_page_shows_last_edited` |

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
- Skip the preview signature or expiry check, leave the kind or token hash out of the
  signature, skip the media ownership check or token authentication, or drop `nosniff`.
- Skip the video extension check, or raise from `rename_video` on storage failure.
- In ad previews: leave ad text or a media link unescaped, sign without the format,
  preview another account's ad, link unowned or unshowable bytes, drop
  `Referrer-Policy`, stop the story autoplaying, or skip the crop warning; skip the
  listing's token check or the page's bearer check, accept expired or tokenless signatures,
  or let page media links outlive the preview URL.

Later fixes were each test-first: the new test failed against the previous
code and passed after the change. Commit reviewers also confirmed by mutation
that the suite fails without the migration write lock or its re-check, or without
the registry file lock.

This shows the suite detects these specific regressions; it is not an exhaustive
mutation score or a proof of correctness.

## Result and limits

Run on 2026-11-18 with `.venv/bin/python` (FastAPI, uvicorn, Playwright, Chromium):

```bash
.venv/bin/python -m unittest discover -s tests -v
```

302 tests ran, all passed, 0 skipped, and the `test_ui_browser` tests ran. Without the
dev dependencies the HTTP API and browser suites are skipped (151 run, 4 skipped).

Not covered: a migration that fails partway (it rolls back, but no test forces
it), and SQLite rolling a transaction back on its own. Remaining limitations:
metadata-only URL ingestion with no dimensions, no image-format validation
(headers are read only for dimensions), a JSON image registry that is scanned in
full on every lookup and once per listed image, serialized with a Unix-only file
lock, and not in a transaction shared with the ads database; rejected uploads
(duplicate name, failed video insert) can leave unowned bytes in storage. Disk
exhaustion and process crashes are simulated only at the registry write and the
copy loop.
