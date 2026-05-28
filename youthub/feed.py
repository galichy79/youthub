"""Parse InnerTube TVHTML5 responses into flat dataclasses.

The raw JSON is a deeply nested renderer tree. UI code shouldn't care
about that — it just wants `feed.shelves[0].videos[0].title`.

Robustness: many fields are optional in YouTube responses (badges,
durations, channel names sometimes missing on shorts/livestreams).
We default to None / empty rather than crashing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# --- raw helpers -----------------------------------------------------------


def _text(node: Optional[dict]) -> Optional[str]:
    """Extract plain text from a YouTube text node — handles simpleText / runs."""
    if not isinstance(node, dict):
        return None
    if "simpleText" in node:
        return node["simpleText"]
    runs = node.get("runs")
    if isinstance(runs, list):
        return "".join(r.get("text", "") for r in runs if isinstance(r, dict))
    return None


def _best_thumbnail(thumbs: list) -> Optional[str]:
    """Pick the largest thumbnail URL. Thumbs are ordered small→large already
    but we don't rely on that — explicit max-by-area."""
    best = None
    best_area = -1
    for t in thumbs or []:
        if not isinstance(t, dict):
            continue
        url = t.get("url")
        if not url:
            continue
        area = int(t.get("width") or 0) * int(t.get("height") or 0)
        if area > best_area:
            best = url
            best_area = area
    return best


# --- models ----------------------------------------------------------------


@dataclass
class Video:
    video_id: str
    title: str
    channel: Optional[str] = None
    views: Optional[str] = None       # "3.4K views" — raw, includes the word "views"
    age: Optional[str] = None         # "1 month ago"
    duration: Optional[str] = None    # "5:28:00" or "12:34"
    badges: list[str] = field(default_factory=list)  # ["4K", "CC", ...]
    thumbnail_url: Optional[str] = None
    playlist_id: Optional[str] = None  # for autoplay-after-video queue
    params: Optional[str] = None       # watchEndpoint params (signed nav token)


@dataclass
class Shelf:
    title: str
    videos: list[Video] = field(default_factory=list)


@dataclass
class Feed:
    shelves: list[Shelf] = field(default_factory=list)
    continuation: Optional[str] = None  # for paging the home feed

    def all_videos(self) -> list[Video]:
        return [v for sh in self.shelves for v in sh.videos]


# --- parsers ---------------------------------------------------------------


def parse_tile(tile: dict) -> Optional[Video]:
    """Parse one tileRenderer into a Video. Returns None if it isn't a video tile."""
    on_select = tile.get("onSelectCommand", {})
    watch = on_select.get("watchEndpoint")
    if not watch or "videoId" not in watch:
        return None

    header = tile.get("header", {}).get("tileHeaderRenderer", {})
    metadata = tile.get("metadata", {}).get("tileMetadataRenderer", {})

    thumb_url = _best_thumbnail(header.get("thumbnail", {}).get("thumbnails", []))

    duration = None
    for ov in header.get("thumbnailOverlays", []) or []:
        ts = ov.get("thumbnailOverlayTimeStatusRenderer")
        if ts:
            duration = _text(ts.get("text"))
            break

    title = _text(metadata.get("title")) or ""

    channel = None
    views = None
    age = None
    badges: list[str] = []
    for line in metadata.get("lines", []) or []:
        line_items = line.get("lineRenderer", {}).get("items", []) or []
        for item in line_items:
            li = item.get("lineItemRenderer", {})
            if "badge" in li:
                b = li["badge"].get("metadataBadgeRenderer", {})
                lbl = b.get("label")
                if lbl:
                    badges.append(lbl)
                continue
            txt = _text(li.get("text"))
            if not txt:
                continue
            low = txt.lower()
            is_views = "view" in low or "просмотр" in low
            is_age = ("ago" in low
                      or "назад" in low
                      or low.startswith("стрим"))
            if channel is None and not is_views and not is_age and txt != "•":
                channel = txt
            elif is_views:
                views = txt
            elif is_age:
                age = txt

    return Video(
        video_id=watch["videoId"],
        title=title,
        channel=channel,
        views=views,
        age=age,
        duration=duration,
        badges=badges,
        thumbnail_url=thumb_url,
        playlist_id=watch.get("playlistId"),
        params=watch.get("params"),
    )


def parse_lockup(lockup: dict) -> Optional[Video]:
    """Parse one lockupViewModel into a Video. Newer YouTube surfaces (search,
    short-form shelves) use this instead of tileRenderer."""
    cid = lockup.get("contentId")
    if not cid:
        return None

    # Only video/short content types
    ctype = lockup.get("contentType", "")
    if ctype not in ("LOCKUP_CONTENT_TYPE_VIDEO", "LOCKUP_CONTENT_TYPE_SHORT"):
        return None

    # Thumbnail
    thumb_vm = lockup.get("contentImage", {}).get("thumbnailViewModel", {})
    thumb_url = _best_thumbnail(thumb_vm.get("image", {}).get("sources", []))

    # Duration from overlay badges
    duration = None
    for ov in thumb_vm.get("overlays", []) or []:
        bottom = ov.get("thumbnailBottomOverlayViewModel", {})
        for badge in bottom.get("badges", []) or []:
            dur = badge.get("thumbnailBadgeViewModel", {}).get("text")
            if dur:
                duration = dur
                break

    # Metadata
    meta = lockup.get("metadata", {}).get("lockupMetadataViewModel", {})
    title = meta.get("title", {}).get("content") or ""

    cmv = meta.get("metadata", {}).get("contentMetadataViewModel", {})
    rows = cmv.get("metadataRows", []) or []
    channel = None
    views = None
    age = None
    badges: list[str] = []
    for row in rows:
        parts = row.get("metadataParts", []) or []
        for part in parts:
            txt = part.get("text", {}).get("content")
            if not txt:
                continue
            low = txt.lower()
            is_views = "view" in low or "просмотр" in low
            is_age = ("ago" in low
                      or "назад" in low
                      or low.startswith("стрим"))
            if channel is None and not is_views and not is_age:
                channel = txt
            elif is_views:
                views = txt
            elif is_age:
                age = txt

    # watchEndpoint — nested deep inside rendererContext
    cmd = (
        lockup.get("rendererContext", {})
        .get("commandContext", {})
        .get("onTap", {})
        .get("innertubeCommand", {})
    )
    watch = cmd.get("watchEndpoint", {})
    return Video(
        video_id=watch.get("videoId") or cid,
        title=title,
        channel=channel,
        views=views,
        age=age,
        duration=duration,
        badges=badges,
        thumbnail_url=thumb_url,
        playlist_id=watch.get("playlistId"),
        params=watch.get("params"),
    )


def parse_shelf(shelf_node: dict) -> Optional[Shelf]:
    """Parse one shelfRenderer into a Shelf with its videos."""
    sh = shelf_node.get("shelfRenderer")
    if not sh:
        return None
    header = sh.get("headerRenderer", {}).get("shelfHeaderRenderer", {})
    # TVHTML5 wraps the title inside an avatarLockup; older surfaces put it
    # directly on the header. Try both.
    title = (
        _text(header.get("title"))
        or _text(header.get("avatarLockup", {})
                       .get("avatarLockupRenderer", {})
                       .get("title"))
        or "<untitled>"
    )

    items = (
        sh.get("content", {})
        .get("horizontalListRenderer", {})
        .get("items", [])
    )
    videos: list[Video] = []
    for it in items:
        tile = it.get("tileRenderer")
        if tile:
            v = parse_tile(tile)
            if v:
                videos.append(v)
            continue
        lockup = it.get("lockupViewModel")
        if lockup:
            v = parse_lockup(lockup)
            if v:
                videos.append(v)
    return Shelf(title=title, videos=videos)


def _walk_sections(sections: list) -> Feed:
    """Common section-list walker — works for both home and /next pivot."""
    feed = Feed()
    for sec in sections:
        if "shelfRenderer" in sec:
            sh = parse_shelf(sec)
            if sh:
                feed.shelves.append(sh)
        elif "continuationItemRenderer" in sec:
            cont = (
                sec["continuationItemRenderer"]
                   .get("continuationEndpoint", {})
                   .get("continuationCommand", {})
                   .get("token")
            )
            if cont:
                feed.continuation = cont
    return feed


def parse_home(raw: dict) -> Feed:
    """Parse the FEwhat_to_watch response into a Feed."""
    try:
        sections = (
            raw["contents"]["tvBrowseRenderer"]["content"]
               ["tvSurfaceContentRenderer"]["content"]
               ["sectionListRenderer"]["contents"]
        )
    except (KeyError, TypeError):
        return Feed()
    return _walk_sections(sections)


def parse_search(raw: dict) -> Feed:
    """Parse a TVHTML5 /search response into a Feed.

    Search returns sectionListRenderer → shelfRenderer →
    horizontalListRenderer → lockupViewModel (newer) or tileRenderer (older).
    """
    try:
        sections = raw["contents"]["sectionListRenderer"]["contents"]
    except (KeyError, TypeError):
        return Feed()
    return _walk_sections(sections)


def parse_next_pivot(raw: dict) -> Feed:
    """Parse `/next` response into a Feed using the `pivot` recommendations.

    TVHTML5 puts ~10 shelves of related content under
    contents.singleColumnWatchNextResults.pivot.sectionListRenderer —
    these are the up-next / related videos, much richer than home.
    """
    try:
        sections = (
            raw["contents"]["singleColumnWatchNextResults"]
               ["pivot"]["sectionListRenderer"]["contents"]
        )
    except (KeyError, TypeError):
        return Feed()
    return _walk_sections(sections)
