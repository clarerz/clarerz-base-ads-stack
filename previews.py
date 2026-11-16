"""Ad preview formats rendered as static HTML pages; no storage or HTTP here."""

from html import escape

# Page and media sizes in CSS pixels. Each media frame has its format's aspect ratio:
# feed 1:1, story 9:16, banner 1.91:1 (1200x628). Text is cut at text_limit characters.
FORMATS = {
    "feed": {"width": 500, "height": 640, "media": (500, 500), "text_limit": 125},
    "story": {"width": 360, "height": 640, "media": (360, 640), "text_limit": 125},
    "banner": {"width": 600, "height": 354, "media": (600, 314), "text_limit": 40},
}
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


def render(ad_format, account_name, text, media_kind, media_src):
    """Return the preview page. media_kind is "image" or "video"; no media_src shows an empty box."""
    spec = FORMATS[ad_format]
    truncated = len(text) > spec["text_limit"]
    shown = escape(text[:spec["text_limit"]].rstrip() + "…" if truncated else text)
    header = f"<header><strong>{escape(account_name)}</strong> · Sponsored</header>"
    media = _media(media_kind, media_src, autoplay=ad_format == "story")
    if ad_format == "feed":
        more = ' <span class="more">See more</span>' if truncated else ""
        body = f'{header}<p class="text">{shown}{more}</p>{media}'
    elif ad_format == "story":
        body = f'{media}{header}<p class="text">{shown}</p>'
    else:
        body = f'{media}<p class="text">{shown}</p>'
    media_width, media_height = spec["media"]
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Ad preview</title><style>
*{{box-sizing:border-box}}
body{{margin:0;width:{spec["width"]}px;height:{spec["height"]}px;overflow:hidden;
font:14px/1.4 system-ui,sans-serif;color:#1c1e21;background:#fff}}
.ad{{position:relative;width:100%;height:100%}}
.media{{display:block;width:{media_width}px;height:{media_height}px;object-fit:cover;
background:#e4e6eb;color:#65676b}}
.empty{{display:flex;align-items:center;justify-content:center}}
header{{height:44px;padding:12px 16px}}
.text{{margin:0;overflow:hidden}}
.feed .text{{height:96px;padding:0 16px 12px}}
.more{{color:#65676b;font-weight:600}}
.story .media{{position:absolute;inset:0}}
.story header{{position:absolute;top:0;width:100%;color:#fff;text-shadow:0 1px 2px #000}}
.story .text{{position:absolute;bottom:0;width:100%;padding:48px 16px 128px;color:#fff;
background:linear-gradient(transparent,rgba(0,0,0,.7))}}
.banner .text{{height:40px;padding:0 12px;line-height:40px;white-space:nowrap;
text-overflow:ellipsis}}
</style></head>
<body><article class="ad {ad_format}">{body}</article></body></html>
"""


def _media(kind, src, autoplay):
    if src is None:
        return ('<div class="media empty" role="img" aria-label="Media unavailable">'
                "Media unavailable</div>")
    src = escape(src)
    if kind == "image":
        return f'<img class="media" src="{src}" alt="Ad image">'
    # No scripts run in the page, so playback is set by attributes alone.
    playback = "autoplay muted loop playsinline" if autoplay else "controls muted playsinline"
    return f'<video class="media" src="{src}" {playback} preload="metadata" aria-label="Ad video"></video>'
