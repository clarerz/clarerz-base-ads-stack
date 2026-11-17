"use strict";
// Ads web UI. Every request is same-origin and carries X-Ads-UI, which lets the server accept
// the HttpOnly session cookie. Server values are only ever inserted as text.

const $ = (id) => document.getElementById(id);
const UI_HEADERS = { "X-Ads-UI": "1" };

// Friendlier copy for the field an API error names; the API's message is the fallback.
const HINTS = {
  account_id: "Unknown account, or it already has a login.",
  access_token: "That access token is not valid for this account.",
  username: "Use 3–32 letters, digits, or _ . - including at least one letter; it may be taken.",
  password: "Use 8–128 characters, different from the access token.",
};

class ApiError extends Error {
  constructor(status, body) {
    const message = (body && body.error_message) || `Request failed (${status})`;
    super(message);
    this.status = status;
    this.body = body || {};
    // validation.ValidationError words every 002 message this way.
    const match = /^The value provided for (\S+) is invalid$/.exec(message);
    this.field = match ? match[1] : null;
  }
}

async function api(method, path, json, headers = {}) {
  const options = { method, credentials: "same-origin", headers: { ...UI_HEADERS, ...headers } };
  if (json !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(json);
  }
  const response = await fetch(path, options);
  const body = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(response.status, body);
  return body;
}

// The signed-in account; panels read it and register hooks for sign-in and sign-out.
let session = null;
const onSignedIn = [];
const onSignedOut = [];

function setStatus(message) {
  $("status").textContent = message;
}

function setBusy(form, busy) {
  form.querySelectorAll("button").forEach((button) => { button.disabled = busy; });
}

function clearErrors(form) {
  form.querySelectorAll(".error").forEach((element) => { element.textContent = ""; });
  form.querySelectorAll("[aria-invalid]").forEach((input) => input.removeAttribute("aria-invalid"));
}

function showError(prefix, error, message) {
  const target = (error.field && $(`${prefix}-${error.field}-error`)) || $(`${prefix}-error`);
  target.textContent = message || HINTS[error.field] || error.message;
  document.querySelectorAll(`[aria-describedby~="${target.id}"]`)
    .forEach((input) => input.setAttribute("aria-invalid", "true"));
}

function showSignedIn(data) {
  session = data;
  $("account-label").textContent =
    `${data.username} · account ${data.account_id} (${data.account_name})`;
  $("signed-out").hidden = true;
  $("whoami").hidden = false;
  $("signed-in").hidden = false;
  onSignedIn.forEach((hook) => hook());
}

function showSignedOut(message = "") {
  session = null;
  $("signed-in").hidden = true;
  $("whoami").hidden = true;
  $("account-label").textContent = "";
  $("signed-out").hidden = false;
  onSignedOut.forEach((hook) => hook());
  setStatus(message);
}

// For calls made while signed in: a 401 means the session ended. Returns whether it did.
function sessionEnded(error) {
  if (error.status !== 401) return false;
  showSignedOut("Your session expired. Sign in again.");
  return true;
}

$("sign-in-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  clearErrors(form);
  setBusy(form, true);
  try {
    const data = await api("POST", "/session", { login: $("login").value, password: $("password").value });
    form.reset();
    setStatus("");
    showSignedIn(data);
  } catch (error) {
    showError("signin", error,
      error.status === 401 ? "Wrong username, account ID, or password." : error.message);
  } finally {
    setBusy(form, false);
  }
});

$("sign-up-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const token = $("signup-access_token");
  const username = $("signup-username").value;
  const password = $("signup-password").value;
  clearErrors(form);
  setBusy(form, true);
  try {
    const accountId = encodeURIComponent($("signup-account_id").value.trim());
    await api("POST", `/accounts/${accountId}/users`, { username, password },
      { Authorization: `Bearer ${token.value}` });
    form.reset();
    showSignedIn(await api("POST", "/session", { login: username, password }));
    setStatus("Login created.");
  } catch (error) {
    showError("signup", error);
  } finally {
    token.value = "";  // The token is only needed for this one request.
    setBusy(form, false);
  }
});

$("sign-out").addEventListener("click", async () => {
  try {
    await api("DELETE", "/session");
  } catch (error) {
    if (error.status !== 401) {
      setStatus(error.message);
      return;
    }
  }
  showSignedOut("Signed out.");
});

// ---- Media: upload and the two lists ----

const IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".gif", ".webp"];
const VIDEO_EXTENSIONS = [".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"];
const MEDIA = {
  image: { path: "images", key: (item) => item.hash, cursor: null, known: new Map() },
  video: { path: "videos", key: (item) => String(item.id), cursor: null, known: new Map() },
};
Object.assign(HINTS, {
  file: "Choose an allowed image or video file within the size limit.",
  name: "Give the video a name.",
  image_name: "Another of your images already has that name.",
});

const shortDate = (seconds) => new Date(seconds * 1000).toLocaleString();

function mediaLabel(kind, item) {
  return kind === "image"
    ? `${item.image_name || "Unnamed image"} · ${item.hash.slice(0, 8)}… · ${shortDate(item.creation_time)}`
    : `${item.name} · video #${item.id} · ${shortDate(item.upload_time)}`;
}

// Add an item to its list once (a fresh upload may come back on a later page); return its radio.
function addMedia(kind, item, atTop = false) {
  const state = MEDIA[kind];
  const key = state.key(item);
  const id = `media-${kind}-${key}`;
  state.known.set(key, item);
  if (!$(id)) {
    const row = document.createElement("li");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "media";
    radio.id = id;
    radio.value = `${kind}:${key}`;
    const label = document.createElement("label");
    label.htmlFor = id;
    label.textContent = mediaLabel(kind, item);
    row.append(radio, label);
    $(`${kind}-list`)[atTop ? "prepend" : "append"](row);
  }
  $(`${kind}-empty`).hidden = true;
  return $(id);
}

async function loadMedia(kind, more = false) {
  const state = MEDIA[kind];
  const query = more && state.cursor ? `?cursor=${encodeURIComponent(state.cursor)}` : "";
  const account = session.account_id;
  try {
    const page = await api("GET", `/accounts/${account}/${state.path}${query}`);
    if (!session || session.account_id !== account) return;  // Signed out meanwhile.
    page.data.forEach((item) => addMedia(kind, item));
    state.cursor = page.next_cursor;
    $(`${kind}-more`).hidden = !page.next_cursor;
    $(`${kind}-empty`).hidden = $(`${kind}-list`).children.length > 0;
  } catch (error) {
    if (!sessionEnded(error)) setStatus(error.message);
  }
}

function clearMedia() {
  for (const kind of Object.keys(MEDIA)) {
    MEDIA[kind].cursor = null;
    MEDIA[kind].known.clear();
    $(`${kind}-list`).replaceChildren();
    $(`${kind}-more`).hidden = true;
    $(`${kind}-empty`).hidden = false;
  }
}

// Fetch one item by its handle and select it in its list.
async function selectMedia(kind, key) {
  const item = kind === "image"
    ? await api("GET", `/accounts/${session.account_id}/images/lookup?image_hash=${encodeURIComponent(key)}`)
    : await api("GET", `/videos/${encodeURIComponent(key)}`);
  const radio = addMedia(kind, item, true);
  radio.checked = true;
  radio.dispatchEvent(new Event("change", { bubbles: true }));
  return item;
}

function kindOf(filename) {
  const extension = filename.slice(filename.lastIndexOf(".")).toLowerCase();
  if (IMAGE_EXTENSIONS.includes(extension)) return "image";
  return VIDEO_EXTENSIONS.includes(extension) ? "video" : null;
}

// XMLHttpRequest rather than fetch, for upload progress. The body is the file's raw bytes.
function sendFile(path, file, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", path);
    request.setRequestHeader("X-Ads-UI", "1");
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    });
    request.addEventListener("load", () => {
      let body = null;
      try { body = JSON.parse(request.responseText); } catch { /* Not JSON. */ }
      if (request.status >= 200 && request.status < 300) resolve(body);
      else reject(new ApiError(request.status, body));
    });
    request.addEventListener("error", () => reject(new ApiError(0, { error_message: "Network error" })));
    request.send(file);
  });
}

$("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const file = $("upload-file").files[0];
  const name = $("upload-name").value.trim();
  clearErrors(form);
  const kind = file && kindOf(file.name);
  if (!kind) {
    showError("upload", { field: "file" });
    return;
  }
  if (kind === "video" && !name) {
    showError("upload", { field: "name" });
    return;
  }
  const params = new URLSearchParams({ filename: file.name });
  if (name) params.set(kind === "image" ? "image_name" : "name", name);
  const progress = $("upload-progress");
  progress.value = 0;
  progress.hidden = false;
  setBusy(form, true);
  try {
    const created = await sendFile(`/accounts/${session.account_id}/${MEDIA[kind].path}?${params}`,
      file, (fraction) => { progress.value = Math.round(fraction * 100); });
    await selectMedia(kind, kind === "image" ? created.hash : created.id);
    form.reset();
    setStatus(`Uploaded ${file.name}.`);
  } catch (error) {
    if (error.status === 409) {
      // The account already has these bytes; the response names the existing item.
      await selectMedia(kind, kind === "image" ? error.body.hash : error.body.id).catch(() => {});
      form.reset();
      setStatus(`Already uploaded${name && kind === "image" ? " (the new name was not applied)" : ""}.`);
    } else if (!sessionEnded(error)) {
      const field = error.field === "image_name" ? "name" : error.field;
      showError("upload", { ...error, message: error.message, field }, HINTS[error.field]);
    }
  } finally {
    progress.hidden = true;
    setBusy(form, false);
  }
});

for (const kind of Object.keys(MEDIA)) {
  $(`${kind}-more`).addEventListener("click", () => loadMedia(kind, true));
}
// Settles when the first page of both lists has loaded (or failed).
let mediaLoaded = Promise.resolve();
onSignedIn.push(() => {
  clearMedia();
  mediaLoaded = Promise.all([loadMedia("image"), loadMedia("video")]);
});
onSignedOut.push(() => { clearMedia(); $("upload-form").reset(); });

// ---- Create ad ----

Object.assign(HINTS, {
  text: "Use at least 3 characters; the text cannot be only a link or a hash.",
  image: "Enter an http:// or https:// link.",
  image_hash: "Choose an image you uploaded.",
  video_id: "Choose a video you uploaded.",
});

const selectedMedia = () => document.querySelector('input[name="media"]:checked');

// One media source at a time: choosing an item clears the link, and typing a link clears the item.
function describeMedia() {
  const radio = selectedMedia();
  const link = $("ad-image").value.trim();
  let text = "none; choose an image or video above, or enter a link";
  if (radio) {
    const [kind, key] = radio.value.split(":");
    const item = MEDIA[kind].known.get(key);
    text = kind === "image" ? `Image “${item.image_name || item.hash.slice(0, 8)}”` : `Video “${item.name}”`;
  } else if (link) {
    text = "Image link";
  }
  $("ad-media").textContent = text;
}

$("signed-in").addEventListener("change", (event) => {
  if (event.target.name !== "media") return;
  $("ad-image").value = "";
  describeMedia();
});
$("ad-image").addEventListener("input", () => {
  if ($("ad-image").value.trim() && selectedMedia()) selectedMedia().checked = false;
  describeMedia();
});

$("ad-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  clearErrors(form);
  const body = { text: $("ad-text").value };
  const radio = selectedMedia();
  const link = $("ad-image").value.trim();
  if (radio) {
    const [kind, key] = radio.value.split(":");
    if (kind === "image") body.image_hash = key;
    else body.video_id = Number(key);
  } else if (link) {
    body.image = link;
  } else {
    showError("ad", {}, "Choose an image or video above, or enter an image link.");
    return;
  }
  setBusy(form, true);
  try {
    const ad = await api("POST", `/accounts/${session.account_id}/ads`, body);
    $("ad-text").value = "";
    setStatus(`Ad #${ad.id} created.`);
    addAd(await api("GET", `/ads/${ad.id}`), true);
  } catch (error) {
    if (!sessionEnded(error)) showError("ad", error);
  } finally {
    setBusy(form, false);
  }
});

onSignedOut.push(() => { $("ad-form").reset(); describeMedia(); });

// ---- Existing ads ----

const ADS_PER_PAGE = 10;
const TEXT_PREVIEW_LENGTH = 120;
let adCursor = null;

function adMedia(ad) {
  if (ad.video_id !== null) {
    const video = MEDIA.video.known.get(String(ad.video_id));
    return `Video ${video ? video.name : `#${ad.video_id}`}`;
  }
  const image = MEDIA.image.known.get(ad.image_hash);
  return `Image ${(image && image.image_name) || `${ad.image_hash.slice(0, 8)}…`}`;
}

function addAd(ad, atTop = false) {
  if ($(`ad-${ad.id}`)) return;
  const cell = (tag, text) => {
    const element = document.createElement(tag);
    element.textContent = text;
    return element;
  };
  const row = document.createElement("li");
  row.id = `ad-${ad.id}`;
  const link = cell("a", `#${ad.id}`);
  link.href = `/ads/${ad.id}`;
  const created = new Date(ad.creation_time * 1000);
  const time = cell("time", created.toLocaleString());
  time.dateTime = created.toISOString();
  const cut = ad.text.length > TEXT_PREVIEW_LENGTH;
  const text = cell("span", cut ? `${ad.text.slice(0, TEXT_PREVIEW_LENGTH)}…` : ad.text);
  text.title = ad.text;
  text.className = "ad-text";
  const preview = cell("button", "Preview");
  preview.type = "button";
  preview.setAttribute("aria-label", `Preview ad #${ad.id}`);
  preview.addEventListener("click", () => openPreview(ad.id));
  row.append(link, time, cell("span", adMedia(ad)), text, preview);
  $("ad-list")[atTop ? "prepend" : "append"](row);
  $("ad-empty").hidden = true;
}

async function loadAds(more = false) {
  const account = session.account_id;
  const params = new URLSearchParams({ limit: ADS_PER_PAGE });
  if (more && adCursor) params.set("cursor", adCursor);
  try {
    const page = await api("GET", `/accounts/${account}/ads?${params}`);
    if (!session || session.account_id !== account) return;  // Signed out meanwhile.
    page.data.forEach((ad) => addAd(ad));
    adCursor = page.next_cursor;
    $("ad-more").hidden = !page.next_cursor;
    $("ad-empty").hidden = $("ad-list").children.length > 0;
  } catch (error) {
    if (!sessionEnded(error)) setStatus(error.message);
  }
}

function clearAds() {
  adCursor = null;
  $("ad-list").replaceChildren();
  $("ad-more").hidden = true;
  $("ad-empty").hidden = false;
}

$("ad-more").addEventListener("click", () => loadAds(true));
// Media first, so ad rows can show image and video names.
onSignedIn.push(() => { clearAds(); mediaLoaded.then(() => loadAds()); });
onSignedOut.push(clearAds);

// ---- Ad previews ----

const WARNINGS = { text_truncated: "Text is cut off", media_cropped: "Image is cropped to fit" };
let previewRequest = 0;  // A response is dropped when a newer openPreview has started.

function closePreview() {
  previewRequest += 1;
  // Nothing from the dialog stays in the DOM once it is closed or the session ends.
  $("preview-frames").replaceChildren();
  $("preview-share").hidden = true;
  $("share-link").value = "";
  if ($("preview-dialog").open) $("preview-dialog").close();  // Returns focus to the opener.
}

async function openPreview(adId) {
  const request = ++previewRequest;
  const dialog = $("preview-dialog");
  $("preview-heading").textContent = `Preview of ad #${adId}`;
  $("preview-frames").replaceChildren();
  $("preview-share").hidden = true;
  $("share-link").value = "";
  $("share-expires").textContent = "";
  $("share-status").textContent = "";
  $("preview-message").textContent = "Loading preview…";
  if (!dialog.open) dialog.showModal();
  try {
    const page = await api("GET", `/accounts/${session.account_id}/ads/${adId}/previews`);
    if (request !== previewRequest) return;
    $("preview-message").textContent = "";
    if (page.data.some((item) => item.preview_url === null)) {
      $("preview-message").textContent = "Previews are unavailable on this server.";
      return;
    }
    $("preview-frames").replaceChildren(...page.data.map((item) => {
      const figure = document.createElement("figure");
      figure.dataset.format = item.format;
      const name = item.format.charAt(0).toUpperCase() + item.format.slice(1);
      const caption = document.createElement("figcaption");
      caption.textContent = name;
      const frame = document.createElement("iframe");
      frame.src = item.preview_url;
      frame.width = item.width;
      frame.height = item.height;
      frame.title = `${name} preview of ad #${adId}`;
      figure.append(caption, frame);
      for (const warning of item.warnings) {
        const note = document.createElement("p");
        note.className = "warning";
        note.textContent = WARNINGS[warning] || warning;
        figure.append(note);
      }
      return figure;
    }));
    if (page.share_url) {
      $("share-link").value = page.share_url;
      $("share-expires").textContent = `Expires ${new Date(page.share_expires * 1000).toLocaleString()}`;
      $("preview-share").hidden = false;
    }
  } catch (error) {
    if (request !== previewRequest) return;
    if (sessionEnded(error)) closePreview();
    else $("preview-message").textContent = error.message;
  }
}

$("copy-share").addEventListener("click", async () => {
  const input = $("share-link");
  try {
    await navigator.clipboard.writeText(input.value);
    $("share-status").textContent = "Link copied";
  } catch {
    input.focus();
    input.select();
    $("share-status").textContent = "Press Ctrl+C (\u2318C on Mac) to copy";
  }
});
$("preview-close").addEventListener("click", closePreview);
$("preview-dialog").addEventListener("close", closePreview);  // Escape closes it natively.
onSignedOut.push(closePreview);

async function start() {
  try {
    showSignedIn(await api("GET", "/session"));
  } catch (error) {
    showSignedOut(error.status === 401 ? "" : error.message);
  }
}

// Panels above have registered their hooks by now.
start();
