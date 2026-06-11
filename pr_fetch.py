#!/usr/bin/env python3.11
"""Fetch ytInitialPlayerResponse with sticky-rotation strategy selection.

Why this exists: YouTube's bot detection is stateful and adaptive. A
single fixed approach (one TLS profile + one proxy mode + one cookie
policy) inevitably gets fingerprinted and walled after some number of
sessions. Each "strategy" here is a different fingerprint combination
along three independent axes:

  * **transport**: curl_cffi (real Chrome / Firefox / Safari TLS) +
                   /watch HTML scrape  OR  /youtubei/v1/player POST
  * **proxy**:     env-configured HTTPS_PROXY or direct
  * **impersonate**: chrome131, firefox133, safari184, edge101, …

**Selection model (sticky):** there is always a single "current"
strategy. Each request makes ONE attempt with it. Success → stay on it
for next request. Failure → mark it dead, advance the pointer to the
next alive strategy, surface the error to the caller (no in-request
retry — the bridge already wraps us in a retry, which gets the next
strategy automatically).

The rotation list is pre-interleaved so each advance moves to a
different TLS family AND a different proxy mode — e.g. after
chrome131-direct dies, next is chrome145-proxy, not chrome131-proxy.
When every strategy is dead, round counter increments, all strategies
respawn alive, and rotation restarts from the top.

State lives in `cache/strategy_state.json` and survives program restart.

Modes:
    python3.11 pr_fetch.py <video_id>       # main: fetch PR, exit 0/2
    python3.11 pr_fetch.py --telemetry IDS  # background, fired by main
    python3.11 pr_fetch.py --stats          # human-readable state dump
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from curl_cffi import requests

PROJECT_DIR = Path(__file__).resolve().parent
CACHE_DIR = PROJECT_DIR / "cache"
STATE_FILE = CACHE_DIR / "strategy_state.json"

CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CHROME145_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
CHROME_ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)
FIREFOX_UA = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0"
)
SAFARI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"
)
SAFARI_IOS_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)


# InnerTube /youtubei/v1/player context per client. Different clients
# hit different YT backends with different bot-scoring rules. Most don't
# require PO Token; some return signed direct URLs instead of SABR (we
# still grab streamingData regardless — the sabr_bridge consumes
# whatever URL form is present).
INNERTUBE_CLIENTS = {
    "ANDROID_VR": {
        "clientName": "ANDROID_VR", "clientVersion": "1.61.48",
        "clientNameInt": 28,
        "deviceMake": "Oculus", "deviceModel": "Quest 3",
        "osName": "Android", "osVersion": "12L", "androidSdkVersion": 32,
        "platform": "MOBILE",
        "userAgent":
            "com.google.android.apps.youtube.vr.oculus/1.61.48 "
            "(Linux; U; Android 12L) gzip",
    },
    "IOS": {
        "clientName": "IOS", "clientVersion": "19.45.4",
        "clientNameInt": 5,
        "deviceModel": "iPhone16,2",
        "osName": "iOS", "osVersion": "18.1.0.22B83",
        "platform": "MOBILE",
        "userAgent":
            "com.google.ios.youtube/19.45.4 "
            "(iPhone16,2; U; CPU iOS 18_1_0 like Mac OS X;)",
    },
    "MWEB": {
        "clientName": "MWEB", "clientVersion": "2.20240910.01.00",
        "clientNameInt": 2, "platform": "MOBILE",
        "userAgent":
            "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/132.0.0.0 Mobile Safari/537.36",
    },
    "TVHTML5": {
        "clientName": "TVHTML5", "clientVersion": "7.20250122.14.00",
        "clientNameInt": 7, "platform": "TV",
        "userAgent":
            "Mozilla/5.0 (PlayStation; PlayStation 4/12.50) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.1.2 "
            "Safari/605.1.15",
    },
    "WEB_EMBEDDED_PLAYER": {
        "clientName": "WEB_EMBEDDED_PLAYER",
        "clientVersion": "1.20240801.00.00",
        "clientNameInt": 56, "platform": "DESKTOP",
        "userAgent": CHROME_UA,
    },
}

BASE_HEADERS = {
    "Accept":
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}

_PR_PATTERNS = [
    re.compile(r"ytInitialPlayerResponse\s*=\s*({.+?})\s*;\s*var", re.S),
    re.compile(r"ytInitialPlayerResponse\s*=\s*({.+?})\s*;\s*</script>", re.S),
    re.compile(r'"ytInitialPlayerResponse"\s*:\s*({.+?})\s*,\s*"ytInitialData"', re.S),
]


# --------------------------- strategies ---------------------------


@dataclass(frozen=True)
class Strategy:
    id: str            # stable identifier for stats
    impersonate: str   # curl_cffi browser profile (TLS / HTTP/2 fingerprint)
    proxy: str         # "env" (use HTTPS_PROXY) or "direct"
    ua: str            # User-Agent that matches impersonate
    transport: str = "html"               # "html" /watch or "innertube" /player
    innertube_client: Optional[str] = None  # key in INNERTUBE_CLIENTS

    def ua_for_warmup(self) -> dict:
        return {**BASE_HEADERS, "User-Agent": self.ua}

    def ua_for_watch(self) -> dict:
        return {**BASE_HEADERS, "User-Agent": self.ua,
                "Referer": "https://www.youtube.com/",
                "Sec-Fetch-Site": "same-origin"}

    def proxies_arg(self):
        # None ⇒ curl_cffi reads HTTPS_PROXY from env.
        if self.proxy == "direct":
            return {"http": None, "https": None}
        return None


# Three independent axes: TLS family × transport endpoint × proxy mode.
# Build the rotation list pre-interleaved so each step forward changes
# BOTH the base (TLS/endpoint) AND the proxy mode. After a strategy
# dies, the very next pick differs maximally — new IP, new fingerprint.


def _build_rotation(*specs) -> list[Strategy]:
    """Each spec = (suffix, impersonate, ua) or
    (suffix, impersonate, ua, transport, client).

    Layout: two passes through bases. Pass 1 stamps i-th base with
    direct if i even else proxy. Pass 2 stamps the opposite. So adjacent
    items always differ in BOTH base and proxy mode (when there's more
    than one base left to pick from)."""
    pairs = []
    for spec in specs:
        suffix, imp, ua, *rest = spec
        kwargs = {}
        if rest:
            kwargs["transport"] = rest[0]
            kwargs["innertube_client"] = rest[1]
        pairs.append((
            Strategy(f"{suffix}-direct", imp, "direct", ua, **kwargs),
            Strategy(f"{suffix}-proxy",  imp, "env",    ua, **kwargs),
        ))
    out: list[Strategy] = []
    for i, (direct_s, proxy_s) in enumerate(pairs):
        out.append(direct_s if i % 2 == 0 else proxy_s)
    for i, (direct_s, proxy_s) in enumerate(pairs):
        out.append(proxy_s if i % 2 == 0 else direct_s)
    return out


STRATEGIES = _build_rotation(
    # --- HTML /watch path × browser TLS profile ---
    ("cffi-chrome131",         "chrome131",         CHROME_UA),
    ("cffi-chrome145",         "chrome145",         CHROME145_UA),
    ("cffi-firefox133",        "firefox133",        FIREFOX_UA),
    ("cffi-safari184",         "safari184",         SAFARI_UA),
    ("cffi-safari180_ios",     "safari180_ios",     SAFARI_IOS_UA),
    ("cffi-chrome131_android", "chrome131_android", CHROME_ANDROID_UA),
    ("cffi-edge101",           "edge101",           EDGE_UA),
    # --- InnerTube /youtubei/v1/player × client × always chrome131 TLS ---
    ("innertube-android_vr",   "chrome131", CHROME_UA, "innertube", "ANDROID_VR"),
    ("innertube-ios",          "chrome131", CHROME_UA, "innertube", "IOS"),
    ("innertube-mweb",         "chrome131", CHROME_UA, "innertube", "MWEB"),
    ("innertube-tvhtml5",      "chrome131", CHROME_UA, "innertube", "TVHTML5"),
    ("innertube-embedded",     "chrome131", CHROME_UA, "innertube", "WEB_EMBEDDED_PLAYER"),
)
STRATEGIES_BY_ID = {s.id: s for s in STRATEGIES}
STRATEGY_INDEX = {s.id: i for i, s in enumerate(STRATEGIES)}


def _log(msg: str) -> None:
    sys.stderr.write(f"[pr_fetch] {msg}\n")


# --------------------------- state persistence ---------------------------


def _now() -> int:
    return int(time.time())


def _empty_state() -> dict:
    return {
        "current": STRATEGIES[0].id,  # default head of rotation
        "round": 1,
        "strategies": {},  # sid -> {alive, wins, deaths, last_win, last_death}
    }


def _load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = _empty_state()
    # Heal any drift from edits to STRATEGIES (removed/renamed ids).
    state.setdefault("round", 1)
    state.setdefault("strategies", {})
    if state.get("current") not in STRATEGIES_BY_ID:
        state["current"] = STRATEGIES[0].id
    return state


def _save_state(state: dict) -> None:
    # Atomic write: tmp + rename. Survives concurrent pr_fetch
    # invocations (last-writer-wins on the rename).
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, STATE_FILE)


def _strategy_record(state: dict, sid: str) -> dict:
    r = state["strategies"].get(sid)
    if r is None:
        r = {"alive": True, "wins": 0, "deaths": 0,
             "last_win": 0, "last_death": 0}
        state["strategies"][sid] = r
    return r


def _is_alive(state: dict, sid: str) -> bool:
    return _strategy_record(state, sid)["alive"]


def _advance_to_next_alive(state: dict) -> Strategy:
    """Move `state["current"]` to the next alive strategy in rotation.
    If everyone is dead, start a new round: resurrect all and point at
    the head of the list. Mutates state in place."""
    n = len(STRATEGIES)
    cur_idx = STRATEGY_INDEX.get(state["current"], 0)
    for offset in range(1, n + 1):
        cand = STRATEGIES[(cur_idx + offset) % n]
        if _is_alive(state, cand.id):
            state["current"] = cand.id
            return cand
    # All dead: new round. Start it from the strategy with the best
    # track record, not the head of the list — in practice only a few
    # strategies ever get through the wall (chrome131-direct & co.),
    # so a fresh round should probe those first instead of burning
    # time on combos that have never won.
    state["round"] = state.get("round", 1) + 1
    for rec in state["strategies"].values():
        rec["alive"] = True
    best = max(
        STRATEGIES,
        key=lambda s: (
            state["strategies"].get(s.id, {}).get("wins", 0),
            state["strategies"].get(s.id, {}).get("last_win", 0),
        ),
    )
    if state["strategies"].get(best.id, {}).get("wins", 0) == 0:
        best = STRATEGIES[0]  # no history yet — keep old behaviour
    state["current"] = best.id
    _log(f"all strategies died — starting round {state['round']} "
         f"from {best.id}")
    return best


def get_current() -> Strategy:
    """Return the strategy to try right now. If the persisted current
    is somehow dead (e.g. another invocation just killed it), advance."""
    state = _load_state()
    cur = STRATEGIES_BY_ID.get(state["current"], STRATEGIES[0])
    if not _is_alive(state, cur.id):
        cur = _advance_to_next_alive(state)
        _save_state(state)
    return cur


def record_success(sid: str) -> None:
    """Strategy worked — keep it as current, bump stats. Stay sticky."""
    state = _load_state()
    rec = _strategy_record(state, sid)
    rec["wins"] += 1
    rec["last_win"] = _now()
    rec["alive"] = True
    state["current"] = sid
    _save_state(state)


def record_death(sid: str) -> None:
    """Strategy got bot-walled — mark dead and advance to next alive."""
    state = _load_state()
    rec = _strategy_record(state, sid)
    rec["alive"] = False
    rec["deaths"] += 1
    rec["last_death"] = _now()
    # Advance current pointer if this was the current strategy.
    if state["current"] == sid:
        _advance_to_next_alive(state)
    _save_state(state)


# --------------------------- fetch primitives ---------------------------


def _new_session() -> requests.Session:
    """Brand-new session. CONSENT/SOCS hardcoded — they don't identify
    the visitor, just satisfy the EU consent gate."""
    sess = requests.Session()
    sess.cookies.set("CONSENT", "YES+", domain=".youtube.com", path="/")
    sess.cookies.set("SOCS", "CAI", domain=".youtube.com", path="/")
    return sess


def _warmup(sess: requests.Session, strat: Strategy) -> bool:
    """GET / to get a fresh VISITOR_INFO1_LIVE under the strategy's
    fingerprint. Returns True if HTTP 200, False otherwise (we still
    try /watch even on warm-up failure — sometimes / 4xx but /watch
    200, especially on direct connections)."""
    try:
        r = sess.get(
            "https://www.youtube.com/",
            headers=strat.ua_for_warmup(),
            impersonate=strat.impersonate,
            proxies=strat.proxies_arg(),
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        _log(f"warm-up failed ({strat.id}): {e}")
        return False


def _fetch_watch(sess: requests.Session, strat: Strategy,
                 vid: str) -> Optional[str]:
    """Return PR JSON blob if streamingData present, else None."""
    url = f"https://www.youtube.com/watch?v={vid}"
    try:
        r = sess.get(url, headers=strat.ua_for_watch(),
                     impersonate=strat.impersonate,
                     proxies=strat.proxies_arg(),
                     timeout=20)
    except Exception as e:
        _log(f"{strat.id}: network error: {e}")
        return None
    if r.status_code != 200:
        _log(f"{strat.id}: HTTP {r.status_code}")
        return None
    for pat in _PR_PATTERNS:
        m = pat.search(r.text)
        if not m:
            continue
        blob = m.group(1)
        try:
            pr = json.loads(blob)
        except Exception:
            continue
        if pr.get("streamingData"):
            return blob
        reason = (pr.get("playabilityStatus", {}).get("reason")
                  or pr.get("playabilityStatus", {}).get("status"))
        _log(f"{strat.id}: bot-walled ({reason})")
        return None
    _log(f"{strat.id}: no ytInitialPlayerResponse in body")
    return None


def _fetch_innertube(sess: requests.Session, strat: Strategy,
                     vid: str) -> Optional[str]:
    """POST /youtubei/v1/player with a non-WEB client context.

    Returns the response JSON as a string (serialised back from dict)
    so the caller treats this exactly like the /watch HTML path:
    a blob that downstream parses as JSON with streamingData inside.
    """
    ctx = INNERTUBE_CLIENTS[strat.innertube_client]
    body = {
        "context": {
            "client": {
                **{k: v for k, v in ctx.items() if k != "clientNameInt"},
                "hl": "en", "gl": "US", "utcOffsetMinutes": 0,
            },
            "user": {"lockedSafetyMode": False},
            "request": {"useSsl": True, "internalExperimentFlags": []},
        },
        "videoId": vid,
        "contentCheckOk": True,
        "racyCheckOk": True,
        "playbackContext": {
            "contentPlaybackContext": {
                "html5Preference": "HTML5_PREF_WANTS",
                "signatureTimestamp": 20100,
            },
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": ctx["userAgent"],
        "X-YouTube-Client-Name": str(ctx["clientNameInt"]),
        "X-YouTube-Client-Version": ctx["clientVersion"],
        "Origin": "https://www.youtube.com",
    }
    try:
        r = sess.post(
            "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
            json=body, headers=headers,
            impersonate=strat.impersonate,
            proxies=strat.proxies_arg(), timeout=20,
        )
    except Exception as e:
        _log(f"{strat.id}: network error: {e}")
        return None
    if r.status_code != 200:
        _log(f"{strat.id}: HTTP {r.status_code}")
        return None
    try:
        pr = r.json()
    except Exception as e:
        _log(f"{strat.id}: bad JSON: {e}")
        return None
    if pr.get("streamingData"):
        # Re-serialise to a string so the rest of the pipeline treats
        # this identically to the /watch HTML-extracted blob.
        return json.dumps(pr)
    reason = (pr.get("playabilityStatus", {}).get("reason")
              or pr.get("playabilityStatus", {}).get("status"))
    _log(f"{strat.id}: bot-walled ({reason})")
    return None


def _try_strategy(strat: Strategy, vid: str) -> Optional[str]:
    t0 = time.time()
    sess = _new_session()
    if strat.transport == "innertube":
        # No warm-up for InnerTube — it's a JSON POST to a different
        # endpoint, the warm-up cookies don't apply the same way.
        blob = _fetch_innertube(sess, strat, vid)
    else:
        _warmup(sess, strat)
        blob = _fetch_watch(sess, strat, vid)
    if blob:
        _log(f"{strat.id} OK in {(time.time()-t0)*1000:.0f}ms")
    return blob


# --------------------------- telemetry ---------------------------


def _spawn_telemetry(vid: str, pr_blob: str, strategy_id: str) -> None:
    rec_ids = []
    try:
        rec_ids = _extract_recommended_video_ids(pr_blob)[:8]
    except Exception:
        pass
    ids = [vid] + rec_ids
    args = [sys.executable, str(Path(__file__).resolve()),
            "--telemetry", strategy_id, ",".join(ids)]
    try:
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ.copy(),
        )
    except Exception as e:
        _log(f"failed to spawn telemetry: {e}")


def _extract_recommended_video_ids(pr_blob: str) -> list[str]:
    ids = re.findall(r'"videoId"\s*:\s*"([A-Za-z0-9_-]{11})"', pr_blob)
    seen, out = set(), []
    for v in ids:
        if v not in seen:
            seen.add(v); out.append(v)
    return out


def _telemetry_main() -> int:
    # argv: pr_fetch.py --telemetry <strategy_id> <comma_ids>
    try:
        strategy_id = sys.argv[2]
        ids = sys.argv[3].split(",") if len(sys.argv) > 3 else []
    except Exception:
        return 0
    strat = STRATEGIES_BY_ID.get(strategy_id, STRATEGIES[0])
    if not ids:
        return 0
    sess = _new_session()
    headers = {
        "User-Agent": strat.ua,
        "Accept": "image/avif,image/webp,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.youtube.com/watch?v={ids[0]}",
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }
    for vid in ids:
        for path in ("mqdefault.jpg", "hqdefault.jpg"):
            try:
                sess.get(f"https://i.ytimg.com/vi/{vid}/{path}",
                         headers=headers, impersonate=strat.impersonate,
                         proxies=strat.proxies_arg(), timeout=5)
            except Exception:
                pass
    return 0


# --------------------------- main ---------------------------


def _stats_dump() -> int:
    state = _load_state()
    now = _now()
    print(f"round: {state['round']}   current: {state['current']}")
    print()
    print(f"{'#':>3} {'strategy':<36} {'state':<6} {'last_win':>10} "
          f"{'last_death':>10} {'wins':>5} {'deaths':>6}")
    def ago(ts):
        if ts == 0: return "never"
        d = now - ts
        if d < 60: return f"{d}s"
        if d < 3600: return f"{d//60}m"
        return f"{d//3600}h"
    for i, s in enumerate(STRATEGIES):
        rec = state["strategies"].get(s.id, {})
        alive = rec.get("alive", True)
        marker = "→" if state["current"] == s.id else " "
        st_label = "alive" if alive else "DEAD"
        print(f"{marker:>1}{i:>2} {s.id:<36} {st_label:<6} "
              f"{ago(rec.get('last_win', 0)):>10} "
              f"{ago(rec.get('last_death', 0)):>10} "
              f"{rec.get('wins', 0):>5} {rec.get('deaths', 0):>6}")
    return 0


def _advance_main() -> int:
    """Force-advance the sticky pointer to the next alive strategy.

    For callers whose failure pr_fetch can't see: bridge_player uses
    this when a strategy fetches a PR fine but the SABR stream built
    from it produces zero bytes — without this nudge the strategy
    stays "current" forever and every retry rolls the same dice.
    The current strategy is NOT marked dead: it does work for PR
    fetching, we just want different inputs for the next attempt.
    """
    state = _load_state()
    old = state["current"]
    new = _advance_to_next_alive(state)
    _save_state(state)
    _log(f"advance: {old} → {new.id}")
    return 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--telemetry":
        return _telemetry_main()
    if len(sys.argv) >= 2 and sys.argv[1] == "--stats":
        return _stats_dump()
    if len(sys.argv) >= 2 and sys.argv[1] == "--advance":
        return _advance_main()
    if len(sys.argv) < 2:
        sys.stderr.write(
            "usage: pr_fetch.py <video_id>\n"
            "       pr_fetch.py --stats\n"
            "       pr_fetch.py --advance\n")
        return 64

    vid = sys.argv[1]
    strat = get_current()
    _log(f"using {strat.id} (round {_load_state()['round']})")

    blob = _try_strategy(strat, vid)
    if blob is None:
        record_death(strat.id)
        new_current = _load_state()["current"]
        _log(f"died → next current: {new_current}")
        sys.stderr.write(
            f"bot-wall on {strat.id}; advanced to {new_current}\n")
        return 2

    record_success(strat.id)
    sys.stdout.write(blob)
    sys.stdout.flush()
    _spawn_telemetry(vid, blob, strat.id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
