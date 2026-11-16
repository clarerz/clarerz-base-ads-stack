VERDICT: NEEDS_REVISION

## Summary Assessment

The core architecture holds up. Extending `validation.authenticate` with a session check is the smallest correct way to reuse every route, and the cookie plus `X-Ads-UI` CSRF scheme is sound for this app. It cannot ship as written for three reasons. First, the session/login lifecycle does not respect token rotation or sign-up's "access token only" proof. Second, the lockout design leaks user existence through timing and lets anyone lock out every user. Third, one acceptance criterion contradicts existing behaviour.

## Critical Issues (must fix)

### C1. Rotating the access token does not revoke the web login or its sessions, and a leaked token can claim the account's only login for good

- Evidence: `service.py:55-61` `issue_access_token` is the operator's revocation tool. `api.py:306-309` says "issuing it a new token revokes its old URLs". The design adds a `users` row and `sessions` rows that never refer back to the token. The design says "A session authenticates even if the account never had a token issued". It also has one user per account, no reset, and no operator tool.
- Consequence: anyone who sees a token once (logs, CI, a shared script) can `POST /users` before the owner does. That person then keeps working in the UI, uploading and publishing, after the operator rotates the token. The real owner gets `400 002 account_id` forever, with no recovery path.
- Fix (pick one, state it as a requirement):
  - (a) Store the claiming token's hash on the user (`users.token_hash`, the same value already in `accounts.token_hash`, so nothing new is exposed). Login and the session branch of `authenticate` then require `users.token_hash == accounts.token_hash`. Rotation then kills the login and all sessions with no extra code. A sign-up with the new token replaces a stale user row. REQ-002's "The `users` table holds no token hash" must change to allow this.
  - (b) Make `issue_access_token` delete the account's user and sessions in the same transaction, and document it in the README operator section.
- Either way, add a REQ: "After `issue_access_token`, old sessions return 401 and a sign-up with the new token succeeds."

### C2. The widened `authenticate` lets a session satisfy sign-up's "current access token" proof (contradicts REQ-002)

- Evidence: after commit 4, `authenticate` accepts a token whose SHA-256 is a live session. After commit 5, `bearer_token` falls back to the cookie when there is no `Authorization` header and `X-Ads-UI: 1` is present. So `POST /users` with the cookie and header but no bearer token reaches `authenticate` with a session token, and the check passes. REQ-002 says sign-up "MUST require the account's current access token".
- Today the one-user-per-account constraint happens to block this. It becomes a real hole as soon as C1 (re-claim) or any user reset exists.
- Fix:
  - Give `authenticate` a `sessions=True` keyword, or split out `authenticate_token`. `create_user` calls the token-only form.
  - `POST /users` reads the `Authorization` header directly and never falls back to the cookie.
  - Add an acceptance criterion: "a session cookie or session token as bearer does not authorize `POST /users`".

### C3. The locked-user path leaks existence through timing (violates the anti-goal and REQ-005)

- Evidence: Design > Sessions says "locked attempts get the same 401 and do not verify the password", while unknown logins run scrypt against a dummy hash. Measured `hashlib.scrypt(n=2**14, r=8, p=1)` takes about 47 ms here. A locked user answers in about 1 ms.
- Attack: send 5 wrong passwords for a candidate username, then time the 6th. A fast answer means the user exists. This is exactly the anti-goal "Login errors or timing that reveal whether a username or account ID exists".
- Fix: every login attempt runs exactly one scrypt verification (stored hash, or the dummy for unknown logins), and the lock is checked after that. Change REQ-005/REQ-006 to say "locked attempts still run the password hash" and add a source-inspection check for it.

### C4. Per-user hard lockout on a public identifier is a one-request-per-3-minutes DoS against every user, and unauthenticated scrypt can starve the whole API

- Evidence:
  - Login accepts the numeric account ID, and account IDs are public. `GET /accounts/{id}` is unauthenticated (`api.py:124-126`), and `GET /accounts/{id}/ads` exposes `creator_id`.
  - Five bad passwords lock a user for 15 minutes, correct password included (REQ-006). One attacker who walks the ID space keeps every UI user locked out indefinitely at 5 requests per account per 15 minutes.
  - Separately, every `POST /session` costs about 47 ms of CPU and 16 MiB, for unknown logins too. All routes in `api.py` are sync `def` and share AnyIO's 40-thread pool, so a modest login flood blocks uploads, publishes and listings for everyone.
- Fix:
  - Open question 1 (who uses this first) must be answered before implementation, because it decides this design.
  - Minimum acceptable without a new dependency:
    - Replace the hard lock with a per-user exponential delay, capped low (for example 1 s doubling to 60 s), that rejects without running scrypt only during the delay window. Because of C3, the time spent must still be uniform: sleep to a fixed floor, or keep the delay and accept that it is visible.
    - Add a small in-process per-client-IP token bucket on `POST /session` and `POST /users`, with the IP taken from `request.client.host`.
    - Bound concurrent scrypt work with a semaphore so logins cannot take every worker thread.
  - Mark the in-process limiter with a `ponytail:` note: single process only.
  - Write the chosen numbers into a REQ with clock-injected tests.

### C5. REQ-008's acceptance criterion contradicts existing behaviour (and so contradicts REQ-010)

- Evidence: REQ-008 says the listed routes "return 401 `002` for `access_token` for any other account's path". "Get and rename video" have no account in the path (`/videos/{video_id}`). For another account's video, `service._owned_video` authenticates with `field="video_id"` (`service.py:166-171`), and `api.py:294-303` maps that to 404. The existing test `tests/test_api.py:327-334` asserts `404 002 video_id`.
- A test written to REQ-008 will fail, or the implementer will "fix" the route and break REQ-010 and an existing test.
- Fix: split the criterion. Account-path routes return 401 `002` `access_token`. `/videos/{id}` of another account returns 404 `002` `video_id`, as today.

### C6. The `Secure` cookie flag is taken from the request scheme, which fails open behind a TLS proxy

- Evidence: Design > Sessions sets "`Secure` when the request scheme is HTTPS". Uvicorn honours `X-Forwarded-Proto` only from `--forwarded-allow-ips`, which defaults to 127.0.0.1. Behind a proxy on another host, or a misconfigured one, the scheme is `http`.
- Result: a 12-hour credential with full account power is sent without `Secure`, so it can leak on any plain-HTTP request to the host, including the first request before an HTTP-to-HTTPS redirect.
- Fix: set `Secure` always by default, and add an explicit opt-out (for example `ADS_INSECURE_COOKIES=1`) for plain-HTTP local development. Chrome and Firefox already accept `Secure` cookies on `http://localhost`. Consider the `__Host-ads_session` name, which forces `Secure`, `Path=/` and no `Domain`. Update REQ-004 so the cookie is `Secure` unless the opt-out is set. TestClient tests need `base_url="https://testserver"`, or httpx will not send the cookie back.

### C7. Commit 5 breaks the stated commit rules (one concern, about 300 lines)

- Evidence: commit 5 adds four routes (`POST /users`, `POST/GET/DELETE /session`) and changes `bearer_token` for every existing route. It also carries TestClient tests for REQ-001, 002, 004, 005 and 007–011. That is two concerns (new endpoints vs. changing how all existing routes authenticate) and well over 300 lines with tests.
- Fix: split it into:
  - 5a: user and session routes, plus their tests (REQ-001/002/004/005/007).
  - 5b: cookie fallback in `bearer_token` with `X-Ads-UI` gating, plus tests that drive the existing routes with a session (REQ-008/009/011).
- Also fix the mappings:
  - REQ-007's "request at 12 h is 401" needs commit 4, not only commit 2.
  - REQ-017 (accessibility) applies to commits 7–9 as well as 6.
  - REQ-011 depends on both commits 3 and 5b.

## Suggestions (nice to have)

1. **Drop commit 3 and REQ-011 (YAGNI).** The UI shows no thumbnails and no previews, and the CSP is `img-src 'self'`, so nothing in scope ever uses `preview_url`. Without the refactor, session callers just get `preview_url`s that do not verify. Either accept that, or have `image_json`/`video_json` emit `null` when the caller authenticated by session. If you keep it: the refactor is behaviour-preserving for bearer callers (see Verified), but `image_json` runs once per listed image. Read `get_token_hash` once per request, not 25 times per page, and wrap it in `call(...)` so a failure maps to 003. REQ-011's "`null` when the account has no issued token" can never happen: sign-up requires a token, and `set_token_hash` never clears one. The design's line "A session authenticates even if the account never had a token issued" is moot for the same reason. Delete both.
2. **Decide the clock now instead of leaving it to the builder.** `authenticate` is called from `AdValidator` and `UploadValidator`, which have no clock (`publishing.py:37-42`, `service.py:33-38`). REQ-007's injected-clock acceptance at the HTTP level only works if `now` is threaded through validators, or the session branch reads a clock the service owns. Simplest: `authenticate(..., now=None)` defaulting to `time.time()`, with `AdsService` passing its clock into the validators it builds.
3. **Error mapping.** `status_for` (`api.py:60-70`) maps only `002`/`access_token` to 401. `002`/`login` must use `with_status(..., 401)`. Say so, so nobody adds a field special case to `status_for`.
4. **Sign-up races.** Hash the password before opening the transaction. Map the `IntegrityError` from `UNIQUE(account_id)` to `400 002 account_id` and from `UNIQUE(username)` to `400 002 username`, telling them apart by constraint name the way `grant_image` does (`repository.py:161-164`). Otherwise the loser of a race gets 003.
5. **Specify where `account_id` goes in `POST /users`.** "body `{username, password}`, an `account_id`" is ambiguous. Use the body (and list it in `check_fields`) or the path (`POST /accounts/{id}/users`), which matches the other account routes.
6. **"Authorization present" vs `HTTPBearer`.** `HTTPBearer(auto_error=False)` returns `None` for a missing header, a non-Bearer scheme, and `Bearer ` with an empty value (checked in FastAPI 0.162). If "prefer Authorization" means any `Authorization` header disables the cookie, test `request.headers` directly, not `credentials is None`. This is not a CSRF hole either way, because cross-site pages cannot set `Authorization`, but REQ-009 should say which.
7. **Login parsing.** Define "all-digit" as ASCII `[0-9]+`. `coerce_id("٤٢")` returns 42 because `str.isdigit`/`int` accept non-ASCII digits. Leading zeros and values above 2^63−1 must take the dummy-hash path.
8. **Username enumeration through sign-up.** `400 002 username` (taken) tells any account holder that a username exists. It is unavoidable for self sign-up, but the anti-goal should scope itself to login, or the design should state this trade-off.
9. **XSS rule for vanilla JS.** Ad text, account name, image names and video names are user-controlled. Require `textContent`, `setAttribute` and `title` assignment only (no `innerHTML`, no `insertAdjacentHTML`), and add a source-inspection criterion such as "`app.js` contains no `innerHTML`". The CSP blocks script, but HTML injection could still redress the sign-up form, which collects the access token.
10. **UI 401 handling.** "401 on any call returns to the signed-out view with 'Session expired'" would also fire for a wrong password on `POST /session` and a wrong token on `POST /users`. Exclude those two calls.
11. **Media list.** Merging two independently paged lists "newest first" with a separate Load more for each is not correctly ordered. A video on page 2 can be newer than an image on page 1. Two plain lists (Images, Videos) are simpler and correct.
12. **Duplicate image upload with a new name.** The route deliberately changes nothing, name included (`api.py:229-233`). The UI should say the name was not applied. Otherwise the 409 response may name an item older than the loaded pages, so select it by `hash`/`id` instead of by list position.
13. **Caching and headers.** Add `Cache-Control: no-store` to `GET/POST /session` responses (they carry account data) and to `/ui/index.html`. `GET /ui` currently 307-redirects to `/ui/` with `StaticFiles` mounted (checked), so the explicit `/ui` route is required, not optional.
14. **Session hygiene.** On login, delete the session named by any incoming cookie. Also delete expired rows in `GET /session`, not only at login.
15. **tenx/v1 hygiene.**
    - Compound obligations should be split, or each clause given its own criterion:
      - REQ-002 (require token, no storage, not the password).
      - REQ-003 (hash format, length).
      - REQ-006 (lock, reset).
      - REQ-007 (expiry, logout, hash only).
      - REQ-009 (ignore cookie, prefer Authorization, header on three routes).
      - REQ-012 (headers, no inline).
      - REQ-013 (five behaviours).
      - REQ-017 (four behaviours).
    - Requirement gaps: `GET /session`, `DELETE /session` clearing the cookie, and "expired rows deleted on login" have no REQ.
    - REQ-010's "161 tests" is correct today but brittle. Say "the existing suite passes unchanged".
    - REQ-005 states that timing is not measured. Pair it with the C3 source-inspection check so the obligation can be verified.

### Open questions

- Q1 must be resolved before implementation (C4).
- Q2 and Q3 should be answered together with C1. Operator-created logins would remove both the claim race and the token-in-browser step.
- Q4 can wait.
- Missing question: "What happens to the web login when the operator rotates the token?" (C1).

## Verified Claims (things you confirmed are correct)

- **`authenticate` call sites:**
  - service: `rename_account`, `list_images`, `get_image`, `_owned_video`, `list_videos`.
  - validation: `AdValidator.validate`, `UploadValidator.validate_request` (which covers video uploads).
  - api: `_authenticated_owner`, `signed_access`.
  - All of them pass the raw token. `require_admin` uses `bearer_token` but not `authenticate`, and the cookie fallback cannot satisfy it.
- **Token storage:** only `sha256(token)` is stored (`accounts.token_hash`), so login cannot unlock the token. A separate credential is required.
- **Signing refactor is behaviour-preserving for bearer callers.** Every `hash_token(token)` call in signing comes after `authenticate` has passed with that token, so it equals the stored hash:
  - `image_json` via `list_images`/`get_image`
  - `video_json` via `list/get/rename_video`
  - `list_ad_previews` after `_authenticated_owner`
  - `signed_access` after `authenticate`
  - `preview_page` takes the `access` tuple and needs no change.
- **Preview routes:** with commit 4, a session can also mint preview URLs. That is harmless (same account) but wider than the stated scope.
- **CSRF:** a cross-origin preflight to an existing route returns 405 with no `Access-Control-Allow-Origin` (checked with TestClient). Cross-site pages therefore cannot send `X-Ads-UI`, and the header-gated cookie scheme holds. `SameSite=Strict` is defence in depth.
- **`JsonBodyLimit`:** returns 411 only for `Transfer-Encoding` without `Content-Length`. A bodiless `DELETE /session` passes it (checked), and `fetch` JSON bodies carry `Content-Length`. Upload exemption applies to POST on `/accounts/{id}/(images|videos)` only. XHR with a `File` body sets `Content-Length`, as `spool_upload` requires.
- **Response shapes the UI relies on:**
  - 409 duplicate is `{"error_code": "004", "error_message": ..., "hash" | "id": ...}`.
  - List endpoints return `{"data", "next_cursor"}` with `next_cursor: null` on the last page.
  - `limit=10` is within 1–100.
  - `GET /accounts/{id}/ads` is public.
- **Schema:** migration via `PRAGMA user_version` (2) under `BEGIN IMMEDIATE` with `if version < N` steps, so a `_create_v3` fits.
- **Tests:** isolation needs no new module in `MODULE_NAMES` if code stays in the existing files. A new module (for example `web.py`) must be added to the loader lists.
- **Existing suite:** 161 tests pass (`Ran 161 tests ... OK`).
- **scrypt:** `n=2**14, r=8, p=1, dklen=32` runs in about 47 ms and uses 16 MiB, under `hashlib`'s default 32 MiB `maxmem`.
- **REQ IDs:** REQ-001 to REQ-017 are unique and sequential, and each has Applies when / Obligation / Acceptance criteria / Verification needs.
