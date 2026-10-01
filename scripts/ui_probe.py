#!/usr/bin/env python3.11
"""Player UI probe — drive ffplay-yt in a real window and check what it draws.

Usage:
    .venv/bin/python3.11 scripts/ui_probe.py [--player PATH]

Runs the real player on a synthetic clip, drives it with the keyboard
through xdotool, captures the window with `import`, and judges the
result by pixels (Pillow). It answers the one question a log cannot:
did the interface actually reach the screen?

Why this exists: pressing Tab on a paused or finished video left the
recommendations panel closed, and it looked exactly like a dead key —
the state changed, nothing was drawn. Nothing in cache/play.log could
show that. The render loop only redraws on request (`force_refresh`),
and the re-arm that animations rely on was being cleared right after the
draw, so they only advanced while video frames kept arriving.

Checks (each prints PASS/FAIL, exit code 1 if any fail):
  * Tab while playing        - panel opens
  * Tab while paused         - panel opens (the regression above)
  * Tab after the clip ends  - panel opens (the reported symptom)
  * seek                     - progress bar appears, then hides again
  * hover over the bottom    - bar appears, stays while the pointer rests
                              there, fades once it leaves

Needs an X session (DISPLAY) and takes window focus for the duration —
it activates its own window to send keys to it, so expect the pointer
and focus to move while it runs. Runs in about 40 seconds.
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PLAYER = PROJECT_DIR / "ffplay-yt" / "bin" / "ffplay-yt"
CLIP = Path("/tmp/youthub_ui_probe_clip.mkv")
SHOTS = Path("/tmp/youthub_ui_shots")
CLIP_SECONDS = 4
WIN_W, WIN_H = 900, 560

# The clip is flat grey so the panel (18,18,22) and the bar's accent
# (90,200,255) are unambiguous against it.
CLIP_LUMA = 128
PANEL_LUMA_MAX = 60      # left strip darker than this => the column is drawn
VIDEO_LUMA_MIN = 90      # right strip must still show the picture


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def make_clip(path: Path) -> None:
    if path.exists():
        return
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"color=c=gray:s=640x360:r=30", "-t", str(CLIP_SECONDS),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


class Player:
    """One ffplay-yt window, with keys and screenshots."""

    def __init__(self, player: Path, clip: Path, tag: str):
        self.tag = tag
        self.sock = Path(f"/tmp/youthub_ui_probe_{tag}.sock")
        self.title = f"youthub-ui-probe-{tag}"
        env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":0"))
        self.env = env
        self.sock.unlink(missing_ok=True)
        self._pointer = self._read_pointer()
        self.proc = subprocess.Popen(
            [str(player), "-hide_banner", "-loglevel", "warning",
             "-window_title", self.title, "-x", str(WIN_W), "-y", str(WIN_H),
             "-an", "-ipc", str(self.sock), "-i", str(clip)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wid = self._wait_window()
        run(["xdotool", "windowactivate", "--sync", self.wid], env=env)
        run(["xdotool", "windowfocus", "--sync", self.wid], env=env)
        time.sleep(0.4)

    def _read_pointer(self) -> tuple[str, str] | None:
        out = run(["xdotool", "getmouselocation", "--shell"], env=self.env).stdout
        x = y = None
        for line in out.splitlines():
            if line.startswith("X="):
                x = line[2:]
            elif line.startswith("Y="):
                y = line[2:]
        return (x, y) if x and y else None

    def _wait_window(self) -> str:
        for _ in range(150):
            if self.proc.poll() is not None:
                raise RuntimeError("player exited before opening a window")
            found = run(["xdotool", "search", "--name", self.title],
                        env=self.env).stdout.split()
            for wid in found:
                # Window ids get reused by X, so never trust the title
                # alone: the window has to belong to *our* process.
                pid = run(["xdotool", "getwindowpid", wid],
                          env=self.env).stdout.strip()
                if pid and int(pid) == self.proc.pid:
                    return wid
            time.sleep(0.1)
        raise RuntimeError(f"no window for {self.title!r} owned by pid "
                           f"{self.proc.pid}")

    def ipc(self, line: str) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(str(self.sock))
        s.sendall(line.encode() + b"\n")
        time.sleep(0.15)
        s.close()

    def key(self, name: str) -> None:
        run(["xdotool", "key", "--clearmodifiers", name], env=self.env)

    def mouse(self, xf: float, yf: float) -> None:
        """Warp the pointer to a fraction of the window (0..1)."""
        w, h = self.geometry()
        run(["xdotool", "mousemove", "--sync", "--window", self.wid,
             str(int(w * xf)), str(int(h * yf))], env=self.env)

    def geometry(self) -> tuple[int, int]:
        out = run(["xdotool", "getwindowgeometry", self.wid],
                  env=self.env).stdout
        for line in out.splitlines():
            if "Geometry" in line:
                w, h = line.split(":")[1].strip().split("x")
                return int(w), int(h)
        raise RuntimeError(f"cannot read geometry: {out!r}")

    def shot(self, name: str) -> Path:
        path = SHOTS / f"{name}.png"
        run(["import", "-window", self.wid, str(path)], env=self.env)
        return path

    def stop(self) -> None:
        self.proc.terminate()
        time.sleep(0.4)
        if self.proc.poll() is None:
            self.proc.kill()
        self.sock.unlink(missing_ok=True)
        # The hover checks warp the pointer; put it back where it was.
        if self._pointer:
            run(["xdotool", "mousemove", self._pointer[0], self._pointer[1]],
                env=self.env)


# ---------------------------------------------------------------- pixels

def _crop_luma(img, x0f, y0f, x1f, y1f) -> float:
    w, h = img.size
    box = (int(w * x0f), int(h * y0f), int(w * x1f), int(h * y1f))
    px = img.crop(box).convert("L")
    hist = px.histogram()
    total = sum(hist)
    return sum(i * n for i, n in enumerate(hist)) / max(1, total)


def _count_accent(img, y0f: float, y1f: float) -> int:
    """Pixels of the bar's accent colour (90,200,255) — blue-dominant."""
    w, h = img.size
    box = (0, int(h * y0f), w, int(h * y1f))
    data = img.crop(box).convert("RGB").tobytes()
    n = 0
    for i in range(0, len(data), 3):
        r, g, b = data[i], data[i + 1], data[i + 2]
        if b > 170 and b - r > 50 and g > 120:
            n += 1
    return n


def panel_is_open(png: Path) -> tuple[bool, str]:
    # Both boxes are taken from the vertical middle: the picture is
    # letterboxed differently with the panel open (the video area is
    # narrower), so a taller box would sample black bars and call an
    # open panel a dark picture.
    from PIL import Image
    with Image.open(png) as img:
        left = _crop_luma(img, 0.02, 0.35, 0.40, 0.65)
        right = _crop_luma(img, 0.65, 0.35, 0.95, 0.65)
    ok = left < PANEL_LUMA_MAX and right > VIDEO_LUMA_MIN
    return ok, f"left={left:.0f} right={right:.0f}"


def bar_is_visible(png: Path) -> tuple[bool, str]:
    from PIL import Image
    with Image.open(png) as img:
        n = _count_accent(img, 0.88, 1.0)
    return n > 30, f"accent pixels={n}"


# ------------------------------------------------------------- scenarios

def check(name: str, ok: bool, detail: str) -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  — {detail}")
    return ok


def sidebar_checks(player: Path, results: list) -> None:
    p = Player(player, CLIP, "sidebar")
    try:
        # A couple of tiles, so the panel has something to draw.
        p.ipc("RECS_ITEM aaaaaaaaaaa\tUI probe tile one\tsecond line\t/tmp/nope.jpg")
        p.ipc("RECS_ITEM bbbbbbbbbbb\tUI probe tile two\tsecond line\t/tmp/nope.jpg")
        time.sleep(0.6)

        print("Tab while playing")
        p.key("Tab")
        time.sleep(0.7)
        results.append(check("panel opens while playing",
                             *panel_is_open(p.shot("01_tab_playing"))))
        p.key("Tab")
        time.sleep(0.6)

        print("Tab while paused")
        p.key("space")
        time.sleep(0.4)
        p.key("Tab")
        time.sleep(1.2)          # the slide takes 0.22 s; give it slack
        results.append(check("panel opens while paused",
                             *panel_is_open(p.shot("02_tab_paused"))))
        p.key("Tab")
        time.sleep(0.6)
        p.key("space")           # resume
        time.sleep(0.5)

        print("Tab after the clip ends")
        time.sleep(CLIP_SECONDS + 3.0)
        p.key("Tab")
        time.sleep(1.2)
        results.append(check("panel opens after the clip ends",
                             *panel_is_open(p.shot("03_tab_eof"))))
    finally:
        p.stop()


def bar_checks(player: Path, results: list) -> None:
    p = Player(player, CLIP, "bar")
    try:
        p.ipc("META 4.000 4.000")     # duration, so the bar has a scale
        time.sleep(1.0)
        p.ipc("SEEK_ABS 1")
        time.sleep(0.6)
        results.append(check("progress bar appears after a seek",
                             *bar_is_visible(p.shot("04_bar_after_seek"))))
        time.sleep(3.5)               # it hides itself after 3 s
        still_drawn, detail = bar_is_visible(p.shot("05_bar_after_hide"))
        results.append(check("progress bar hides itself again",
                             not still_drawn, detail))
    finally:
        p.stop()


def hover_checks(player: Path, results: list) -> None:
    """The bar answers the pointer in the bottom strip.

    The interesting case is the second one: a mouse that is not moving
    sends no motion events, so keeping the readout up while the pointer
    rests there cannot be driven by events — the hold has to survive on
    its own.
    """
    p = Player(player, CLIP, "hover")
    try:
        p.ipc("META 4.000 4.000")     # duration, so the bar has a scale
        p.mouse(0.5, 0.4)
        time.sleep(3.6)               # let the opening announce fade away
        visible, detail = bar_is_visible(p.shot("06_hover_away"))
        results.append(check("bar hidden while the pointer is away",
                             not visible, detail))

        p.mouse(0.5, 0.97)            # into the bottom strip
        time.sleep(0.5)
        results.append(check("bar appears when the pointer enters",
                             *bar_is_visible(p.shot("07_hover_in"))))

        time.sleep(3.5)               # longer than the 3 s hold
        results.append(check("bar stays while the pointer rests there",
                             *bar_is_visible(p.shot("08_hover_still"))))

        p.mouse(0.5, 0.4)             # leave the strip
        time.sleep(1.5)               # the fade is 0.75 s
        visible, detail = bar_is_visible(p.shot("09_hover_left"))
        results.append(check("bar fades out after the pointer leaves",
                             not visible, detail))
    finally:
        p.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--player", type=Path, default=DEFAULT_PLAYER,
                    help=f"player binary (default: {DEFAULT_PLAYER})")
    args = ap.parse_args()

    if not args.player.exists():
        print(f"player not found: {args.player}")
        return 1
    if not os.environ.get("DISPLAY"):
        print("DISPLAY is not set — this probe needs an X session")
        return 1
    for tool in ("xdotool", "import", "ffmpeg"):
        if not shutil.which(tool):
            print(f"missing tool: {tool}")
            return 1
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("Pillow is missing (pip install Pillow)")
        return 1

    SHOTS.mkdir(parents=True, exist_ok=True)
    make_clip(CLIP)
    print(f"player: {args.player}")
    print(f"clip:   {CLIP}  ({CLIP_SECONDS}s, flat grey)")
    print(f"shots:  {SHOTS}")
    print()

    results: list = []
    sidebar_checks(args.player, results)
    bar_checks(args.player, results)
    hover_checks(args.player, results)

    print()
    failed = results.count(False)
    if failed:
        print(f"{failed} of {len(results)} checks FAILED — screenshots in {SHOTS}")
        return 1
    print(f"all {len(results)} checks passed — screenshots in {SHOTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
