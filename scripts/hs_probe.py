#!/usr/bin/env python3.11
"""Headless SABR probe — run one stream session and watch it, no player window.

Usage:
    .venv/bin/python3.11 scripts/hs_probe.py <videoId> [watch_seconds]

Raises the bridge exactly the way bridge_player.py does (same node
resolver, same PATH pinning, same control protocol) but skips ffplay
entirely: it starts a session, watches the .mkv grow, and reports.

Why this exists: a probe that only lives in /tmp gets lost (/tmp is
cleaned), and every live-window test costs two sessions (the start plus
the automatic retry) and takes over the screen.

Reference points measured on this machine 2026-09-30:
  * one cold-start burst is ~61 s of media, ~17.3 MB
  * un-finalised .mkv reports format=duration as N/A, but the last
    video PTS is readable — that is how media_end is computed below.

Exit code is 0 when the stream was still growing at the end of the
watch window, 1 when it stalled.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import bridge_player as bp  # noqa: E402


def media_end_sec(path: Path) -> float:
    """Last video PTS in the file, in seconds (0.0 if unknown)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except Exception:
        return 0.0
    vals = [float(v) for v in out.replace("\n", ",").split(",") if v.strip()]
    return vals[-1] if vals else 0.0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    video_id = sys.argv[1]
    watch_sec = float(sys.argv[2]) if len(sys.argv) > 2 else 120.0

    node_exe = bp._resolve_node()
    env = dict(os.environ)
    env["PATH"] = str(Path(node_exe).parent) + os.pathsep + env.get("PATH", "")

    log_path = Path(tempfile.mkstemp(prefix=f"hs_probe_{video_id}_",
                                     suffix=".log")[1])
    sock_path = Path(tempfile.mkstemp(prefix=f"hs_probe_{video_id}_",
                                      suffix=".sock")[1])
    sock_path.unlink(missing_ok=True)
    mkv = Path(tempfile.mkstemp(prefix=f"hs_probe_{video_id}_",
                                suffix=".mkv")[1])

    cmd = [node_exe, str(PROJECT_DIR / "sabr_bridge.mjs"),
           video_id, "--control", str(sock_path)]
    print(f"probe: {video_id}  watch={watch_sec:.0f}s")
    print(f"  node: {node_exe}")
    print(f"  log:  {log_path}")
    print(f"  mkv:  {mkv}")
    print(f"  cmd:  {shlex.join(cmd)}")

    log = open(log_path, "ab")
    bridge = subprocess.Popen(cmd, cwd=PROJECT_DIR, env=env,
                              stdout=log, stderr=log,
                              stdin=subprocess.DEVNULL,
                              start_new_session=True)
    ctrl = bp.BridgeControl(sock_path)
    rc = 1
    try:
        if not ctrl.connect(timeout=25.0):
            print("FAIL: bridge did not open its control socket in 25s")
            return 1
        print("  control socket connected")

        t0 = time.time()
        # Generous: the bridge may sweep all 24 TLS strategies first.
        reply = ctrl.send(f"START_SESSION path={mkv} start_at=0",
                          reply_timeout=180.0)
        print(f"  START_SESSION -> {reply!r}")
        if not reply or not reply.startswith("OK"):
            print("FAIL: session refused (bot-wall / wall sweep)")
            return 1
        duration_sec = 0.0
        m = re.search(r"duration_ms=(\d+)", reply)
        if m:
            duration_sec = int(m.group(1)) / 1000.0
        print(f"  session started after {time.time() - t0:.1f}s"
              f"  (video is {duration_sec:.0f}s)\n")

        print(f"  {'t':>6}  {'bytes':>12}  {'MB':>7}  {'media_s':>8}")
        last_size = -1
        stalled_at = None
        t_start = time.time()
        while time.time() - t_start < watch_sec:
            time.sleep(5.0)
            elapsed = time.time() - t_start
            size = mkv.stat().st_size if mkv.exists() else 0
            grew = size > last_size
            last_size = size
            print(f"  {elapsed:6.0f}  {size:12d}  {size/1048576:7.1f}  "
                  f"{media_end_sec(mkv):8.1f}{'' if grew else '   <- no growth'}")
            if not grew and stalled_at is None:
                stalled_at = elapsed
            elif grew:
                stalled_at = None

        final_size = mkv.stat().st_size if mkv.exists() else 0
        final_media = media_end_sec(mkv)
        log.flush()
        text = log_path.read_text(errors="replace")
        statuses = [ln for ln in text.splitlines() if "stream protection" in ln]
        attest = sum(1 for ln in text.splitlines()
                     if "attestation required" in ln)

        # Growth stopping is only a failure if there is still media left
        # to fetch: a session that delivered the whole video also stops.
        complete = duration_sec and final_media >= duration_sec - 2.0
        print("\n--- summary ---")
        print(f"  bytes:        {final_size} ({final_size/1048576:.1f} MB)")
        print(f"  media end:    {final_media:.1f}s"
              + (f" of {duration_sec:.0f}s" if duration_sec else ""))
        if complete:
            print("  growth:       COMPLETE — whole video was delivered")
        elif stalled_at is None:
            print("  growth:       STILL GROWING at the end of the window")
        else:
            print(f"  growth:       stalled at {stalled_at:.0f}s")
        print(f"  protection:   {len(statuses)} events, "
              f"{attest} 'attestation required'")
        for ln in statuses[:6]:
            print(f"    {ln.strip()[-90:]}")
        if len(statuses) > 6:
            print(f"    … +{len(statuses) - 6} more")
        rc = 0 if (complete or stalled_at is None) else 1
    finally:
        try:
            ctrl.send("STOP_SESSION", reply_timeout=5.0)
        except Exception:
            pass
        try:
            ctrl.send("QUIT", reply_timeout=2.0)
        except Exception:
            pass
        try:
            bridge.wait(timeout=5.0)
        except Exception:
            try:
                os.killpg(os.getpgid(bridge.pid), 9)
            except Exception:
                pass
        log.close()
        sock_path.unlink(missing_ok=True)
        mkv.unlink(missing_ok=True)
        print(f"\n  bridge log kept at {log_path}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
