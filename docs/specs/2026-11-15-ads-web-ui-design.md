# Ads Web UI: login, media upload, ad creation

**Invariant format:** tenx/v1

**Spec ID:** simple-ads-stack-web-ui

**Status:** Revised after design review 1
(`2026-11-15-ads-web-ui-design-review-1.md`). Calls marked *(author's call)* were made
by the author, not the requester.

## Intent

Give people running **internal demos** of the ads stack a browser page where they can
sign in, upload images and videos, create ads from that media, and see the account's
existing ads. Today the only clients are the Python API (`publish.py`, `service.py`) and
the HTTP API (`api.py`), both of which need the raw ad account ID and access token.

Requester decisions:

- Scope: media upload, ad creation, and a list of existing ads. No ad previews.
- Users sign in with a **username and password**; the login field also accepts the
  **numeric ad account ID**. The account access token is **never** the web password.
- The UI shows the account's existing ads with a reasonable page size.
- First users are internal demos.
- Logins are created by **self sign-up that claims an account with its access token**.
- **One login per ad account.**
- UI behaviour gets **automated browser tests**.

## Goals

- Sign-up, sign-in, and sign-out that never reuse the access token as a password.
- Upload an image (optional name) or a video (required name), with progress shown.
- Create an ad: text plus one of an uploaded image, an uploaded video, or an image link.
- List the account's ads, newest first, 10 at a time, with "Load more".
- Reuse the existing HTTP routes and service rules for media and ads.

## Non-goals

- Ad previews and media thumbnails *(author's call: thumbnails are a form of preview)*.
- Creating ad accounts from the UI (still admin key + `POST /accounts`).
- Password reset or change, email, MFA, several users per account, roles.
- Editing or deleting ads; renaming media.
- A frontend build pipeline or JavaScript framework.
- Per-IP login rate limiting (internal demos; see Known limits).

## Anti-goals

- The access token kept in browser storage or used as the password.
- **Login** responses or timing that reveal whether a username or account ID exists.
  (Sign-up necessarily reveals that a username is taken to someone holding a valid
  account token; accepted.)
- A cookie-authenticated request that a third-party page can trigger (CSRF).
- A web login that survives rotation of the account's access token.
- Any change to existing bearer-token behaviour or error shapes.

## Constraints from the existing code

- Every account write goes through `validation.authenticate(repository, account_id,
  token)`, called from `service.py` (`rename_account`, `list_images`, `get_image`,
  `_owned_video`, `list_videos`), `validation.py` (`AdValidator.validate`,
  `UploadValidator.validate_request`) and `api.py` (`_authenticated_owner`,
  `signed_access`). All pass a raw token string.
- Only `sha256(token)` is stored (`accounts.token_hash`), so a login cannot recover the
  token; it needs its own credential that `authenticate` accepts.
- `api.py` signs media URLs with `hash_token(<presented token>)`; a session token would
  produce URLs that never verify.
- `status_for` maps only `002`/`access_token` to 401; other 401s use `with_status`.
- `JsonBodyLimit` lets bodiless requests through and caps JSON at 64 KiB.
- Schema versioned with `PRAGMA user_version` (currently 2), migrated in
  `SQLiteAdsRepository._migrate`.
- `HTTPBearer(auto_error=False)` returns `None` for a missing header, another scheme, or
  an empty token.
- With only a `StaticFiles` mount, `GET /ui` redirects to `/ui/`; explicit routes avoid it.

## Approaches considered

**Frontend**

1. **Static HTML/CSS/vanilla JS under `web/`, served by explicit FastAPI routes at `/ui`
   (chosen).** Same origin, no CORS, no build step, no new runtime packages.
2. React/Vite SPA: adds Node and a build pipeline for a few forms. Rejected.
3. Jinja2 forms: needs `jinja2` and `python-multipart`, and duplicates the raw-bytes
   upload path. Rejected.

**Login** *(author's call)*

1. **Server-side sessions in SQLite, HttpOnly cookie, accepted by `authenticate`
   (chosen).** One new credential checked in the one shared function; every route works
   unchanged.
2. Separate `/ui/api/*` routes calling the service "pre-authenticated": two auth paths.
   Rejected.
3. Encrypted copy of the token released at login: needs a crypto dependency and puts
   the token in the browser. Rejected.
4. Stateless signed cookie: no server-side logout; `validation.py` would need the key.
   Rejected.

## Design

### Users

- Table `users (account_id INTEGER PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE,
  password_hash TEXT NOT NULL, token_hash TEXT NOT NULL, failed_logins INTEGER NOT NULL
  DEFAULT 0, last_failure INTEGER NOT NULL DEFAULT 0)` with a unique index on `username`.
  The primary key enforces one login per account.
- `token_hash` is the hash of the access token that claimed the account (the same value
  as `accounts.token_hash` at that moment). A user is **current** while it equals the
  account's `token_hash`. Rotating the token with `issue_access_token` makes the user
  stale: its logins and sessions stop working, and a sign-up with the new token replaces
  it (deleting its sessions).
- Username: ASCII `[A-Za-z0-9_.-]`, 3–32 characters, at least one letter (so it is
  never all digits). Unique ignoring case; stored as given.
- Password: 8–128 characters, not equal to the access token. Stored as
  `scrypt$16384$8$1$<salt hex>$<hash hex>` (`hashlib.scrypt`, 16-byte random salt,
  `dklen=32`).
- `POST /accounts/{account_id}/users` with body `{"username", "password"}`, the account's
  access token as `Authorization: Bearer`, and header `X-Ads-UI: 1`. The token is checked
  **token-only** (a session never authorizes sign-up) and read from the header only (no
  cookie fallback). The repository writes the user in one transaction that re-checks the
  account's current token hash, so races map to `002` errors, never `003`.

### Sessions

- Table `sessions (token_hash TEXT PRIMARY KEY, account_id INTEGER NOT NULL, expires_at
  INTEGER NOT NULL)`, indexed by `account_id`.
- `POST /session` with `{"login", "password"}` and `X-Ads-UI: 1`. A `login` of ASCII
  digits only is an account ID; anything else is a username (case-insensitive).
- **Every attempt runs exactly one scrypt verification** (the user's hash, or a fixed
  dummy hash when there is no current user), before any other decision. Then it succeeds
  only if the user exists, is current, the password matches, and the user is not
  throttled.
- **Throttle** *(author's call)*: after 5 consecutive failures, a user's logins fail for
  30 seconds after the most recent failure. Each failure, including throttled ones,
  updates the count and time; a success resets them.
- At most 4 scrypt verifications run at once per process (a semaphore), so a login
  flood cannot take every worker thread.
- On success: 32-byte `secrets.token_urlsafe` token; its SHA-256 is stored with expiry
  now + 12 hours; expired rows are deleted; the session named by an incoming cookie, if
  any, is deleted. The raw token is set as cookie `ads_session` (`HttpOnly`,
  `SameSite=Strict`, `Path=/`, `Max-Age=43200`, `Secure` unless the server opts out with
  `ADS_INSECURE_COOKIES=1` or `create_app(secure_cookies=False)`). Body 200
  `{"username", "account_id", "account_name"}`.
- Every failure: 401 `002` for `login`, no cookie.
- `GET /session`: the same body for a live session of a current user, else 401 `002`
  for `login`. Deletes expired rows.
- `DELETE /session` with `X-Ads-UI: 1`: deletes the session, clears the cookie, 204.
- Successful `/session` responses carry `Cache-Control: no-store` (errors carry no
  account data).
- Session expiry is checked against the service clock in the service and against
  `time.time()` in `authenticate` *(author's call: validators have no clock)*.

### How a session authenticates existing routes

- `authenticate(..., sessions=True)` accepts the token if it is the account's access
  token (as today), or if its SHA-256 is an unexpired session for that `account_id`
  whose user is current. `sessions=False` (used by sign-up) accepts only the access
  token.
- `api.bearer_token` returns the bearer token when an `Authorization` header is present
  (any `Authorization` header disables the cookie). Otherwise, only if the request has
  `X-Ads-UI: 1`, it returns the `ads_session` cookie and marks the request as
  session-authenticated. Cross-origin pages cannot send that header (a preflight gets
  405 with no allow-origin), so the cookie cannot be used cross-site.
- `require_admin` and sign-up read the `Authorization` header only.
- Session-authenticated responses set `preview_url` to `null` (the UI does not use it,
  and session-signed URLs would not verify).

### Web UI

Files `web/index.html`, `web/app.js`, `web/app.css`, served by `GET /ui`,
`GET /ui/app.js`, `GET /ui/app.css` with `Content-Security-Policy: default-src 'none';
script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; form-action
'self'; frame-ancestors 'none'; base-uri 'none'`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, and `Cache-Control: no-store`. No inline script or style.
Every API call uses same-origin credentials and `X-Ads-UI: 1`. User-controlled values are
rendered only with `textContent`/`title`/`value` (no `innerHTML`).

```
┌ Signed out ─────────────────────────────────────────┐
│ Sign in                    │ Create a login          │
│ Username or account ID [ ] │ Account ID   [ ]        │
│ Password [ ]   [Sign in]   │ Access token [ ] (once) │
│                            │ Username [ ] Password[ ]│
│                            │ [Create login]          │
└─────────────────────────────────────────────────────┘
┌ Signed in ── acme (account 42, "Acme Inc") [Sign out] ┐
│ Upload media                                          │
│ [Choose file] Name [____] [Upload]  ▓▓▓▓░░ 64%        │
│ Images                     │ Videos                   │
│ ( ) summer-hero  a1b2c3d4… │ ( ) Promo 15s   #7       │
│ [Load more]                │ [Load more]              │
│ Create ad                                             │
│ Text [________________________________]               │
│ Media: Image "summer-hero"   or image link [______]   │
│ [Publish]   ✓ Ad #43 created                          │
│ Your ads (newest first)                               │
│ #43  2026-11-15 10:02  Image summer-hero  "Summer…"   │
│ [Load more]                                           │
└───────────────────────────────────────────────────────┘
```

- **Startup:** `GET /session` decides the view.
- **Sign-up** posts the account ID, token, username, and password, then signs in. The
  token field is cleared after the request either way.
- **Upload:** one file input accepting the image and video extensions; kind follows the
  extension. `XMLHttpRequest` for progress. 409 `004` shows "Already uploaded" (and that
  a new name was not applied) and selects the item by its returned `hash`/`id`, adding it
  to the list if not loaded. Other errors show the API's message.
- **Media lists:** images and videos as two lists, 25 per page, "Load more" each.
  Choosing an item selects it as the ad's media. No thumbnails.
- **Create ad:** text plus the selected media or an image link. Errors show next to the
  named field; success shows "Ad #N created" and prepends the ad.
- **Ads list:** 10 per page, "Load more" while `next_cursor` is set. Row: ID (link to
  `/ads/{id}`), local time, media kind with the known name (else short hash or video
  ID), text cut at 120 characters with the full text as `title`. Empty: "No ads yet".
- **401** from any call except sign-in and sign-up returns to the signed-out view with
  "Session expired".
- **Accessibility:** labels for every input, errors linked by `aria-describedby`,
  `<progress>` for uploads, an `aria-live="polite"` status region, keyboard-only use.

### Error codes

`002` for `username` (invalid or taken, 400), `password` (invalid, 400), `account_id`
(account has a current login, 400), `login` (401). Sign-up with a missing or wrong token,
or without `X-Ads-UI`, returns 401 `002` for `access_token`; `/session` writes without
`X-Ads-UI` return 401 `002` for `login`.

### Known limits

- *ponytail:* the per-user throttle lets anyone who knows an account ID (they are public)
  keep that user's login failing by sending one bad attempt every 30 seconds. Add per-IP
  limits before non-demo use.
- The scrypt semaphore is per process.

## Requirements

### REQ-001: Usernames identify one ad account

**Applies when:** A client signs up through `POST /accounts/{account_id}/users`.

**Obligation:** The server MUST store at most one current user per ad account, with a
case-insensitively unique ASCII username of 3–32 characters from `A–Z a–z 0–9 _ . -`
containing at least one letter.

**Acceptance criteria:** A valid sign-up returns 201 `{"username", "account_id"}`. An
all-digit, 2- or 33-character, or out-of-alphabet username returns 400 `002` for
`username`; 3 and 32 characters are accepted. A username equal ignoring case to another
account's returns 400 `002` for `username`. A second sign-up for an account with a
current user returns 400 `002` for `account_id`.

**Verification needs:** Service unit tests; TestClient test for the route.

### REQ-002: Sign-up requires the current access token

**Applies when:** A client calls `POST /accounts/{account_id}/users`.

**Obligation:** Sign-up MUST be authorized only by the account's current access token
in the `Authorization` header, never by a session.

**Acceptance criteria:** A missing or wrong token returns 401 `002` for `access_token`
and creates no user. A session cookie (with `X-Ads-UI: 1`) or a session token sent as
bearer returns the same 401.

**Verification needs:** Service and TestClient tests.

### REQ-003: Passwords are stored as salted scrypt hashes

**Applies when:** A user is created.

**Obligation:** The server MUST store only `scrypt$16384$8$1$<salt>$<hash>` with a
random 16-byte salt per user.

**Acceptance criteria:** Two users with the same password have different stored values,
and neither contains the password.

**Verification needs:** Unit test; source inspection of the hashing call.

### REQ-004: Login accepts a username or an account ID

**Applies when:** A client calls `POST /session` with `{"login", "password"}` and
`X-Ads-UI: 1`.

**Obligation:** The server MUST treat an ASCII-digits-only `login` as an ad account ID
and anything else as a case-insensitive username, and MUST start a session when the
password matches a current, unthrottled user.

**Acceptance criteria:** The username, the username in other case, and the account ID
each return 200 `{"username", "account_id", "account_name"}` with an `ads_session` cookie
that is `HttpOnly`, `SameSite=Strict`, `Path=/`, and `Secure` (unless the server opted
out). A non-ASCII digit login is treated as a username.

**Verification needs:** TestClient tests over HTTPS base URL.

### REQ-005: Login failures are indistinguishable

**Applies when:** A login fails for any reason.

**Obligation:** The server MUST give the same response for an unknown username, an
unknown account ID, an account without a user, a stale user, a wrong password, and a
throttled user, and MUST run exactly one scrypt verification on every attempt.

**Acceptance criteria:** Each case returns 401 with body exactly `{"error_code": "002",
"error_message": "The value provided for login is invalid"}` and no cookie. Source shows
one verification call on every path before the outcome is decided.

**Verification needs:** TestClient tests for each case; source inspection (timing is
not measured).

### REQ-006: Repeated failures throttle the user

**Applies when:** A user's login has failed 5 times in a row.

**Obligation:** The server MUST refuse that user's logins, including correct passwords,
until 30 seconds after the most recent failure.

**Acceptance criteria:** With an injected clock: after 5 failures, the right password at
+29 s fails; at +30 s after the last failure it succeeds. A success after 4 failures
resets the count.

**Verification needs:** Service unit tests with an injected clock.

### REQ-007: Sessions expire after 12 hours

**Applies when:** A session exists.

**Obligation:** A session MUST stop authenticating 12 hours after login, and the server
MUST store only its SHA-256.

**Acceptance criteria:** A session at 12 h − 1 s authenticates and at 12 h does not. The
`sessions` table holds no raw token.

**Verification needs:** Service and validation unit tests.

### REQ-008: A session authenticates the existing account routes

**Applies when:** A request to an existing authenticated route carries a live session
cookie of a current user, no `Authorization` header, and `X-Ads-UI: 1`.

**Obligation:** The route MUST behave as if the account's access token were presented.

**Acceptance criteria:** Upload image and video, list images and videos, image lookup,
rename account, publish ad, and get/rename video succeed for the session's account.
Account-path routes for another account return 401 `002` for `access_token`;
`/videos/{id}` of another account returns 404 `002` for `video_id`, as today.

**Verification needs:** TestClient tests.

### REQ-009: Cookie authentication requires the UI header

**Applies when:** A request carries the session cookie.

**Obligation:** The server MUST ignore the cookie unless `X-Ads-UI: 1` is present and no
`Authorization` header is present.

**Acceptance criteria:** Cookie without the header: the same 401 as unauthenticated.
Wrong bearer plus valid cookie and header: 401. `POST /session` and `DELETE /session`
without the header: 401 `002` for `login`; sign-up without it: 401 `002` for
`access_token`.

**Verification needs:** TestClient tests.

### REQ-010: Existing bearer-token behaviour is unchanged

**Applies when:** Any client uses the Python or HTTP API with an access token.

**Obligation:** Responses, error codes, and signed media URLs MUST stay as they are.

**Acceptance criteria:** The existing test suite passes unchanged.

**Verification needs:** Full suite run.

### REQ-012: The UI is served with a strict policy

**Applies when:** A browser requests `/ui`, `/ui/app.js`, or `/ui/app.css`.

**Obligation:** The server MUST serve them with the CSP, `nosniff`, `no-referrer`, and
`no-store` headers in the design, without redirects.

**Acceptance criteria:** Each returns 200 with the right content type and those exact
headers; `index.html` has no inline `<script>`, `<style>`, or `style=`.

**Verification needs:** TestClient test; source inspection.

### REQ-013: Upload media from the UI

**Applies when:** A signed-in user uploads a file with an allowed extension.

**Obligation:** The UI MUST upload it to the matching route, show progress, and select
the resulting item.

**Acceptance criteria:** A new image appears in Images and is selected; a new video
(with a name) appears in Videos and is selected; a duplicate shows "Already uploaded"
and selects the existing item; a video without a name is not sent and shows an error.

**Verification needs:** Playwright test against a real server.

### REQ-014: Create an ad from the UI

**Applies when:** A signed-in user enters text and one media source.

**Obligation:** The UI MUST publish through `POST /accounts/{id}/ads` and show the API's
errors next to the named field.

**Acceptance criteria:** Publishing with an uploaded image, a video, and an image link
each shows "Ad #N created" and prepends the ad. Text "ab" shows the API's `text` error.

**Verification needs:** Playwright test.

### REQ-015: Show the account's existing ads

**Applies when:** A user is signed in.

**Obligation:** The UI MUST list the account's ads newest first, 10 per page, with
"Load more" while `next_cursor` is not null.

**Acceptance criteria:** 23 ads show 10, then 20, then 23, then no "Load more". None
shows "No ads yet". Rows show ID, time, media, and text cut at 120 characters.

**Verification needs:** Playwright test.

### REQ-016: Expired sessions return to sign-in

**Applies when:** A UI request other than sign-in or sign-up gets a 401.

**Obligation:** The UI MUST show the signed-out view with "Session expired" and no
account data.

**Acceptance criteria:** Deleting the session server-side, then clicking "Load more",
shows the sign-in form and the message. A wrong password shows the login error, not
"Session expired".

**Verification needs:** Playwright test.

### REQ-017: The UI is keyboard and screen-reader usable

**Applies when:** Any UI view is shown.

**Obligation:** Every input MUST have a label, errors MUST be referenced by
`aria-describedby`, and status changes MUST be announced politely.

**Acceptance criteria:** Inputs have `<label for>`; errors use `aria-describedby`;
progress uses `<progress>`; status text is in an `aria-live="polite"` region; the
sign-in flow works with the keyboard alone.

**Verification needs:** Source inspection; Playwright keyboard test for sign-in.

### REQ-018: Token rotation revokes the web login

**Applies when:** An operator calls `issue_access_token` for an account with a user.

**Obligation:** The user's existing sessions and logins MUST stop working, and a sign-up
with the new token MUST replace the user.

**Acceptance criteria:** After rotation, an old session gets 401, a login gets the
REQ-005 401, and sign-up with the new token returns 201 (old username free again).

**Verification needs:** Service and TestClient tests.

### REQ-019: Session status and sign-out

**Applies when:** A client calls `GET /session` or `DELETE /session`.

**Obligation:** `GET` MUST report a live session and `DELETE` MUST end it and clear the
cookie.

**Acceptance criteria:** `GET` returns 200 with the login body for a live session, else
401 `002` for `login`. `DELETE` returns 204 with an expiring `ads_session` cookie, after
which the old token gets 401. Successful `/session` responses carry
`Cache-Control: no-store`.

**Verification needs:** TestClient tests.

### REQ-020: The UI never renders user content as HTML

**Applies when:** The UI displays any server value.

**Obligation:** The UI MUST insert values only as text.

**Acceptance criteria:** `app.js` contains no `innerHTML`, `outerHTML`, or
`insertAdjacentHTML`; an ad whose text is `<b>x</b>` displays the literal characters.

**Verification needs:** Source inspection; Playwright test.

## Retired requirements

- **REQ-011** (media URLs issued to a session verify): retired after design review 1.
  The UI never uses `preview_url`; session responses return `null` instead.

## Implementation plan (one commit each)

1. **Spec.** This document and its review.
2. **Schema v3: users and sessions.** Migration, `User` model, repository methods,
   repository tests.
3. **Service: sign-up and login.** Credential rules, scrypt, throttle, sessions; service
   tests. (REQ-001–003, 005–007 at the service level, 018)
4. **`authenticate` accepts sessions.** Session branch with `sessions=` switch; tests.
   (REQ-007, 008, 018 at the service level)
5. **HTTP: user and session routes.** `POST /accounts/{id}/users`, `POST/GET/DELETE
   /session`; TestClient tests. (REQ-001, 002, 004, 005, 009 partly, 019)
6. **HTTP: session cookie on account routes.** Cookie fallback gated by `X-Ads-UI`;
   `preview_url` null for sessions; tests. (REQ-008, 009, 010, 018)
7. **UI: page and sign-in.** `/ui` routes, `web/` sign-in/sign-up view, Playwright
   harness. (REQ-012, 016, 017, 020)
8. **UI: upload and media lists.** (REQ-013)
9. **UI: create ad.** (REQ-014)
10. **UI: existing ads.** (REQ-015)
11. **README.**

## Changes from design review 1

All are the author's calls within the requester's decisions:

- C1: users store the claiming token's hash; rotation revokes logins and sessions; a new
  token re-claims (REQ-018).
- C2: sign-up is token-only and header-only (REQ-002).
- C3: one scrypt verification on every attempt (REQ-005).
- C4: requester confirmed internal demos; hard 15-minute lock replaced by a 30-second
  throttle, scrypt concurrency capped at 4; per-IP limits listed as a known limit.
- C5: REQ-008 keeps `/videos/{id}` at 404.
- C6: cookie always `Secure` unless explicitly opted out.
- C7: HTTP commit split into routes (5) and cookie fallback (6); mappings fixed.
- Suggestions taken: commit 3 and REQ-011 dropped; clock decided; `with_status` for
  `login`; race-safe sign-up; ASCII-only digits; separate image and video lists;
  `no-store`; explicit `/ui` routes; text-only rendering (REQ-020); 401 handling excludes
  sign-in and sign-up; session status/sign-out requirement (REQ-019).
