"""Ad preview formats rendered as static HTML pages; no storage or HTTP here."""

from html import escape
from pathlib import Path
import time

# Page and media sizes in CSS pixels. Each media frame has its format's aspect ratio:
# feed 1:1, story 9:16, banner 1.91:1 (1200x628). Text is cut at text_limit characters.
FORMATS = {
    "feed": {"width": 500, "height": 640, "media": (500, 500), "text_limit": 125},
    "story": {"width": 360, "height": 640, "media": (360, 640), "text_limit": 125},
    "banner": {"width": 600, "height": 354, "media": (600, 314), "text_limit": 40},
}
PREVIEW_CSS = (Path(__file__).parent / "web" / "preview.css").read_text()
# Media whose aspect ratio is off by more than this fraction is reported as cropped.
CROP_TOLERANCE = 0.01


def warnings(ad_format, text, width=None, height=None):
    """Return what the format changes about the ad: cut text, or media cropped to fit."""
    spec = FORMATS[ad_format]
    found = ["text_truncated"] if len(text) > spec["text_limit"] else []
    if width and height:
        frame = spec["media"][0] / spec["media"][1]
        if abs(width / height - frame) > CROP_TOLERANCE * frame:
            found.append("media_cropped")
    return found

# What each warning from warnings() means, for the share page and the UI.
WARNING_TEXT = {"text_truncated": "Text is cut off", "media_cropped": "Image is cropped to fit"}

SHARE_NOT_FOUND = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Preview unavailable</title></head>
<body><p>This preview link is invalid or has expired.</p></body></html>
"""


def shown_text(ad_format, text):
    """Return the text as the format displays it, cut at its limit, before HTML escaping."""
    limit = FORMATS[ad_format]["text_limit"]
    return text[:limit].rstrip() + "…" if len(text) > limit else text


def render(ad_format, account_name, text, media_kind, media_src):
    """Return the preview page. media_kind is "image" or "video"; no media_src shows an empty box."""
    spec = FORMATS[ad_format]
    truncated = len(text) > spec["text_limit"]
    shown = escape(shown_text(ad_format, text))
    header = f"<header><strong>{escape(account_name)}</strong> · Sponsored</header>"
    media = _media(media_kind, media_src, autoplay=ad_format == "story")
    if ad_format == "feed":
        more = ' <span class="more">See more</span>' if truncated else ""
        body = f'{header}<p class="text">{shown}{more}</p>{media}'
    elif ad_format == "story":
        body = f'{media}{header}<p class="text">{shown}</p>'
    else:
        body = f'{media}<p class="text">{shown}</p>'
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Ad preview</title><style>body{{margin:0;overflow:hidden;font:14px/1.4 system-ui,sans-serif;color:#1c1e21;background:#fff}}{PREVIEW_CSS}</style></head>
<body><article class="ad {ad_format}">{body}</article></body></html>
"""


def render_share(account_name, ad_id, expires, entries, update_time=None):
    """Return the page framing every placement. Each entry has format, url, width, height and
    warnings; the page runs no script."""
    until = time.strftime("%Y-%m-%d %H:%M", time.gmtime(expires))
    edited = ""
    if update_time is not None:
        edited = (f'<p class="expires">Last edited '
                  f'{time.strftime("%Y-%m-%d %H:%M", time.gmtime(update_time))} UTC</p>')
    figures = "".join(
        f'<figure><figcaption>{escape(entry["format"].capitalize())}</figcaption>'
        f'<iframe src="{escape(entry["url"])}" width="{entry["width"]}" height="{entry["height"]}" '
        f'title="{escape(entry["format"].capitalize())} preview of ad #{ad_id}"></iframe>'
        + "".join(f"<p>{escape(WARNING_TEXT[w])}</p>" for w in entry["warnings"])
        + "</figure>"
        for entry in entries)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Ad preview</title><style>
body{{margin:0;padding:16px;font:14px/1.4 system-ui,sans-serif;color:#1c1e21;background:#f0f2f5}}
h1{{margin:0;font-size:18px}}
.expires{{margin:4px 0 16px;color:#65676b}}
.frames{{display:flex;flex-wrap:wrap;gap:24px;align-items:flex-start}}
figure{{margin:0}}
figcaption{{margin-bottom:4px;font-weight:600}}
iframe{{display:block;max-width:100%;border:1px solid #ccd0d5;background:#fff}}
figure p{{margin:4px 0 0;color:#65676b}}
</style></head>
<body><h1>Ad preview · {escape(account_name)} · Ad #{ad_id}</h1>
{edited}<p class="expires">Link expires {until} UTC</p>
<div class="frames">{figures}</div></body></html>
"""


def _media(kind, src, autoplay):
    if src is None:
        return ('<div class="media empty" role="img" aria-label="Media unavailable">'
                "Media unavailable</div>")
    src = escape(src)
    if kind == "image":
        return f'<img class="media" src="{src}" alt="Ad image">'
    # No scripts run in the page, so playback is set by attributes alone.
    playback = "controls autoplay muted loop playsinline" if autoplay else "controls muted playsinline"
    return f'<video class="media" src="{src}" {playback} preload="metadata" aria-label="Ad video"></video>'
