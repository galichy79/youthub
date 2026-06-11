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
        # not a video tile (could be channel/playlist tile) — skip for now
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

    # Lines: typically line[0] = channel, line[1] = badges + views + age
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
            # Multilingual heuristics — YT localises these strings
            # based on the hl/gl context (we send hl=ru so Russian
            # videos return Russian text). Keep English markers too
            # so mixed-language users still parse correctly.
            is_views = "view" in low or "просмотр" in low
            is_age = ("ago" in low
                      or "назад" in low
                      or low.startswith("стрим"))   # "Стрим был ... назад"
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
    """Parse one lockupViewModel into a Video.

    YouTube is migrating TVHTML5 surfaces from tileRenderer to the new
    ViewModel architecture; search shelves now return a mix of both.
    The lockup is flatter than a tile: videoId sits in `contentId`,
    title/channel/views/age live under lockupMetadataViewModel, and the
    duration is a thumbnail overlay badge.
    """
    watch = (
        lockup.get("rendererContext", {})
        .get("commandContext", {})
        .get("onTap", {})
        .get("innertubeCommand", {})
        .get("watchEndpoint", {})
    )
    # Same rule as parse_tile: anything that opens a watchEndpoint is
    # playable (VIDEO and MUSIC lockups both do); channel/playlist
    # lockups navigate elsewhere and get skipped.
    video_id = watch.get("videoId")
    if not video_id:
        return None

    md = lockup.get("metadata", {}).get("lockupMetadataViewModel", {})
    title = (md.get("title") or {}).get("content") or ""

    thumb = lockup.get("contentImage", {}).get("thumbnailViewModel", {})
    thumb_url = _best_thumbnail(thumb.get("image", {}).get("sources", []))

    # Duration is an overlay badge ("6:10:58"); live streams put their
    # status text there instead ("В ЭФИРЕ") — same slot the old
    # thumbnailOverlayTimeStatusRenderer used, so keep the semantics.
    duration = None
    for ov in thumb.get("overlays", []) or []:
        for b in (ov.get("thumbnailBottomOverlayViewModel", {})
                    .get("badges", []) or []):
            txt = b.get("thumbnailBadgeViewModel", {}).get("text")
            if txt:
                duration = txt
                break
        if duration:
            break

    # Rows: typically row[0] = channel, row[1] = views + age (+ badges).
    # Reuse the same multilingual heuristics as parse_tile.
    channel = None
    views = None
    age = None
    badges: list[str] = []
    rows = (
        md.get("metadata", {})
        .get("contentMetadataViewModel", {})
        .get("metadataRows", [])
    )
    for row in rows or []:
        for b in row.get("badges", []) or []:
            lbl = b.get("badgeViewModel", {}).get("badgeText")
            if lbl:
                badges.append(lbl)
        for part in row.get("metadataParts", []) or []:
            txt = (part.get("text") or {}).get("content")
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
        video_id=video_id,
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
        lockup = it.get("lockupViewModel")
        if tile:
            v = parse_tile(tile)
        elif lockup:
            v = parse_lockup(lockup)
        else:
            continue
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

    Same shape as home — sectionListRenderer → shelfRenderer →
    horizontalListRenderer → tileRenderer — just without the
    `tvBrowseRenderer` wrapper that home uses.
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
