#!/usr/bin/env python3.11
"""Fetch recommendation tiles for a videoId and render them for the
ffplay-yt sidebar.

Output: prints lines to stdout, one per ready tile, in this format —

    RECS_ITEM <video_id>\t<thumb_path>\t<text_path>

The caller (bridge_player.py) forwards each line to ffplay-yt's IPC.
We also print `CLEAR_RECS` on the first line so callers can pipe stdout
directly into the socket.

Usage:
    python3.11 recs_pipeline.py <video_id>

Uses youthub.innertube (authenticated TVHTML5 / personalized recs) if
OAuth is set up; otherwise falls back to an unauth WEB-client call.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image, ImageDraw, ImageFont

PROJECT = Path(__file__).resolve().parent
CACHE = PROJECT / "cache"
THUMB_DIR = CACHE / "recs_thumbs"
TEXT_DIR = CACHE / "recs_text"

TILE_W = 456
TEXT_H = 70
BG = (26, 26, 28)
TITLE_FG = (240, 240, 240)
META_FG = (165, 165, 170)

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
TITLE_SIZE = 15
META_SIZE = 13

_HTTP: Optional[httpx.Client] = None


def _log(msg: str) -> None:
    sys.stderr.write(f"[recs] {msg}\n")


# ------------------- player-compatible image container ---------------
# ffplay-yt is built with --disable-everything and carries neither the
# image2 demuxer nor the mjpeg/png decoders, so it cannot open our
# .jpg/.png tiles: load_image_texture() fails and the sidebar draws grey
# placeholders. It *can* read mp4 — so each image is repackaged as a
# single-frame mp4 (mpeg4 is in the build's decoder list) before the
# path is handed over. Result is cached next to the original, so the
# conversion cost is paid once per tile, not per launch.
#
# If the player is ever rebuilt with image2+mjpeg+png (see
# План_панели_рекомендаций.md, путь Б), this stays harmless: mp4 still
# decodes, and the original .jpg/.png remain on disk untouched.
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
PLAYER_SUFFIX = ".mp4"


def to_player_format(src: Path) -> Optional[Path]:
    """Repackage an image as a one-frame mp4 the player can decode.

    Returns None on failure, so callers can fall back to the original
    path (the tile then shows a placeholder, as it does today).
    """
    dst = src.with_suffix(PLAYER_SUFFIX)
    if dst.exists() and dst.stat().st_size > 1000:
        return dst
    try:
        r = subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-i", str(src),
             "-c:v", "mpeg4", "-q:v", "2", "-frames:v", "1", str(dst)],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        _log(f"player-format conversion failed for {src.name}: {e}")
        return None
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size < 1000:
        _log(f"player-format conversion rc={r.returncode} for {src.name}: "
             f"{r.stderr.strip()[:160]}")
        dst.unlink(missing_ok=True)
        return None
    return dst


def _http() -> httpx.Client:
    global _HTTP
    if _HTTP is None:
        _HTTP = httpx.Client(http2=True, timeout=15.0)
    return _HTTP


# --------------------------- fetch ---------------------------


def fetch_recs_authed(video_id: str, *,
                      continuation: Optional[str] = None
                      ) -> Optional[tuple[list[dict], Optional[str]]]:
    """Try authenticated TVHTML5 InnerTube .next() — gives personalized
    recs. Returns (items, next_continuation_token) or None if auth not
    available."""
    try:
        from youthub import innertube, auth  # noqa
    except Exception as e:
        _log(f"youthub import failed: {e}")
        return None
    try:
        tokens = auth.get_tokens()
    except Exception:
        return None
    if tokens is None:
        return None
    try:
        with innertube.InnerTube(tokens) as it:
            data = it.next(video_id, continuation=continuation)
    except Exception as e:
        _log(f"authed next() failed: {e}")
        return None
    return _parse_next_secondary(data), _extract_continuation(data)


def fetch_recs_unauth(video_id: str, *,
                      continuation: Optional[str] = None
                      ) -> Optional[tuple[list[dict], Optional[str]]]:
    """Plain WEB-client unauth /next call. Public key is well-known."""
    body: dict = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20251201.00.00",
                # Match the TVHTML5 path — original Russian titles
                # rather than auto-translated English ones.
                "hl": "ru", "gl": "RU",
            },
        },
    }
    if continuation:
        body["continuation"] = continuation
    else:
        body["videoId"] = video_id
    url = ("https://www.youtube.com/youtubei/v1/next"
           "?key=AIzaSyAO_FL9IIRPjAExvbcQ8e_GRm7HX-V1aH4")
    try:
        r = _http().post(
            url, json=body,
            headers={
                "X-YouTube-Client-Name": "1",
                "X-YouTube-Client-Version": "2.20251201.00.00",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Origin": "https://www.youtube.com",
                "Referer": f"https://www.youtube.com/watch?v={video_id}",
            },
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        _log(f"unauth next() failed: {e}")
        return None
    return _parse_next_secondary(data), _extract_continuation(data)


def _parse_next_secondary(data: dict) -> list[dict]:
    """Walk the InnerTube .next() response to find recommendation
    items. Schema differs slightly between WEB and TVHTML5; we cover
    both by searching for `compactVideoRenderer` recursively."""
    results: list[dict] = []
    seen_ids: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            cvr = node.get("compactVideoRenderer")
            if cvr:
                item = _extract_compact(cvr)
                if item and item["video_id"] not in seen_ids:
                    seen_ids.add(item["video_id"])
                    results.append(item)
            tvr = node.get("tileRenderer")
            if tvr:
                item = _extract_tile(tvr)
                if item and item["video_id"] not in seen_ids:
                    seen_ids.add(item["video_id"])
                    results.append(item)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return results


def _extract_continuation(data: dict) -> Optional[str]:
    """Find the secondary-results continuation token if present.
    Handles both schema variants:
      WEB:     continuationItemRenderer → continuationEndpoint →
               continuationCommand → token
      TVHTML5: nextContinuationData → continuation"""
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            cir = node.get("continuationItemRenderer")
            if cir:
                try:
                    t = (cir["continuationEndpoint"]["continuationCommand"]
                         ["token"])
                    if isinstance(t, str) and t:
                        found.append(t)
                        return
                except (KeyError, TypeError):
                    pass
            ncd = node.get("nextContinuationData")
            if ncd:
                t = ncd.get("continuation")
                if isinstance(t, str) and t:
                    found.append(t)
                    return
            for v in node.values():
                walk(v)
                if found:
                    return
        elif isinstance(node, list):
            for v in node:
                walk(v)
                if found:
                    return

    walk(data)
    return found[0] if found else None


def _txt(node) -> str:
    if not node:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if "simpleText" in node:
            return node["simpleText"] or ""
        runs = node.get("runs") or []
        return "".join(r.get("text", "") for r in runs if isinstance(r, dict))
    return ""


def _extract_compact(cvr: dict) -> Optional[dict]:
    vid = cvr.get("videoId")
    if not vid or len(vid) != 11:
        return None
    title = _txt(cvr.get("title"))
    channel = (_txt(cvr.get("longBylineText"))
               or _txt(cvr.get("shortBylineText")))
    duration = _txt(cvr.get("lengthText"))
    views = _txt(cvr.get("shortViewCountText")) or _txt(cvr.get("viewCountText"))
    return {
        "video_id": vid,
        "title": title.strip(),
        "channel": channel.strip(),
        "duration": duration.strip(),
        "views": views.strip(),
    }


def _extract_tile(tvr: dict) -> Optional[dict]:
    # TVHTML5 schema: tileRenderer.onSelectCommand.watchEndpoint.videoId
    vid = None
    sc = tvr.get("onSelectCommand") or {}
    we = sc.get("watchEndpoint") or {}
    vid = we.get("videoId")
    if not vid or len(vid) != 11:
        return None
    title = _txt(tvr.get("metadata", {}).get("tileMetadataRenderer", {}).get("title"))
    lines = tvr.get("metadata", {}).get("tileMetadataRenderer", {}).get("lines") or []
    channel = ""
    duration = ""
    views = ""
    for ln in lines:
        items = ln.get("lineRenderer", {}).get("items") or []
        for it in items:
            t = _txt(it.get("lineItemRenderer", {}).get("text"))
            if "view" in t.lower():
                views = t
            elif ":" in t and len(t) <= 8:
                duration = t
            elif not channel and t:
                channel = t
    return {
        "video_id": vid,
        "title": title.strip(),
        "channel": channel.strip(),
        "duration": duration.strip(),
        "views": views.strip(),
    }


# --------------------------- thumbnails ---------------------------


def download_thumb(video_id: str) -> Optional[Path]:
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    dst = THUMB_DIR / f"{video_id}.jpg"
    if dst.exists() and dst.stat().st_size > 1000:
        return dst
    for fname in ("mqdefault.jpg", "hqdefault.jpg"):
        url = f"https://i.ytimg.com/vi/{video_id}/{fname}"
        try:
            r = _http().get(url, timeout=8)
            if r.status_code == 200 and len(r.content) > 1000:
                dst.write_bytes(r.content)
                return dst
        except Exception:
            continue
    return None


# --------------------------- text strip ---------------------------


def render_text_strip(item: dict) -> Path:
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    dst = TEXT_DIR / f"{item['video_id']}.png"
    # Cache by content hash so title edits invalidate the file
    meta_line = " · ".join(x for x in (item["channel"], item["views"], item["duration"]) if x)
    sig = f"{item['title']}|{meta_line}"
    sig_path = dst.with_suffix(".sig")
    if dst.exists() and sig_path.exists() and sig_path.read_text() == sig:
        return dst

    img = Image.new("RGB", (TILE_W, TEXT_H), BG)
    draw = ImageDraw.Draw(img)
    try:
        title_font = ImageFont.truetype(FONT_BOLD, TITLE_SIZE)
        meta_font = ImageFont.truetype(FONT_REG, META_SIZE)
    except Exception:
        title_font = ImageFont.load_default()
        meta_font = ImageFont.load_default()

    pad_x = 10
    avail_w = TILE_W - 2 * pad_x

    # Title: wrap to 2 lines max, ellipsize tail if longer.
    title = item["title"] or ""
    lines = _wrap_text(draw, title, title_font, avail_w, max_lines=2)
    y = 4
    for line in lines:
        draw.text((pad_x, y), line, font=title_font, fill=TITLE_FG)
        y += TITLE_SIZE + 4

    if meta_line:
        draw.text((pad_x, TEXT_H - META_SIZE - 8), meta_line[:80],
                  font=meta_font, fill=META_FG)
    img.save(dst, "PNG", optimize=True)
    sig_path.write_text(sig)
    return dst


def _wrap_text(draw, text: str, font, max_w: int, max_lines: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    cur: list[str] = []
    for w in words:
        trial = " ".join(cur + [w])
        if draw.textlength(trial, font=font) <= max_w:
            cur.append(w)
        else:
            if cur:
                lines.append(" ".join(cur))
                cur = [w]
            else:
                # Single word too long — cut hard.
                lines.append(_truncate_to_fit(draw, w, font, max_w))
                cur = []
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(" ".join(cur))
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    # Add ellipsis if title got truncated
    consumed = sum(len(line) + 1 for line in lines)
    if consumed < len(text) and lines:
        last = lines[-1]
        while draw.textlength(last + "…", font=font) > max_w and last:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def _truncate_to_fit(draw, word: str, font, max_w: int) -> str:
    while word and draw.textlength(word + "…", font=font) > max_w:
        word = word[:-1]
    return word + "…" if word else ""


# --------------------------- main ---------------------------


def main() -> int:
    """Two invocation forms:

        recs_pipeline.py <video_id>
            First batch. Prints CLEAR_RECS, then RECS_ITEM lines, then
            CONTINUATION <token> if a continuation is available.

        recs_pipeline.py --continuation <video_id> <token>
            Continuation batch. Prints RECS_ITEM lines (no CLEAR_RECS),
            then CONTINUATION <token> if there are more pages.
    """
    cont_mode = "--continuation" in sys.argv
    if cont_mode:
        idx = sys.argv.index("--continuation")
        if len(sys.argv) <= idx + 2:
            sys.stderr.write("usage: recs_pipeline.py --continuation "
                             "<video_id> <token>\n")
            return 2
        vid = sys.argv[idx + 1]
        token: Optional[str] = sys.argv[idx + 2]
    else:
        if len(sys.argv) < 2:
            sys.stderr.write("usage: recs_pipeline.py <video_id>\n")
            return 2
        vid = sys.argv[1]
        token = None

    result = fetch_recs_authed(vid, continuation=token)
    if result is None:
        result = fetch_recs_unauth(vid, continuation=token)
    if result is None:
        _log("no recs")
        return 1
    items, next_token = result
    if not items and not next_token:
        _log("empty response")
        return 1

    items = items[:40]

    if not cont_mode:
        print("CLEAR_RECS", flush=True)
    for it in items:
        thumb = download_thumb(it["video_id"])
        if thumb is None:
            continue
        try:
            text = render_text_strip(it)
        except Exception as e:
            _log(f"render failed for {it['video_id']}: {e}")
            continue
        # Hand the player mp4s: it has no image demuxer/decoders (see
        # to_player_format). Falls back to the original on failure, which
        # is no worse than the previous behaviour.
        thumb_play = to_player_format(thumb) or thumb
        text_play = to_player_format(text) or text
        print(f"RECS_ITEM {it['video_id']}\t{thumb_play}\t{text_play}",
              flush=True)
    if next_token:
        print(f"CONTINUATION {next_token}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
